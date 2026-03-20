"""Tests for DriveEngine (redrive.py lines 2440-2964).

Uses _handle_command_data(cmd) directly (async) and _handle_state(None)
to inspect engine state.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from redrive import PRESETS


class TestDriveEngineCommands:
    @pytest.mark.asyncio
    async def test_pattern_command(self, drive_engine):
        await drive_engine._handle_command_data({"pattern": "Sine"})
        assert drive_engine._pattern.pattern == "Sine"

    @pytest.mark.asyncio
    async def test_intensity_command(self, drive_engine):
        await drive_engine._handle_command_data({"intensity": 0.75})
        assert drive_engine._pattern.intensity == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_hz_command(self, drive_engine):
        await drive_engine._handle_command_data({"hz": 2.0})
        assert drive_engine._pattern.hz == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_depth_command(self, drive_engine):
        await drive_engine._handle_command_data({"depth": 0.5})
        assert drive_engine._pattern.depth == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_stop_command(self, drive_engine):
        await drive_engine._handle_command_data({"intensity": 0.8})
        await drive_engine._handle_command_data({"stop": True})
        assert drive_engine._pattern.intensity == 0.0
        assert drive_engine._ramp_active is False
        assert drive_engine._gesture_active is False

    @pytest.mark.asyncio
    async def test_ramp_start(self, drive_engine):
        await drive_engine._handle_command_data({"intensity": 0.2})
        await drive_engine._handle_command_data({
            "ramp": {"target": 1.0, "duration": 60}
        })
        assert drive_engine._ramp_active is True
        assert drive_engine._ramp_target == pytest.approx(1.0)
        assert drive_engine._ramp_duration == pytest.approx(60.0)

    @pytest.mark.asyncio
    async def test_ramp_stop(self, drive_engine):
        await drive_engine._handle_command_data({
            "ramp": {"target": 1.0, "duration": 60}
        })
        assert drive_engine._ramp_active is True
        await drive_engine._handle_command_data({"ramp_stop": True})
        assert drive_engine._ramp_active is False

    @pytest.mark.asyncio
    async def test_beta_mode_command(self, drive_engine):
        await drive_engine._handle_command_data({"beta_mode": "sweep"})
        assert drive_engine._beta_mode == "sweep"

    @pytest.mark.asyncio
    async def test_beta_sweep_params(self, drive_engine):
        await drive_engine._handle_command_data({
            "beta_sweep": {
                "hz": 1.0,
                "centre": 5000,
                "width": 2000,
                "skew": 0.5,
            }
        })
        assert drive_engine._beta_sweep_hz == pytest.approx(1.0)
        assert drive_engine._beta_sweep_centre == 5000
        assert drive_engine._beta_sweep_width == 2000
        assert drive_engine._beta_sweep_skew == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_preset_load(self, drive_engine):
        await drive_engine._handle_command_data({"load_preset": "Milking"})
        p = PRESETS["Milking"]
        assert drive_engine._pattern.pattern == p["pattern"]
        assert drive_engine._pattern.intensity == pytest.approx(p["intensity"])
        assert drive_engine._beta_mode == p["beta_mode"]
        assert drive_engine._alpha_on == p.get("alpha", True)

    @pytest.mark.asyncio
    async def test_preset_load_unknown(self, drive_engine):
        # Should be a no-op - no crash
        await drive_engine._handle_command_data({"load_preset": "Nonexistent"})
        # Engine defaults are unchanged
        assert drive_engine._pattern.pattern == "Hold"


class TestDriveEngineState:
    @pytest.mark.asyncio
    async def test_state_endpoint_fields(self, drive_engine):
        resp = await drive_engine._handle_state(None)
        d = json.loads(resp.text)
        expected_keys = {
            "pattern", "intensity", "ramp_active", "ramp_progress",
            "ramp_target", "ramp_duration", "beta_mode", "sweep_hz",
            "sweep_centre", "sweep_width", "sweep_skew", "alpha_on",
            "vol", "beta", "alpha", "spiral_amp", "spiral_tighten",
            "gesture_active", "gesture_dur", "presets",
            "hz", "depth",
        }
        for key in expected_keys:
            assert key in d, f"Missing key '{key}' in state response"

    @pytest.mark.asyncio
    async def test_state_after_preset(self, drive_engine):
        await drive_engine._handle_command_data({"load_preset": "Milking"})
        resp = await drive_engine._handle_state(None)
        d = json.loads(resp.text)
        p = PRESETS["Milking"]
        assert d["pattern"] == p["pattern"]
        assert d["intensity"] == pytest.approx(p["intensity"])
        assert d["beta_mode"] == p["beta_mode"]
        assert d["alpha_on"] == p.get("alpha", True)
        assert d["ramp_target"] == pytest.approx(p["ramp_target"])
        assert d["ramp_duration"] == pytest.approx(p["ramp_duration"])
        bs = p["beta_sweep"]
        assert d["sweep_centre"] == bs["centre"]
        assert d["sweep_width"] == bs["width"]

    @pytest.mark.asyncio
    async def test_sweep_hz_envelope_after_preset(self, drive_engine):
        """After loading Milking (which has sweep_hz_envelope), verify the
        envelope was activated."""
        await drive_engine._handle_command_data({"load_preset": "Milking"})
        env = drive_engine._sweep_hz_env
        assert env is not None, "sweep_hz_envelope should be activated"
        assert env["base"] == pytest.approx(0.34)
        assert env["peak"] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_state_has_hz_and_depth(self, drive_engine):
        """State endpoint should include hz and depth fields."""
        resp = await drive_engine._handle_state(None)
        d = json.loads(resp.text)
        assert "hz" in d, "State missing 'hz' field"
        assert "depth" in d, "State missing 'depth' field"

    @pytest.mark.asyncio
    async def test_preset_load_state_roundtrip_with_hz_depth(self, drive_engine):
        """After loading a preset, state should include hz and depth matching PRESETS values."""
        await drive_engine._handle_command_data({"load_preset": "Milking"})
        resp = await drive_engine._handle_state(None)
        d = json.loads(resp.text)
        p = PRESETS["Milking"]
        assert abs(d["hz"] - p["hz"]) < 0.01
        assert abs(d["depth"] - p["depth"]) < 0.01


class TestSessionManagement:
    """Tests for proper cleanup of aiohttp ClientSession and WebSocket."""

    @pytest.mark.asyncio
    async def test_session_initialized_to_none(self, drive_engine):
        """Session should start as None."""
        assert drive_engine._session is None

    @pytest.mark.asyncio
    async def test_connect_closes_existing_session(self, drive_engine):
        """Reconnecting should close the previous session and ws."""
        old_session = AsyncMock()
        old_session.closed = False
        drive_engine._session = old_session

        old_ws = AsyncMock()
        old_ws.closed = False
        drive_engine._ws = old_ws

        # Disable send_hook so _connect actually runs the real path
        drive_engine._send_hook = None

        # _connect will fail (no real server) but should still close old resources
        with patch("aiohttp.ClientSession") as mock_cls:
            mock_new_session = AsyncMock()
            mock_new_session.ws_connect = AsyncMock(side_effect=Exception("no server"))
            mock_cls.return_value = mock_new_session
            await drive_engine._connect()

        old_ws.close.assert_awaited()
        old_session.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_connect_skips_close_if_already_closed(self, drive_engine):
        """Should not close resources that are already closed."""
        old_session = AsyncMock()
        old_session.closed = True
        drive_engine._session = old_session

        old_ws = AsyncMock()
        old_ws.closed = True
        drive_engine._ws = old_ws

        drive_engine._send_hook = None

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_new_session = AsyncMock()
            mock_new_session.ws_connect = AsyncMock(side_effect=Exception("no server"))
            mock_cls.return_value = mock_new_session
            await drive_engine._connect()

        old_ws.close.assert_not_awaited()
        old_session.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_error_closes_ws_and_session(self, drive_engine):
        """On send error, both ws and session should be closed."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_str = AsyncMock(side_effect=Exception("broken pipe"))
        drive_engine._ws = mock_ws

        mock_session = AsyncMock()
        mock_session.closed = False
        drive_engine._session = mock_session

        # Disable send_hook so _send uses the real ws path
        drive_engine._send_hook = None
        # Need a loop for the cooldown check
        drive_engine._loop = asyncio.get_event_loop()

        await drive_engine._send("L0500I50")

        mock_ws.close.assert_awaited()
        mock_session.close.assert_awaited()
        assert drive_engine._ws is None
        assert drive_engine._session is None

    @pytest.mark.asyncio
    async def test_send_error_handles_close_failure(self, drive_engine):
        """If closing ws/session raises, should still set them to None."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_str = AsyncMock(side_effect=Exception("broken pipe"))
        mock_ws.close = AsyncMock(side_effect=Exception("close failed"))
        drive_engine._ws = mock_ws

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.close = AsyncMock(side_effect=Exception("close failed"))
        drive_engine._session = mock_session

        drive_engine._send_hook = None
        drive_engine._loop = asyncio.get_event_loop()

        await drive_engine._send("L0500I50")

        assert drive_engine._ws is None
        assert drive_engine._session is None


class TestLanModeFeatures:
    @pytest.mark.asyncio
    async def test_set_driver_name(self, drive_engine):
        """set_driver_name command should store the name."""
        await drive_engine._handle_command_data({"set_driver_name": "Scott"})
        assert drive_engine._driver_name == "Scott"

    @pytest.mark.asyncio
    async def test_set_driver_name_truncates(self, drive_engine):
        """Driver name should be truncated to 40 chars."""
        await drive_engine._handle_command_data({"set_driver_name": "A" * 100})
        assert len(drive_engine._driver_name) == 40

    @pytest.mark.asyncio
    async def test_bottle_command(self, drive_engine):
        """bottle command should set bottle state."""
        import time
        await drive_engine._handle_command_data({"bottle": {"mode": "deep_huff", "duration": 15}})
        assert drive_engine._bottle_mode == "deep_huff"
        assert drive_engine._bottle_until > time.monotonic()

    @pytest.mark.asyncio
    async def test_bottle_defaults(self, drive_engine):
        """bottle command with no mode/duration should use defaults."""
        import time
        await drive_engine._handle_command_data({"bottle": {}})
        assert drive_engine._bottle_mode == "normal"
        assert drive_engine._bottle_until > time.monotonic()

    @pytest.mark.asyncio
    async def test_rider_state_endpoint(self, drive_engine):
        """rider-state should return intensity, bottle, and driver name."""
        drive_engine._driver_name = "TestDriver"
        resp = await drive_engine._handle_rider_state(None)
        d = json.loads(resp.text)
        assert "intensity" in d
        assert "bottle_active" in d
        assert "bottle_remaining" in d
        assert "bottle_mode" in d
        assert d["driver_name"] == "TestDriver"

    @pytest.mark.asyncio
    async def test_rider_state_bottle_active(self, drive_engine):
        """rider-state should reflect active bottle."""
        import time
        drive_engine._bottle_until = time.monotonic() + 30
        drive_engine._bottle_mode = "normal"
        resp = await drive_engine._handle_rider_state(None)
        d = json.loads(resp.text)
        assert d["bottle_active"] is True
        assert d["bottle_remaining"] > 0

    @pytest.mark.asyncio
    async def test_rider_state_bottle_inactive(self, drive_engine):
        """rider-state should show inactive bottle when expired."""
        resp = await drive_engine._handle_rider_state(None)
        d = json.loads(resp.text)
        assert d["bottle_active"] is False
        assert d["bottle_remaining"] == 0

    @pytest.mark.asyncio
    async def test_set_driver_name_shared_dict(self, drive_engine):
        """set_driver_name should push name to shared dict for GUI polling."""
        await drive_engine._handle_command_data({"set_driver_name": "TestPilot"})
        assert drive_engine._shared["__driver_name__"] == "TestPilot"

    @pytest.mark.asyncio
    async def test_bottle_shared_dict(self, drive_engine):
        """bottle command should push state to shared dict for GUI polling."""
        import time
        await drive_engine._handle_command_data({"bottle": {"mode": "deep_huff", "duration": 20}})
        assert drive_engine._shared["__bottle_mode__"] == "deep_huff"
        assert drive_engine._shared["__bottle_until__"] > time.monotonic()


class TestWebSocketHeartbeat:
    @pytest.mark.asyncio
    async def test_connect_no_heartbeat(self, drive_engine):
        """ws_connect should not use heartbeat (ReStim doesn't respond to pings)."""
        drive_engine._send_hook = None
        with patch("aiohttp.ClientSession") as mock_cls:
            mock_session = AsyncMock()
            mock_cls.return_value = mock_session
            mock_session.ws_connect = AsyncMock(return_value=AsyncMock(closed=False))
            await drive_engine._connect()
            mock_session.ws_connect.assert_awaited_once()
            call_kwargs = mock_session.ws_connect.call_args
            assert 'heartbeat' not in (call_kwargs.kwargs if call_kwargs.kwargs else {})
