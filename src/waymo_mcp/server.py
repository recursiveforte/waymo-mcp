"""MCP tool surface for Waymo ride management."""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import WaymoClient, WaymoError

mcp = MCPServer(
    "waymo-rides",
    instructions=(
        "Manage Waymo rides. Booking is two-step: waymo_get_quote returns a "
        "quote_id, fare, pickup_eta {at, minutes} and dropoff_eta (Pacific "
        "ISO); show pickup, dropoff, fare and both ETAs to the user, "
        "and only after they explicitly approve that quote call "
        "waymo_book_ride with the same quote_id and fare. Quotes expire after "
        "five minutes and are single-use. Never retry an OUTCOME_UNKNOWN "
        "mutation; check status."
    ),
)

_client: WaymoClient | None = None
_client_lock = threading.Lock()
# quote_id -> (wall-clock time issued, quote). With WAYMO_QUOTES_FILE set, quotes live in that 0600 file
# instead, so a quote_id issued by one server process (e.g. Mandelbot's ride_quote) books from another.
_quotes: dict[str, tuple[float, dict[str, Any]]] = {}
_quotes_lock = threading.Lock()
QUOTE_TTL_SECONDS = 5 * 60
SF_LATITUDE = 37.7749
SF_LONGITUDE = -122.4194
_cancel_preview: tuple[float, str, str] | None = None
_cancel_preview_lock = threading.Lock()
CANCEL_PREVIEW_TTL_SECONDS = 5 * 60


def _get_client() -> WaymoClient:
    global _client
    with _client_lock:
        if _client is None:
            try:
                _client = WaymoClient.from_environment()
                atexit.register(_client.close)
            except (OSError, ValueError) as exc:
                raise ToolError(
                    "Waymo credentials are not configured. Set WAYMO_REFRESH_TOKEN "
                    "or WAYMO_CREDENTIALS_FILE."
                ) from exc
        return _client


def _call(function: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except WaymoError as exc:
        raise ToolError(f"{exc.code}: {exc.detail}") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(
    title="Search Waymo places",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def waymo_search_places(
    query: Annotated[str, Field(min_length=1, description="Place name or address")],
    bias_latitude: Annotated[
        float,
        Field(ge=-90, le=90, description="Search center latitude"),
    ] = SF_LATITUDE,
    bias_longitude: Annotated[
        float,
        Field(ge=-180, le=180, description="Search center longitude"),
    ] = SF_LONGITUDE,
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> dict[str, Any]:
    """Resolve a place name or address to canonical Waymo locations."""
    places = _call(
        _get_client().search_places,
        query,
        bias_lat=bias_latitude,
        bias_lng=bias_longitude,
        limit=limit,
    )
    return {
        "query": query,
        "results": [place.public(index) for index, place in enumerate(places)],
    }


@mcp.tool(
    title="Get Waymo ride history",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def waymo_ride_history(
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Return recent Waymo trips, including cancelled and completed rides."""
    return {"trips": _call(_get_client().history, limit)}


@mcp.tool(
    title="Get current Waymo ride status",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def waymo_ride_status() -> dict[str, Any]:
    """Return the active ride and any terminal trip still cached by Waymo."""
    return _call(_get_client().status)


@mcp.tool(
    title="List Waymo payment methods",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def waymo_list_payment_methods() -> dict[str, Any]:
    """List safe payment-method labels and IDs accepted by the booking tool."""
    return {"payment_methods": _call(_get_client().payment_methods)}


@mcp.tool(
    title="Get a Waymo ride quote",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def waymo_get_quote(
    pickup: Annotated[str, Field(min_length=1, description="Pickup name or address")],
    dropoff: Annotated[
        str,
        Field(min_length=1, description="Dropoff name or address"),
    ],
    intermediate_stops: Annotated[
        list[str] | None,
        Field(
            max_length=3,
            description="Ordered intermediate stop names or addresses",
        ),
    ] = None,
    intermediate_stop_result_indices: Annotated[
        list[int] | None,
        Field(
            max_length=3,
            description=(
                "Search-result index for each intermediate stop; defaults to 0"
            ),
        ),
    ] = None,
    pickup_result_index: Annotated[int, Field(ge=0)] = 0,
    dropoff_result_index: Annotated[int, Field(ge=0)] = 0,
    pickup_near: Annotated[
        str | None,
        Field(description="'lat,lng': search around it and take the result closest to it (overrides the index)"),
    ] = None,
    dropoff_near: Annotated[
        str | None,
        Field(description="'lat,lng': as pickup_near, for the dropoff"),
    ] = None,
    passengers: Annotated[int, Field(ge=1, le=4)] = 1,
    scheduled_option_token: Annotated[
        str | None,
        Field(
            description=(
                "Token from a prior quote's scheduled_options; omit for ASAP"
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Quote an ASAP route, or re-quote using one returned scheduling option."""
    quote = _call(
        _get_client().quote,
        pickup,
        dropoff,
        intermediate_stop_queries=intermediate_stops,
        intermediate_stop_indices=intermediate_stop_result_indices,
        pickup_index=pickup_result_index,
        dropoff_index=dropoff_result_index,
        pickup_near=_point(pickup_near),
        dropoff_near=_point(dropoff_near),
        passengers=passengers,
        scheduled_option_token=scheduled_option_token,
    )
    quote = {
        **quote,
        "waymo_expires_at": None,
        "expiry_note": (
            "Waymo does not expose an authoritative expiry; re-quote if "
            "booking reports PROPOSAL_STALE"
        ),
        "mcp_cache_ttl_seconds": QUOTE_TTL_SECONDS,
        "mcp_cache_expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=QUOTE_TTL_SECONDS)
        ).isoformat(),
    }
    quote_id = secrets.token_urlsafe(9)
    _store_quote(quote_id, quote)
    public = {key: value for key, value in quote.items() if key != "proposal_token"}
    return {"quote_id": quote_id, **public}


@mcp.tool(
    title="Book a Waymo ride",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def waymo_book_ride(
    quote_id: Annotated[
        str,
        Field(min_length=1, description="quote_id returned by waymo_get_quote"),
    ],
    expected_fare: Annotated[
        str | None,
        Field(description="The estimated_fare from that quote, as shown to the user"),
    ],
    confirmation: Literal["BOOK"],
    payment_method_id: Annotated[
        str | None,
        Field(
            description=(
                "ID from waymo_list_payment_methods; may be omitted when the "
                "account has exactly one"
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Book a quoted ride. Call only after the user approved this exact quote."""
    del confirmation
    # Single-use: consume before booking so a retry or duplicate call can
    # never order a second ride from the same quote.
    record = _take_quote(quote_id)
    if record is None or time.time() - record[0] > QUOTE_TTL_SECONDS:
        raise ToolError("Unknown, used or expired quote_id. Call waymo_get_quote again.")
    quote = record[1]
    if expected_fare != quote.get("estimated_fare"):
        raise ToolError(
            "expected_fare does not match the quote. Get a fresh quote and "
            "confirm its fare with the user."
        )
    if payment_method_id is None:
        methods = _call(_get_client().payment_methods)
        if len(methods) != 1:
            raise ToolError(
                "Pass payment_method_id; the account has "
                f"{len(methods)} payment methods."
            )
        payment_method_id = methods[0]["id"]
    return _call(_get_client().book, quote["proposal_token"], payment_method_id)


@mcp.tool(
    title="Preview Waymo ride cancellation",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def waymo_preview_cancellation() -> dict[str, Any]:
    """Preview cancellation and its fee without cancelling the active ride."""
    global _cancel_preview
    preview = _call(_get_client().preview_cancel)
    token = preview.pop("_cancellation_token", None)
    if preview.get("can_cancel") and token:
        with _cancel_preview_lock:
            _cancel_preview = (
                time.monotonic(),
                preview["trip_id"],
                token,
            )
        preview["preview_expires_at"] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=CANCEL_PREVIEW_TTL_SECONDS)
        ).isoformat()
    return preview


@mcp.tool(
    title="Cancel the active Waymo ride",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def waymo_cancel_ride(confirmation: Literal["CANCEL"]) -> dict[str, Any]:
    """Cancel after a fresh fee preview and explicit user confirmation."""
    global _cancel_preview
    del confirmation
    with _cancel_preview_lock:
        preview = _cancel_preview
        if (
            preview is None
            or time.monotonic() - preview[0] > CANCEL_PREVIEW_TTL_SECONDS
        ):
            _cancel_preview = None
            raise ToolError(
                "No fresh cancellation preview. Call "
                "waymo_preview_cancellation first and show its fee to the user."
            )
    result = _call(_get_client().confirm_cancel, preview[2], preview[1])
    with _cancel_preview_lock:
        _cancel_preview = None
    return result


def _point(value: str | None) -> tuple[float, float] | None:
    if value is None:
        return None
    try:
        lat, lng = (float(part) for part in value.split(","))
    except ValueError as exc:
        raise ToolError(f"expected 'lat,lng', got {value!r}") from exc
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ToolError(f"coordinates out of range: {value!r}")
    return lat, lng


def _purge_quotes(quotes: dict[str, Any]) -> None:
    now = time.time()
    for token in [t for t, (created_at, _q) in quotes.items() if now - created_at > QUOTE_TTL_SECONDS]:
        quotes.pop(token, None)


def _with_quotes(fn: Any) -> Any:
    """Run fn(quotes) on the quote cache under a lock: the in-memory dict, or the shared file."""
    path = os.environ.get("WAYMO_QUOTES_FILE")
    with _quotes_lock:
        if not path:
            return fn(_quotes)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with os.fdopen(os.dup(fd), "r+") as f:
                raw = f.read()
                quotes = {k: tuple(v) for k, v in (json.loads(raw) if raw.strip() else {}).items()}
                result = fn(quotes)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(quotes))
            os.fchmod(fd, 0o600)
            return result
        finally:
            os.close(fd)


def _store_quote(quote_id: str, quote: dict[str, Any]) -> None:
    def put(quotes: dict[str, Any]) -> None:
        _purge_quotes(quotes)
        quotes[quote_id] = (time.time(), quote)
    _with_quotes(put)


def _take_quote(quote_id: str) -> tuple[float, dict[str, Any]] | None:
    return _with_quotes(lambda quotes: quotes.pop(quote_id, None))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
