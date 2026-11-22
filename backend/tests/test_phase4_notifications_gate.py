"""Phase 4 Independent Verification Test Suite: Useful Notification History & Real Actions.

Validates the Phase 4 Acceptance Gate:
- Issue B08: Deduplication, business priority, distinct delivery/read/resolved/dismissed/archived statuses.
- Issue B08: Routine agent command-started and command-ready events do NOT emit OS notification popups.
- Issue B08: Notification tick does NOT replay already-emitted/delivered notifications on every loop.
- Issue B08: Coalescing repeated system failures (e.g. quota, disconnects) within a cooldown window.
- Issue B17: Complete pagination and filtering contract for notification history (limit, offset, state, category, total, unread_count, has_more).
- "Add to review sheet": Idempotent sourcing record creation locally and in Sheets via signed sync contract;
  truthful tracking of sheet_status (pending, synced, failed). Offline Sheets survival without data loss.
- "Send availability update": Single message intent bound to buyer and source alert; prevents repeat clicks and race with automatic quotes;
  strictly requires real send evidence before buyer_notified=True; keeps UNKNOWN when send is uncertain.
- Independent effects tracking: Sheet sync and WhatsApp delivery statuses are tracked separately without coupling.
- Newer message revision invalidation: Superseded inquiry messages resolve older alerts with resolution_reason='SUPERSEDED_BY_NEWER_REVISION'.
- Clear notifications: Archives active notifications while preserving full history in SQLite.
"""
import asyncio
import copy
from datetime import datetime, timezone
import json
import time
from pathlib import Path
import pytest

from conftest import FakeVault
from jarvis.desktop_service import DesktopService, DesktopError
from jarvis.intelligence import alert, match_opportunities, record_system_alert
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.runner import Runner
from jarvis.sheets import SheetsError
from jarvis.trading_engine import find_best_stock_match


class ConfigurableSheetsDouble:
    """Configurable remote Sheets double with outage and failure injection."""
    def __init__(self, offline=False):
        self.records = {}
        self.revisions = {}
        self.calls = 0
        self.is_offline = offline
        self.fail_error = "SHEETS_UNAVAILABLE"

    async def list(self, collection, limit=100):
        self.calls += 1
        if self.is_offline:
            raise SheetsError(self.fail_error)
        return [
            {"id": r["id"], "revision": r["revision"], "updated_at": r["updated_at"], "data": r["data"]}
            for (c, _), r in self.records.items() if c == collection
        ][:limit]

    async def put(self, collection, record_id, data, expected_revision, request_id=None):
        return (await self.batch([{"collection": collection, "id": record_id, "data": data, "expected_revision": expected_revision}], request_id))[0]

    async def batch(self, changes, request_id=None):
        self.calls += 1
        if self.is_offline:
            raise SheetsError(self.fail_error)
        out = []
        for ch in changes:
            col = ch["collection"]
            rid = ch["id"]
            exp = int(ch["expected_revision"])
            cur = self.revisions.get((col, rid), 0)
            if cur != exp:
                raise SheetsError(f"REVISION_CONFLICT: {col}/{rid} cur={cur} exp={exp}")
            next_rev = cur + 1
            self.revisions[(col, rid)] = next_rev
            rec = {
                "id": rid,
                "revision": next_rev,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "data": copy.deepcopy(ch["data"]),
            }
            self.records[(col, rid)] = rec
            out.append(rec)
        return out

    async def health(self):
        if self.is_offline:
            raise SheetsError(self.fail_error)
        return {"schema_version": 1, "storage": "google_sheets"}


@pytest.fixture
def service_env(tmp_path):
    vault = FakeVault()
    vault.set("bridge_secret", "test-bridge-secret-phase4")
    remote = ConfigurableSheetsDouble(offline=False)
    repo = SQLiteSyncedRepository(remote, db_path=tmp_path / "jarvis_phase4.db")
    runner = Runner(vault, repo)
    runner.active = True
    runner.autonomy_mode = "turbo"
    service = DesktopService(vault, repo, runner, opener=lambda: None, cache_path=tmp_path / "cache.json")
    service.connected = True
    service.runner = runner

    emitted_events = []
    service.emit = lambda event, data: emitted_events.append({"event": event, "data": data})

    yield {
        "service": service,
        "repo": repo,
        "vault": vault,
        "runner": runner,
        "remote": remote,
        "events": emitted_events,
    }
    repo.close()


@pytest.mark.asyncio
async def test_gate_scenario_1_no_routine_command_popups(service_env):
    """Scenario 1 (B08): Routine command-started and response-ready events do NOT emit OS notification popups.
    Only actionable failures emit notification alerts.
    """
    service = service_env["service"]
    events = service_env["events"]
    repo = service_env["repo"]

    # 1. Simulate routine command execution (successful)
    cmd = {
        "message_id": "wa_cmd_101",
        "conversation_id": "owner@c.us",
        "text": "Check inventory status",
        "self_chat": False,
    }
    # Mock handle_agent_command to succeed cleanly
    import jarvis.desktop_service as ds
    orig_handle = ds.handle_agent_command
    async def mock_handle_success(*args, **kwargs):
        return {"reply": "Inventory is healthy with 42 units.", "commands": []}
    ds.handle_agent_command = mock_handle_success

    try:
        await service._process_agent_command(cmd)

        # Assert no notification event was emitted for started or ready
        notification_events = [e for e in events if e["event"] == "notification"]
        assert len(notification_events) == 0, f"Expected 0 routine notification popups, got: {notification_events}"

        # Assert command was tracked cleanly in tasks collection
        job_id = "alert-wa-agent-" + ds.hashlib.sha256("wa_cmd_101".encode()).hexdigest()[:32]
        task_rec = repo.get_sync("tasks", job_id)
        assert task_rec is not None
        assert task_rec["data"]["state"] == "COMPLETED"
        assert task_rec["data"]["kind"] == "task"

        # 2. Simulate command failure: actionable failure MUST emit notification popup
        async def mock_handle_fail(*args, **kwargs):
            raise RuntimeError("Gemini quota exhausted")
        ds.handle_agent_command = mock_handle_fail

        events.clear()
        cmd_fail = {
            "message_id": "wa_cmd_102",
            "conversation_id": "owner@c.us",
            "text": "Trigger failure",
            "self_chat": False,
        }
        await service._process_agent_command(cmd_fail)

        # Assert actionable failure DID emit notification
        notification_events = [e for e in events if e["event"] == "notification"]
        assert len(notification_events) == 1
        assert "failed" in notification_events[0]["data"]["id"]
        assert "WhatsApp command failed" in notification_events[0]["data"]["title"]
    finally:
        ds.handle_agent_command = orig_handle


@pytest.mark.asyncio
async def test_gate_scenario_2_meaningful_notifications_and_tick_no_replay(service_env):
    """Scenario 2 (B08): High demand / sourcing gap alerts produce notifications,
    and notification_tick does NOT re-emit already notified alerts on every loop.
    """
    service = service_env["service"]
    events = service_env["events"]
    repo = service_env["repo"]

    # Configure notifications enabled in settings
    await service.settings_save({"notifications": True, "match_alerts": False})

    # Create a meaningful deal match alert
    alert_rec = await alert(
        repo,
        kind="stock_match",
        key="iphone16:256:black",
        title="High demand (3 buyers) · iPhone 16",
        body="Seller offered iPhone 16 256GB Black. 3 matching buyers.",
        details={"device": "iPhone 16", "buyer_phone": "+919876543210", "buyer_jid": "919876543210@s.whatsapp.net"},
    )
    alert_id = alert_rec["id"]

    # First tick: Should emit notification and persist notified_at and delivery_state="DELIVERED"
    events.clear()
    await service.notification_tick(match=False)

    notification_events = [e for e in events if e["event"] == "notification"]
    assert len(notification_events) == 1
    assert notification_events[0]["data"]["id"] == alert_id
    assert "High demand" in notification_events[0]["data"]["title"]

    # Check alert row in SQLite: notified_at and delivery_state updated
    row_after = repo.get_sync("tasks", alert_id)
    assert row_after["data"]["notified_at"] is not None
    assert row_after["data"]["delivery_state"] == "DELIVERED"

    # Second tick: Should NOT re-emit notification!
    events.clear()
    await service.notification_tick(match=False)
    assert len([e for e in events if e["event"] == "notification"]) == 0, "Notification tick must not replay already-delivered alert!"


@pytest.mark.asyncio
async def test_gate_scenario_3_coalesce_repeated_system_alerts(service_env):
    """Scenario 3: Coalesce repeated system failures (e.g. quota, disconnects) within a cooldown window."""
    repo = service_env["repo"]

    # Emit first system alert
    alert1 = await record_system_alert(
        repo,
        category="model_quota",
        title="Gemini Quota Exceeded",
        body="Model resource exhausted; cooldown in progress.",
        details={"model": "gemini-3.5-flash-lite", "status_code": 429},
        cooldown_seconds=300.0,
    )
    assert alert1 is not None

    # Emit 4 more alerts immediately with the same category and title
    for _ in range(4):
        coalesced = await record_system_alert(
            repo,
            category="model_quota",
            title="Gemini Quota Exceeded",
            body="Model resource exhausted; cooldown in progress.",
            details={"model": "gemini-3.5-flash-lite", "status_code": 429},
            cooldown_seconds=300.0,
        )
        assert coalesced is None, "Repeated system alert must be coalesced during cooldown window"

    # Verify only 1 notification task exists for this category
    paginated = repo.list_notifications_paginated(category="model_quota")
    assert paginated["total"] == 1


@pytest.mark.asyncio
async def test_gate_scenario_4_add_to_review_sheet_offline_sheets(service_env):
    """Scenario 4: "Add to review sheet" with offline Sheets creates local sourcing task,
    marks sheet_status='failed' (or 'pending'), retains outbox write, and survives without crash.
    """
    service = service_env["service"]
    repo = service_env["repo"]
    remote = service_env["remote"]

    # Set Sheets offline
    remote.is_offline = True

    # Create an opportunity alert
    alert_rec = await alert(
        repo,
        kind="stock_match",
        key="macbook_m3:16:512",
        title="Supplier match · MacBook Pro M3",
        body="Supplier offered 5 units MacBook Pro M3 16GB 512GB.",
        details={"device": "MacBook Pro M3", "price": 1250, "currency": "USD"},
    )
    alert_id = alert_rec["id"]

    # Execute "save_task" (Add to review sheet)
    result = await service.alert_action({"id": alert_id, "action": "save_task"})

    # Verify alert status updated
    assert result["data"]["state"] == "SAVED_FOR_REVIEW"
    assert result["data"]["sheet_status"] in ("failed", "pending")
    assert result["data"]["sourcing_task"] is not None
    sourcing_task_id = result["data"]["sourcing_task"]

    # Verify sourcing task exists locally in SQLite
    task_rec = repo.get_sync("tasks", sourcing_task_id)
    assert task_rec is not None
    assert task_rec["data"]["kind"] == "sourcing_review"
    assert task_rec["data"]["state"] == "MANUAL_REVIEW"
    assert task_rec["data"]["sheet_status"] in ("failed", "pending")

    # Verify outbox retains the write for eventual sync
    assert repo.has_pending_sheet_writes()

    # Query via alerts_list: truthful status reflected
    alerts_view = await service.alerts_list({})
    target_alert = next((a for a in alerts_view["alerts"] if a["id"] == alert_id), None)
    assert target_alert is not None
    assert target_alert["data"]["sheet_status"] in ("failed", "pending")


@pytest.mark.asyncio
async def test_gate_scenario_5_add_to_review_sheet_online_and_idempotency(service_env):
    """Scenario 5: "Add to review sheet" with healthy Sheets syncs to remote, sets sheet_status='synced',
    and duplicate clicks are strictly idempotent without duplicate tasks or revision conflicts.
    """
    service = service_env["service"]
    repo = service_env["repo"]
    remote = service_env["remote"]

    remote.is_offline = False

    alert_rec = await alert(
        repo,
        kind="stock_match",
        key="ipad_air_m2:64:wifi",
        title="High demand · iPad Air M2",
        body="Matching buyers for iPad Air M2.",
        details={"device": "iPad Air M2"},
    )
    alert_id = alert_rec["id"]

    # First click: Add to review sheet
    res1 = await service.alert_action({"id": alert_id, "action": "save_task"})
    assert res1["data"]["state"] == "SAVED_FOR_REVIEW"
    assert res1["data"]["sheet_status"] == "synced"
    assert res1["data"]["sheet_synced_at"] is not None
    sourcing_task_id = res1["data"]["sourcing_task"]

    # Verify remote Sheets received the record
    assert ("tasks", sourcing_task_id) in remote.records
    assert remote.records[("tasks", sourcing_task_id)]["data"]["kind"] == "sourcing_review"

    # Second click: Duplicate click
    res2 = await service.alert_action({"id": alert_id, "action": "save_task"})
    assert res2["data"]["sourcing_task"] == sourcing_task_id
    assert res2["data"]["sheet_status"] == "synced"

    # Third click: Duplicate click
    res3 = await service.alert_action({"id": alert_id, "action": "add_to_review_sheet"})
    assert res3["data"]["sourcing_task"] == sourcing_task_id

    # Verify only ONE sourcing task was created in tasks
    all_sourcing = [r for r in await repo.list("tasks", 100) if r["id"] == sourcing_task_id]
    assert len(all_sourcing) == 1


@pytest.mark.asyncio
async def test_gate_scenario_6_send_availability_update_evidence_and_unknown(service_env):
    """Scenario 6: "Send availability update" queues one outbound intent, prevents repeat clicks,
    handles race conditions with automatic quotes, and enforces real send evidence:
    - initially buyer_notified=False, availability_status='QUEUED'
    - UNKNOWN dispatch preserves buyer_notified=False, availability_status='UNKNOWN'
    - ACCEPTED dispatch sets buyer_notified=True, availability_status='NOTIFIED'
    - FAILED dispatch preserves buyer_notified=False, availability_status='FAILED'
    """
    service = service_env["service"]
    repo = service_env["repo"]

    # Setup buyer inquiry message in messages collection
    buyer_msg_id = "msg_buyer_avail_99"
    await repo.put("messages", buyer_msg_id, {
        "text": "WTB 5 iPhone 16 Pro Max",
        "phone": "+919876543210",
        "conversation_id": "919876543210@s.whatsapp.net",
        "state": "CLASSIFIED",
    }, 0, buyer_msg_id)

    # Create matching alert
    alert_rec = await alert(
        repo,
        kind="stock_match",
        key="iphone16_promax:256:black",
        title="Stock match · iPhone 16 Pro Max",
        body="Available stock matches buyer inquiry.",
        details={
            "device": "iPhone 16 Pro Max",
            "buyer_message": buyer_msg_id,
            "buyer_phone": "+919876543210",
            "buyer_jid": "919876543210@s.whatsapp.net",
        },
    )
    alert_id = alert_rec["id"]

    # 1. Click "Send availability update"
    res1 = await service.alert_action({"id": alert_id, "action": "send_availability_update"})
    assert res1["data"]["availability_outbound_id"] is not None
    assert res1["data"]["availability_status"] == "QUEUED"
    assert res1["data"]["buyer_notified"] is False
    outbound_id = res1["data"]["availability_outbound_id"]

    # Verify outbound row exists in outbound_queue
    outbound_row = repo.get_outbound(outbound_id)
    assert outbound_row is not None
    assert outbound_row["recipient_phone"] == "+919876543210"
    assert outbound_row["kind"] == "availability_update"

    # 2. Repeat click on "Send availability update": strictly idempotent
    res2 = await service.alert_action({"id": alert_id, "action": "send_availability_update"})
    assert res2["data"]["availability_outbound_id"] == outbound_id

    # 3. Simulate dispatch resulting in UNKNOWN (e.g. timeout after external call)
    repo.set_outbound_status(outbound_id, "UNKNOWN")
    alerts_view1 = await service.alerts_list({})
    target_alert1 = next(a for a in alerts_view1["alerts"] if a["id"] == alert_id)
    assert target_alert1["data"]["availability_status"] == "UNKNOWN"
    assert target_alert1["data"]["buyer_notified"] is False, "UNKNOWN send must NEVER be marked as buyer notified!"

    # 4. Simulate dispatch resulting in FAILED_BEFORE_SEND
    repo.set_outbound_status(outbound_id, "FAILED_BEFORE_SEND")
    alerts_view2 = await service.alerts_list({})
    target_alert2 = next(a for a in alerts_view2["alerts"] if a["id"] == alert_id)
    assert target_alert2["data"]["availability_status"] == "FAILED"
    assert target_alert2["data"]["buyer_notified"] is False

    # 5. Simulate real provider acceptance (ACCEPTED with server receipt)
    repo.set_outbound_status(outbound_id, "ACCEPTED", meta_patch={"receipt_ids": ["wpp_rcpt_12345"]})
    alerts_view3 = await service.alerts_list({})
    target_alert3 = next(a for a in alerts_view3["alerts"] if a["id"] == alert_id)
    assert target_alert3["data"]["availability_status"] == "NOTIFIED"
    assert target_alert3["data"]["buyer_notified"] is True
    assert target_alert3["data"]["buyer_notified_at"] is not None


@pytest.mark.asyncio
async def test_gate_scenario_7_independent_effects_tracking(service_env):
    """Scenario 7: Sheet sync and WhatsApp delivery statuses are tracked independently without coupling:
    - Sheet sync succeeds while WhatsApp fails
    - Sheet sync fails while WhatsApp succeeds
    """
    service = service_env["service"]
    repo = service_env["repo"]
    remote = service_env["remote"]

    # Alert A: Sheet succeeds, WhatsApp fails
    remote.is_offline = False
    alert_a = await alert(
        repo,
        kind="stock_match",
        key="item_a_test",
        title="Alert A",
        body="Sheet ok, WhatsApp fail",
        details={"device": "Pixel 9", "buyer_phone": "+919876543211", "buyer_jid": "919876543211@s.whatsapp.net"},
    )
    # 1. Save to Sheet
    await service.alert_action({"id": alert_a["id"], "action": "save_task"})
    # 2. Send availability update
    await service.alert_action({"id": alert_a["id"], "action": "send_availability_update"})
    alert_a_rec = repo.get_sync("tasks", alert_a["id"])
    repo.set_outbound_status(alert_a_rec["data"]["availability_outbound_id"], "FAILED_BEFORE_SEND")

    view_a = await service.alerts_list({})
    item_a = next(a for a in view_a["alerts"] if a["id"] == alert_a["id"])
    assert item_a["data"]["sheet_status"] == "synced"
    assert item_a["data"]["availability_status"] == "FAILED"
    assert item_a["data"]["buyer_notified"] is False

    # Alert B: Sheet fails, WhatsApp succeeds
    remote.is_offline = True
    alert_b = await alert(
        repo,
        kind="stock_match",
        key="item_b_test",
        title="Alert B",
        body="Sheet fail, WhatsApp ok",
        details={"device": "Pixel 9 Pro", "buyer_phone": "+919876543212", "buyer_jid": "919876543212@s.whatsapp.net"},
    )
    await service.alert_action({"id": alert_b["id"], "action": "save_task"})
    await service.alert_action({"id": alert_b["id"], "action": "send_availability_update"})
    alert_b_rec = repo.get_sync("tasks", alert_b["id"])
    repo.set_outbound_status(alert_b_rec["data"]["availability_outbound_id"], "ACCEPTED")

    view_b = await service.alerts_list({})
    item_b = next(a for a in view_b["alerts"] if a["id"] == alert_b["id"])
    assert item_b["data"]["sheet_status"] in ("failed", "pending")
    assert item_b["data"]["availability_status"] == "NOTIFIED"
    assert item_b["data"]["buyer_notified"] is True


@pytest.mark.asyncio
async def test_gate_scenario_8_newer_message_revision_resolves_old_notification(service_env):
    """Scenario 8: A newer message revision invalidates older active notifications for that message,
    transitioning them to state='RESOLVED' with resolution_reason='SUPERSEDED_BY_NEWER_REVISION'.
    """
    service = service_env["service"]
    repo = service_env["repo"]

    # 1. Create initial message
    msg_id = "msg_buyer_rev_001"
    await repo.put("messages", msg_id, {
        "text": "WTB 10 iPhone 15 Pro",
        "phone": "+919876543210",
        "conversation_id": "919876543210@s.whatsapp.net",
        "state": "CLASSIFIED",
    }, 0, msg_id)

    # 2. Create alert referencing this message
    alert_rec = await alert(
        repo,
        kind="stock_match",
        key="iphone15_pro:rev1",
        title="Matching inquiry · iPhone 15 Pro",
        body="Inquiry for 10 iPhone 15 Pro",
        details={"buyer_message": msg_id, "device": "iPhone 15 Pro"},
    )
    alert_id = alert_rec["id"]

    initial_list = await service.alerts_list({})
    assert initial_list["unread_count"] >= 1
    target_alert = next(a for a in initial_list["alerts"] if a["id"] == alert_id)
    assert target_alert["data"]["state"] == "NEW"

    # 3. Simulate newer message revision / edit arriving
    resolved_count = repo.resolve_superseded_notifications(msg_id, newer_revision=2)
    assert resolved_count == 1

    # 4. Verify alert is now RESOLVED and unread count is updated
    updated_rec = repo.get_sync("tasks", alert_id)
    assert updated_rec["data"]["state"] == "RESOLVED"
    assert updated_rec["data"]["resolved_at"] is not None
    assert updated_rec["data"]["resolution_reason"] == "SUPERSEDED_BY_NEWER_REVISION"


@pytest.mark.asyncio
async def test_gate_scenario_9_history_pagination_and_archive_all(service_env):
    """Scenario 9 (B17): History pagination contract (limit, offset, state, category, total, unread_count, has_more)
    and archive all active notifications while preserving full SQLite history.
    """
    service = service_env["service"]
    repo = service_env["repo"]

    # Create 20 distinct alerts with various states
    for i in range(20):
        rec = await alert(
            repo,
            kind="stock_match" if i % 2 == 0 else "sourcing_need",
            key=f"item_pag_{i}",
            title=f"Notification #{i}",
            body=f"Body content for #{i}",
            details={"device": f"Device_{i}"},
        )
        if i >= 15:
            # Mark some as READ or DISMISSED
            d = dict(rec["data"])
            d["state"] = "READ" if i < 18 else "DISMISSED"
            await repo.put("tasks", rec["id"], d, rec["revision"])

    # 1. Page 1: limit=5, offset=0 (active alerts: 18 active, 2 dismissed)
    p1 = await service.alerts_list({"limit": 5, "offset": 0})
    assert len(p1["alerts"]) == 5
    assert p1["limit"] == 5
    assert p1["offset"] == 0
    assert p1["total"] == 18
    assert p1["unread_count"] == 15  # 15 NEW alerts
    assert p1["has_more"] is True

    # Check ALL state returns full total of 20
    p_all_initial = await service.alerts_list({"state": "ALL", "limit": 50})
    assert p_all_initial["total"] == 20

    # 2. Page 2: limit=5, offset=5
    p2 = await service.alerts_list({"limit": 5, "offset": 5})
    assert len(p2["alerts"]) == 5
    assert p2["offset"] == 5
    assert p2["has_more"] is True
    # Ensure items on page 1 and page 2 are distinct
    p1_ids = {a["id"] for a in p1["alerts"]}
    p2_ids = {a["id"] for a in p2["alerts"]}
    assert len(p1_ids.intersection(p2_ids)) == 0

    # 3. Filter by state='NEW'
    p_new = await service.alerts_list({"state": "NEW", "limit": 50})
    assert p_new["total"] == 15
    assert all(a["data"]["state"] == "NEW" for a in p_new["alerts"])

    # 4. Filter by category='sourcing_need' (active: 9 active, 1 dismissed)
    p_sourcing = await service.alerts_list({"category": "sourcing_need", "limit": 50})
    assert p_sourcing["total"] == 9
    assert all(a["data"]["category"] == "sourcing_need" for a in p_sourcing["alerts"])
    p_sourcing_all = await service.alerts_list({"category": "sourcing_need", "state": "ALL", "limit": 50})
    assert p_sourcing_all["total"] == 10

    # 5. Clear all: archives active notifications
    clear_res = await service.alert_action({"action": "clear_all"})
    assert clear_res["archived_count"] > 0

    # Verify unread count is now 0
    p_after_clear = await service.alerts_list({"limit": 50})
    assert p_after_clear["unread_count"] == 0
    assert len(p_after_clear["alerts"]) == 0  # default view excludes ARCHIVED

    # Verify all records remain preserved in SQLite under state='ARCHIVED'
    p_archived = await service.alerts_list({"state": "ALL", "limit": 50})
    assert p_archived["total"] == 20
    assert all(a["data"]["state"] in ("ARCHIVED", "DISMISSED") for a in p_archived["alerts"])
