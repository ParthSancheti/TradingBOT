"""Behavioral regression tests asserting the boundary contracts identified in
PRODUCTION_CHANGE_AUDIT_2026-11-21.md (A01-A18).

These tests run against real DesktopService, LocalRepo, Runner, and boundary schemas
to demonstrate the red -> green progression required by Phase 0 through Phase 7.
"""
import asyncio
import json
import os
import secrets
from pathlib import Path
import pytest

from jarvis.desktop_service import DesktopService, Params, OutboundListParams
from jarvis.intelligence import DeskSettings
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.runner import Runner


class FakeVault:
    def __init__(self, initial=None):
        self._data = dict(initial or {})
    def get(self, k, default=None):
        return self._data.get(k, default)
    def set(self, k, v):
        self._data[k] = v
    def delete(self, k):
        self._data.pop(k, None)


class FakeSheet:
    def __init__(self):
        self.rows = {}
    async def get(self, c, i):
        return self.rows.get((c, i))
    async def list(self, c, limit=100):
        return [r for (k, _), r in self.rows.items() if k == c][:limit]
    async def batch(self, changes, request_id=None):
        out = []
        for ch in changes:
            key = (ch["collection"], ch["id"])
            rev = self.rows.get(key, {}).get("revision", 0) + 1
            self.rows[key] = {"id": ch["id"], "revision": rev, "data": ch["data"]}
            out.append(self.rows[key])
        return {"records": out}
    async def health(self):
        return {"storage": "memory"}


@pytest.fixture
def test_env(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    vault = FakeVault({
        "bridge_secret": "test_secret_1234567890abcdef",
        "extension_id": "afbnhhklkdgmljgfcmmdedkndolmkgnd",
    })
    sheet = FakeSheet()
    repo = SQLiteSyncedRepository(sheet, tmp_path / "test_workspace.db")
    runner = Runner(vault, repo)
    runner.service_loop = loop
    runner.cached_setup = {
        "owner_phone": "+919800000001",
        "sources": [{"phone": "+919800000002", "label": "Supplier 1", "active": True}],
    }
    svc = DesktopService(vault, repo, runner, opener=lambda: None)
    yield {"vault": vault, "sheet": sheet, "repo": repo, "runner": runner, "svc": svc, "tmp_path": tmp_path, "loop": loop}
    loop.close()


def test_a03_direct_pump_list_params_extra_forbidden():
    """A03: Direct pump passes statuses and limit to outbound/list.
    OutboundListParams schema must accept them instead of failing with extra_forbidden.
    """
    payload = {"statuses": ["READY"], "limit": 10}
    validated = OutboundListParams.model_validate(payload)
    assert validated.statuses == ["READY"]
    assert validated.limit == 10


def test_a02_legacy_auto_mode_dispatch_policy(test_env):
    """A02: Reproduce legacy auto causing DISPATCH_DISABLED, and verify migration to draft enables send."""
    runner = test_env["runner"]
    runner.active = True
    runner.autonomy_mode = "auto"
    assert runner.can_send is False, "Legacy unmigrated auto mode reproduces DISPATCH_DISABLED"

    # After settings migration, auto becomes draft
    cfg = DeskSettings.model_validate({"autonomy_mode": "auto"})
    assert cfg.autonomy_mode == "draft"
    runner.autonomy_mode = cfg.autonomy_mode
    assert runner.can_send is True, "Migrated draft mode enables supervised send"


def test_a02_settings_accepts_legacy_modes():
    """A02: DeskSettings must accept approved_auto and migrate legacy modes safely."""
    # Currently fails: approved_auto is rejected by Literal["observe","draft","send","auto"]
    cfg = DeskSettings.model_validate({"autonomy_mode": "approved_auto"})
    assert cfg.autonomy_mode in ("draft", "send", "approved_auto")


def test_a08_client_request_id_replay_preserves_ready_state(test_env):
    """A08: Replaying the same client_request_id with approve_now must report READY,
    not DRAFT / DRAFT_CHANGED.
    """
    svc = test_env["svc"]
    runner = test_env["runner"]
    runner.active = True
    runner.autonomy_mode = "send"
    loop = test_env["loop"]
    
    req_id = "req_test_dedupe_001"
    create_params = {
        "recipient_phone": "+919800000002",
        "text": "Hello test offer 10 pcs",
        "approve_now": True,
        "client_request_id": req_id,
    }
    res1 = loop.run_until_complete(svc.outbound_create(create_params))
    assert res1["status"] == "READY", f"Initial create failed: {res1}"
    
    # Replay same client_request_id
    res2 = loop.run_until_complete(svc.outbound_create(create_params))
    # Currently fails: res2 returns status="DRAFT", reason="DRAFT_CHANGED"
    assert res2["status"] == "READY", f"Replay reported wrong status: {res2}"
    assert res2["approved"] is True


def test_a02_price_review_drafts_in_outbound_list(test_env):
    """A02: Price-review drafts must be returned in outbound/list."""
    repo = test_env["repo"]
    svc = test_env["svc"]
    loop = test_env["loop"]
    # Queue a draft and set status to NEEDS_PRICE_REVIEW
    oid = repo.queue_outbound("+919800000002@c.us", ["Quote text"], kind="price_review", recipient_phone="+919800000002")
    with repo._lock, repo._conn:
        repo._conn.execute("UPDATE outbound_queue SET status='NEEDS_PRICE_REVIEW' WHERE id=?", (oid,))
    
    res = loop.run_until_complete(svc.outbound_list({}))
    items = res.get("items", [])
    item_ids = [it["id"] for it in items]
    # Currently fails: outbound_list query does not include NEEDS_PRICE_REVIEW in status tuple
    assert oid in item_ids, "NEEDS_PRICE_REVIEW draft was omitted from outbound/list"


def test_a14_chime_and_base_currency_persistence(test_env):
    """A14: Chime and default trading currency must be accepted by settings/save
    and returned by settings/get.
    """
    svc = test_env["svc"]
    loop = test_env["loop"]
    save_res = loop.run_until_complete(svc.settings_save({
        "chime": True,
        "base_currency": "AED",
    }))
    assert save_res.get("chime") is True
    assert save_res.get("base_currency") == "AED"
    
    get_res = loop.run_until_complete(svc.settings_get({}))
    assert get_res.get("chime") is True
    assert get_res.get("base_currency") == "AED"


def test_a09_attachment_outbound_queue_and_claim(test_env):
    """A09: Outbound create with attachment must be accepted, approved to READY,
    and dispatch_claim must include attachment_path, caption, and metadata.
    Replay with mismatched attachment must raise CLIENT_REQUEST_CONFLICT.
    """
    svc = test_env["svc"]
    runner = test_env["runner"]
    repo = test_env["repo"]
    runner.active = True
    runner.autonomy_mode = "send"
    loop = test_env["loop"]

    att_file = str(test_env["tmp_path"] / "quote.pdf")
    with open(att_file, "w") as f:
        f.write("%PDF-1.4 test")

    req_id = "req_att_test_001"
    create_params = {
        "recipient_phone": "+919800000002",
        "text": "Please see attached quote",
        "attachment_path": att_file,
        "caption": "Please see attached quote",
        "attachment_name": "quote.pdf",
        "attachment_mime": "application/pdf",
        "attachment_type": "document",
        "attachment_size": 13,
        "approve_now": True,
        "client_request_id": req_id,
    }
    res = loop.run_until_complete(svc.outbound_create(create_params))
    assert res["status"] == "READY", f"Attachment create failed: {res}"
    oid = res["id"]

    # Test dispatch_claim
    claim_res = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid, "session_id": "test_session_0123456789"},
        "+919800000001",
        allowed_phones=None,
        enabled=True,
    )
    assert "item" in claim_res, f"Claim failed: {claim_res}"
    item = claim_res["item"]
    assert item["attachment_path"] == att_file
    assert item["caption"] == "Please see attached quote"
    assert item["meta"]["attachment_name"] == "quote.pdf"

    # Test replay with different attachment raises CLIENT_REQUEST_CONFLICT
    conflict_params = dict(create_params)
    conflict_params["attachment_path"] = str(test_env["tmp_path"] / "other.pdf")
    with pytest.raises(Exception) as exc_info:
        loop.run_until_complete(svc.outbound_create(conflict_params))
    assert "CLIENT_REQUEST_CONFLICT" in str(exc_info.value)


def test_a16_work_vs_social_filtering(test_env):
    """A16: Work filter must exclude SOCIAL messages at query time.
    ActivityParams must validate category='WORK'.
    """
    repo = test_env["repo"]
    svc = test_env["svc"]
    loop = test_env["loop"]

    import time
    now = time.time()
    # Insert one WORK message (BUY) and one SOCIAL message
    with repo._lock, repo._conn:
        repo._conn.execute(
            """INSERT INTO records(collection, id, revision, data_json, updated_at)
               VALUES('messages', 'msg-work-1', 1, ?, ?)""",
            (json.dumps({
                "text": "Need 50 pcs iPhone 15",
                "phone": "+919800000002",
                "state": "CLASSIFIED",
                "triage": {"category": "BUY", "proceed": True},
            }), now),
        )
        repo._conn.execute(
            """INSERT INTO records(collection, id, revision, data_json, updated_at)
               VALUES('messages', 'msg-social-1', 1, ?, ?)""",
            (json.dumps({
                "text": "Good morning bhai kaise ho",
                "phone": "+919800000003",
                "state": "IGNORED_SOCIAL",
                "triage": {"category": "OTHER", "proceed": False},
            }), now),
        )

    # Query with category WORK
    work_res = loop.run_until_complete(svc.activity({"category": "WORK", "limit": 10}))
    work_ids = [m["id"] for m in work_res["messages"]]
    assert "msg-work-1" in work_ids
    assert "msg-social-1" not in work_ids, "Social message leaked into WORK feed!"

    # Query with category ALL
    all_res = loop.run_until_complete(svc.activity({"category": "ALL", "limit": 10}))
    all_ids = [m["id"] for m in all_res["messages"]]
    assert "msg-work-1" in all_ids
    assert "msg-social-1" in all_ids


def test_a07_a11_a15_outgoing_echo_provenance_and_mute(test_env):
    """A07, A11, A15: Outgoing echo must persist delivery_status, ack, and classification_source.
    Human takeover mute duration must reflect configured human_mute_hours.
    """
    repo = test_env["repo"]
    loop = test_env["loop"]
    from jarvis.pipeline import IntakePipeline
    pipeline = IntakePipeline(repo, None, local_first=True)

    event = {
        "message_id": "true_919800000002@c.us_3EB0123456789",
        "conversation_id": "919800000002@c.us",
        "phone": "+919800000002",
        "text": "Manual staff reply from phone",
        "outgoing": True,
        "ack": 2,
        "delivery_status": "DELIVERED",
        "timestamp": 1700000000,
        "identity_verified": True,
    }
    setup = {
        "owner_phone": "+919800000001",
        "sources": [{"phone": "+919800000002", "active": True}],
        "human_mute_hours": 2.5,
    }

    res = loop.run_until_complete(pipeline.process(event, setup))
    assert res["status"] == "OUTGOING"

    # Verify persisted record
    rec = repo.get_sync("messages", res["id"])
    data = rec["data"]
    assert data["delivery_status"] == "DELIVERED"
    assert data["ack"] == 2
    assert data["classification_source"] == "outbound_echo"

    # Verify human takeover contact mute
    contact = repo.get_contact("919800000002@c.us")
    assert contact is not None
    assert contact.get("manual_mute_until") is not None


def test_turbo_mode_policy_and_auto_dispatch(test_env):
    """Turbo mode: DeskSettings accepts turbo, runner.can_send is True,
    ready_outbound_ids returns quotes without manual_only restriction,
    and switching mode or stop revokes unsent work.
    """
    svc = test_env["svc"]
    runner = test_env["runner"]
    repo = test_env["repo"]
    loop = test_env["loop"]

    # 1. Settings validation
    cfg = DeskSettings.model_validate({"autonomy_mode": "turbo"})
    assert cfg.autonomy_mode == "turbo"

    runner.active = True
    runner.autonomy_mode = "turbo"
    assert runner.can_send is True

    # 2. Queue an automated quote in READY status
    oid = repo.queue_outbound(
        "+919800000002@c.us",
        ["Quote: iPhone 16 Pro 256GB @ 115000 INR"],
        kind="quote",
        status="READY",
        account_phone="+919800000001",
        recipient_phone="+919800000002",
    )

    # 3. In turbo mode, ready_outbound_ids must return automated quote (manual_only=False)
    ready = repo.ready_outbound_ids("+919800000001", manual_only=False)
    assert any(r["id"] == oid for r in ready), "Turbo mode must select automated READY quotes"

    # In send mode, manual_only=True excludes automated quotes
    ready_manual_only = repo.ready_outbound_ids("+919800000001", manual_only=True)
    assert not any(r["id"] == oid for r in ready_manual_only), "Manual mode must exclude automated quotes"

    # 4. Mode switch revokes unsent work
    loop.run_until_complete(svc.settings_save({"autonomy_mode": "draft"}))
    with repo._lock, repo._conn:
        row = repo._conn.execute("SELECT status FROM outbound_queue WHERE id=?", (oid,)).fetchone()
        assert row["status"] == "CANCELLED", "Mode switch must revoke unsent work"


def test_stock_add_vs_overwrite_bulk_semantics(test_env):
    """Stock & Demand: inventory/bulk supports mode='add' and mode='overwrite'.
    In 'add' mode, matched rows with expected_revision != 0 are skipped.
    In 'overwrite' mode, matched rows update with expected revision.
    """
    svc = test_env["svc"]
    repo = test_env["repo"]
    loop = test_env["loop"]

    # Insert an existing inventory item
    save_res = loop.run_until_complete(svc.inventory_save({
        "id": "inv_test_item_1",
        "expected_revision": 0,
        "device": "iPhone 16 Pro",
        "variant": "256GB Desert Titanium",
        "quantity": 10,
        "unit_cost": 114000.0,
        "currency": "INR",
    }))
    assert save_res["revision"] == 1

    # Test mode: "add"
    # Provide matched row (expected_revision=1) and a brand new row (expected_revision=0)
    bulk_add_params = {
        "request_id": "test_req_bulk_add_01",
        "mode": "add",
        "rows": [
            {
                "id": "inv_test_item_1",
                "expected_revision": 1,
                "device": "iPhone 16 Pro",
                "variant": "256GB Desert Titanium",
                "quantity": 25,
                "unit_cost": 110000.0,
                "currency": "INR",
            },
            {
                "id": "inv_test_item_2",
                "expected_revision": 0,
                "device": "Samsung S25 Ultra",
                "variant": "512GB Titanium Black",
                "quantity": 5,
                "unit_cost": 120000.0,
                "currency": "INR",
            },
        ],
    }
    add_res = loop.run_until_complete(svc.inventory_bulk(bulk_add_params))
    # In "add" mode, inv_test_item_1 was skipped; inv_test_item_2 was added
    rec1 = repo.get_sync("inventory", "inv_test_item_1")
    assert rec1["revision"] == 1, "Existing item revision must remain unchanged in 'add' mode"
    assert rec1["data"]["quantity"] == 10, "Existing item quantity must not be overwritten in 'add' mode"

    rec2 = repo.get_sync("inventory", "inv_test_item_2")
    assert rec2 is not None, "New item must be inserted in 'add' mode"
    assert rec2["data"]["quantity"] == 5

    # Test mode: "overwrite"
    # Update inv_test_item_1 with expected_revision=1
    bulk_overwrite_params = {
        "request_id": "test_req_bulk_overwrite_01",
        "mode": "overwrite",
        "rows": [
            {
                "id": "inv_test_item_1",
                "expected_revision": 1,
                "device": "iPhone 16 Pro",
                "variant": "256GB Desert Titanium",
                "quantity": 25,
                "unit_cost": 110000.0,
                "currency": "INR",
            },
        ],
    }
    ow_res = loop.run_until_complete(svc.inventory_bulk(bulk_overwrite_params))
    rec1_updated = repo.get_sync("inventory", "inv_test_item_1")
    assert rec1_updated["revision"] == 2, "Existing item must be updated to rev 2 in 'overwrite' mode"
    assert rec1_updated["data"]["quantity"] == 25, "Quantity must be updated to 25 in 'overwrite' mode"
    assert rec1_updated["data"]["unit_cost"] == 110000.0
