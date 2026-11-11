from __future__ import annotations

from typing import Any

import pytest
from mcp import Client

from waymo_mcp import server
from waymo_mcp.wire import Place, lat_lng, message, string


def place(name: str) -> Place:
    location = message(1, lat_lng(37.5, -122.2)) + message(8, string(1, name))
    return Place(name, f"{name} address", 37.5, -122.2, None, location)


class FakeWaymoClient:
    def __init__(self) -> None:
        self.book_calls: list[tuple[str, str]] = []
        self.quote_calls: list[dict[str, Any]] = []
        self.preview_cancel_calls = 0
        self.cancel_calls = 0

    def search_places(self, query: str, **_: Any) -> list[Place]:
        return [place(query)]

    def history(self, limit: int) -> list[dict[str, Any]]:
        return [{"trip_id": "1", "status": "Completed"}][:limit]

    def status(self) -> dict[str, Any]:
        return {"active_trip": None, "stale_trip": None}

    def payment_methods(self) -> list[dict[str, Any]]:
        return [{"id": "card-1", "type": "card", "label": "Card ending in 1234"}]

    def quote(self, pickup: str, dropoff: str, **options: Any) -> dict[str, Any]:
        self.quote_calls.append(options)
        stops = options.get("intermediate_stop_queries") or []
        return {
            "proposal_token": "proposal-1",
            "estimated_fare": "$12.34",
            "pickup": place(pickup).public(),
            "intermediate_stops": [place(stop).public() for stop in stops],
            "dropoff": place(dropoff).public(),
            "passengers": 1,
        }

    def book(self, token: str, payment_id: str) -> dict[str, Any]:
        self.book_calls.append((token, payment_id))
        return {"booked": True, "active_trip": {"trip_id": "2"}}

    def preview_cancel(self) -> dict[str, Any]:
        self.preview_cancel_calls += 1
        return {
            "can_cancel": True,
            "trip_id": "2",
            "cancellation_fee": "$0.00",
            "fee_available": True,
            "_cancellation_token": "cancel-token",
        }

    def confirm_cancel(self, token: str, trip_id: str) -> dict[str, Any]:
        assert token == "cancel-token"
        assert trip_id == "2"
        self.cancel_calls += 1
        return {"cancelled": True, "trip_id": "2"}


@pytest.fixture(autouse=True)
def fake_waymo() -> FakeWaymoClient:
    fake = FakeWaymoClient()
    server._client = fake  # type: ignore[assignment]
    server._quotes.clear()
    server._cancel_preview = None
    yield fake
    server._client = None
    server._quotes.clear()
    server._cancel_preview = None


@pytest.mark.asyncio
async def test_server_exposes_only_the_eight_core_tools() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.list_tools()
    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {
        "waymo_search_places",
        "waymo_ride_history",
        "waymo_ride_status",
        "waymo_list_payment_methods",
        "waymo_get_quote",
        "waymo_book_ride",
        "waymo_preview_cancellation",
        "waymo_cancel_ride",
    }
    assert tools["waymo_ride_status"].annotations.read_only_hint is True
    assert tools["waymo_book_ride"].annotations.destructive_hint is True
    assert tools["waymo_book_ride"].annotations.idempotent_hint is False


async def _quote(client: Client) -> dict[str, Any]:
    quote = await client.call_tool(
        "waymo_get_quote",
        {"pickup": "SFO", "dropoff": "Oracle"},
    )
    assert quote.is_error is False
    return quote.structured_content


async def _book(client: Client, quote_id: str, fare: str | None = "$12.34") -> Any:
    return await client.call_tool(
        "waymo_book_ride",
        {"quote_id": quote_id, "expected_fare": fare, "confirmation": "BOOK"},
    )


@pytest.mark.asyncio
async def test_quote_then_book_uses_server_issued_quote_id(
    fake_waymo: FakeWaymoClient,
) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        quote = await _quote(client)
        assert "proposal_token" not in quote
        booked = await _book(client, quote["quote_id"])
    assert booked.is_error is False
    assert fake_waymo.book_calls == [("proposal-1", "card-1")]


@pytest.mark.asyncio
async def test_quote_id_is_single_use(fake_waymo: FakeWaymoClient) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        quote = await _quote(client)
        first = await _book(client, quote["quote_id"])
        second = await _book(client, quote["quote_id"])
    assert first.is_error is False
    assert second.is_error is True
    assert len(fake_waymo.book_calls) == 1


@pytest.mark.asyncio
async def test_booking_rejects_raw_waymo_proposal_token(
    fake_waymo: FakeWaymoClient,
) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        await _quote(client)
        result = await _book(client, "proposal-1")
    assert result.is_error is True
    assert fake_waymo.book_calls == []


@pytest.mark.asyncio
async def test_booking_rejects_fare_mismatch(fake_waymo: FakeWaymoClient) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        quote = await _quote(client)
        result = await _book(client, quote["quote_id"], "$1.00")
        retry = await _book(client, quote["quote_id"])
    assert result.is_error is True
    assert retry.is_error is True  # a failed attempt still burns the quote
    assert fake_waymo.book_calls == []


@pytest.mark.asyncio
async def test_booking_rejects_expired_quote(
    fake_waymo: FakeWaymoClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        quote = await _quote(client)
        created_at, record = server._quotes[quote["quote_id"]]
        server._quotes[quote["quote_id"]] = (
            created_at - server.QUOTE_TTL_SECONDS - 1,
            record,
        )
        result = await _book(client, quote["quote_id"])
    assert result.is_error is True
    assert fake_waymo.book_calls == []


@pytest.mark.asyncio
async def test_booking_requires_literal_confirmation(
    fake_waymo: FakeWaymoClient,
) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        quote = await _quote(client)
        result = await client.call_tool(
            "waymo_book_ride",
            {
                "quote_id": quote["quote_id"],
                "expected_fare": "$12.34",
                "confirmation": "yes",
            },
        )
    assert result.is_error is True
    assert fake_waymo.book_calls == []


@pytest.mark.asyncio
async def test_quote_accepts_ordered_intermediate_stops(
    fake_waymo: FakeWaymoClient,
) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        quote = await client.call_tool(
            "waymo_get_quote",
            {
                "pickup": "SFO",
                "intermediate_stops": ["Stop 1", "Stop 2"],
                "intermediate_stop_result_indices": [1, 2],
                "dropoff": "Oracle",
                "scheduled_option_token": "timestamp_1788337500",
            },
        )
    assert quote.is_error is False
    assert [
        stop["name"] for stop in quote.structured_content["intermediate_stops"]
    ] == ["Stop 1", "Stop 2"]
    assert fake_waymo.quote_calls[-1]["intermediate_stop_queries"] == [
        "Stop 1",
        "Stop 2",
    ]
    assert fake_waymo.quote_calls[-1]["intermediate_stop_indices"] == [1, 2]
    assert (
        fake_waymo.quote_calls[-1]["scheduled_option_token"]
        == "timestamp_1788337500"
    )
    assert quote.structured_content["waymo_expires_at"] is None
    assert quote.structured_content["mcp_cache_ttl_seconds"] == 300
    assert quote.structured_content["mcp_cache_expires_at"]


@pytest.mark.asyncio
async def test_booking_rejects_unknown_proposal_token(
    fake_waymo: FakeWaymoClient,
) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "waymo_book_ride",
            {
                "quote_id": "not-issued-here",
                "expected_fare": "$12.34",
                "confirmation": "BOOK",
            },
        )
    assert result.is_error is True
    assert fake_waymo.book_calls == []


@pytest.mark.asyncio
async def test_cancel_requires_literal_confirmation(
    fake_waymo: FakeWaymoClient,
) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        preview = await client.call_tool("waymo_preview_cancellation", {})
        rejected = await client.call_tool(
            "waymo_cancel_ride",
            {"confirmation": "yes"},
        )
        accepted = await client.call_tool(
            "waymo_cancel_ride",
            {"confirmation": "CANCEL"},
        )
    assert rejected.is_error is True
    assert preview.structured_content["cancellation_fee"] == "$0.00"
    assert "_cancellation_token" not in preview.structured_content
    assert accepted.is_error is False
    assert fake_waymo.preview_cancel_calls == 1
    assert fake_waymo.cancel_calls == 1


@pytest.mark.asyncio
async def test_cancel_rejects_missing_preview(
    fake_waymo: FakeWaymoClient,
) -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "waymo_cancel_ride",
            {"confirmation": "CANCEL"},
        )
    assert result.is_error is True
    assert fake_waymo.cancel_calls == 0
