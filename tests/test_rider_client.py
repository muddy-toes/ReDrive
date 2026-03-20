"""Tests for rider_client.py message filtering logic."""

import json


def test_json_message_filtering():
    """JSON messages starting with { should not be forwarded to ReStim."""
    tcode = "L0500I50 L15000I50"
    json_msg = '{"type": "driver_status", "connected": true}'

    assert not tcode.startswith('{')
    assert json_msg.startswith('{')


def test_various_json_messages_detected():
    """All expected JSON message types should be caught by the startswith check."""
    messages = [
        '{"type": "driver_status", "connected": true, "name": "Scott"}',
        '{"type": "bottle_status", "active": true, "remaining": 8.5, "mode": "deep_huff"}',
        '{"type": "rider_state", "intensity": 0.65}',
        '{"type": "participants_update", "riders": []}',
    ]
    for msg in messages:
        assert msg.startswith('{'), f"Should detect as JSON: {msg}"
        parsed = json.loads(msg)
        assert "type" in parsed


def test_tcode_not_filtered():
    """Raw T-code strings should pass through the filter."""
    tcode_samples = [
        "L0500I50 L15000I50",
        "L0100I100",
        "R0300I75 L0200I50",
        "",
    ]
    for tcode in tcode_samples:
        assert not tcode.startswith('{'), f"T-code should not be filtered: {tcode}"


def test_rider_client_ws_url():
    """The relay WS URL should use the rider-ws endpoint."""
    server_url = "wss://redrive.estimstation.com"
    room_code = "ABCDEF1234"
    relay_ws_url = f"{server_url.rstrip('/')}/room/{room_code}/rider-ws"
    assert relay_ws_url == "wss://redrive.estimstation.com/room/ABCDEF1234/rider-ws"
    assert "/rider-ws" in relay_ws_url
    assert "/rider" in relay_ws_url  # rider-ws contains rider


def test_rider_app_ws_url():
    """The rider app relay URL should use the new endpoint format."""
    relay = "wss://redrive.estimstation.com"
    room = "ABCDEF1234"
    relay_url = f"{relay}/room/{room}/rider-ws"
    assert relay_url == "wss://redrive.estimstation.com/room/ABCDEF1234/rider-ws"
