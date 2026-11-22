import asyncio
import os
import sys
import time
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from conftest import FakeSheets, FakeVault
from jarvis.desktop_service import DesktopService, DesktopError
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.runner import Runner
from jarvis.pipeline import IntakePipeline
from jarvis.onboarding import BusinessSetup
from jarvis.providers import Triage, TradeItem


class Bridge:
    ready = False
    last_seen = None
    def stop(self):
        pass


class MockRawRepo:
    def __init__(self):
        self._collections = {}

    async def get(self, collection, record_id):
        return self._collections.get((collection, record_id))

    async def put(self, collection, record_id, data, revision=0, idempotency_key=None):
        rev = revision + 1
        record = {"id": record_id, "data": data, "revision": rev, "updated_at": time.time()}
        self._collections[(collection, record_id)] = record
        return record

    async def list(self, collection, limit=100):
        return [v for k, v in self._collections.items() if k[0] == collection][:limit]

    def invalidate(self, *collections):
        pass

    def close(self):
        pass


class MockClassifier:
    async def classify(self, text, media_b64=None, media_mime=None):
        return Triage(
            category="BUY",
            proceed=True,
            needs_review=False,
            reason="Commercial buyer inquiry",
            items=[TradeItem(device="iPhone 17", variant="256GB", quantity=50, unit_price=820.0, currency="USD", condition="NON_ACTIVE")],
        )


@pytest.mark.asyncio
async def test_phase1_unconfigured_start_fails_cleanly(tmp_path):
    vault = FakeVault(); vault.set("bridge_secret", "test-secret")
    sql_repo = SQLiteSyncedRepository(FakeSheets(), db_path=tmp_path / "ws.db")
    runner = Runner(vault, sql_repo); runner.bridge = Bridge()
    app = DesktopService(vault, sql_repo, runner, opener=lambda: None, cache_path=tmp_path / "cache.json")
    try:
        # Starting with zero configuration MUST raise SETUP_REQUIRED, never silently fall back
        with pytest.raises(DesktopError) as exc_info:
            await app.start({})
        assert exc_info.value.code == "SETUP_REQUIRED"
    finally:
        app.close()


@pytest.mark.asyncio
async def test_phase1_unknown_buyer_intake_unblocked_and_approved(tmp_path):
    vault = FakeVault(); vault.set("bridge_secret", "test-secret")
    sql_repo = SQLiteSyncedRepository(FakeSheets(), db_path=tmp_path / "ws.db")
    runner = Runner(vault, sql_repo); runner.bridge = Bridge()
    app = DesktopService(vault, sql_repo, runner, opener=lambda: None, cache_path=tmp_path / "cache.json")
    try:
        # 1. Save fresh setup with custom business phone (UK desk owner + Dubai supplier)
        owner_phone = "+447700900077"
        stock_supplier = "+97142000001"
        await app.save_setup({
            "owner_phone": owner_phone,
            "stock_phones": [stock_supplier],
            "database_phone": stock_supplier,
        })
        await app.start({})

        # Verify lead_capture defaults to True (Inbound Lead Capture enabled)
        setup_data = app.configuration["data"]
        assert setup_data["lead_capture"] is True

        # 2. Receive message from brand-new UNKNOWN buyer (India trader)
        unknown_buyer_phone = "+919123456789"
        unknown_buyer_jid = "919123456789@c.us"
        event = {
            "conversation_id": unknown_buyer_jid,
            "sender_id": unknown_buyer_jid,
            "phone": unknown_buyer_phone,
            "sender_name": "New Trade Partner",
            "chat_name": "New Trade Partner",
            "message_id": f"false_{unknown_buyer_jid}_msg101",
            "text": "WTB 50 pcs iPhone 17 256GB ready payment",
            "timestamp": time.time(),
            "identity_verified": True,
        }

        # Process through IntakePipeline with MockClassifier
        pipeline = IntakePipeline(sql_repo, MockClassifier(), local_first=True)
        res = await pipeline.process(event, setup_data)

        # MUST be captured & classified, NOT dropped as IGNORED_SOURCE
        assert res["status"] == "CLASSIFIED"
        saved = sql_repo.get_sync("messages", res["id"])
        assert saved is not None
        assert saved["data"]["state"] == "CLASSIFIED"
        assert saved["data"]["phone"] == unknown_buyer_phone

        # 3. Create outbound quote draft to this new buyer
        draft_res = await app.outbound_create({
            "recipient_phone": unknown_buyer_phone,
            "text": "iPhone 17 256GB ready 50pcs @ 825 USD",
        })
        outbound_id = draft_res["id"]
        assert bool(outbound_id)

        # 4. Approve the outbound draft - MUST succeed for unknown buyer (not locked to stock phones)
        runner.active = True
        runner.autonomy_mode = "draft"
        assert runner.can_send is True
        draft_row = sql_repo._conn.execute("SELECT * FROM outbound_queue WHERE id=?", (outbound_id,)).fetchone()
        approve_res = await app.outbound_action({
            "id": outbound_id,
            "action": "approve",
            "expected_updated_at": draft_row["updated_at"],
        })
        assert approve_res is not None

        # Verify draft transitioned to READY in dispatch table
        updated_row = sql_repo._conn.execute("SELECT * FROM outbound_queue WHERE id=?", (outbound_id,)).fetchone()
        assert updated_row["status"] == "READY"
        dispatch_row = sql_repo._conn.execute("SELECT * FROM outbound_dispatch WHERE id=?", (outbound_id,)).fetchone()
        assert dispatch_row is not None
        assert dispatch_row["account_phone"] == owner_phone

    finally:
        app.close()


@pytest.mark.asyncio
async def test_phase1_adversarial_blocks_and_safety_checks(tmp_path):
    vault = FakeVault(); vault.set("bridge_secret", "test-secret")
    sql_repo = SQLiteSyncedRepository(FakeSheets(), db_path=tmp_path / "ws.db")
    runner = Runner(vault, sql_repo); runner.bridge = Bridge()
    app = DesktopService(vault, sql_repo, runner, opener=lambda: None, cache_path=tmp_path / "cache.json")
    try:
        owner_phone = "+447700900077"
        stock_supplier = "+97142000001"
        await app.save_setup({
            "owner_phone": owner_phone,
            "stock_phones": [stock_supplier],
        })
        await app.start({})
        setup_data = app.configuration["data"]

        # ATTACK 1: Inbound message from owner's own number -> MUST be IGNORED_OWNER
        owner_self_event = {
            "conversation_id": "447700900077@c.us",
            "sender_id": "447700900077@c.us",
            "phone": owner_phone,
            "message_id": "false_447700900077@c.us_self1",
            "text": "Note to self: buy 10 pcs iPhone",
            "timestamp": time.time(),
            "identity_verified": True,
        }
        pipeline = IntakePipeline(sql_repo, MockClassifier(), local_first=True)
        res_self = await pipeline.process(owner_self_event, setup_data)
        assert res_self["status"] == "IGNORED_OWNER"

        # ATTACK 2: Outbound draft to owner's own number -> MUST FAIL with SELF_SEND_BLOCKED
        with pytest.raises(DesktopError) as exc_self:
            await app.outbound_create({
                "recipient_phone": owner_phone,
                "text": "Spam myself",
            })
        assert exc_self.value.code == "SELF_SEND_BLOCKED"

        # ATTACK 3: Family / Ignored contact inbound & outbound blocks
        family_phone = "+919888877777"
        family_jid = "919888877777@c.us"
        sql_repo.upsert_contact(family_jid, name="Mom", phone=family_phone, category="FAMILY_PERSONAL")

        family_event = {
            "conversation_id": family_jid,
            "sender_id": family_jid,
            "phone": family_phone,
            "message_id": f"false_{family_jid}_fam1",
            "text": "Beta ghar kab aaoge dinner ready hai",
            "timestamp": time.time(),
            "identity_verified": True,
        }
        res_fam = await pipeline.process(family_event, setup_data)
        assert res_fam["status"] == "IGNORED_FAMILY"

        # Outbound draft to family contact -> MUST FAIL with RECIPIENT_BLOCKED
        with pytest.raises(DesktopError) as exc_fam:
            await app.outbound_create({
                "recipient_phone": family_phone,
                "text": "iPhone 17 256GB rate 820$",
            })
        assert exc_fam.value.code == "RECIPIENT_BLOCKED"

        # ATTACK 4: Spoofed JID attack in approve_outbound
        # Direct queue insert with spoofed JID mismatching phone
        bad_oid = sql_repo.queue_outbound("919999999999@c.us", ["Spoofed text"], kind="manual", recipient_phone="+919123456789")
        bad_row = sql_repo._conn.execute("SELECT * FROM outbound_queue WHERE id=?", (bad_oid,)).fetchone()
        reason = sql_repo.approve_outbound(bad_oid, bad_row["updated_at"], owner_phone, allowed_phones=None)
        assert reason == "RECIPIENT_NOT_ALLOWED"

        # ATTACK 5: Direct sync with classifier=None enters PENDING_CLASSIFICATION (NOT MODEL_KEY_REQUIRED)
        unclassified_event = {
            "conversation_id": "919555544444@c.us",
            "sender_id": "919555544444@c.us",
            "phone": "+919555544444",
            "message_id": "false_919555544444@c.us_unclass1",
            "text": "Special component lot 1000 pcs STM32F405",
            "timestamp": time.time(),
            "identity_verified": True,
        }
        setup_with_classifier = {**setup_data, "classifier_configured": True}
        sync_pipeline = IntakePipeline(sql_repo, classifier=None, local_first=False)
        sync_res = await sync_pipeline.process(unclassified_event, setup_with_classifier)
        assert sync_res["status"] == "CAPTURED_REVIEW"
        assert sync_res.get("queue_state") == "PENDING_CLASSIFICATION"
        saved_msg = sql_repo.get_sync("messages", sync_res["id"])
        assert saved_msg["data"]["state"] == "RECEIVED"
        assert saved_msg["data"]["reason"] == "PENDING_CLASSIFICATION"

    finally:
        app.close()


def test_phase1_encoding_linter():
    from scripts.audit_repo_encoding import audit_encoding
    # Runs the full repository encoding scan across all text files
    # System exit code 0 or successful return proves zero mojibake
    try:
        audit_encoding()
    except SystemExit as e:
        assert e.code == 0
