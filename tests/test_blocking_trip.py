from waymo_mcp.client import _blocking_trip


def test_finished_trip_does_not_block_a_new_ride():
    assert not _blocking_trip({"active_trip": None})
    assert not _blocking_trip({"active_trip": {"state_code": 110}})
    assert not _blocking_trip({"active_trip": {"state_code": 120}})
    assert _blocking_trip({"active_trip": {"state_code": 70}})
