import os
import time

import pytest
from conftest import FakeVault

from jarvis import desktop
from jarvis.runner import Runner


@pytest.mark.skipif(os.name == "nt", reason="Unix socket transport")
def test_bridge_survives_accept_errors_and_keeps_serving(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "runtime_directory", lambda: tmp_path)
    vault = FakeVault()
    vault.set("bridge_secret", "s" * 43)
    calls = []
    bridge = desktop.LocalBridge(vault, lambda body: calls.append(body) or {"ok": True})
    bridge.start()
    try:
        real_accept = bridge.listener.accept
        failures = {"left": 3}

        def flaky_accept():
            if failures["left"]:
                failures["left"] -= 1
                raise OSError("client vanished before accept")
            return real_accept()

        bridge.listener.accept = flaky_accept
        time.sleep(0.8)
        assert bridge.alive, "serve thread must not exit on a transient accept error"
        assert desktop.local_call(vault, {"operation": "poll_config"}) == {"ok": True}
        assert calls and calls[-1]["operation"] == "poll_config"
    finally:
        bridge.stop()


@pytest.mark.skipif(os.name == "nt", reason="Unix socket transport")
def test_bridge_watchdog_restarts_a_dead_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "runtime_directory", lambda: tmp_path)
    vault = FakeVault()
    vault.set("bridge_secret", "s" * 43)
    bridge = desktop.LocalBridge(vault, lambda body: {"ok": True})
    bridge.start()
    try:
        bridge.stop_event.set()
        bridge.thread.join(timeout=3)
        bridge.stop_event.clear()
        assert bridge.ready and not bridge.alive
        assert bridge.ensure_running()
        assert desktop.local_call(vault, {"operation": "poll_config"}) == {"ok": True}
    finally:
        bridge.stop()


def test_owner_whatsapp_commands_are_forwarded_once(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "runtime_directory", lambda: tmp_path)
    runner = Runner(FakeVault(), None)
    runner.scan_since_ts = 1_800_000_000
    seen = []
    runner.on_agent_command = seen.append
    command = {"message_id": "true_919800000000@c.us_ABC", "text": "@Jarvis stock of S25?",
               "conversation_id": "919800000000@c.us", "self_chat": True, "timestamp": 1_800_000_100}
    old = {**command, "message_id": "true_919800000000@c.us_OLD", "timestamp": 1_700_000_000}
    assert runner._accept_agent_commands([command, old]) == 1
    assert runner._accept_agent_commands([command]) == 0
    assert [c["message_id"] for c in seen] == ["true_919800000000@c.us_ABC"]
    assert seen[0]["self_chat"] is True


def test_start_with_previous_last_event_ts_preserves_immutable_floor():
    runner = Runner(FakeVault(), None)
    runner.last_event_ts = 1_800_000_000.0  # Event from prior session
    now = 1_800_003_600.0  # User clicks Start 1 hour later with new_only
    runner.begin_session(now=now, mode="new_only")

    assert runner.immutable_floor_ts == now
    assert runner.scan_since_ts == now, "New-only start must not rewind cutoff to previous last_event_ts - 300"

    # Simulate reconnect during session: floor must not widen backwards
    runner.begin_session(now=now + 60.0)
    assert runner.scan_since_ts == now, "Reconnect must never widen cutoff before immutable floor"

    # Simulate stop/end session: floor must not rewind backwards
    runner.end_session(now=now + 120.0)
    assert runner.scan_since_ts == now, "End session must not rewind cutoff before immutable floor"


def test_accept_event_ts_rejects_missing_timestamp_and_old_unread_with_floor():
    runner = Runner(FakeVault(), None)
    floor = 1_800_000_000.0
    runner.begin_session(now=floor, mode="new_only")

    # Missing timestamp fails closed
    no_ts = {"conversation_id": "123@c.us", "text": "Hi", "message_id": "m1"}
    accepted, reason = runner.accept_event_ts(no_ts)
    assert accepted is False
    assert reason == "UNCERTAIN_TIMESTAMP"

    # Old unread fails closed (no bypass)
    old_unread = {"conversation_id": "123@c.us", "text": "Old", "unread": True, "timestamp": floor - 100, "message_id": "m2"}
    accepted, reason = runner.accept_event_ts(old_unread)
    assert accepted is False
    assert reason == "IGNORED_OLD"

    # Exact boundary is inclusive (>= floor)
    exact_boundary = {"conversation_id": "123@c.us", "text": "Exact", "timestamp": floor, "message_id": "m3"}
    accepted, reason = runner.accept_event_ts(exact_boundary)
    assert accepted is True

    # Newer message accepted
    newer = {"conversation_id": "123@c.us", "text": "Newer", "timestamp": floor + 50, "message_id": "m4"}
    accepted, reason = runner.accept_event_ts(newer)
    assert accepted is True


@pytest.mark.asyncio
async def test_observations_partial_ack_and_stop_mid_batch(monkeypatch):
    from conftest import FakeSheets
    repo = FakeSheets()
    runner = Runner(FakeVault(), repo)
    runner.active = True
    setup = {
        "business_name": "Test Trader",
        "owner_phone": "+14155552671",
        "classifier_model": "test-model",
        "sources": [{"phone": "+919800000001", "label": "Source 1", "active": True}],
        "scan_all": True,
    }
    runner.cached_setup = setup

    events = [
        {"conversation_id": "919800000001@c.us", "phone": "+919800000001", "message_id": "false_919800000001@c.us_1",
         "text": "WTB 10 iPhone 16", "identity_verified": True, "timestamp": 1_800_000_010},
        {"conversation_id": "919800000002@c.us", "phone": "+919800000002", "message_id": "false_919800000002@c.us_2",
         "text": "WTB 20 iPhone 16", "identity_verified": True, "timestamp": 1_800_000_020},
    ]

    # Simulate runner stopping mid-batch (after event 1 is processed)
    from jarvis.pipeline import IntakePipeline
    orig_process = IntakePipeline.process
    async def process_and_stop(self, event, s):
        res = await orig_process(self, event, s)
        runner.active = False
        return res
    monkeypatch.setattr(IntakePipeline, "process", process_and_stop)

    result = await runner._handle({"operation": "observations", "events": events})

    assert "results" in result
    assert len(result["results"]) == 2, "Every submitted event must have an explicit result entry"
    assert result["results"][0]["message_id"] == "false_919800000001@c.us_1"
    assert result["results"][0]["status"] in ("CAPTURED_REVIEW", "CLASSIFIED")
    assert result["results"][1]["message_id"] == "false_919800000002@c.us_2"
    assert result["results"][1]["status"] == "RETRY_REQUIRED"
    assert result["results"][1]["reason"] == "RUNNER_INACTIVE"


@pytest.mark.asyncio
async def test_unresolved_to_resolved_lid_dedupe_stability(tmp_path):
    from conftest import FakeSheets
    from jarvis.pipeline import IntakePipeline
    from jarvis.local_repo import SQLiteSyncedRepository
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    try:
        pipeline = IntakePipeline(repo, None, local_first=True)
        setup = {"sources": [{"phone": "+919028794222", "active": True}], "scan_all": True}

        ev_unresolved = {
            "conversation_id": "179126040305838@lid",
            "sender_id": "179126040305838@lid",
            "phone": None,
            "sender_name": "Trader",
            "message_id": "false_179126040305838@lid_STABLE1",
            "text": "WTS 5 iPhone 16 128GB sealed",
            "identity_verified": True,
            "timestamp": 1_800_000_000,
        }

        first = await pipeline.process(ev_unresolved, setup)
        first_id = first["id"]

        # Now LID is resolved and event arrives with phone attached
        repo.save_lid_phone("179126040305838@lid", "+919028794222")
        ev_resolved = {
            **ev_unresolved,
            "phone": "+919028794222",
        }

        second = await pipeline.process(ev_resolved, setup)
        assert second["id"] == first_id, "Record ID must remain stable across LID phone enrichment"
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_dom_wpp_switch_dedupe_stability():
    from conftest import FakeSheets
    from jarvis.pipeline import IntakePipeline
    repo = FakeSheets()
    pipeline = IntakePipeline(repo, None, local_first=True)
    setup = {"sources": [{"phone": "+919876543210", "active": True}], "scan_all": True}

    # Ingest via DOM-captured event
    dom_event = {
        "conversation_id": "919876543210@c.us",
        "phone": "+919876543210",
        "sender_id": "919876543210@c.us",
        "sender_name": "Supplier",
        "message_id": "false_919876543210@c.us_3EB0123456789ABCDEF0",
        "text": "WTS 50 S25 256GB sealed",
        "identity_verified": True,
        "timestamp": 1_800_000_100,
    }
    res1 = await pipeline.process(dom_event, setup)
    assert res1["status"] in ("CAPTURED_REVIEW", "CLASSIFIED")

    # Ingest same message via WPP-captured event
    wpp_event = {
        **dom_event,
        "chat_name": "Supplier Direct",
    }
    res2 = await pipeline.process(wpp_event, setup)
    assert res2["id"] == res1["id"]
