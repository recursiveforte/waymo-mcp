"""Minimal protobuf wire codec for the Waymo RPCs used by this server."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator


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


def _waypoint(place: Place, role: int) -> bytes:
    uid = secrets.randbelow(2**62 - 1) + 1
    return message(3, place._ggq) + integer(7, role) + integer(9, uid)


def propose_trip_request(
    pickup: Place,
    dropoff: Place,
    passengers: int = 1,
    *,
    plan_template: bytes | None = None,
) -> bytes:
    if passengers < 1:
        raise ValueError("passengers must be at least 1")
    passenger_value = passengers - 1
    passenger = integer(1, passenger_value) if passenger_value else b""
    if plan_template:
        legs = bytes_fields(plan_template, 5)
        if not legs:
            raise ValueError("trip plan template has no leg")
        leg = replace_messages(
            legs[0],
            {2: _waypoint(pickup, 2), 3: _waypoint(dropoff, 7)},
        )
        plan = replace_messages(plan_template, {5: leg})
    else:
        leg = (
            message(2, _waypoint(pickup, 2))
            + message(3, _waypoint(dropoff, 7))
            + message(8, passenger)
        )
        plan = integer(1, 1) + message(3, passenger) + message(5, leg)
    return (
        message(2, plan)
        + integer(3, 1)
        + integer(6, 7)
        + integer(7, 1)
        + integer(8, 0)
        + integer(13, 1)
    )


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
    return {
        "proposal_token": token,
        "estimated_fare": f"${estimated_total[1]}" if estimated_total else None,
    }


def create_trip_request(proposal_token: str, payment_token: str) -> bytes:
    payment = string(1, payment_token) + integer(3, 3)
    return string(8, proposal_token) + integer(5, 1) + message(12, payment)


def cancel_trip_request(cancellation_token: str) -> bytes:
    return integer(3, 7) + string(4, cancellation_token)
