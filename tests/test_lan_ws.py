"""Tests for LAN mode WebSocket endpoints (/driver-ws, /rider-ws).

These test the WS handlers added to DriveEngine's aiohttp app, using
the _build_app() method to get a testable Application instance.
"""

import json
import asyncio
import pytest
import aiohttp


class TestLanDriverWS:
    @pytest.mark.asyncio
    async def test_driver_ws_connect(self, aiohttp_client, drive_engine):
        """Driver can connect to /driver-ws in LAN mode."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/driver-ws')
        msg = await ws.receive_json()
        assert msg['type'] == 'state'
        assert 'data' in msg
        assert 'pattern' in msg['data']
        assert 'intensity' in msg['data']
        await ws.close()

    @pytest.mark.asyncio
    async def test_driver_ws_command(self, aiohttp_client, drive_engine):
        """Commands sent over WS are processed."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/driver-ws')
        await ws.receive_json()  # initial state

        await ws.send_json({"type": "command", "data": {"pattern": "Sine"}})
        ack = await ws.receive_json()
        assert ack['type'] == 'command_ack'
        assert ack['ok'] is True
        assert drive_engine._pattern.pattern == "Sine"
        await ws.close()

    @pytest.mark.asyncio
    async def test_driver_ws_set_driver_name(self, aiohttp_client, drive_engine):
        """Driver name command over WS."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/driver-ws')
        await ws.receive_json()  # initial state

        await ws.send_json({"type": "command", "data": {"set_driver_name": "TestDriver"}})
        ack = await ws.receive_json()
        assert ack['ok'] is True
        assert drive_engine._driver_name == "TestDriver"
        await ws.close()

    @pytest.mark.asyncio
    async def test_driver_ws_ping(self, aiohttp_client, drive_engine):
        """Ping returns pong."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/driver-ws')
        await ws.receive_json()  # initial state

        await ws.send_json({"type": "ping"})
        pong = await ws.receive_json()
        assert pong['type'] == 'pong'
        await ws.close()

    @pytest.mark.asyncio
    async def test_driver_ws_bad_json(self, aiohttp_client, drive_engine):
        """Malformed JSON should return error ack, not crash."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/driver-ws')
        await ws.receive_json()  # initial state

        await ws.send_str("not json at all {{{")
        ack = await ws.receive_json()
        assert ack['type'] == 'command_ack'
        assert ack['ok'] is False
        assert 'error' in ack
        await ws.close()

    @pytest.mark.asyncio
    async def test_driver_ws_tracked(self, aiohttp_client, drive_engine):
        """Connected driver WS should be tracked in _driver_wss."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/driver-ws')
        await ws.receive_json()  # initial state
        assert len(drive_engine._driver_wss) == 1
        await ws.close()

    @pytest.mark.asyncio
    async def test_driver_ws_intensity_command(self, aiohttp_client, drive_engine):
        """Intensity change via WS."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/driver-ws')
        await ws.receive_json()

        await ws.send_json({"type": "command", "data": {"intensity": 0.75}})
        ack = await ws.receive_json()
        assert ack['ok'] is True
        assert drive_engine._pattern.intensity == pytest.approx(0.75)
        await ws.close()

    @pytest.mark.asyncio
    async def test_driver_ws_stop_command(self, aiohttp_client, drive_engine):
        """Stop command via WS."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/driver-ws')
        await ws.receive_json()

        await ws.send_json({"type": "command", "data": {"intensity": 0.8}})
        await ws.receive_json()
        await ws.send_json({"type": "command", "data": {"stop": True}})
        ack = await ws.receive_json()
        assert ack['ok'] is True
        assert drive_engine._pattern.intensity == 0.0
        await ws.close()


class TestLanRiderWS:
    @pytest.mark.asyncio
    async def test_rider_ws_connect(self, aiohttp_client, drive_engine):
        """Rider can connect to /rider-ws."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/rider-ws')
        assert not ws.closed
        # Should receive initial rider_state
        msg = await ws.receive_json()
        assert msg['type'] == 'rider_state'
        await ws.close()

    @pytest.mark.asyncio
    async def test_rider_ws_gets_driver_status(self, aiohttp_client, drive_engine):
        """On connect, rider receives driver_status showing no driver connected."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/rider-ws')
        # First message: rider_state
        await ws.receive_json()
        # Second message: driver_status
        msg = await ws.receive_json()
        assert msg['type'] == 'driver_status'
        assert msg['connected'] is False
        await ws.close()

    @pytest.mark.asyncio
    async def test_rider_receives_driver_connect(self, aiohttp_client, drive_engine):
        """When driver connects, rider gets driver_status connected=True."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)

        # Connect rider first
        rider_ws = await client.ws_connect('/rider-ws')
        # Drain the initial messages (rider_state + driver_status)
        await rider_ws.receive_json()  # rider_state
        await rider_ws.receive_json()  # driver_status (connected=False)

        # Connect driver
        driver_ws = await client.ws_connect('/driver-ws')
        await driver_ws.receive_json()  # driver gets initial state

        # Rider should get driver_status connected=True
        msg = await asyncio.wait_for(rider_ws.receive_json(), timeout=2.0)
        assert msg['type'] == 'driver_status'
        assert msg['connected'] is True

        await driver_ws.close()
        await rider_ws.close()

    @pytest.mark.asyncio
    async def test_rider_ws_tracked(self, aiohttp_client, drive_engine):
        """Connected rider WS should be tracked in _rider_wss."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/rider-ws')
        await ws.receive_json()  # rider_state
        assert len(drive_engine._rider_wss) == 1
        await ws.close()

    @pytest.mark.asyncio
    async def test_rider_state_fields(self, aiohttp_client, drive_engine):
        """Initial rider_state should have expected fields."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect('/rider-ws')
        msg = await ws.receive_json()
        assert msg['type'] == 'rider_state'
        assert 'intensity' in msg
        assert 'bottle_active' in msg
        assert 'bottle_mode' in msg
        assert 'driver_name' in msg
        await ws.close()


class TestLanWSBottleBroadcast:
    @pytest.mark.asyncio
    async def test_bottle_broadcasts_to_rider(self, aiohttp_client, drive_engine):
        """Bottle command should immediately broadcast to rider WS."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)

        # Connect rider
        rider_ws = await client.ws_connect('/rider-ws')
        await rider_ws.receive_json()  # rider_state
        await rider_ws.receive_json()  # driver_status

        # Connect driver and send bottle command
        driver_ws = await client.ws_connect('/driver-ws')
        await driver_ws.receive_json()  # initial state

        await driver_ws.send_json({
            "type": "command",
            "data": {"bottle": {"mode": "deep_huff", "duration": 15}}
        })
        await driver_ws.receive_json()  # command_ack

        # Rider should get driver_status (from driver connect) then bottle_status
        found_bottle = False
        for _ in range(5):
            try:
                msg = await asyncio.wait_for(rider_ws.receive_json(), timeout=2.0)
                if msg.get('type') == 'bottle_status':
                    assert msg['active'] is True
                    assert msg['mode'] == 'deep_huff'
                    assert msg['remaining'] > 0
                    found_bottle = True
                    break
            except asyncio.TimeoutError:
                break
        assert found_bottle, "Rider never received bottle_status"

        await driver_ws.close()
        await rider_ws.close()


class TestLanWSBackwardCompat:
    @pytest.mark.asyncio
    async def test_http_state_still_works(self, aiohttp_client, drive_engine):
        """HTTP GET /state should still work alongside WS endpoints."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        resp = await client.get('/state')
        assert resp.status == 200
        d = await resp.json()
        assert 'pattern' in d
        assert 'intensity' in d

    @pytest.mark.asyncio
    async def test_http_command_still_works(self, aiohttp_client, drive_engine):
        """HTTP POST /command should still work alongside WS endpoints."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        resp = await client.post('/command', json={"pattern": "Sine"})
        assert resp.status == 200
        assert drive_engine._pattern.pattern == "Sine"

    @pytest.mark.asyncio
    async def test_http_rider_state_still_works(self, aiohttp_client, drive_engine):
        """HTTP GET /rider-state should still work alongside WS endpoints."""
        app = drive_engine._build_app()
        client = await aiohttp_client(app)
        resp = await client.get('/rider-state')
        assert resp.status == 200
        d = await resp.json()
        assert 'intensity' in d
        assert 'bottle_active' in d
