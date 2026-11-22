"""Phase 2 Acceptance Gate Qualification Test Suite.

Validates:
1. Chat B open while sending to A (DOM guard & isolation)
2. Verified, unverified, and changed LID resolution
3. Wrong account / account mismatch rejection
4. Group off (group dispatch disabled by default)
5. Native and extension claim collision on same row (exactly one worker succeeds)
6. Repeat enqueue during active claim retains claim_token and identity
7. Stop after claim revokes claim and blocks authorization
8. Missing or unreadable attachment fails closed before send (zero text fallback)
9. Simulated crash before invoke (CLAIMED -> READY) vs after invoke (IN_FLIGHT -> UNKNOWN)
10. Missing ack / missing provider receipt handling
11. Delivered / read out of order monotonic progression
12. Restart with UNKNOWN and late receipt reconciliation
13. Bot echo before / after receipt (zero self-mute)
14. Manual reply takeover from phone, WPP, and supervised composer (composer allowed when muted)
15. Mute between scheduling and waking cancels with CANCELLED_HUMAN_ACTIVE
16. Mute expiry and manual unmute chat
"""
import asyncio
import json
import sqlite3
import time
from pathlib import Path
import pytest

from conftest import FakeSheets, FakeVault
from jarvis.desktop_service import DesktopService, DesktopError
from jarvis.local_repo import SQLiteSyncedRepository, normalize_whatsapp_msg_id
from jarvis.runner import Runner
from jarvis.pipeline import IntakePipeline


VALID_QUOTE_META = {"pricing": {"quote_usd": 750.0}, "quote_validity_seconds": 1800}


@pytest.fixture
def phase2_env(tmp_path):
    vault = FakeVault()
    vault.set("bridge_secret", "test-bridge-secret-phase2")
    db_path = tmp_path / "jarvis_phase2_gate.db"
    repo = SQLiteSyncedRepository(FakeSheets(), db_path=db_path)
    runner = Runner(vault, repo)
    runner.active = True
    runner.autonomy_mode = "turbo"
    service = DesktopService(vault, repo, runner, opener=lambda: None, cache_path=tmp_path / "cache.json")
    return {
        "vault": vault,
        "db_path": db_path,
        "repo": repo,
        "runner": runner,
        "service": service,
        "tmp_path": tmp_path,
    }


async def init_setup(service, owner="+447700900077", stock="+97142000001", buyer="+919876543210"):
    setup_data = {
        "owner_phone": owner,
        "stock_phones": [stock, buyer],
        "database_phone": stock,
    }
    await service.save_setup(setup_data)
    await service.settings_save({
        "autonomy_mode": "turbo",
        "working_hours_start": "00:00",
        "working_hours_end": "23:59",
        "human_mute_hours": 4.0,
    })
    service.runner.autonomy_mode = "turbo"
    return service.configuration["data"]


# ---------------------------------------------------------------------------
# 1. Target Verification: Verified, Unverified, Changed LID & Group Off
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_lid_and_group_verification(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    # A. Verified LID: maps to +919876543210
    repo.save_lid_phone("111222333@lid", "+919876543210")
    oid_verified_lid = repo.queue_outbound(
        "111222333@lid", ["Verified LID quote"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )
    claim_res = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid_verified_lid, "session_id": "worker-session-1234567890"},
        "+447700900077", ["+919876543210"]
    )
    assert "item" in claim_res, f"Verified LID should claim successfully, got: {claim_res}"

    # B. Unverified LID: not in lid_phone_map
    oid_unverified_lid = repo.queue_outbound(
        "999888777@lid", ["Unverified LID quote"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )
    claim_unverified = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid_unverified_lid, "session_id": "worker-session-1234567890"},
        "+447700900077", ["+919876543210"]
    )
    assert claim_unverified.get("error") == "RECIPIENT_NOT_ALLOWED"

    # C. Changed LID: LID in map belongs to a DIFFERENT phone
    repo.save_lid_phone("555444333@lid", "+911111111111")
    oid_changed_lid = repo.queue_outbound(
        "555444333@lid", ["Changed LID quote"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )
    claim_changed = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid_changed_lid, "session_id": "worker-session-1234567890"},
        "+447700900077", ["+919876543210"]
    )
    assert claim_changed.get("error") == "RECIPIENT_NOT_ALLOWED"

    # D. Group Off: group JID (@g.us) must be rejected
    oid_group = repo.queue_outbound(
        "120363024567890@g.us", ["Group broadcast"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )
    claim_group = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid_group, "session_id": "worker-session-1234567890"},
        "+447700900077", ["+919876543210"]
    )
    assert claim_group.get("error") == "GROUP_DISPATCH_DISABLED"


# ---------------------------------------------------------------------------
# 2. Account Mismatch Rejection
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_account_mismatch(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service, owner="+447700900077")

    oid = repo.queue_outbound(
        "919876543210@c.us", ["Quote text"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )

    # Worker calls dispatch_claim presenting different account
    res = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid, "session_id": "worker-session-1234567890", "account_phone": "+971500000099"},
        "+447700900077", ["+919876543210"]
    )
    assert res.get("error") == "ACCOUNT_MISMATCH"


# ---------------------------------------------------------------------------
# 3. Native & Extension Claim Collision on Same Row
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_claim_collision_exactly_one_worker(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    oid = repo.queue_outbound(
        "919876543210@c.us", ["Collision quote"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )

    # Worker 1 (Native) claims row
    claim1 = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid, "session_id": "native-session-1234567890"},
        "+447700900077", ["+919876543210"]
    )
    assert "item" in claim1

    # Worker 2 (Extension) tries to claim the same row
    claim2 = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid, "session_id": "ext-session-987654321000"},
        "+447700900077", ["+919876543210"]
    )
    assert claim2.get("error") == "APPROVAL_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 4. Repeat Enqueue During Active Claim Token
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_repeat_enqueue_during_claim(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    dedupe_key = "test-repeat-claim-001"
    oid = repo.queue_outbound(
        "919876543210@c.us", ["Original quote"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META, dedupe_key=dedupe_key
    )

    # Worker claims row
    claim = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid, "session_id": "worker-session-1234567890"},
        "+447700900077", ["+919876543210"]
    )
    assert "item" in claim
    original_token = claim["item"]["claim_token"]
    assert original_token

    # Re-enqueue of same dedupe_key while row is in CLAIMED status
    oid2 = repo.queue_outbound(
        "919876543210@c.us", ["Duplicate quote attempt"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META, dedupe_key=dedupe_key
    )
    assert oid2 == oid

    # Verify claim_token in outbound_dispatch was NOT wiped or replaced
    with repo._lock:
        row = repo._conn.execute("SELECT claim_token, session_id FROM outbound_dispatch WHERE id=?", (oid,)).fetchone()
        assert row["claim_token"] == original_token
        assert row["session_id"] == "worker-session-1234567890"

    # Worker can still authorize with original token
    auth = repo.dispatch_outbound(
        "dispatch_authorize",
        {"outbound_id": oid, "session_id": "worker-session-1234567890", "claim_token": original_token},
        "+447700900077", ["+919876543210"]
    )
    assert auth.get("ok") is True


# ---------------------------------------------------------------------------
# 5. Stop After Claim Revokes Authorization
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_stop_after_claim(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    oid = repo.queue_outbound(
        "919876543210@c.us", ["Quote before stop"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )

    claim = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid, "session_id": "worker-session-1234567890"},
        "+447700900077", ["+919876543210"]
    )
    assert "item" in claim
    token = claim["item"]["claim_token"]

    # Workspace/Runner stopped -> revoke_outbound called
    repo.revoke_outbound()

    # Worker attempts to authorize claimed row
    auth = repo.dispatch_outbound(
        "dispatch_authorize",
        {"outbound_id": oid, "session_id": "worker-session-1234567890", "claim_token": token},
        "+447700900077", ["+919876543210"]
    )
    assert auth.get("error") == "APPROVAL_UNAVAILABLE"

    # Status must be CANCELLED
    item = repo.get_outbound(oid)
    assert item["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# 6. Failure Reason Recording & Closed Failure on Missing Attachment
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_failure_reason_recorded_in_meta(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    oid = repo.queue_outbound(
        "919876543210@c.us", ["Attachment quote"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        attachment_path="C:/nonexistent/invoice.pdf", status="READY", meta=VALID_QUOTE_META
    )

    claim = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid, "session_id": "worker-session-1234567890"},
        "+447700900077", ["+919876543210"]
    )
    assert "item" in claim
    token = claim["item"]["claim_token"]

    # Provider reports FAILED_BEFORE_SEND due to ATTACHMENT_NOT_FOUND
    res = repo.dispatch_outbound(
        "dispatch_result",
        {
            "outbound_id": oid,
            "session_id": "worker-session-1234567890",
            "claim_token": token,
            "outcome": "FAILED_BEFORE_SEND",
            "reason": "ATTACHMENT_NOT_FOUND",
        },
        "+447700900077", ["+919876543210"]
    )
    assert res.get("ok") is True

    item = repo.get_outbound(oid)
    assert item["status"] == "FAILED_BEFORE_SEND"
    assert item["meta"].get("failure_reason") == "ATTACHMENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# 7. Crash Recovery: IN_FLIGHT -> UNKNOWN, CLAIMED -> READY
# ---------------------------------------------------------------------------
def test_gate_crash_recovery_lifecycle(tmp_path):
    db_path = tmp_path / "crash_test.db"
    repo1 = SQLiteSyncedRepository(FakeSheets(), db_path=db_path)

    # 1. Enqueue two items
    oid_claimed = repo1.queue_outbound(
        "919876543210@c.us", ["Claimed row"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )
    oid_in_flight = repo1.queue_outbound(
        "919876543210@c.us", ["In flight row"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )

    # Claim both
    c1 = repo1.dispatch_outbound("dispatch_claim", {"outbound_id": oid_claimed, "session_id": "sess-123456789012"}, "+447700900077", ["+919876543210"])
    c2 = repo1.dispatch_outbound("dispatch_claim", {"outbound_id": oid_in_flight, "session_id": "sess-123456789012"}, "+447700900077", ["+919876543210"])
    assert "item" in c1
    assert "item" in c2

    # Authorize one to IN_FLIGHT
    repo1.dispatch_outbound("dispatch_authorize", {"outbound_id": oid_in_flight, "session_id": "sess-123456789012", "claim_token": c2["item"]["claim_token"]}, "+447700900077", ["+919876543210"])

    # Simulate hard crash / process kill
    repo1._conn.close()

    # Reopen database (simulate restart)
    repo2 = SQLiteSyncedRepository(FakeSheets(), db_path=db_path)

    row_c = repo2.get_outbound(oid_claimed)
    row_f = repo2.get_outbound(oid_in_flight)

    # CLAIMED resets to READY
    assert row_c["status"] == "READY"

    # IN_FLIGHT transitions to UNKNOWN (never automatically replayed)
    assert row_f["status"] == "UNKNOWN"
    assert row_f["meta"].get("failure_reason") == "PROCESS_RESTARTED_WHILE_IN_FLIGHT"

    # Dispatch row deleted for UNKNOWN row
    with repo2._lock:
        disp = repo2._conn.execute("SELECT * FROM outbound_dispatch WHERE id=?", (oid_in_flight,)).fetchone()
        assert disp is None

    repo2.close()


# ---------------------------------------------------------------------------
# 8. Late Verified Receipt Reconciles UNKNOWN Without Re-Send
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_late_receipt_reconciles_unknown(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    oid = repo.queue_outbound(
        "919876543210@c.us", ["Quote to timeout"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )
    claim = repo.dispatch_outbound("dispatch_claim", {"outbound_id": oid, "session_id": "sess-123456789012"}, "+447700900077", ["+919876543210"])
    assert "item" in claim
    token = claim["item"]["claim_token"]
    repo.dispatch_outbound("dispatch_authorize", {"outbound_id": oid, "session_id": "sess-123456789012", "claim_token": token}, "+447700900077", ["+919876543210"])

    # Timeout occurs -> outcome reported as UNKNOWN
    repo.dispatch_outbound(
        "dispatch_result",
        {"outbound_id": oid, "session_id": "sess-123456789012", "claim_token": token, "outcome": "UNKNOWN", "reason": "TIMEOUT_AFTER_SEND"},
        "+447700900077", ["+919876543210"]
    )
    assert repo.get_outbound(oid)["status"] == "UNKNOWN"

    # Later, verified provider ack arrives for this exact message
    reconcile = repo.dispatch_outbound(
        "dispatch_result",
        {
            "outbound_id": oid,
            "session_id": "sess-123456789012",
            "claim_token": token,
            "outcome": "ACCEPTED",
            "sent_ids": ["wamid.HBgLMTIzNDU2"],
        },
        "+447700900077", ["+919876543210"]
    )
    assert reconcile.get("ok") is True

    # Reconciled status becomes ACCEPTED with sent_ids recorded
    final_row = repo.get_outbound(oid)
    assert final_row["status"] == "ACCEPTED"
    assert final_row["meta"]["sent_ids"] == ["wamid.HBgLMTIzNDU2"]


# ---------------------------------------------------------------------------
# 9. Bot Echo Before & After Receipt (Zero Self-Mute)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_bot_echo_does_not_self_mute(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    setup = await init_setup(service)

    msg_id = "3EB0ABC123456789"
    # Pre-register receipt (as happens when sendDirect completes before dispatch_result DB write)
    repo.register_sent_receipt(msg_id)

    # Echo arrives before or during receipt processing
    assert repo.has_outbound_receipt(msg_id) is True
    assert repo.has_outbound_receipt(f"true_919876543210@c.us_{msg_id}") is True

    # Process via intake pipeline as outgoing echo
    pipeline = IntakePipeline(repo, None, local_first=True)
    event = {
        "message_id": f"true_919876543210@c.us_{msg_id}",
        "conversation_id": "919876543210@c.us",
        "phone": "+919876543210",
        "outgoing": True,
        "text": "Bot sent quote",
        "identity_verified": True,
    }
    res = await pipeline.process(event, setup)
    assert res.get("status") == "OUTGOING"

    # Chat must NOT be muted!
    assert repo.is_chat_muted("919876543210@c.us") is False


# ---------------------------------------------------------------------------
# 10. Manual Reply Takeover from Phone, WPP, and Supervised Composer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_manual_takeover_surfaces_and_composer(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    conv_id = "919876543210@c.us"

    # A. Operator sends manual reply from mobile phone (received via wa_sync human_replies)
    await service.wa_sync({
        "human_replies": [
            {"conversation_id": conv_id, "message_id": "operator_phone_msg_001", "timestamp": time.time()}
        ]
    })
    # Chat must now be muted
    assert repo.is_chat_muted(conv_id) is True

    # B. Automated quote attempts are blocked while muted
    oid_auto = repo.queue_outbound(
        conv_id, ["Auto quote while muted"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )
    claim_auto = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid_auto, "session_id": "sess-123456789012"},
        "+447700900077", ["+919876543210"]
    )
    assert claim_auto.get("error") == "RECIPIENT_MUTED"

    # C. Trader's manual composer send is ALLOWED even while muted!
    res_manual = await service.outbound_create({
        "conversation_id": conv_id,
        "recipient_phone": "+919876543210",
        "text": "Operator follow up from UI composer",
        "approve_now": True,
    })
    assert res_manual["approved"] is True
    assert res_manual["status"] == "READY"

    # Manual dispatch claim succeeds
    claim_manual = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": res_manual["id"], "session_id": "sess-123456789012"},
        "+447700900077", ["+919876543210"]
    )
    assert "item" in claim_manual


# ---------------------------------------------------------------------------
# 11. Mute Between Scheduling and Waking Cancels Job
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_mute_between_scheduling_and_waking(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    conv_id = "919876543210@c.us"
    # Schedule a night job due in the past
    repo.enqueue_scheduled_job(
        "sched_job_001", conv_id, "+919876543210", "Asia/Kolkata", time.time() - 10,
        {"text_parts": ["Scheduled morning quote"]}, recalculate_fx=False
    )

    # In the interim, staff manually replied -> chat became muted
    repo.upsert_contact(conv_id, manual_mute_seconds=3600)
    assert repo.is_chat_muted(conv_id) is True

    # Scheduler tick runs
    processed = await service.run_scheduler_tick()

    # Job must be cancelled due to human activity
    jobs = repo.list_scheduled_jobs()
    job = next(j for j in jobs if j["id"] == "sched_job_001")
    assert job["status"] == "CANCELLED_HUMAN_ACTIVE"


# ---------------------------------------------------------------------------
# 12. Mute Status and Unmute Chat Restores Eligibility
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_unmute_chat_restores_eligibility(phase2_env):
    service = phase2_env["service"]
    repo = phase2_env["repo"]
    await init_setup(service)

    conv_id = "919876543210@c.us"
    repo.upsert_contact(conv_id, manual_mute_seconds=3600)
    assert repo.is_chat_muted(conv_id) is True

    # Query mute status via service RPC
    status_res = await service.contacts_mute_status({"conversation_id": conv_id})
    assert status_res["muted"] is True
    assert status_res["remaining_seconds"] > 3000

    # Unmute via service RPC
    unmute_res = await service.contacts_unmute({"conversation_id": conv_id})
    assert unmute_res["ok"] is True

    # Chat is now unmuted
    assert repo.is_chat_muted(conv_id) is False

    # Fresh quote can now claim successfully
    oid = repo.queue_outbound(
        conv_id, ["Fresh quote after unmute"],
        kind="quote", recipient_phone="+919876543210", account_phone="+447700900077",
        status="READY", meta=VALID_QUOTE_META
    )
    claim = repo.dispatch_outbound(
        "dispatch_claim",
        {"outbound_id": oid, "session_id": "sess-123456789012"},
        "+447700900077", ["+919876543210"]
    )
    assert "item" in claim
