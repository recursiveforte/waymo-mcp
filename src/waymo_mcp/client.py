"""Client for the private Waymo rider API."""

from __future__ import annotations

import base64
import os
import sys
import threading
import time
from typing import Any

import grpc

from .auth import (
    DEFAULT_DEVICE_ID,
    Credentials,
    USER_AGENT,
    authenticate,
    load_credentials,
)
from .wire import (
    Place,
    bytes_fields,
    cancel_trip_request,
    create_trip_request,
    decode,
    parse_places,
    parse_quote,
    place_search_request,
    propose_trip_request,
    text,
)

HOST = "cartrip-pa.googleapis.com:443"
SERVICE = "/car.taas.client.v2alpha.ClientTripService"
DEFAULT_CREDENTIALS = "~/.config/waymo/credentials.json"
RPC = {
    "history": f"{SERVICE}/GetTransactionHistory",
    "status": f"{SERVICE}/GetActiveTrip",
    "places": f"{SERVICE}/GetPlaceCompletions",
    "payments": f"{SERVICE}/ListPaymentMethods",
    "quote": f"{SERVICE}/ProposeTripPlan",
    "book": f"{SERVICE}/CreateTrip",
    "propose_cancel": f"{SERVICE}/ProposeTripCancellation",
    "cancel": f"{SERVICE}/CancelActiveTrip",
}


class WaymoError(RuntimeError):
    def __init__(self, operation: str, code: str, detail: str):
        super().__init__(f"{operation}: {code}: {detail}")
        self.operation = operation
        self.code = code
        self.detail = detail


class WaymoClient:
    def __init__(self, credentials: Credentials):
        self.credentials = credentials
        self._auth: dict[str, Any] = {}
        self._auth_lock = threading.Lock()
        self._channel = grpc.secure_channel(HOST, grpc.ssl_channel_credentials())
        self._payments_cache: tuple[float, list[dict[str, Any]]] | None = None

    @classmethod
    def from_environment(cls) -> WaymoClient:
        refresh_token = os.environ.get("WAYMO_REFRESH_TOKEN")
        if refresh_token:
            return cls(
                Credentials(
                    refresh_token,
                    os.environ.get("WAYMO_DEVICE_ID", DEFAULT_DEVICE_ID),
                )
            )
        path = os.environ.get("WAYMO_CREDENTIALS_FILE", DEFAULT_CREDENTIALS)
        return cls(load_credentials(path))

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        with self._auth_lock:
            expires_at = int(self._auth.get("expires_at", 0))
            if not self._auth or expires_at <= time.time() + 60:
                self._auth = authenticate(self.credentials)
            token = self._auth["chauffeur_token"]
        return (
            ("authorization", f"Bearer {token}"),
            ("user-agent", USER_AGENT.replace(" iSL/3.4", "")),
        )

    def _unary(self, operation: str, payload: bytes = b"", timeout: int = 20) -> bytes:
        started = time.perf_counter()
        try:
            method = self._channel.unary_unary(
                RPC[operation],
                request_serializer=lambda value: value,
                response_deserializer=lambda value: value,
            )
            return method(payload, metadata=self._metadata(), timeout=timeout)
        except grpc.RpcError as exc:
            code = exc.code().name if exc.code() else "UNKNOWN"
            raise WaymoError(operation, code, exc.details() or "request failed") from exc
        finally:
            self._trace(operation, started)

    def _stream_once(
        self,
        operation: str,
        payload: bytes,
        timeout: int = 15,
    ) -> bytes:
        call: grpc.Call | None = None
        started = time.perf_counter()
        try:
            method = self._channel.stream_stream(
                RPC[operation],
                request_serializer=lambda value: value,
                response_deserializer=lambda value: value,
            )
            call = method(iter([payload]), metadata=self._metadata(), timeout=timeout)
            return next(call)
        except grpc.RpcError as exc:
            code = exc.code().name if exc.code() else "UNKNOWN"
            raise WaymoError(operation, code, exc.details() or "request failed") from exc
        except StopIteration as exc:
            raise WaymoError(operation, "EMPTY_RESPONSE", "Waymo returned no result") from exc
        finally:
            if call is not None:
                call.cancel()
            self._trace(operation, started)

    def close(self) -> None:
        self._channel.close()

    @staticmethod
    def _trace(operation: str, started: float) -> None:
        if os.environ.get("WAYMO_DEBUG_TIMING") == "1":
            elapsed = time.perf_counter() - started
            print(f"waymo rpc {operation}: {elapsed:.3f}s", file=sys.stderr)

    def search_places(
        self,
        query: str,
        *,
        bias_lat: float = 37.55,
        bias_lng: float = -122.30,
        limit: int = 5,
    ) -> list[Place]:
        query = query.strip()
        if not query:
            raise ValueError("place query cannot be empty")
        raw = self._stream_once(
            "places",
            place_search_request(query, bias_lat, bias_lng),
        )
        return parse_places(raw)[:limit]

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        decoded = decode(self._unary("history"))
        items = decoded.get("2", {}).get("2", []) if isinstance(decoded, dict) else []
        if not isinstance(items, list):
            return []
        trips: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            trip_id = text(item.get("1"))
            detail = item.get("3", {})
            if trip_id.startswith("header_") or not isinstance(detail, dict):
                continue
            trips.append(
                {
                    "trip_id": _normalize_trip_id(trip_id),
                    "destination": _deep_text(detail.get("2")),
                    "date": _deep_text(detail.get("3")),
                    "status": _deep_text(detail.get("4")),
                    "price": _deep_text(detail.get("5")),
                }
            )
            if len(trips) >= limit:
                break
        return trips

    def status(self) -> dict[str, Any]:
        status, _template = self._status_snapshot()
        return status

    def _status_snapshot(self) -> tuple[dict[str, Any], bytes | None]:
        response = self._unary("status")
        decoded = decode(response)
        active = _active_trip(decoded)
        stale = None
        if active:
            for trip in self.history(limit=5):
                if (
                    trip["trip_id"] == active["trip_id"]
                    and str(trip.get("status", "")).lower() == "cancelled"
                ):
                    stale, active = active, None
                    break
        trips = bytes_fields(response, 2)
        plans = bytes_fields(trips[0], 5) if trips else []
        return {"active_trip": active, "stale_trip": stale}, plans[0] if plans else None

    def payment_methods(self) -> list[dict[str, Any]]:
        return [record["public"] for record in self._payment_records()]

    def quote(
        self,
        pickup_query: str,
        dropoff_query: str,
        *,
        pickup_index: int = 0,
        dropoff_index: int = 0,
        passengers: int = 1,
    ) -> dict[str, Any]:
        ride_state, plan_template = self._status_snapshot()
        if ride_state["active_trip"]:
            raise WaymoError("quote", "ACTIVE_TRIP", "an active trip already exists")
        pickups = self.search_places(pickup_query)
        if not pickups:
            raise WaymoError("quote", "PICKUP_NOT_FOUND", pickup_query)
        try:
            pickup = pickups[pickup_index]
        except IndexError as exc:
            raise ValueError("pickup_index is outside the search results") from exc
        dropoffs = self.search_places(
            dropoff_query,
            bias_lat=pickup.latitude,
            bias_lng=pickup.longitude,
        )
        if not dropoffs:
            raise WaymoError("quote", "DROPOFF_NOT_FOUND", dropoff_query)
        try:
            dropoff = dropoffs[dropoff_index]
        except IndexError as exc:
            raise ValueError("dropoff_index is outside the search results") from exc
        result = parse_quote(
            self._unary(
                "quote",
                propose_trip_request(
                    pickup,
                    dropoff,
                    passengers,
                    plan_template=plan_template,
                ),
                timeout=20,
            )
        )
        return {
            **result,
            "pickup": pickup.public(),
            "dropoff": dropoff.public(),
            "passengers": passengers,
        }

    def book(self, proposal_token: str, payment_method_id: str) -> dict[str, Any]:
        if self.status()["active_trip"]:
            raise WaymoError("book", "ACTIVE_TRIP", "an active trip already exists")
        payment = next(
            (
                record
                for record in self._payment_records()
                if record["public"]["id"] == payment_method_id
            ),
            None,
        )
        if payment is None:
            raise WaymoError("book", "PAYMENT_NOT_FOUND", payment_method_id)
        try:
            self._unary(
                "book",
                create_trip_request(proposal_token, payment["booking_token"]),
                timeout=30,
            )
        except WaymoError as exc:
            if exc.code in {"DEADLINE_EXCEEDED", "UNAVAILABLE", "UNKNOWN"}:
                raise WaymoError(
                    "book",
                    "OUTCOME_UNKNOWN",
                    "do not retry; check ride status first",
                ) from exc
            raise
        active = self.status()["active_trip"]
        if not active:
            raise WaymoError(
                "book",
                "OUTCOME_UNKNOWN",
                "CreateTrip returned but no active trip is visible",
            )
        return {"booked": True, "active_trip": active}

    def cancel(self) -> dict[str, Any]:
        before = self.status()["active_trip"]
        if not before:
            return {"cancelled": False, "reason": "no_active_trip"}
        proposal = self._unary("propose_cancel")
        tokens = bytes_fields(proposal, 2)
        if not tokens:
            raise WaymoError(
                "cancel",
                "TOKEN_MISSING",
                "Waymo returned no cancellation token",
            )
        try:
            self._unary(
                "cancel",
                cancel_trip_request(tokens[0].decode(errors="replace")),
                timeout=20,
            )
        except WaymoError as exc:
            if exc.code in {"DEADLINE_EXCEEDED", "UNAVAILABLE", "UNKNOWN"}:
                raise WaymoError(
                    "cancel",
                    "OUTCOME_UNKNOWN",
                    "do not retry; check ride status first",
                ) from exc
            raise
        return {
            "cancelled": self.status()["active_trip"] is None,
            "trip_id": before["trip_id"],
        }

    def _payment_records(self) -> list[dict[str, Any]]:
        if self._payments_cache and time.monotonic() - self._payments_cache[0] < 300:
            return self._payments_cache[1]
        records: list[dict[str, Any]] = []
        for block in bytes_fields(self._unary("payments"), 2):
            parsed = decode(block)
            if not isinstance(parsed, dict):
                continue
            method_id = text(parsed.get("3"))
            booking_token = _find_hex_token(parsed)
            details = parsed.get("1", {})
            last_four = text(details.get("2")) if isinstance(details, dict) else ""
            if method_id and booking_token:
                records.append(
                    {
                        "booking_token": booking_token,
                        "public": {
                            "id": method_id,
                            "type": "card",
                            "label": (
                                f"Card ending in {last_four}" if last_four else "Card"
                            ),
                        },
                    }
                )
        self._payments_cache = (time.monotonic(), records)
        return records


def _deep_text(value: Any) -> str | None:
    if isinstance(value, dict):
        for child in value.values():
            result = _deep_text(child)
            if result:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _deep_text(child)
            if result:
                return result
    else:
        result = text(value)
        if result and not result.startswith("header_"):
            return result
    return None


def _normalize_trip_id(value: str) -> str:
    if value.isdigit():
        return value
    try:
        decoded = base64.b64decode(value + "==").decode(errors="ignore")
        digits = "".join(character for character in decoded if character.isdigit())
        return digits or value
    except Exception:
        return value


def _location(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    desired = value.get("3")
    if not isinstance(desired, dict):
        return None
    coords = desired.get("1", {})
    display = desired.get("8", {})
    if not isinstance(coords, dict):
        return None
    try:
        latitude = int(coords["1"]) * 1e-7
        longitude = int(coords["2"]) * 1e-7
    except (KeyError, TypeError, ValueError):
        return None
    name = ""
    address = ""
    if isinstance(display, dict):
        name = text(display.get("1"))
        lines = display.get("2", [])
        if not isinstance(lines, list):
            lines = [lines]
        address = ", ".join(filter(None, (text(line) for line in lines)))
    return {
        "name": name or address,
        "address": address,
        "latitude": latitude,
        "longitude": longitude,
    }


def _active_trip(decoded: Any) -> dict[str, Any] | None:
    trip = decoded.get("2") if isinstance(decoded, dict) else None
    if not isinstance(trip, dict):
        return None
    trip_id = text(trip.get("1"))
    if not trip_id:
        return None
    plan = trip.get("5", {})
    legs = plan.get("5") if isinstance(plan, dict) else None
    leg = legs[0] if isinstance(legs, list) and legs else legs
    if not isinstance(leg, dict):
        leg = {}
    return {
        "trip_id": _normalize_trip_id(trip_id),
        "pickup": _location(leg.get("2")),
        "dropoff": _location(leg.get("3")),
        "tracking_url": text(trip.get("24")) or None,
    }


def _find_hex_token(value: Any) -> str | None:
    if isinstance(value, dict):
        for child in value.values():
            result = _find_hex_token(child)
            if result:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _find_hex_token(child)
            if result:
                return result
    else:
        candidate = text(value)
        if len(candidate) == 32 and all(
            character in "0123456789abcdef" for character in candidate
        ):
            return candidate
    return None
