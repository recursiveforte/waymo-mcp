"""Client for the private Waymo rider API."""

from __future__ import annotations

import base64
import math
import os
import sys
import threading
import time
from datetime import datetime, timezone
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
    parse_cancellation_fee,
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
    "cancel_fee_ux": f"{SERVICE}/GetCancellationFeeUx",
    "cancel": f"{SERVICE}/CancelActiveTrip",
}

TRIP_STATES = {
    0: "STATE_UNSPECIFIED",
    10: "REQUESTED",
    20: "INFEASIBLE",
    25: "QUEUED",
    30: "TO_PICKUP",
    35: "PICKUP_MOVE_ALONG",
    41: "AT_PICKUP",
    45: "NO_SHOW",
    60: "BOARDING",
    70: "RIDING",
    80: "STOP_DROPOFF",
    90: "FINAL_DROPOFF",
    91: "AT_STOP_ARRIVED",
    92: "AT_STOP_WAITING",
    93: "AT_STOP_READY",
    94: "AT_STOP_VEHICLE_NEARBY",
    95: "AT_STOP_SUSPENDED",
    105: "STRANDED",
    110: "COMPLETED",
    120: "CANCELLED",
}
# A finished trip can stay in Waymo's status as "active_trip" for a while; it doesn't block a new ride.
TERMINAL_TRIP_STATES = {110, 120}


def _blocking_trip(ride_state: dict[str, Any]) -> bool:
    trip = ride_state.get("active_trip")
    return bool(trip) and trip.get("state_code") not in TERMINAL_TRIP_STATES


VEHICLE_TYPES = {
    0: "VEHICLE_TYPE_UNSPECIFIED",
    2: "V5",
    3: "LIBERTY",
    5: "WAV",
    6: "AUX",
    7: "W12",
    8: "APOLLO",
}
ARRIVAL_STATES = {
    0: "WRA_STATE_UNSPECIFIED",
    1: "DISPATCHED",
    2: "ARRIVED",
    3: "CONTINUING_IN_MANUAL",
    4: "CANCELLED",
    5: "INTERRUPTED",
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
        self._channel = grpc.secure_channel(
            HOST,
            grpc.ssl_channel_credentials(),
            options=(("grpc.max_metadata_size", 64 * 1024),),
        )
        self._payments_cache: tuple[float, list[dict[str, Any]]] | None = None

    @classmethod
    def from_environment(cls) -> WaymoClient:
        master_token = os.environ.get("WAYMO_MASTER_TOKEN")
        if master_token:
            return cls(
                Credentials.android(
                    os.environ["WAYMO_EMAIL"],
                    master_token,
                    os.environ["WAYMO_ANDROID_ID"],
                )
            )
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
        bias_lat: float = 37.7749,
        bias_lng: float = -122.4194,
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
        intermediate_stop_queries: list[str] | None = None,
        intermediate_stop_indices: list[int] | None = None,
        pickup_index: int = 0,
        dropoff_index: int = 0,
        pickup_near: tuple[float, float] | None = None,
        dropoff_near: tuple[float, float] | None = None,
        passengers: int = 1,
        scheduled_option_token: str | None = None,
    ) -> dict[str, Any]:
        intermediate_stop_queries = intermediate_stop_queries or []
        intermediate_stop_indices = intermediate_stop_indices or [
            0
        ] * len(intermediate_stop_queries)
        if len(intermediate_stop_queries) > 3:
            raise ValueError("at most 3 intermediate stops are supported")
        if len(intermediate_stop_indices) != len(intermediate_stop_queries):
            raise ValueError(
                "intermediate_stop_indices must match intermediate_stop_queries"
            )
        if any(index < 0 for index in intermediate_stop_indices):
            raise ValueError("intermediate stop indices cannot be negative")
        ride_state, plan_template = self._status_snapshot()
        if _blocking_trip(ride_state):
            raise WaymoError("quote", "ACTIVE_TRIP", "an active trip already exists")
        if pickup_near:
            pickups = self.search_places(pickup_query, bias_lat=pickup_near[0], bias_lng=pickup_near[1])
        else:
            pickups = self.search_places(pickup_query)
        if not pickups:
            raise WaymoError("quote", "PICKUP_NOT_FOUND", pickup_query)
        if pickup_near:
            pickup = closest(pickups, pickup_near)
        else:
            try:
                pickup = pickups[pickup_index]
            except IndexError as exc:
                raise ValueError("pickup_index is outside the search results") from exc

        intermediate_stops: list[Place] = []
        previous = pickup
        for query, result_index in zip(
            intermediate_stop_queries,
            intermediate_stop_indices,
        ):
            results = self.search_places(
                query,
                bias_lat=previous.latitude,
                bias_lng=previous.longitude,
            )
            if not results:
                raise WaymoError("quote", "STOP_NOT_FOUND", query)
            try:
                previous = results[result_index]
            except IndexError as exc:
                raise ValueError(
                    f"result index for intermediate stop {query!r} is outside "
                    "the search results"
                ) from exc
            intermediate_stops.append(previous)

        bias = dropoff_near or (previous.latitude, previous.longitude)
        dropoffs = self.search_places(
            dropoff_query,
            bias_lat=bias[0],
            bias_lng=bias[1],
        )
        if not dropoffs:
            raise WaymoError("quote", "DROPOFF_NOT_FOUND", dropoff_query)
        if dropoff_near:
            dropoff = closest(dropoffs, dropoff_near)
        else:
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
                    intermediate_stops=intermediate_stops,
                    scheduled_option_token=scheduled_option_token,
                    plan_template=plan_template,
                ),
                timeout=20,
            )
        )
        selected_scheduled_option = next(
            (
                option
                for option in result["scheduled_options"]
                if option["token"] == scheduled_option_token
            ),
            None,
        )
        if scheduled_option_token and selected_scheduled_option is None:
            selected_scheduled_option = {"token": scheduled_option_token}
        return {
            **result,
            "pickup": pickup.public(),
            "intermediate_stops": [stop.public() for stop in intermediate_stops],
            "dropoff": dropoff.public(),
            "passengers": passengers,
            "selected_scheduled_option": selected_scheduled_option,
        }

    def book(self, proposal_token: str, payment_method_id: str) -> dict[str, Any]:
        if _blocking_trip(self.status()):
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
                create_trip_request(proposal_token, payment["public"]["id"]),
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

    def preview_cancel(self) -> dict[str, Any]:
        before = self.status()["active_trip"]
        if not before:
            return {"can_cancel": False, "reason": "no_active_trip"}
        proposal = self._unary("propose_cancel")
        tokens = bytes_fields(proposal, 2)
        if not tokens:
            raise WaymoError(
                "cancel",
                "TOKEN_MISSING",
                "Waymo returned no cancellation token",
            )
        fee = parse_cancellation_fee(proposal)
        if fee is None:
            try:
                fee = parse_cancellation_fee(self._unary("cancel_fee_ux"))
            except WaymoError:
                pass
        return {
            "can_cancel": True,
            "trip_id": before["trip_id"],
            "cancellation_fee": fee,
            "fee_available": fee is not None,
            "_cancellation_token": tokens[0].decode(errors="replace"),
        }

    def confirm_cancel(
        self,
        cancellation_token: str,
        expected_trip_id: str,
    ) -> dict[str, Any]:
        before = self.status()["active_trip"]
        if not before:
            return {"cancelled": False, "reason": "no_active_trip"}
        if before["trip_id"] != expected_trip_id:
            raise WaymoError(
                "cancel",
                "TRIP_CHANGED",
                "the active trip changed after cancellation preview",
            )
        try:
            self._unary(
                "cancel",
                cancel_trip_request(cancellation_token),
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
            details = parsed.get("1", {})
            last_four = text(details.get("2")) if isinstance(details, dict) else ""
            if method_id:
                records.append(
                    {
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
    if isinstance(legs, list):
        leg = next(
            (
                candidate
                for candidate in legs
                if isinstance(candidate, dict) and candidate.get("1")
            ),
            legs[0] if legs else {},
        )
    else:
        leg = legs
    if not isinstance(leg, dict):
        leg = {}
    state = trip.get("4", {})
    state_code = (
        int(state.get("1", 0))
        if isinstance(state, dict) and isinstance(state.get("1", 0), int)
        else 0
    )
    result = {
        "trip_id": _normalize_trip_id(trip_id),
        "state": TRIP_STATES.get(state_code, f"UNKNOWN_{state_code}"),
        "state_code": state_code,
        "pickup": _location(leg.get("2")),
        "dropoff": _location(leg.get("3")),
        "tracking_url": text(trip.get("16")) or None,
    }
    pickup_eta = leg.get("4")
    if isinstance(pickup_eta, dict):
        eta_timestamp = _timestamp(pickup_eta.get("1"))
        if eta_timestamp is not None:
            result["pickup_eta_seconds"] = max(
                0,
                round(eta_timestamp - time.time()),
            )
            result["pickup_eta_at"] = datetime.fromtimestamp(
                eta_timestamp,
                timezone.utc,
            ).isoformat()
    vehicle = _vehicle(trip.get("2"))
    if vehicle:
        result["vehicle"] = vehicle
    arrival = _arrival(trip.get("17"))
    if arrival:
        result["arrival"] = arrival
    configurations = plan.get("3") if isinstance(plan, dict) else None
    scheduled_token = (
        text(configurations.get("2"))
        if isinstance(configurations, dict)
        else ""
    )
    if scheduled_token:
        result["scheduled_option_token"] = scheduled_token
    return result


def _vehicle(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    vehicle_id = text(value.get("1"))
    license_plate = text(value.get("10"))
    if not vehicle_id and not license_plate:
        return None
    vehicle: dict[str, Any] = {
        "vehicle_id": vehicle_id or None,
        "license_plate": license_plate or None,
    }
    position = value.get("2")
    if isinstance(position, dict):
        try:
            vehicle["position"] = {
                "latitude": int(position["1"]) * 1e-7,
                "longitude": int(position["2"]) * 1e-7,
            }
        except (KeyError, TypeError, ValueError):
            pass
    heading = value.get("3")
    if isinstance(heading, int):
        vehicle["heading_degrees"] = heading / 1000
    speed = value.get("18")
    if isinstance(speed, int):
        vehicle["speed_mph"] = speed
    vehicle_type = value.get("8")
    if isinstance(vehicle_type, int):
        vehicle["type"] = VEHICLE_TYPES.get(
            vehicle_type,
            f"UNKNOWN_{vehicle_type}",
        )
    reported_at = _timestamp(value.get("7"))
    if reported_at is not None:
        vehicle["last_reported_at"] = datetime.fromtimestamp(
            reported_at,
            timezone.utc,
        ).isoformat()
    return vehicle


def _arrival(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    state_code = value.get("1")
    position = value.get("4")
    if not isinstance(state_code, int) and not isinstance(position, dict):
        return None
    arrival: dict[str, Any] = {}
    if isinstance(state_code, int):
        arrival["state"] = ARRIVAL_STATES.get(
            state_code,
            f"UNKNOWN_{state_code}",
        )
        arrival["state_code"] = state_code
    if isinstance(position, dict):
        try:
            arrival["position"] = {
                "latitude": int(position["1"]) * 1e-7,
                "longitude": int(position["2"]) * 1e-7,
            }
        except (KeyError, TypeError, ValueError):
            pass
    return arrival or None


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, dict) or not isinstance(value.get("1"), int):
        return None
    seconds = int(value["1"])
    nanos = int(value.get("2", 0))
    return seconds + nanos / 1_000_000_000



def closest(places: list[Place], point: tuple[float, float]) -> Place:
    """The place nearest point (equirectangular distance; fine at city scale)."""
    lat0 = math.radians(point[0])
    return min(
        places,
        key=lambda p: (p.latitude - point[0]) ** 2
        + ((p.longitude - point[1]) * math.cos(lat0)) ** 2,
    )
