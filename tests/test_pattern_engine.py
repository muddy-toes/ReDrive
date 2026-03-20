"""Tests for PatternEngine (engine.py)."""

import pytest
from engine import PatternEngine, PATTERNS


class TestPatternEngineTick:
    def test_hold_returns_constant(self, pattern_engine):
        pattern_engine.pattern = "Hold"
        pattern_engine.intensity = 0.6
        vals = [pattern_engine.tick(0.05) for _ in range(20)]
        assert all(v == pytest.approx(0.6, abs=1e-6) for v in vals)

    def test_sine_oscillates(self, pattern_engine):
        pattern_engine.pattern = "Sine"
        pattern_engine.intensity = 0.8
        pattern_engine.hz = 1.0
        # Collect samples over a full cycle (1 second at hz=1.0)
        vals = [pattern_engine.tick(0.05) for _ in range(20)]
        assert min(vals) < max(vals), "Sine output should vary over a full cycle"

    def test_ramp_up_increases(self, pattern_engine):
        pattern_engine.pattern = "Ramp \u2191"
        pattern_engine.intensity = 1.0
        pattern_engine.hz = 1.0
        vals = [pattern_engine.tick(0.05) for _ in range(10)]
        # First half of a cycle should be increasing
        for i in range(1, len(vals)):
            assert vals[i] >= vals[i - 1] - 1e-6

    def test_ramp_down_decreases(self, pattern_engine):
        pattern_engine.pattern = "Ramp \u2193"
        pattern_engine.intensity = 1.0
        pattern_engine.hz = 1.0
        vals = [pattern_engine.tick(0.05) for _ in range(10)]
        # First half of a cycle should be decreasing
        for i in range(1, len(vals)):
            assert vals[i] <= vals[i - 1] + 1e-6

    def test_pulse_alternates(self, pattern_engine):
        pattern_engine.pattern = "Pulse"
        pattern_engine.intensity = 1.0
        pattern_engine.hz = 1.0
        vals = [pattern_engine.tick(0.05) for _ in range(20)]
        # Should have both high and low values
        assert min(vals) < 0.5
        assert max(vals) > 0.5

    def test_burst_has_gaps(self, pattern_engine):
        pattern_engine.pattern = "Burst"
        pattern_engine.intensity = 1.0
        pattern_engine.hz = 1.0
        pattern_engine.depth = 1.0
        vals = [pattern_engine.tick(0.05) for _ in range(20)]
        highs = [v for v in vals if v > 0.9]
        lows = [v for v in vals if v < 0.1]
        assert len(highs) > 0, "Burst should have active (high) phases"
        assert len(lows) > 0, "Burst should have silent (low) phases"

    def test_random_varies(self, pattern_engine):
        pattern_engine.pattern = "Random"
        pattern_engine.intensity = 1.0
        pattern_engine.hz = 2.0
        vals = [pattern_engine.tick(0.1) for _ in range(50)]
        unique = set(round(v, 4) for v in vals)
        assert len(unique) > 2, "Random output should change between ticks"

    def test_edge_approaches_peak(self, pattern_engine):
        pattern_engine.pattern = "Edge"
        pattern_engine.intensity = 1.0
        pattern_engine.hz = 0.5
        # Collect many samples to cover the ramp-up phase
        vals = [pattern_engine.tick(0.05) for _ in range(40)]
        peak = max(vals)
        # Edge ramps to 0.92 * intensity, never reaches full 1.0
        assert peak <= 0.92 + 1e-3, "Edge should pull back before full peak"
        assert peak > 0.5, "Edge should approach close to peak"


class TestPatternEngineSetCommand:
    def test_set_command_changes_pattern(self, pattern_engine):
        assert pattern_engine.pattern == "Hold"
        pattern_engine.set_command({"pattern": "Sine"})
        assert pattern_engine.pattern == "Sine"

    def test_set_command_changes_intensity(self, pattern_engine):
        pattern_engine.set_command({"intensity": 0.5})
        assert pattern_engine.intensity == pytest.approx(0.5)
        # Test clamping
        pattern_engine.set_command({"intensity": 2.0})
        assert pattern_engine.intensity == pytest.approx(1.0)
        pattern_engine.set_command({"intensity": -0.5})
        assert pattern_engine.intensity == pytest.approx(0.0)

    def test_stop_resets(self, pattern_engine):
        pattern_engine.intensity = 0.8
        pattern_engine._phase = 0.5
        pattern_engine.pattern = "Sine"
        pattern_engine.stop()
        assert pattern_engine.intensity == 0.0
        assert pattern_engine._phase == 0.0
        # Pattern name is NOT reset by stop()
        assert pattern_engine.pattern == "Sine"

    def test_depth_zero_is_flat(self, pattern_engine):
        pattern_engine.pattern = "Sine"
        pattern_engine.intensity = 0.7
        pattern_engine.depth = 0.0
        pattern_engine.hz = 1.0
        vals = [pattern_engine.tick(0.05) for _ in range(20)]
        # With depth=0, floor=1.0, so output should be constant at intensity
        for v in vals:
            assert v == pytest.approx(0.7, abs=1e-3)

    def test_tick_output_range(self, pattern_engine):
        """Output of tick() is always in [0.0, 1.0] for all patterns."""
        for pat_name in PATTERNS:
            pattern_engine.pattern = pat_name
            pattern_engine.intensity = 1.0
            pattern_engine.depth = 1.0
            pattern_engine.hz = 2.0
            pattern_engine._phase = 0.0
            pattern_engine._edge_phase = 0
            pattern_engine._edge_t = 0.0
            for _ in range(100):
                v = pattern_engine.tick(0.02)
                assert 0.0 <= v <= 1.0, (
                    f"Pattern {pat_name} produced out-of-range value {v}"
                )
