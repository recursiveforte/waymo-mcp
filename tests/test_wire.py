from __future__ import annotations

import pytest

from waymo_mcp.client import _active_trip, _normalize_trip_id
from waymo_mcp.wire import (
    Place,
    bytes_fields,
    decode,
    integer,
    lat_lng,
    message,
    parse_cancellation_fee,
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
    assert decode(bytes_fields(leg, 8)[0])["1"] == 1
    assert decode(request)["13"] == 1


def test_multi_stop_plan_builds_linked_legs() -> None:
    request = propose_trip_request(
        sample_place("Pickup", 37.51, -122.25),
        sample_place("Dropoff", 37.54, -122.28),
        intermediate_stops=[
            sample_place("Stop 1", 37.52, -122.26),
            sample_place("Stop 2", 37.53, -122.27),
        ],
    )
    legs = bytes_fields(bytes_fields(request, 2)[0], 5)
    assert len(legs) == 3

    decoded_legs = [decode(leg) for leg in legs]
    assert decoded_legs[0]["2"]["3"]["8"]["1"] == b"Pickup"
    assert decoded_legs[0]["3"]["3"]["8"]["1"] == b"Stop 1"
    assert decoded_legs[1]["2"]["3"]["8"]["1"] == b"Stop 1"
    assert decoded_legs[1]["3"]["3"]["8"]["1"] == b"Stop 2"
    assert decoded_legs[2]["2"]["3"]["8"]["1"] == b"Stop 2"
    assert decoded_legs[2]["3"]["3"]["8"]["1"] == b"Dropoff"
    assert decoded_legs[0]["3"]["9"] == decoded_legs[1]["2"]["9"]
    assert decoded_legs[1]["3"]["9"] == decoded_legs[2]["2"]["9"]


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
        "pickup_eta": None,
        "dropoff_eta": None,
        "scheduled_options": [],
    }


def _eta_waypoint(seconds: int, nanos: int) -> bytes:
    # Response waypoint: ETA as a Duration from proposal creation (field 3).
    return message(1, b"") + message(3, integer(1, seconds) + integer(2, nanos))


def test_quote_parser_reads_pickup_and_dropoff_etas(monkeypatch) -> None:
    # Shape and values from a live quote (85 Burlwood Dr -> 500 Howard St):
    # the token is the creation time in µs; the app showed "Pickup in 6 min".
    monkeypatch.setattr("time.time", lambda: 1794431610.0)
    legs = [
        message(1, _eta_waypoint(339, 34484863))
        + message(2, _eta_waypoint(1200, 0)),
        message(1, _eta_waypoint(1300, 0))
        + message(2, _eta_waypoint(2259, 36288219)),
    ]
    offer = string(1, "1794431607252710") + b"".join(message(4, leg) for leg in legs)
    quote = parse_quote(message(3, offer))
    assert quote["pickup_eta"] == {"at": "2026-11-11T13:19:06-08:00", "minutes": 6}
    assert quote["dropoff_eta"] == "2026-11-11T13:51:06-08:00"


def test_quote_eta_falls_back_to_now_for_opaque_tokens(monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1794431607.0)
    leg = message(1, _eta_waypoint(300, 0)) + message(2, _eta_waypoint(1500, 0))
    quote = parse_quote(message(3, string(1, "proposal-123") + message(4, leg)))
    assert quote["pickup_eta"] == {"at": "2026-11-11T13:18:27-08:00", "minutes": 5}
    assert quote["dropoff_eta"] == "2026-11-11T13:38:27-08:00"


def test_quote_parser_ignores_large_route_payload() -> None:
    money = string(1, "USD") + string(2, "12.34")
    pricing = message(3, message(1, money))
    route = message(4, b"\xff" * 1_000_000)
    offer = string(1, "proposal-123") + message(2, pricing) + route
    assert parse_quote(message(3, offer))["estimated_fare"] == "$12.34"


def test_cancellation_fee_parser_reads_nested_money() -> None:
    fee = string(1, "USD") + string(2, "5.00")
    assert parse_cancellation_fee(message(2, message(7, fee))) == "$5.00"


def test_quote_parser_returns_scheduled_options() -> None:
    money = string(1, "USD") + string(2, "12.34")
    pricing = message(3, message(1, money))
    scheduled = (
        string(1, "timestamp_1788337500")
        + string(6, "1:25 AM")
        + integer(9, 2)
    )
    offer = (
        string(1, "proposal-123")
        + message(2, pricing)
        + message(9, scheduled)
    )
    options = parse_quote(message(3, offer))["scheduled_options"]
    assert options == [
        {
            "token": "timestamp_1788337500",
            "label": "1:25 AM",
            "subtitle": None,
            "is_asap": False,
            "departure_time": "2026-09-02T08:25:00+00:00",
        }
    ]


def test_scheduled_quote_request_places_token_in_verified_field() -> None:
    request = propose_trip_request(
        sample_place("Pickup", 37.51, -122.25),
        sample_place("Dropoff", 37.54, -122.28),
        scheduled_option_token="timestamp_1788337500",
    )
    decoded = decode(request)
    assert decoded["10"] == b"timestamp_1788337500"


def test_active_trip_parser() -> None:
    decoded = {
        "2": {
            "1": b"123",
            "2": {
                "1": b"vehicle-7",
                "2": {"1": 375200000, "2": -1222600000},
                "3": 182500,
                "7": {"1": 1788337500},
                "8": 3,
                "10": b"ABC123",
                "18": 15,
            },
            "4": {"1": 30},
            "5": {
                "5": {
                    "1": 1,
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
                    "4": {"1": {"1": 4000000000}},
                }
            },
            "16": b"https://waymo.example/trip/123",
            "17": {
                "1": 1,
                "4": {"1": 375200000, "2": -1222600000},
            },
        }
    }
    active = _active_trip(decoded)
    assert active is not None
    assert active["pickup"]["name"] == "Pickup"
    assert active["dropoff"]["name"] == "Dropoff"
    assert active["state"] == "TO_PICKUP"
    assert active["vehicle"]["license_plate"] == "ABC123"
    assert active["vehicle"]["position"]["latitude"] == pytest.approx(37.52)
    assert active["vehicle"]["heading_degrees"] == pytest.approx(182.5)
    assert active["vehicle"]["speed_mph"] == 15
    assert active["vehicle"]["type"] == "LIBERTY"
    assert active["pickup_eta_seconds"] > 0
    assert active["tracking_url"] == "https://waymo.example/trip/123"
    assert active["arrival"]["state"] == "DISPATCHED"


def test_history_trip_id_normalization() -> None:
    assert _normalize_trip_id("ChAxNzg4MzMwNTgxNDg3ODQ0") == "1788330581487844"


def test_closest_picks_the_nearest_place() -> None:
    from waymo_mcp.client import closest
    from waymo_mcp.wire import Place

    showpad = Place("Showpad", "301 Howard St #500", 37.7897, -122.3943, None, b"")
    howard = Place("500 Howard St", "500 Howard St", 37.7884, -122.3965, None, b"")
    assert closest([showpad, howard], (37.7883, -122.3963)) is howard
