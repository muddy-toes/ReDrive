"""Tests for Room class (server.py)."""

import asyncio
import time
from unittest.mock import patch, MagicMock

import pytest
from server import Room, _ROOM_EXPIRY, _DRIVER_GRACE


@pytest.fixture
def event_loop_for_room():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def room(event_loop_for_room):
    return Room("TESTCODE01", event_loop_for_room)


@pytest.fixture
def waiting_room(event_loop_for_room):
    return Room("WAITCODE01", event_loop_for_room, waiting=True)


class TestRoomCreation:
    def test_room_creation(self, room):
        assert room.code == "TESTCODE01"
        assert room.driver_key != ""
        assert room.engine is not None

    def test_room_not_expired_fresh(self, room):
        assert room.expired() is False

    def test_room_expired_24h(self, room):
        with patch("time.monotonic", return_value=room.created_at + _ROOM_EXPIRY + 1):
            assert room.expired() is True

    def test_room_expired_driver_idle(self, room):
        with patch("time.monotonic", return_value=room.driver_last_seen + _DRIVER_GRACE + 1):
            assert room.expired() is True

    def test_touch_driver_resets(self, room):
        old_seen = room.driver_last_seen
        # Simulate some time passing
        with patch("time.monotonic", return_value=old_seen + 100):
            room.touch_driver()
            # After touch, driver_last_seen should be updated to "now"
            assert room.driver_last_seen == old_seen + 100

    def test_waiting_room_no_engine(self, waiting_room):
        assert waiting_room.engine is None
        assert waiting_room.waiting is True
        assert waiting_room.driver_key == ""

    def test_rider_count(self, room):
        assert room.rider_count == 0
        room.rider_wss.add(MagicMock())
        assert room.rider_count == 1
        room.rider_wss.add(MagicMock())
        assert room.rider_count == 2
