import time

from jarvis.runner import Runner


def test_local_diagnostic_is_not_reported_as_an_extension_session():
    runner=Runner(None,None)
    runner.bridge.ready=True
    runner.bridge.last_seen=time.time()
    assert runner.status()["verified"] is False
    runner.handle({"operation":"hello","session_id":"session-1234567890"})
    assert runner.status()["verified"] is True


def test_extension_activity_updates_and_notifies_bridge_even_when_stopped():
    changes = []
    runner = Runner(None, None, on_change=lambda scope: changes.append(scope))
    runner.bridge.ready = True
    out = runner.handle({
        "operation": "poll_config",
        "session_id": "session-1234567890",
        "activity": {"phase": "idle", "detail": "Connected to desktop · Stopped in app"},
    })
    assert out["active"] is False
    st = runner.status()
    assert st["verified"] is True
    assert st["activity"]["detail"] == "Connected to desktop · Stopped in app"
    assert "bridge" in changes


def test_atomic_start_and_wa_scan_mode_sets_immutable_floor():
    runner = Runner(None, None)
    t0 = time.time()
    runner.begin_session(now=t0, mode="new_only")
    assert runner.scan_mode == "new_only"
    assert runner.immutable_floor_ts == t0
    assert runner.scan_since_ts == t0

    # Switch to all
    runner.begin_session(mode="all")
    assert runner.scan_mode == "all"
    assert runner.immutable_floor_ts is None
    assert runner.scan_since_ts is None


import pytest
from pydantic import ValidationError
from jarvis.desktop_service import WaScanModeParams, StartParams

def test_failed_scan_mode_request():
    with pytest.raises(ValidationError):
        WaScanModeParams.model_validate({"mode": "invalid_mode"})
    with pytest.raises(ValidationError):
        StartParams.model_validate({"mode": "unrecognized"})
