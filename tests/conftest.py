"""Shared fixtures for ReDrive test suite."""

import queue

import pytest
from engine import DriveConfig, DriveEngine, PatternEngine
from server import build_app, _rooms


@pytest.fixture
def drive_config():
    return DriveConfig()


@pytest.fixture
def shared_dict():
    return {
        "__live__l0": 0.0,
        "__live__l1": 0.0,
        "__live__l2": 0.0,
        "__ramp_progress__": 0.0,
    }


@pytest.fixture
def log_queue():
    return queue.Queue()


@pytest.fixture
def captured_tcode():
    return []


@pytest.fixture
def drive_engine(drive_config, shared_dict, log_queue, captured_tcode):
    engine = DriveEngine(
        drive_config, shared_dict, log_queue,
        send_hook=captured_tcode.append,
    )
    # Do NOT call .start() - no background thread
    return engine


@pytest.fixture
def pattern_engine():
    return PatternEngine()


@pytest.fixture
def aiohttp_app():
    _rooms.clear()
    app = build_app()
    yield app
    _rooms.clear()
