from __future__ import annotations

import pytest

from waymo_mcp.client import _active_trip, _normalize_trip_id
from waymo_mcp.wire import (
    Place,
    bytes_fields,
    decode,
    lat_lng,
    message,
    parse_places,
    parse_quote,
    place_search_request,
    propose_trip_request,
    string,
)


def sample_place(name: str, lat: float, lng: float) -> Place:
    display = string(1, name) + string(2, f"{name} address")
    location = message(1, lat_lng(lat, lng)) + message(8, display)
    return Place(name, f"{name} address", lat, lng, None, location)


def test_place_search_request_fields() -> None:
    request = place_search_request("Oracle", 37.5, -122.2)
    assert bytes_fields(request, 2)
    assert bytes_fields(request, 3) == [b"Oracle"]


def test_place_response_parser() -> None:
    location = sample_place("Oracle", 37.529, -122.266)._ggq
    places = parse_places(message(3, message(1, location)))
    assert len(places) == 1
    assert places[0].name == "Oracle"
    assert places[0].latitude == pytest.approx(37.529)


def test_trip_plan_uses_verified_fields() -> None:
    request = propose_trip_request(
        sample_place("Pickup", 37.51, -122.25),
        sample_place("Dropoff", 37.53, -122.27),
    )
    plan = bytes_fields(request, 2)[0]
    leg = bytes_fields(plan, 5)[0]
    assert bytes_fields(leg, 2)
    assert bytes_fields(leg, 3)
    assert bytes_fields(leg, 8)
    assert not bytes_fields(leg, 4)
    assert decode(request)["13"] == 1


def test_template_replacement_removes_stale_waypoints() -> None:
    stale = propose_trip_request(
        sample_place("Stale pickup", 1.0, 2.0),
        sample_place("Stale dropoff", 3.0, 4.0),
    )
    request = propose_trip_request(
        sample_place("New pickup", 37.51, -122.25),
        sample_place("New dropoff", 37.53, -122.27),
        plan_template=bytes_fields(stale, 2)[0],
    )
    leg = decode(request)["2"]["5"]
    assert leg["2"]["3"]["8"]["1"] == b"New pickup"
    assert leg["3"]["3"]["8"]["1"] == b"New dropoff"


def test_quote_parser_reads_proposal_and_fare() -> None:
    money = string(1, "USD") + string(2, "12.34")
    standard_pricing = message(1, money)
    pricing = message(3, standard_pricing)
    offer = string(1, "proposal-123") + message(2, pricing)
    quote = parse_quote(message(3, offer))
    assert quote == {
        "proposal_token": "proposal-123",
        "estimated_fare": "$12.34",
    }


def test_quote_parser_ignores_large_route_payload() -> None:
    money = string(1, "USD") + string(2, "12.34")
    pricing = message(3, message(1, money))
    route = message(4, b"\xff" * 1_000_000)
    offer = string(1, "proposal-123") + message(2, pricing) + route
    assert parse_quote(message(3, offer))["estimated_fare"] == "$12.34"


def test_active_trip_parser() -> None:
    decoded = {
        "2": {
            "1": b"123",
            "5": {
                "5": {
                    "2": {
                        "3": {
                            "1": {"1": 375100000, "2": -1222500000},
                            "8": {"1": b"Pickup", "2": b"Pickup address"},
                        }
                    },
                    "3": {
                        "3": {
                            "1": {"1": 375300000, "2": -1222700000},
                            "8": {"1": b"Dropoff", "2": b"Dropoff address"},
                        }
                    },
                }
            },
        }
    }
    active = _active_trip(decoded)
    assert active is not None
    assert active["pickup"]["name"] == "Pickup"
    assert active["dropoff"]["name"] == "Dropoff"


def test_history_trip_id_normalization() -> None:
    assert _normalize_trip_id("ChAxNzg4MzMwNTgxNDg3ODQ0") == "1788330581487844"
