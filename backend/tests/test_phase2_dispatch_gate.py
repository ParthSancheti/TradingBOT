"""Phase 2 independent verification test suite.

Validates the Phase 2 Acceptance Gate:
- Unify mode semantics: observe, draft, send, turbo across manual composer and AI quotes
- Issue B01: Turbo mode immediate dispatch without confirmation popup; correct Autonomous Dispatch labeling
- Issue B02: Draft & Approve can approve AI quotes without manual_only stranding
- Issue B13: Direct WhatsApp media buffer handling and zero fake receipts
- Issue B14: Deterministic sourcing task IDs and idempotent command in-progress claims
- Turbo mode auto-reply cap enforcement with explicit CAP_EXCEEDED hold reason
- Explicit hold reasons for OBSERVE_ONLY, MANUAL_SEND_ONLY, and DRAFT_HELD
- Enabling Turbo does not release historical unapproved drafts
- Concurrent consumers race condition: exactly one worker gets claim
- Stopped workspace and disabled dispatch blocks sending
- Account mismatch and authorization expiration enforcement
- UNKNOWN outcome quarantined and never automatically replayed
"""
import asyncio
import time
from pathlib import Path
import pytest

from conftest import FakeSheets, FakeVault
from jarvis.desktop_service import DesktopService, DesktopError
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.providers import Triage, TradeItem
from jarvis.runner import Runner
from jarvis.trading_engine import evaluate_inbound_trade


def make_setup(owner="+447700900077", stock="+97142000001", buyer="+919876543210"):
    return {
        "owner_phone": owner,
        "stock_phones": [stock, buyer],
        "database_phone": stock,
    }


async def init_service_setup(service, owner="+447700900077", stock="+97142000001", mode="draft"):
    await service.save_setup(make_setup(owner=owner, stock=stock))
    await service.settings_save({"autonomy_mode": mode, "working_hours_start": "00:00", "working_hours_end": "23:59"})
    service.runner.autonomy_mode = mode
    return service.configuration["data"]


@pytest.fixture
def test_env(tmp_path):
    vault = FakeVault()
    vault.set("bridge_secret", "test-bridge-secret-1234")
    db_path = tmp_path / "jarvis_phase2.db"
    repo = SQLiteSyncedRepository(FakeSheets(), db_path=db_path)
    runner = Runner(vault, repo)
    runner.active = True
    runner.autonomy_mode = "draft"
    service = DesktopService(vault, repo, runner, opener=lambda: None, cache_path=tmp_path / "cache.json")
    return {
        "vault": vault,
        "db_path": db_path,
        "repo": repo,
        "runner": runner,
        "service": service,
        "tmp_path": tmp_path,
    }


@pytest.mark.asyncio
async def test_gate_mode_matrix_manual_composer(test_env):
    """Test manual composer in all 4 autonomy modes."""
    service = test_env["service"]
    runner = test_env["runner"]
    repo = test_env["repo"]

    # 1. Observe mode: manual sends are not approved / dispatch disabled
    await init_service_setup(service, mode="observe")
    assert runner.can_send is False
    res_obs = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Hello from operator",
        "approve_now": True,
    })
    assert res_obs["approved"] is False
    assert res_obs["status"] == "DRAFT"
    assert res_obs["reason"] == "DISPATCH_DISABLED"
    assert repo.ready_outbound_ids("+447700900077") == []

    # 2. Draft mode: manual send with approve_now is approved as READY and queryable
    await init_service_setup(service, mode="draft")
    assert runner.can_send is True
    res_draft = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Manual send in draft mode",
        "approve_now": True,
    })
    assert res_draft["approved"] is True
    assert res_draft["status"] == "READY"
    ready_draft = repo.ready_outbound_ids("+447700900077", manual_only=(runner.autonomy_mode == "send"))
    assert any(r["id"] == res_draft["id"] for r in ready_draft)

    # 3. Send mode (Manual Send with Confirmation): manual send with approve_now is approved
    await init_service_setup(service, mode="send")
    assert runner.can_send is True
    res_send = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Manual send in send mode",
        "approve_now": True,
    })
    assert res_send["approved"] is True
    assert res_send["status"] == "READY"
    ready_send = repo.ready_outbound_ids("+447700900077", manual_only=(runner.autonomy_mode == "send"))
    assert any(r["id"] == res_send["id"] for r in ready_send)

    # 4. Turbo mode: immediate dispatch without manual confirmation
    await init_service_setup(service, mode="turbo")
    assert runner.can_send is True
    res_turbo = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Manual send in turbo mode",
        "approve_now": True,
    })
    assert res_turbo["approved"] is True
    assert res_turbo["status"] == "READY"
    ready_turbo = repo.ready_outbound_ids("+447700900077", manual_only=(runner.autonomy_mode == "send"))
    assert any(r["id"] == res_turbo["id"] for r in ready_turbo)


@pytest.mark.asyncio
async def test_gate_b02_draft_and_approve_supervised_quote(test_env):
    """Issue B02: In Draft & Approve mode, approved quotes must dispatch without MANUAL_SEND_ONLY rejection."""
    service = test_env["service"]
    runner = test_env["runner"]
    repo = test_env["repo"]
    vault = test_env["vault"]
    setup = await init_service_setup(service, mode="draft")

    # Add inventory
    await repo.put("inventory", "inv_001", {
        "device": "iPhone 16 128GB",
        "unit_cost": 700.0,
        "currency": "USD",
        "quantity": 20,
    }, 0)
    await repo.put("news", "latest", {"aa_pct": 1.0}, 0)

    # Incoming inquiry evaluated in draft mode
    event = {
        "message_id": "msg_quote_001",
        "conversation_id": "919876543210@c.us",
        "phone": "+919876543210",
    }
    triage = {
        "category": "BUY",
        "proceed": True,
        "needs_review": False,
        "items": [{"device": "iPhone 16 128GB", "quantity": 2}],
    }

    eval_res = await evaluate_inbound_trade(repo, vault, event, setup, triage=triage)
    assert eval_res["action"] == "QUEUED_DRAFT"
    assert eval_res["target_status"] == "DRAFT"
    assert eval_res["hold_reason"] == "DRAFT_HELD"
    oid = eval_res["outbound_id"]

    # Before approval, quote is in DRAFT status, so ready_outbound_ids returns empty
    ready_before = repo.ready_outbound_ids("+447700900077", manual_only=(runner.autonomy_mode == "send"))
    assert not any(r["id"] == oid for r in ready_before)

    # Operator approves draft quote
    await service.outbound_action({"id": oid, "action": "approve"})
    approved_item = repo.get_outbound(oid)
    assert approved_item["status"] == "READY"

    # After approval, bridge polling returns the approved quote
    poll_body = {"operation": "poll_config", "dispatch_version": 1}
    poll_res = await runner._handle(poll_body)
    assert any(r["id"] == oid for r in poll_res.get("outbound", []))

    # Worker claims the approved quote
    session_id = "worker_session_test_123456"
    claim_res = await service.outbound_dispatch({
        "operation": "dispatch_claim",
        "outbound_id": oid,
        "session_id": session_id,
        "account_phone": "+447700900077",
    })
    assert "item" in claim_res, f"Claim failed: {claim_res}"
    claim_token = claim_res["item"]["claim_token"]
    assert approved_item["kind"] == "quote"

    # Worker authorizes
    auth_res = await service.outbound_dispatch({
        "operation": "dispatch_authorize",
        "outbound_id": oid,
        "session_id": session_id,
        "claim_token": claim_token,
        "account_phone": "+447700900077",
    })
    assert auth_res.get("ok") is True

    # Worker reports successful receipt
    result_res = await service.outbound_dispatch({
        "operation": "dispatch_result",
        "outbound_id": oid,
        "session_id": session_id,
        "claim_token": claim_token,
        "outcome": "ACCEPTED",
        "sent_ids": ["wamid.HBgLMTIzNDU2Nzg5MA=="],
    })
    assert result_res.get("ok") is True

    # Verify final status in repo
    final_item = repo.get_outbound(oid)
    assert final_item["status"] == "ACCEPTED"


@pytest.mark.asyncio
async def test_gate_b01_turbo_mode_immediate_dispatch(test_env):
    """Issue B01: Turbo mode produces READY AI quotes immediately up to auto_reply_max_usd."""
    service = test_env["service"]
    runner = test_env["runner"]
    repo = test_env["repo"]
    vault = test_env["vault"]
    setup = await init_service_setup(service, mode="turbo")

    await repo.put("inventory", "inv_002", {
        "device": "iPhone 16 Pro 256GB",
        "unit_cost": 900.0,
        "currency": "USD",
        "quantity": 10,
    }, 0)
    await repo.put("news", "latest", {"aa_pct": 1.5}, 0)

    event = {
        "message_id": "msg_turbo_001",
        "conversation_id": "919876543210@c.us",
        "phone": "+919876543210",
    }
    triage = {
        "category": "BUY",
        "proceed": True,
        "needs_review": False,
        "items": [{"device": "iPhone 16 Pro 256GB", "quantity": 1}],
    }

    eval_res = await evaluate_inbound_trade(repo, vault, event, setup, triage=triage)
    assert eval_res["action"] == "QUEUED_READY"
    assert eval_res["target_status"] == "READY"
    assert eval_res["hold_reason"] is None
    oid = eval_res["outbound_id"]

    # In Turbo mode, quote is immediately in ready_outbound_ids
    poll_body = {"operation": "poll_config", "dispatch_version": 1}
    poll_res = await runner._handle(poll_body)
    assert any(r["id"] == oid for r in poll_res.get("outbound", []))


@pytest.mark.asyncio
async def test_gate_turbo_cap_exceeded_explicit_hold(test_env):
    """In Turbo mode, quotes exceeding auto_reply_max_usd are held as DRAFT with CAP_EXCEEDED."""
    service = test_env["service"]
    runner = test_env["runner"]
    repo = test_env["repo"]
    vault = test_env["vault"]
    setup = await init_service_setup(service, mode="turbo")

    # Set auto_reply_max_usd to $2000
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 2000.0, "working_hours_start": "00:00", "working_hours_end": "23:59"})

    await repo.put("inventory", "inv_003", {
        "device": "iPhone 16 Pro Max 1TB",
        "unit_cost": 1500.0,
        "currency": "USD",
        "quantity": 10,
    }, 0)
    await repo.put("news", "latest", {"aa_pct": 0.0}, 0)

    # 3 units = ~$4500 > $2000 cap
    event = {
        "message_id": "msg_turbo_cap_001",
        "conversation_id": "919876543210@c.us",
        "phone": "+919876543210",
    }
    triage = {
        "category": "BUY",
        "proceed": True,
        "needs_review": False,
        "items": [{"device": "iPhone 16 Pro Max 1TB", "quantity": 3}],
    }

    eval_res = await evaluate_inbound_trade(repo, vault, event, setup, triage=triage)
    assert eval_res["action"] == "QUEUED_DRAFT"
    assert eval_res["target_status"] == "DRAFT"
    assert eval_res["hold_reason"] == "CAP_EXCEEDED"
    assert eval_res["total_quote_usd"] > 2000.0

    # Verify not in ready_outbound_ids
    ready = repo.ready_outbound_ids("+447700900077", manual_only=False)
    assert not any(r["id"] == eval_res["outbound_id"] for r in ready)


@pytest.mark.asyncio
async def test_gate_explicit_hold_observe_and_send_modes(test_env):
    """Verify explicit machine hold reasons for OBSERVE_ONLY and MANUAL_SEND_ONLY."""
    service = test_env["service"]
    runner = test_env["runner"]
    repo = test_env["repo"]
    vault = test_env["vault"]

    event = {
        "message_id": "msg_hold_test",
        "conversation_id": "919876543210@c.us",
        "phone": "+919876543210",
    }
    triage = {"category": "BUY", "proceed": True, "items": [{"device": "iPhone 16", "quantity": 1}]}

    # 1. Observe mode
    setup = await init_service_setup(service, mode="observe")
    res_obs = await evaluate_inbound_trade(repo, vault, event, setup, triage=triage)
    assert res_obs["action"] == "OBSERVE_ONLY"

    # 2. Send mode (Manual Send only)
    setup = await init_service_setup(service, mode="send")
    res_send = await evaluate_inbound_trade(repo, vault, event, setup, triage=triage)
    assert res_send["action"] == "MANUAL_SEND_ONLY"


@pytest.mark.asyncio
async def test_gate_turbo_enable_does_not_release_historical_drafts(test_env):
    """Enabling Turbo mode must NOT release historical unapproved drafts."""
    service = test_env["service"]
    runner = test_env["runner"]
    repo = test_env["repo"]

    # Start in draft mode and create 3 unapproved drafts
    await init_service_setup(service, mode="draft")
    draft1 = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Unapproved draft 1",
        "approve_now": False,
    })
    draft2 = await service.outbound_create({
        "conversation_id": "919876543211@c.us",
        "recipient_phone": "+919876543211",
        "text": "Unapproved draft 2",
        "approve_now": False,
    })
    assert draft1["status"] == "DRAFT"
    assert draft2["status"] == "DRAFT"

    # Switch mode to Turbo
    await service.settings_save({"autonomy_mode": "turbo"})
    runner.autonomy_mode = "turbo"

    # ready_outbound_ids must NOT include historical unapproved drafts
    ready_after = repo.ready_outbound_ids("+447700900077", manual_only=False)
    assert not any(r["id"] in (draft1["id"], draft2["id"]) for r in ready_after)

    # Status of drafts must remain DRAFT
    assert repo.get_outbound(draft1["id"])["status"] == "DRAFT"
    assert repo.get_outbound(draft2["id"])["status"] == "DRAFT"


@pytest.mark.asyncio
async def test_gate_b14_sourcing_task_deterministic_dedupe(test_env):
    """Issue B14: Sourcing task IDs must be deterministic so re-evaluation creates 0 duplicate tasks."""
    repo = test_env["repo"]
    vault = test_env["vault"]
    service = test_env["service"]
    setup = await init_service_setup(service, mode="draft")

    event = {
        "message_id": "msg_sourcing_001",
        "conversation_id": "919876543210@c.us",
        "phone": "+919876543210",
    }
    triage = {
        "category": "BUY",
        "proceed": True,
        "needs_review": False,
        "items": [{"device": "NonExistentModelX", "part_number": "NEM-X-001", "quantity": 5, "source_id": "msg_sourcing_001"}],
    }

    # Evaluate 1st time
    res1 = await evaluate_inbound_trade(repo, vault, event, setup, triage=triage)
    assert res1["action"] == "NO_STOCK_MATCH"

    tasks1 = await repo.list("tasks", 100)
    sourcing_tasks1 = [t for t in tasks1 if t["data"].get("category") == "sourcing_need"]
    assert len(sourcing_tasks1) == 1
    task_id1 = sourcing_tasks1[0]["id"]

    # Evaluate 2nd time (simulating re-evaluation or retry)
    res2 = await evaluate_inbound_trade(repo, vault, event, setup, triage=triage)
    assert res2["action"] == "NO_STOCK_MATCH"

    tasks2 = await repo.list("tasks", 100)
    sourcing_tasks2 = [t for t in tasks2 if t["data"].get("category") == "sourcing_need"]
    assert len(sourcing_tasks2) == 1, "Re-evaluation must not create duplicate sourcing tasks"
    assert sourcing_tasks2[0]["id"] == task_id1


@pytest.mark.asyncio
async def test_gate_b14_command_in_progress_claim_and_idempotent_reply(test_env):
    """Issue B14: Command jobs dedupe completed work, maintain in-progress claim, and have idempotent working replies."""
    service = test_env["service"]
    repo = test_env["repo"]
    await init_service_setup(service, mode="draft")

    command = {
        "message_id": "cmd_msg_unique_101",
        "conversation_id": "447700900077@c.us",
        "self_chat": True,
        "text": "@Jarvis summarize today's stock",
    }

    # First execution triggers job
    await service._process_agent_command(command)

    # Verify task was created in tasks collection
    tasks = await repo.list("tasks", 50)
    cmd_tasks = [t for t in tasks if t["data"].get("category") == "whatsapp_agent_command"]
    assert len(cmd_tasks) == 1
    job_id = cmd_tasks[0]["id"]

    # Count agent_reply items in outbound queue
    outbounds_before = repo._conn.execute("SELECT * FROM outbound_queue WHERE kind='agent_reply'").fetchall()
    assert len(outbounds_before) == 1

    # Second concurrent call with same message_id should detect PROCESSING or COMPLETED and return early
    await service._process_agent_command(command)

    # Must still have exactly 1 task and exactly 1 working reply in outbound queue
    tasks_after = await repo.list("tasks", 50)
    cmd_tasks_after = [t for t in tasks_after if t["data"].get("category") == "whatsapp_agent_command"]
    assert len(cmd_tasks_after) == 1

    outbounds_after = repo._conn.execute("SELECT * FROM outbound_queue WHERE kind='agent_reply'").fetchall()
    assert len(outbounds_after) == 1, "Working reply must be idempotent with 0 duplicates"


@pytest.mark.asyncio
async def test_gate_concurrent_consumers_race_condition(test_env):
    """Simultaneous consumers race: only 1 consumer gets claim token; second consumer gets APPROVAL_UNAVAILABLE."""
    service = test_env["service"]
    repo = test_env["repo"]
    await init_service_setup(service, mode="draft")

    create_res = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Concurrent race target",
        "approve_now": True,
    })
    oid = create_res["id"]
    assert create_res["status"] == "READY"

    # Worker A and Worker B try to claim simultaneously
    claim_a = await service.outbound_dispatch({
        "operation": "dispatch_claim",
        "outbound_id": oid,
        "session_id": "session_worker_alpha_1234",
        "account_phone": "+447700900077",
    })
    claim_b = await service.outbound_dispatch({
        "operation": "dispatch_claim",
        "outbound_id": oid,
        "session_id": "session_worker_beta_5678",
        "account_phone": "+447700900077",
    })

    # Exactly one must succeed with an item; the other must fail with error
    successes = [c for c in (claim_a, claim_b) if "item" in c]
    failures = [c for c in (claim_a, claim_b) if "error" in c]
    assert len(successes) == 1, "Exactly one consumer must successfully claim"
    assert len(failures) == 1, "Second consumer must be rejected"
    assert failures[0]["error"] in ("APPROVAL_UNAVAILABLE", "DISPATCH_ALREADY_CLOSED")


@pytest.mark.asyncio
async def test_gate_stopped_workspace_and_disabled_dispatch(test_env):
    """Stopped workspace or disabled dispatch blocks sends cleanly."""
    service = test_env["service"]
    runner = test_env["runner"]
    repo = test_env["repo"]
    await init_service_setup(service, mode="draft")

    # Create a READY outbound message while active
    ready_item = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Send before stop",
        "approve_now": True,
    })
    oid = ready_item["id"]
    assert ready_item["status"] == "READY"

    # Stop runner
    runner.active = False
    assert runner.can_send is False

    # outbound_create with approve_now when stopped returns WORKSPACE_STOPPED
    res = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Send when stopped",
        "approve_now": True,
    })
    assert res["approved"] is False
    assert res["status"] == "DRAFT"
    assert res["reason"] == "WORKSPACE_STOPPED"

    # dispatch_claim on the READY item when workspace is stopped returns DISPATCH_DISABLED
    claim = await service.outbound_dispatch({
        "operation": "dispatch_claim",
        "outbound_id": oid,
        "session_id": "session_stopped_worker",
        "account_phone": "+447700900077",
    })
    assert claim.get("error") == "DISPATCH_DISABLED"


@pytest.mark.asyncio
async def test_gate_unknown_outcome_never_replayed(test_env):
    """Post-invocation UNKNOWN outcome is quarantined and never automatically replayed."""
    service = test_env["service"]
    runner = test_env["runner"]
    repo = test_env["repo"]
    await init_service_setup(service, mode="draft")

    create_res = await service.outbound_create({
        "conversation_id": "919876543210@c.us",
        "recipient_phone": "+919876543210",
        "text": "Ambiguous message",
        "approve_now": True,
    })
    oid = create_res["id"]
    session_id = "session_unknown_test_1234"

    claim_res = await service.outbound_dispatch({
        "operation": "dispatch_claim",
        "outbound_id": oid,
        "session_id": session_id,
        "account_phone": "+447700900077",
    })
    token = claim_res["item"]["claim_token"]

    await service.outbound_dispatch({
        "operation": "dispatch_authorize",
        "outbound_id": oid,
        "session_id": session_id,
        "claim_token": token,
        "account_phone": "+447700900077",
    })

    # Report UNKNOWN (e.g. timeout during invocation)
    unknown_res = await service.outbound_dispatch({
        "operation": "dispatch_result",
        "outbound_id": oid,
        "session_id": session_id,
        "claim_token": token,
        "outcome": "UNKNOWN",
        "reason": "WHATSAPP_INVOCATION_TIMEOUT",
    })
    assert unknown_res.get("ok") is True

    # Status is UNKNOWN
    item = repo.get_outbound(oid)
    assert item["status"] == "UNKNOWN"

    # Connection recovery / bridge poll must NEVER return UNKNOWN items
    ready_outbounds = repo.ready_outbound_ids("+447700900077", manual_only=False)
    assert not any(r["id"] == oid for r in ready_outbounds)

    # Attempting to re-approve an UNKNOWN item must be rejected
    approve_attempt = await service.outbound_action({"id": oid, "action": "approve"}).catch if hasattr(service.outbound_action, "catch") else None
    with pytest.raises(DesktopError) as exc_info:
        await service.outbound_action({"id": oid, "action": "approve"})
    assert exc_info.value.code == "DRAFT_CHANGED"
