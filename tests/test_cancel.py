from __future__ import annotations

from typing import Any

from waymo_mcp import client as client_module
from waymo_mcp.client import (
    WaymoClient,
    _trip_state_after_cancel,
    cancellation_fee_summary,
)
from waymo_mcp.wire import (
    integer,
    message,
    parse_cancellation_ux,
    string,
    ux_texts,
)

# 2026-11-11 14:30 PST
NOW = 1794436200.0
USD_499 = message(7, string(1, "USD") + string(2, "4.99"))
PROPOSAL = string(2, "cancel-token") + USD_499


def trip(trip_id: str, state: int) -> bytes:
    return string(1, trip_id) + message(4, integer(1, state))


def sheet(*lines: str) -> bytes:
    """GetCancellationFeeUx response: {2: {1: sheet}}, copy nested like the app's UI."""
    title = message(1, message(3, message(2, string(2, lines[0]))))
    body = b"".join(message(7, message(1, string(2, line))) for line in lines[1:])
    buttons = message(3, message(1, string(1, "cancel_button_token_abc") + message(5, string(2, "Cancel ride"))))
    return message(2, message(1, title + body + buttons))


def test_ux_texts_skips_tokens_and_keeps_copy() -> None:
    texts = ux_texts(sheet("Cancel ride?", "You can cancel for free"))
    assert texts == ["Cancel ride?", "You can cancel for free", "Cancel ride"]


def test_free_window_with_clock_time() -> None:
    ux = parse_cancellation_ux(
        sheet("Cancel ride?", "Cancel for free until 2:33 PM. After that, a $4.99 fee applies."),
        now=NOW,
    )
    assert ux["cancellation_fee"] == "$0.00"
    assert ux["free_cancellation"] is True
    assert ux["free_until"] == "2026-11-11T14:33:00-08:00"
    assert ux["fee_after_free_window"] == "$4.99"


def test_free_window_in_minutes() -> None:
    ux = parse_cancellation_ux(sheet("Cancel ride?", "No fee if you cancel within 2 minutes"), now=NOW)
    assert ux["cancellation_fee"] == "$0.00"
    assert ux["free_until"] == "2026-11-11T14:32:00-08:00"


def test_fee_applies() -> None:
    ux = parse_cancellation_ux(sheet("Cancel ride?", "You'll be charged a $4.99 cancellation fee"), now=NOW)
    assert ux["cancellation_fee"] == "$4.99"
    assert ux["free_cancellation"] is False
    assert ux["free_until"] is None


def test_ended_free_window_is_not_free() -> None:
    ux = parse_cancellation_ux(
        sheet("Cancel ride?", "Your free cancellation window has ended. A $4.99 fee applies."),
        now=NOW,
    )
    assert ux["cancellation_fee"] == "$4.99"


def test_policy_fee_is_not_reported_when_the_sheet_says_free() -> None:
    # The real bug: ProposeTripCancellation said $4.99, the app showed free.
    summary = cancellation_fee_summary(PROPOSAL, sheet("Cancel ride?", "Cancel for free"))
    assert summary["cancellation_fee"] == "$0.00"
    assert summary["fee_after_free_window"] == "$4.99"
    assert summary["fee_source"] == "app_cancel_sheet"


def test_without_sheet_fee_is_unknown_not_the_policy_amount() -> None:
    summary = cancellation_fee_summary(PROPOSAL, None)
    assert summary["cancellation_fee"] is None
    assert summary["fee_available"] is False
    assert summary["policy_fee"] == "$4.99"


class FakeRpc(WaymoClient):
    def __init__(self, responses: dict[str, list[bytes]]):
        self.responses = responses
        self.calls: list[str] = []

    def _unary(self, operation: str, payload: bytes = b"", timeout: int = 20) -> bytes:
        self.calls.append(operation)
        return self.responses[operation].pop(0)

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return []


def status(trip_id: str, state: int) -> bytes:
    return message(2, trip(trip_id, state))


def test_cancel_reads_cancelled_state_from_response() -> None:
    fake = FakeRpc(
        {
            "status": [status("1794436306624102", 30)],
            "cancel": [message(2, trip("1794436306624102", 120))],
        }
    )
    result = fake.confirm_cancel("cancel-token", "1794436306624102")
    assert result == {"cancelled": True, "trip_id": "1794436306624102", "state": "CANCELLED"}
    assert fake.calls == ["status", "cancel"]


def test_cancel_with_empty_response_checks_status_and_accepts_lingering_cancelled_trip() -> None:
    # The real bug: the trip stays in GetActiveTrip with state 120 after cancelling.
    fake = FakeRpc(
        {
            "status": [status("1794436306624102", 30), status("1794436306624102", 120)],
            "cancel": [b""],
        }
    )
    result = fake.confirm_cancel("cancel-token", "1794436306624102")
    assert result["cancelled"] is True
    assert result["state"] == "CANCELLED"


def test_cancel_reports_failure_when_trip_still_en_route() -> None:
    fake = FakeRpc(
        {
            "status": [status("1794436306624102", 30), status("1794436306624102", 30)],
            "cancel": [b""],
        }
    )
    result = fake.confirm_cancel("cancel-token", "1794436306624102")
    assert result["cancelled"] is False
    assert result["state"] == "TO_PICKUP"


def test_trip_gone_from_status_counts_as_cancelled() -> None:
    assert _trip_state_after_cancel({"active_trip": None, "stale_trip": None}, "1") == "CANCELLED"


def test_preview_uses_cancel_sheet_fee() -> None:
    fake = FakeRpc(
        {
            "status": [status("1794436306624102", 30)],
            "propose_cancel": [PROPOSAL],
            "cancel_fee_ux": [sheet("Cancel ride?", "Cancel for free")],
        }
    )
    preview = fake.preview_cancel()
    assert preview["cancellation_fee"] == "$0.00"
    assert preview["_cancellation_token"] == "cancel-token"


def test_preview_refuses_already_cancelled_trip() -> None:
    fake = FakeRpc({"status": [status("1794436306624102", 120)]})
    assert fake.preview_cancel() == {"can_cancel": False, "reason": "no_active_trip"}


assert client_module.CANCELLED_STATE == 120
