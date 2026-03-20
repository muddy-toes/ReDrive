"""Tests for rider WS receiving pushed status."""

import asyncio
import json
import pytest
import aiohttp

from server.server import build_app, _rooms


async def _create_room(client):
    """Helper: POST /create and return (code, driver_key)."""
    resp = await client.post("/create", allow_redirects=False)
    assert resp.status == 302
    code = list(_rooms.keys())[0]
    key = _rooms[code].driver_key
    return code, key


class TestRiderWSPush:
    """Test rider WS receiving pushed status."""

    @pytest.mark.asyncio
    async def test_rider_ws_connect(self, aiohttp_client):
        """Rider can connect to /rider-ws endpoint."""
        _rooms.clear()
        client = await aiohttp_client(build_app())
        code, key = await _create_room(client)

        ws = await client.ws_connect(f"/room/{code}/rider-ws")
        assert not ws.closed
        await ws.close()
        _rooms.clear()

    @pytest.mark.asyncio
    async def test_rider_receives_driver_status_on_connect(self, aiohttp_client):
        """When driver WS connects, riders get driver_status."""
        _rooms.clear()
        client = await aiohttp_client(build_app())
        code, key = await _create_room(client)

        # Connect rider first
        rider_ws = await client.ws_connect(f"/room/{code}/rider-ws")

        # Now connect driver - rider should get driver_status
        driver_ws = await client.ws_connect(f"/room/{code}/driver-ws?key={key}")
        await driver_ws.receive_json()  # driver gets initial state

        # Rider should receive driver_status (may also get rider_state, T-code, etc.)
        found_status = False
        for _ in range(15):
            try:
                raw = await asyncio.wait_for(rider_ws.receive(), timeout=0.5)
                if raw.type != aiohttp.WSMsgType.TEXT:
                    continue
                if not raw.data.startswith("{"):
                    continue
                msg = json.loads(raw.data)
                if msg.get("type") == "driver_status":
                    assert msg["connected"] is True
                    found_status = True
                    break
            except asyncio.TimeoutError:
                break

        assert found_status, "Rider never received driver_status"
        await driver_ws.close()
        await rider_ws.close()
        _rooms.clear()
