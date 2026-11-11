"""Minimal protobuf wire codec for the Waymo RPCs used by this server."""

from __future__ import annotations

import math
import time
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Sequence
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def varint(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def read_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def key(field: int, wire_type: int) -> bytes:
    return varint((field << 3) | wire_type)


def message(field: int, value: bytes) -> bytes:
    return key(field, 2) + varint(len(value)) + value


def integer(field: int, value: int) -> bytes:
    return key(field, 0) + varint(value)


def string(field: int, value: str) -> bytes:
    return message(field, value.encode())


def iter_fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        field, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, offset = read_varint(data, offset)
            yield field, wire_type, value
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated fixed64 field")
            yield field, wire_type, data[offset:end]
            offset = end
        elif wire_type == 2:
            length, offset = read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated length-delimited field")
            yield field, wire_type, data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated fixed32 field")
            yield field, wire_type, data[offset:end]
            offset = end
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")


def bytes_fields(data: bytes, field: int) -> list[bytes]:
    return [
        value
        for number, wire_type, value in iter_fields(data)
        if number == field and wire_type == 2 and isinstance(value, bytes)
    ]


def replace_messages(data: bytes, replacements: dict[int, bytes]) -> bytes:
    output = bytearray()
    for field, wire_type, value in iter_fields(data):
        if field in replacements and wire_type == 2:
            output.extend(message(field, replacements[field]))
        elif wire_type == 0:
            output.extend(integer(field, int(value)))
        elif wire_type in (1, 5):
            assert isinstance(value, bytes)
            output.extend(key(field, wire_type) + value)
        elif wire_type == 2:
            assert isinstance(value, bytes)
            output.extend(message(field, value))
    return bytes(output)


def replace_repeated_messages(data: bytes, field_number: int, values: list[bytes]) -> bytes:
    output = bytearray()
    inserted = False
    for field, wire_type, value in iter_fields(data):
        if field == field_number and wire_type == 2:
            if not inserted:
                for replacement in values:
                    output.extend(message(field_number, replacement))
                inserted = True
            continue
        if wire_type == 0:
            output.extend(integer(field, int(value)))
        elif wire_type in (1, 5):
            assert isinstance(value, bytes)
            output.extend(key(field, wire_type) + value)
        elif wire_type == 2:
            assert isinstance(value, bytes)
            output.extend(message(field, value))
    if not inserted:
        for replacement in values:
            output.extend(message(field_number, replacement))
    return bytes(output)


def replace_integer(data: bytes, field_number: int, replacement: int) -> bytes:
    output = bytearray()
    replaced = False
    for field, wire_type, value in iter_fields(data):
        if field == field_number and wire_type == 0:
            if not replaced:
                output.extend(integer(field_number, replacement))
                replaced = True
            continue
        if wire_type == 0:
            output.extend(integer(field, int(value)))
        elif wire_type in (1, 5):
            assert isinstance(value, bytes)
            output.extend(key(field, wire_type) + value)
        elif wire_type == 2:
            assert isinstance(value, bytes)
            output.extend(message(field, value))
    if not replaced:
        output.extend(integer(field_number, replacement))
    return bytes(output)


def decode(data: bytes) -> Any:
    import blackboxprotobuf

    value, _typedef = blackboxprotobuf.decode_message(data)
    return value


def text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value if isinstance(value, str) else ""


def lat_lng(lat: float, lng: float) -> bytes:
    return integer(1, round(lat * 1e7)) + integer(2, round(lng * 1e7))


def named_location(query: str, lat: float, lng: float) -> bytes:
    return message(1, lat_lng(lat, lng)) + message(8, string(1, query))


@dataclass(frozen=True)
class Place:
    name: str
    address: str
    latitude: float
    longitude: float
    place_id: str | None
    _ggq: bytes

    def public(self, index: int | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "address": self.address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "place_id": self.place_id,
        }
        if index is not None:
            result["index"] = index
        return result


def place_search_request(query: str, bias_lat: float, bias_lng: float) -> bytes:
    return (
        message(2, named_location(query, bias_lat, bias_lng))
        + string(3, query)
        + integer(5, 1)
    )


def parse_places(response: bytes) -> list[Place]:
    places: list[Place] = []
    for result in bytes_fields(response, 3):
        locations = bytes_fields(result, 1)
        if not locations:
            continue
        raw_location = locations[0]
        parsed = decode(raw_location)
        coords = parsed.get("1", {}) if isinstance(parsed, dict) else {}
        display = parsed.get("8", {}) if isinstance(parsed, dict) else {}
        if not isinstance(coords, dict) or not isinstance(display, dict):
            continue
        try:
            latitude = int(coords["1"]) * 1e-7
            longitude = int(coords["2"]) * 1e-7
        except (KeyError, TypeError, ValueError):
            continue
        lines = display.get("2", [])
        if not isinstance(lines, list):
            lines = [lines]
        address = ", ".join(filter(None, (text(line) for line in lines)))
        place_id = text(parsed.get("7")) or text(parsed.get("4")) or None
        places.append(
            Place(
                name=text(display.get("1")) or address,
                address=address,
                latitude=latitude,
                longitude=longitude,
                place_id=place_id,
                _ggq=raw_location,
            )
        )
    return places


def _waypoint(place: Place, uid: int) -> bytes:
    # ggw: desired location (3), SEARCH source (7), stable waypoint uid (9).
    return message(3, place._ggq) + integer(7, 2) + integer(9, uid)


def propose_trip_request(
    pickup: Place,
    dropoff: Place,
    passengers: int = 1,
    *,
    intermediate_stops: Sequence[Place] = (),
    scheduled_option_token: str | None = None,
    plan_template: bytes | None = None,
) -> bytes:
    if passengers < 1:
        raise ValueError("passengers must be at least 1")
    if len(intermediate_stops) > 3:
        raise ValueError("at most 3 intermediate stops are supported")

    places = [pickup, *intermediate_stops, dropoff]
    first_uid = int(time.time() * 1000)
    waypoint_uids = [first_uid + index for index in range(len(places))]
    passenger = integer(1, passengers)
    legs = [
        message(2, _waypoint(origin, waypoint_uids[index]))
        + message(3, _waypoint(destination, waypoint_uids[index + 1]))
        + message(8, passenger)
        for index, (origin, destination) in enumerate(zip(places, places[1:]))
    ]

    if plan_template:
        configurations = bytes_fields(plan_template, 3)
        configuration = (
            replace_integer(configurations[0], 1, passengers)
            if configurations
            else passenger
        )
        plan = replace_messages(plan_template, {3: configuration})
        plan = replace_repeated_messages(plan, 5, legs)
    else:
        configuration = passenger
        plan = integer(1, 1) + message(3, configuration)
        for leg in legs:
            plan += message(5, leg)
    request = (
        message(2, plan)
        + integer(3, 1)
        + integer(6, 7)
        + integer(7, 1)
        + integer(8, 0)
        + integer(13, 1)
    )
    if scheduled_option_token:
        request += string(10, scheduled_option_token)
    return request


def _money(data: bytes) -> tuple[str, str] | None:
    currencies = bytes_fields(data, 1)
    amounts = bytes_fields(data, 2)
    if not currencies or not amounts:
        return None
    currency = currencies[0].decode(errors="replace")
    amount = amounts[0].decode(errors="replace")
    try:
        if currency == "USD" and Decimal(amount) >= 0:
            return currency, amount
    except (InvalidOperation, ValueError):
        pass
    return None


def parse_quote(response: bytes) -> dict[str, Any]:
    offers = bytes_fields(response, 3)
    if not offers:
        raise ValueError("Waymo returned no trip proposal")
    offer = offers[0]
    tokens = bytes_fields(offer, 1)
    token = tokens[0].decode(errors="replace") if tokens else ""
    if not token:
        raise ValueError("Waymo returned a proposal without a booking token")

    # gjl.pricing (2) -> fws.standard_pricing (3) ->
    # fww.estimated_total (1) -> fxb { currency (1), amount (2) }.
    #
    # Do not run blackboxprotobuf over the full gjl response here. A real
    # proposal contains large repeated route messages that make its heuristic
    # schema inference pathologically expensive.
    prices = bytes_fields(offer, 2)
    standard = bytes_fields(prices[0], 3) if prices else []
    totals = bytes_fields(standard[0], 1) if standard else []
    estimated_total = _money(totals[0]) if totals else None
    try:
        pickup_eta, dropoff_eta = _etas(offer, _proposal_time(token))
    except ValueError:
        pickup_eta = dropoff_eta = None
    return {
        "proposal_token": token,
        "estimated_fare": f"${estimated_total[1]}" if estimated_total else None,
        "pickup_eta": pickup_eta,
        "dropoff_eta": dropoff_eta,
        "scheduled_options": [
            option
            for raw_option in bytes_fields(offer, 9)
            if (option := _scheduled_option(raw_option)) is not None
        ],
    }


def _proposal_time(token: str) -> float:
    # Proposal tokens are the server's creation time in microseconds; waypoint
    # ETAs are durations from that moment. Fall back to now if that changes.
    now = time.time()
    if token.isdigit() and len(token) == 16:
        created = int(token) / 1_000_000
        if abs(created - now) < 86_400:
            return created
    return now


def _duration(data: bytes) -> float | None:
    # google.protobuf.Duration { seconds (1), nanos (2) }
    fields = {f: v for f, wt, v in iter_fields(data) if wt == 0}
    if 1 not in fields:
        return None
    return fields[1] + fields.get(2, 0) / 1_000_000_000


def _waypoint_eta(waypoint: bytes) -> float | None:
    # Response waypoint: ETA duration from proposal creation (3).
    durations = bytes_fields(waypoint, 3)
    return _duration(durations[0]) if durations else None


def _local_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(round(timestamp), PACIFIC).isoformat()


def _etas(offer: bytes, base: float) -> tuple[dict[str, Any] | None, str | None]:
    # gjl.legs (4) -> { origin waypoint (1), destination waypoint (2) }.
    legs = bytes_fields(offer, 4)
    pickup_eta = dropoff_eta = None
    if legs:
        origins = bytes_fields(legs[0], 1)
        seconds = _waypoint_eta(origins[0]) if origins else None
        if seconds is not None:
            pickup_eta = {
                "at": _local_iso(base + seconds),
                "minutes": math.ceil(seconds / 60),
            }
        destinations = bytes_fields(legs[-1], 2)
        seconds = _waypoint_eta(destinations[0]) if destinations else None
        if seconds is not None:
            dropoff_eta = _local_iso(base + seconds)
    return pickup_eta, dropoff_eta


def _scheduled_option(data: bytes) -> dict[str, Any] | None:
    tokens = bytes_fields(data, 1)
    if not tokens:
        return None
    token = tokens[0].decode(errors="replace")
    times = bytes_fields(data, 6)
    labels = bytes_fields(data, 7)
    result: dict[str, Any] = {
        "token": token,
        "label": times[0].decode(errors="replace") if times else None,
        "subtitle": labels[0].decode(errors="replace") if labels else None,
        "is_asap": token == "scheduled_trip_token_asap",
    }
    if token.startswith("timestamp_"):
        try:
            timestamp = int(token.removeprefix("timestamp_"))
            result["departure_time"] = datetime.fromtimestamp(
                timestamp,
                timezone.utc,
            ).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return result


def ux_texts(data: bytes) -> list[str]:
    """Human-readable strings in a server-driven UI message, in wire order."""
    texts: list[str] = []

    def human(value: bytes) -> str | None:
        try:
            rendered = value.decode()
        except UnicodeDecodeError:
            return None
        if not rendered or rendered[0] in "\n\x12\x1a" or not re.search(r"[A-Za-z]", rendered):
            return None
        if any(not (ch.isprintable() or ch == "\n") for ch in rendered):
            return None
        # Tokens, ids and URLs, not copy.
        if " " not in rendered and (
            len(rendered) > 24 or "://" in rendered or "_" in rendered
        ):
            return None
        return rendered.strip()

    def walk(current: bytes, depth: int) -> None:
        try:
            fields = list(iter_fields(current))
        except ValueError:
            return
        for _field, wire_type, value in fields:
            if wire_type != 2 or not isinstance(value, bytes):
                continue
            rendered = human(value)
            if rendered:
                texts.append(rendered)
            elif depth < 40:
                walk(value, depth + 1)

    walk(data, 0)
    return texts


_DOLLARS = re.compile(r"\$\s*(\d+(?:\.\d{2})?)")
_FREE = re.compile(
    r"\b(?:for free|free(?: of charge)?|no (?:cancellation )?(?:fee|charge)|"
    r"won[’']?t be charged|will not be charged|not be charged|at no cost|waived)\b",
    re.IGNORECASE,
)
_FREE_ENDED = re.compile(r"\b(?:ended|expired|no longer|has passed)\b", re.IGNORECASE)
_UNTIL_CLOCK = re.compile(
    r"\b(?:until|before|by)\s+(\d{1,2}:\d{2})\s*([AaPp]\.?[Mm]\.?)?",
    re.IGNORECASE,
)
_MINUTES = re.compile(
    r"\b(?:within|in the next|for (?:the next |another )?|next|after)\s*(\d+)\s*"
    r"(?:more\s+)?(?:min|minute)s?\b",
    re.IGNORECASE,
)


def parse_cancellation_ux(data: bytes, now: float | None = None) -> dict[str, Any]:
    """What the app's cancel sheet (GetCancellationFeeUx) says the rider pays now.

    The Android app (com.waymo.carapp 26092803) never calls ProposeTripCancellation:
    it calls GetCancellationFeeUx and shows the returned server-driven bottom sheet
    (response field 2 -> {1: sheet}) as is. That sheet's copy is the fee the rider
    sees, including free-cancel windows; ProposeTripCancellation's money is only
    the policy amount, which isn't charged inside the free window.
    """
    now = time.time() if now is None else now
    texts = ux_texts(data)
    joined = " ".join(texts)
    amounts = [f"${amount}" for amount in _DOLLARS.findall(joined)]
    free = bool(_FREE.search(joined)) and not _FREE_ENDED.search(joined)
    if free:
        fee: str | None = "$0.00"
        fee_after = next(
            (amount for amount in amounts if amount not in {"$0", "$0.00"}), None
        )
        free_until = None
        clock = _UNTIL_CLOCK.search(joined)
        minutes = _MINUTES.search(joined)
        if clock:
            free_until = _clock_today(clock.group(1), clock.group(2), now)
        elif minutes:
            free_until = _local_iso(now + int(minutes.group(1)) * 60)
    else:
        fee = amounts[0] if amounts else None
        fee_after = free_until = None
    return {
        "cancellation_fee": fee,
        "free_cancellation": free if (free or amounts) else None,
        "free_until": free_until,
        "fee_after_free_window": fee_after,
        "cancel_sheet_text": texts,
    }


def _clock_today(clock: str, meridiem: str | None, now: float) -> str | None:
    hour, minute = (int(part) for part in clock.split(":"))
    if meridiem:
        hour = hour % 12 + (12 if meridiem.lower().startswith("p") else 0)
    if hour > 23 or minute > 59:
        return None
    local = datetime.fromtimestamp(now, PACIFIC)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def parse_cancellation_fee(data: bytes) -> str | None:
    """Extract a structured or rendered USD fee from cancellation preview UX."""
    pending = [data]
    seen: set[bytes] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        money = _money(current)
        if money:
            return f"${money[1]}"
        try:
            fields = list(iter_fields(current))
        except ValueError:
            continue
        for _field, wire_type, value in fields:
            if wire_type != 2 or not isinstance(value, bytes):
                continue
            try:
                rendered = value.decode()
            except UnicodeDecodeError:
                rendered = ""
            match = re.search(r"\$\s*\d+(?:\.\d{2})?", rendered)
            if match:
                return match.group(0).replace(" ", "")
            pending.append(value)
    return None


# CreateTripRequest, as the Android app (com.waymo.carapp) builds it for a saved card:
#   8 proposal token, 5 = 7 (Android client), 6 = the payment method id from
#   ListPaymentMethods (its field 3), 12 = PaymentMethodInfo {3: 3 (card on file)}.
# PaymentMethodInfo field 1 is a Braintree nonce, used only for new Google Pay /
# PayPal payments; putting a saved card's token there made Waymo try to charge it
# as a nonce, failing with FAILED_PRECONDITION "Failed auth charge".
def create_trip_request(proposal_token: str, payment_method_id: str) -> bytes:
    return (
        integer(5, 7)
        + string(6, payment_method_id)
        + string(8, proposal_token)
        + message(12, integer(3, 3))
    )


def cancel_trip_request(cancellation_token: str) -> bytes:
    return integer(3, 7) + string(4, cancellation_token)
