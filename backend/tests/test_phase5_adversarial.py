"""Phase 5 Adversarial Verification Suite: Native WhatsApp Bridge, Routing Safety & Anti-Ban Reliability."""
import asyncio
import time
import pytest
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.pipeline import IntakePipeline


class MockRemote:
    async def list(self, collection, limit=100):
        return []

    async def put(self, collection, record_id, data, expected_revision, request_id=None):
        return {"id": record_id, "revision": expected_revision + 1, "data": data}

    async def batch(self, changes, request_id=None):
        return [{"id": ch["id"], "revision": ch["expected_revision"] + 1, "data": ch["data"]} for ch in changes]

    async def health(self):
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_zero_wrong_chat_routing_and_isolation(tmp_path):
    """Adversarial Test 1: Zero wrong-chat routing: mismatched recipient phone/JID is rejected immediately."""
    db_path = tmp_path / "routing.db"
    repo = SQLiteSyncedRepository(MockRemote(), db_path)
    try:
        account_phone = "+12025550000"
        buyer_phone = "+12025550101"
        buyer_jid = buyer_phone[1:] + "@c.us"

        # Queue a valid draft quote
        oid = repo.queue_outbound(
            buyer_jid,
            ["Dear client, iPhone 16 Pro is USD 1050."],
            kind="manual",
            recipient_phone=buyer_phone,
            status="DRAFT"
        )
        row = repo.list_outbound(["DRAFT"])[0]

        # 1. Attempt to approve with wrong account phone (desk impersonation)
        err = repo.approve_outbound(oid, row["updated_at"], account_phone="", allowed_phones=[buyer_phone])
        assert err == "RECIPIENT_NOT_ALLOWED"

        # 2. Attempt to approve sending to own number (self-echo spam loop)
        err = repo.approve_outbound(oid, row["updated_at"], account_phone=buyer_phone, allowed_phones=[buyer_phone])
        assert err == "RECIPIENT_NOT_ALLOWED"

        # 3. Attempt to approve with allowed_phones that excludes the recipient
        err = repo.approve_outbound(oid, row["updated_at"], account_phone=account_phone, allowed_phones=["+12025559999"])
        assert err == "RECIPIENT_NOT_ALLOWED"

        # 4. Correct authorization succeeds cleanly
        err = repo.approve_outbound(oid, row["updated_at"], account_phone=account_phone, allowed_phones=[buyer_phone])
        assert err is None
        ready_row = repo.list_outbound(["READY"])[0]
        assert ready_row["id"] == oid
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_lid_fail_closed_verification(tmp_path):
    """Adversarial Test 2: Unverified @lid recipient fails closed until cryptographically/reliably bound to phone."""
    db_path = tmp_path / "lid_test.db"
    repo = SQLiteSyncedRepository(MockRemote(), db_path)
    try:
        account_phone = "+12025550000"
        unverified_lid = "98765432109876@lid"
        claimed_phone = "+12025550202"

        # Queue outbound draft to an @lid
        oid = repo.queue_outbound(
            unverified_lid,
            ["Price quote for component batch."],
            kind="manual",
            recipient_phone=claimed_phone,
            status="DRAFT"
        )
        row = repo.list_outbound(["DRAFT"])[0]

        # 1. Without mapping in lid_phone_map, approval FAILS CLOSED
        err = repo.approve_outbound(oid, row["updated_at"], account_phone=account_phone)
        assert err == "RECIPIENT_NOT_ALLOWED", "Unmapped @lid must fail closed"

        # 2. Save mapping for a DIFFERENT phone (mismatch spoof attack)
        repo.save_lid_phone(unverified_lid, "+12025550999")
        err = repo.approve_outbound(oid, row["updated_at"], account_phone=account_phone)
        assert err == "RECIPIENT_NOT_ALLOWED", "Mismatched @lid phone must fail closed"

        # 3. Save matching verified phone mapping
        repo.save_lid_phone(unverified_lid, claimed_phone)
        err = repo.approve_outbound(oid, row["updated_at"], account_phone=account_phone)
        assert err is None, "Verified @lid phone mapping allows safe approval"
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_in_flight_to_unknown_crash_idempotency_on_boot(tmp_path):
    """Adversarial Test 3: Crash recovery converts IN_FLIGHT -> UNKNOWN to prevent double-messaging customers."""
    db_path = tmp_path / "crash_boot.db"
    repo = SQLiteSyncedRepository(MockRemote(), db_path)
    try:
        # Enqueue two messages: one CLAIMED, one IN_FLIGHT
        oid_claimed = repo.queue_outbound("12025550303@c.us", ["Claimed msg"], kind="manual", recipient_phone="+12025550303", status="READY")
        oid_inflight = repo.queue_outbound("12025550404@c.us", ["In flight quote"], kind="manual", recipient_phone="+12025550404", status="READY")

        with repo._lock, repo._conn:
            repo._conn.execute("UPDATE outbound_queue SET status='CLAIMED' WHERE id=?", (oid_claimed,))
            repo._conn.execute("UPDATE outbound_queue SET status='IN_FLIGHT' WHERE id=?", (oid_inflight,))
    finally:
        repo.close()

    # Simulate app crash & cold reboot: open new repository on the same database file
    rebooted_repo = SQLiteSyncedRepository(MockRemote(), db_path)
    try:
        # IN_FLIGHT must transition to UNKNOWN (preventing duplicate sends without human confirmation)
        with rebooted_repo._lock:
            inflight_row = rebooted_repo._conn.execute("SELECT status FROM outbound_queue WHERE id=?", (oid_inflight,)).fetchone()
            assert inflight_row["status"] == "UNKNOWN", "Crashed IN_FLIGHT message must be UNKNOWN"

            # CLAIMED must safely recover to READY
            claimed_row = rebooted_repo._conn.execute("SELECT status FROM outbound_queue WHERE id=?", (oid_claimed,)).fetchone()
            assert claimed_row["status"] == "READY", "CLAIMED message must recover to READY"
    finally:
        rebooted_repo.close()


@pytest.mark.asyncio
async def test_human_takeover_auto_mute_and_approval_block(tmp_path):
    """Adversarial Test 4: Human staff manual reply triggers auto-mute, blocking automatic quotes."""
    db_path = tmp_path / "takeover.db"
    repo = SQLiteSyncedRepository(MockRemote(), db_path)
    try:
        phone = "+12025550505"
        jid = phone[1:] + "@c.us"
        setup = {"sources": [{"phone": phone, "active": True}], "owner_phone": "+12025550000"}
        pipeline = IntakePipeline(repo, None, local_first=True)

        # 1. Initially chat is NOT muted
        assert not repo.is_chat_muted(jid)

        # 2. Human staff manually types a message in WhatsApp Web (outgoing=True, no bot receipt)
        human_event = {
            "phone": phone,
            "conversation_id": jid,
            "message_id": f"true_{jid}_humanMsg1",
            "text": "Hey bhai, main dekh raha hoon, wait.",
            "outgoing": True,
            "identity_verified": True
        }
        res = await pipeline.process(human_event, setup)
        assert res["status"] == "OUTGOING"

        # 3. Chat must now be AUTOMATICALLY MUTED
        assert repo.is_chat_muted(jid) is True

        # 4. Attempt to approve an automatic quote for this muted customer fails closed
        oid = repo.queue_outbound(jid, ["Automatic quote $999"], kind="manual", recipient_phone=phone, status="DRAFT")
        row = repo.list_outbound(["DRAFT"])[0]
        err = repo.approve_outbound(oid, row["updated_at"], account_phone="+12025550000")
        assert err == "RECIPIENT_MUTED", "Human takeover must mute and block bot dispatches"
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_deduplication_and_anti_spam_pacing(tmp_path):
    """Adversarial Test 5: Queue deduplication prevents duplicate bursts for identical trade events."""
    db_path = tmp_path / "dedupe.db"
    repo = SQLiteSyncedRepository(MockRemote(), db_path)
    try:
        phone = "+12025550606"
        jid = phone[1:] + "@c.us"
        dedupe_key = "quote-req-12345"

        # First queuing
        oid1 = repo.queue_outbound(
            jid,
            ["Quote $500 for STM32"],
            kind="quote",
            recipient_phone=phone,
            status="DRAFT",
            dedupe_key=dedupe_key
        )

        # Second queuing with identical dedupe key (e.g. repeated burst from UI or event listener)
        oid2 = repo.queue_outbound(
            jid,
            ["Quote $500 for STM32"],
            kind="quote",
            recipient_phone=phone,
            status="DRAFT",
            dedupe_key=dedupe_key
        )

        assert oid1 == oid2, "Dedupe key must generate deterministic ID"
        all_drafts = repo.list_outbound(["DRAFT"])
        assert len(all_drafts) == 1, "Duplicate quote must be safely discarded"
    finally:
        repo.close()
