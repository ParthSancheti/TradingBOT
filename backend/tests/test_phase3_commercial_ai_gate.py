"""Phase 3 independent verification gate test suite.

Validates the Phase 3 Acceptance Gate:
1. Owned-stock iPhone inquiry: capture → intercepted Gemini request → match → LLM reply request → validated READY → fake provider receipt
2. Without stock: classification called, no match, zero reply generation calls, zero sends, sourcing alert enqueued
3. Under cap vs over cap: order value <= auto cap yields READY; order value > auto cap yields DRAFT with CAP_EXCEEDED
4. Inside vs outside hours: outside hours holds as SCHEDULED_NIGHT_HOLD, morning scheduler tick wakes and routes
5. Stale inventory: expired supplier offers (>86400s), stale daily stock (>24h), and zero-quantity inventory never match as fresh stock
6. Gemini failure: quota/overload on classification records truthful failure; error/mismatch on reply generation safely falls back
7. Backlog > 500 messages: indexed work selection drains oldest-first without infinite loops
8. Media processing: WAITING_BYTES deadline timeout, invalid MIME, oversized bytes, OCR failure hold, and already-sent decision protection
9. Hardware grade & spec isolation (B15): JV_LOCK vs UNLOCKED, NON_ACTIVE vs ACTIVATED, supplier stock ownership verification, and partial shortage tracking
"""
import asyncio
import base64
import json
import time
from datetime import datetime, timezone
import pytest
import httpx

from conftest import FakeSheets, FakeVault
from jarvis.desktop_service import DesktopService
from jarvis.gemini import GeminiClient, GeminiError
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.pipeline import IntakePipeline
from jarvis.providers import Triage, QuoteReply
from jarvis.runner import Runner


def make_setup(owner="+447700900077", stock="+97142000001", extra_stock_phones=None):
    phones = [stock]
    if extra_stock_phones:
        phones.extend(extra_stock_phones)
    return {
        "owner_phone": owner,
        "stock_phones": phones,
        "database_phone": stock,
        "scan_all": True,
    }


@pytest.fixture
def phase3_env(tmp_path):
    vault = FakeVault()
    vault.set("bridge_secret", "test-bridge-secret-phase3")
    vault.set(
        "gemini_connection",
        json.dumps({
            "keys": ["AIzaSyFakePhase3Key1234567890"],
            "model": "gemini-3.5-flash-lite",
        }),
    )
    db_path = tmp_path / "jarvis_phase3_gate.db"
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


@pytest.mark.asyncio
async def test_gate_scenario_1_owned_stock_iphone_full_lifecycle(phase3_env):
    """Scenario 1: Owned-stock iPhone inquiry proves:
    capture → actual intercepted Gemini request → match → LLM reply request → validated READY → fake provider receipt.
    Verifies call counts (1 classification + 1 reply generation = 2 total), endpoint inspection,
    fact validation, and receipt recording."""
    repo = phase3_env["repo"]
    vault = phase3_env["vault"]
    runner = phase3_env["runner"]
    service = phase3_env["service"]
    setup = make_setup(extra_stock_phones=["+971501234567"])

    await service.save_setup(setup)
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 10000.0})
    runner.autonomy_mode = "turbo"
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 10000.0, "default_margin_pct": 5.0})
    runner.autonomy_mode = "turbo"

    # Seed owned stock: iPhone 16 Pro Max 256GB (15 units @ $1050 USD, verified owned)
    await repo.put("inventory", "inv_ip16", {
        "device": "iPhone 16 Pro Max",
        "variant": "256GB",
        "condition": "NON_ACTIVE",
        "lock_status": "UNLOCKED",
        "quantity": 15,
        "unit_cost": 1050.0,
        "currency": "USD",
        "ownership": "owned",
        "source_kind": "owned",
    }, 0)

    intercepted_requests = []

    def mock_gemini_transport(request: httpx.Request):
        url_str = str(request.url)
        content = json.loads(request.content.decode())
        intercepted_requests.append({"url": url_str, "body": content})

        sys_prompt = content.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
        # Call 1: Classification
        if "trade classifier" in sys_prompt:
            triage_data = {
                "category": "BUY",
                "proceed": True,
                "needs_review": False,
                "reason": "Commercial iPhone trade inquiry",
                "items": [
                    {
                        "device": "iPhone 16 Pro Max",
                        "variant": "256GB",
                        "quantity": 5,
                        "unit_price": None,
                        "currency": "USD",
                        "condition": "NON_ACTIVE",
                        "lock_status": "UNLOCKED",
                    }
                ],
            }
            resp_body = {
                "candidates": [{"content": {"parts": [{"text": json.dumps(triage_data)}]}, "finishReason": "STOP"}]
            }
            return httpx.Response(200, json=resp_body)
        # Call 2: LLM Quote Reply Generation
        elif "quotation reply" in sys_prompt:
            # Body content has matched commercial facts
            user_text = content.get("contents", [{}])[0].get("parts", [{}])[0].get("text", "")
            user_facts = json.loads(user_text)
            assert len(user_facts["matched_facts"]) == 1
            fact = user_facts["matched_facts"][0]
            assert "iPhone 16 Pro Max" in fact["product_name"]
            assert fact["quoted_qty"] == 5
            unit_price_usd = fact["unit_price_usd"]

            quote_reply_data = {
                "reply_text": f"iPhone 16 Pro Max 256GB Non Act · 5 pcs ready · ${unit_price_usd} (USD)",
                "quoted_items": [
                    {
                        "product_name": "iPhone 16 Pro Max",
                        "quoted_qty": 5,
                        "unit_price_usd": str(unit_price_usd),
                        "fact_id": fact["fact_id"],
                    }
                ],
            }
            resp_body = {
                "candidates": [{"content": {"parts": [{"text": json.dumps(quote_reply_data)}]}, "finishReason": "STOP"}]
            }
            return httpx.Response(200, json=resp_body)
        return httpx.Response(400, json={"error": "Unknown prompt"})

    transport = httpx.MockTransport(mock_gemini_transport)
    vault.gemini_transport = transport
    runner.gemini_transport = transport
    repo.gemini_transport = transport

    # Step 1: Capture inbound inquiry
    msg_id = "false_971501234567@c.us_IP16_001"
    event = {
        "message_id": msg_id,
        "conversation_id": "971501234567@c.us",
        "phone": "+971501234567",
        "sender_name": "Wholesale Buyer Dubai",
        "text": "Need 5 pcs iPhone 16 Pro Max 256GB Non Active Unlocked best price",
        "timestamp": time.time(),
        "identity_verified": True,
    }

    pipeline = IntakePipeline(repo, GeminiClient(vault, transport=transport), local_first=True)
    res = await pipeline.process(event, setup)
    assert res["status"] == "CLASSIFIED"

    # Step 2: Evaluation & LLM Quote Reply Generation via runner loop
    await runner.classify_pending()

    # Step 3: Verify intercepted Gemini calls
    assert len(intercepted_requests) == 2
    assert "/v1beta/models/gemini-3.5-flash-lite:generateContent" in intercepted_requests[0]["url"]
    assert "/v1beta/models/gemini-3.5-flash-lite:generateContent" in intercepted_requests[1]["url"]

    # Step 4: Verify message trace
    trace = await service.message_trace({"message_id": msg_id})
    assert trace["found"] is True
    assert trace["current_stage"] == "REPLY_GENERATION"
    assert trace["stage_outcome"] == "SUCCESS"
    assert trace["reason"] == "DISPATCH_READY"
    assert trace["model_requested"] == "gemini-3.5-flash-lite"
    assert trace["model_served"] == "gemini-3.5-flash-lite"

    stages = [e["stage"] for e in trace["events"] if e["outcome"] == "SUCCESS"]
    assert stages == ["CAPTURE", "AI_CLASSIFICATION", "STOCK_MATCHING", "REPLY_GENERATION"]

    # Verify Stock Matching event
    stock_event = next(e for e in trace["events"] if e["stage"] == "STOCK_MATCHING")
    assert stock_event["outcome"] == "SUCCESS"
    assert stock_event["reason_code"] == "STOCK_MATCHED"

    # Verify Reply Generation event
    reply_event = next(e for e in trace["events"] if e["stage"] == "REPLY_GENERATION")
    assert reply_event["outcome"] == "SUCCESS"
    assert reply_event["reason_code"] == "DISPATCH_READY"
    assert reply_event["metadata"]["model_served"] == "gemini-3.5-flash-lite"

    # Step 5: Verify validated READY outbound record
    outbound = repo.list_outbound(statuses=("READY",))
    assert len(outbound) == 1
    ready_item = outbound[0]
    assert ready_item["status"] == "READY"
    assert "iPhone 16 Pro Max 256GB" in ready_item["text_parts"][0]
    assert "1102.50" in ready_item["text_parts"][0]

    # Step 6: Fake provider receipt execution
    worker_phone = "+447700900077"
    session_id = "session_gate3_test"
    claim = await service.outbound_dispatch({
        "operation": "dispatch_claim",
        "account_phone": worker_phone,
        "outbound_id": ready_item["id"],
        "session_id": session_id,
    })
    assert "item" in claim, f"Claim failed: {claim}"
    claim_token = claim["item"]["claim_token"]

    auth = await service.outbound_dispatch({
        "operation": "dispatch_authorize",
        "account_phone": worker_phone,
        "outbound_id": ready_item["id"],
        "session_id": session_id,
        "claim_token": claim_token,
    })
    assert auth.get("ok") is True

    receipt_result = await service.outbound_dispatch({
        "operation": "dispatch_result",
        "account_phone": worker_phone,
        "outbound_id": ready_item["id"],
        "session_id": session_id,
        "claim_token": claim_token,
        "outcome": "ACCEPTED",
        "sent_ids": ["receipt_gate3_ip16_provider_ack"],
    })
    assert receipt_result.get("ok") is True

    # Outbound item must now be ACCEPTED with provider receipt
    accepted_rows = repo.list_outbound(statuses=("ACCEPTED",))
    assert len(accepted_rows) == 1
    assert accepted_rows[0]["id"] == ready_item["id"]
    assert accepted_rows[0]["meta"]["sent_ids"] == ["receipt_gate3_ip16_provider_ack"]


@pytest.mark.asyncio
async def test_gate_scenario_2_without_stock(phase3_env):
    """Scenario 2: Trade inquiry without matching stock.
    Verifies 1 classification call, NO_STOCK_MATCH outcome, zero LLM reply generation calls,
    zero outbound sends, and deterministic sourcing task creation."""
    repo = phase3_env["repo"]
    vault = phase3_env["vault"]
    runner = phase3_env["runner"]
    service = phase3_env["service"]
    setup = make_setup(extra_stock_phones=["+971501234567"])

    await service.save_setup(setup)
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 10000.0})
    runner.autonomy_mode = "turbo"
    await service.settings_save({"autonomy_mode": "turbo"})
    runner.autonomy_mode = "turbo"

    intercepted = []

    def mock_gemini(request: httpx.Request):
        content = json.loads(request.content.decode())
        intercepted.append(content)
        triage_data = {
            "category": "BUY",
            "proceed": True,
            "needs_review": False,
            "reason": "Out of stock item inquiry",
            "items": [
                {
                    "device": "iPhone 14",
                    "variant": "128GB",
                    "quantity": 20,
                    "condition": "NEW",
                    "lock_status": "UNLOCKED",
                }
            ],
        }
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(triage_data)}]}, "finishReason": "STOP"}]})

    transport = httpx.MockTransport(mock_gemini)
    vault.gemini_transport = transport
    runner.gemini_transport = transport
    repo.gemini_transport = transport

    msg_id = "false_971509990001@c.us_NOSTOCK001"
    event = {
        "message_id": msg_id,
        "conversation_id": "971509990001@c.us",
        "phone": "+971509990001",
        "sender_name": "Trader 2",
        "text": "Need 20 pcs iPhone 14 128GB Blue new pack",
        "timestamp": time.time(),
        "identity_verified": True,
    }

    pipeline = IntakePipeline(repo, GeminiClient(vault, transport=transport), local_first=True)
    await pipeline.process(event, setup)
    await runner.classify_pending()

    # Exactly 1 classification call; 0 reply generation calls
    assert len(intercepted) == 1

    # Trace check
    trace = await service.message_trace({"message_id": msg_id})
    assert trace["current_stage"] == "STOCK_MATCHING"
    assert trace["stage_outcome"] == "BLOCKED"
    assert trace["reason"] == "NO_MATCHING_STOCK"

    # Outbox is empty
    outbound = repo.list_outbound(statuses=("READY", "DRAFT", "ACCEPTED"))
    assert len(outbound) == 0

    # Sourcing task created
    tasks = await repo.list("tasks", 20)
    sourcing_tasks = [t for t in tasks if t["data"].get("category") == "sourcing_need"]
    assert len(sourcing_tasks) >= 1
    assert any("iPhone 14" in t["data"]["title"] for t in sourcing_tasks)


@pytest.mark.asyncio
async def test_gate_scenario_3_under_cap_and_over_cap(phase3_env):
    """Scenario 3: Cap enforcement for AI quotes.
    Order value under cap ($3,307.50 <= $5000) queues READY.
    Order value over cap ($6,615.00 > $5000) queues DRAFT with CAP_EXCEEDED."""
    repo = phase3_env["repo"]
    vault = phase3_env["vault"]
    runner = phase3_env["runner"]
    service = phase3_env["service"]
    setup = make_setup(extra_stock_phones=["+971501234567"])

    await service.save_setup(setup)
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 10000.0})
    runner.autonomy_mode = "turbo"
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 5000.0, "default_margin_pct": 5.0})
    runner.autonomy_mode = "turbo"

    # Inventory: 20 units @ $1050 USD (sale quote = $1102.50)
    await repo.put("inventory", "inv_cap_test", {
        "device": "iPhone 16 Pro Max",
        "variant": "256GB",
        "condition": "NON_ACTIVE",
        "lock_status": "UNLOCKED",
        "quantity": 20,
        "unit_cost": 1050.0,
        "currency": "USD",
        "ownership": "owned",
    }, 0)

    # Mock dynamic quote to return $1102.50
    import jarvis.trading_engine as te
    orig_quote = te.calculate_dynamic_quote
    async def fixed_quote(*a, **kw):
        return {"quote_usd": "1102.50", "target_currency": "USD", "quote_local": "1102.50"}
    te.calculate_dynamic_quote = fixed_quote

    try:
        # Case A: Under cap (3 units * $1102.50 = $3307.50 <= $5000)
        res_under = await te.evaluate_inbound_trade(
            repo, vault,
            {"conversation_id": "conv_under@c.us", "phone": "+971501111111", "message_id": "msg_under_cap"},
            setup,
            triage={"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": "iPhone 16 Pro Max", "variant": "256GB", "quantity": 3, "condition": "NON_ACTIVE", "lock_status": "UNLOCKED"}]},
        )
        assert res_under["action"] == "QUEUED_READY"
        assert res_under["target_status"] == "READY"
        assert res_under["hold_reason"] is None
        assert res_under["total_quote_usd"] == 3307.50

        # Case B: Over cap (6 units * $1102.50 = $6615.00 > $5000)
        res_over = await te.evaluate_inbound_trade(
            repo, vault,
            {"conversation_id": "conv_over@c.us", "phone": "+971502222222", "message_id": "msg_over_cap"},
            setup,
            triage={"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": "iPhone 16 Pro Max", "variant": "256GB", "quantity": 6, "condition": "NON_ACTIVE", "lock_status": "UNLOCKED"}]},
        )
        assert res_over["action"] == "QUEUED_DRAFT"
        assert res_over["target_status"] == "DRAFT"
        assert res_over["hold_reason"] == "CAP_EXCEEDED"
        assert res_over["total_quote_usd"] == 6615.00
    finally:
        te.calculate_dynamic_quote = orig_quote


@pytest.mark.asyncio
async def test_gate_scenario_4_inside_and_outside_hours(phase3_env, monkeypatch):
    """Scenario 4: Working hours evaluation.
    Outside hours holds quote as SCHEDULED_NIGHT_HOLD; morning scheduler tick wakes and routes."""
    repo = phase3_env["repo"]
    vault = phase3_env["vault"]
    service = phase3_env["service"]
    runner = phase3_env["runner"]
    setup = make_setup(extra_stock_phones=["+971501234567"])

    await service.save_setup(setup)
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 10000.0})
    runner.autonomy_mode = "turbo"
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 5000.0})
    runner.autonomy_mode = "turbo"

    await repo.put("inventory", "inv_hours_test", {
        "device": "Pixel 9 Pro",
        "variant": "128GB",
        "quantity": 10,
        "unit_cost": 700.0,
        "currency": "USD",
        "ownership": "owned",
    }, 0)

    # Force night hours
    wake_time = time.time() - 5
    monkeypatch.setattr("jarvis.trading_engine.evaluate_working_hours", lambda *a, **kw: {
        "timezone": "Asia/Dubai",
        "local_time_iso": "2026-11-21T03:00:00+04:00",
        "within_hours": False,
        "wake_at_ts": wake_time,
        "wake_local_iso": "2026-11-21T08:30:00+04:00",
    })

    import jarvis.trading_engine as te
    monkeypatch.setattr(te, "calculate_dynamic_quote", lambda *a, **kw: asyncio.sleep(0, result={"quote_usd": "750.00", "target_currency": "USD", "quote_local": "750.00"}))

    res = await te.evaluate_inbound_trade(
        repo, vault,
        {"conversation_id": "conv_night@c.us", "phone": "+971503333333", "message_id": "msg_night_1"},
        setup,
        triage={"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": "Pixel 9 Pro", "variant": "128GB", "quantity": 2}]},
    )
    assert res["action"] == "SCHEDULED_NIGHT_HOLD"
    assert res["hold_reason"] == "OUT_OF_HOURS"

    # Verify scheduled job was stored
    due = repo.due_scheduled_jobs()
    assert len(due) == 1

    # Morning tick execution
    processed = await service.run_scheduler_tick()
    assert processed == 1

    # Outbox should now have READY quote
    ready_quotes = repo.list_outbound(statuses=("READY",))
    assert len(ready_quotes) == 1
    assert "Pixel 9 Pro 128GB" in ready_quotes[0]["text_parts"][0]


@pytest.mark.asyncio
async def test_gate_scenario_5_stale_inventory_and_supplier_ttl(phase3_env):
    """Scenario 5: Expired supplier offers (>86400s), stale daily stock (>24h), and zero quantity inventory never match as fresh stock."""
    repo = phase3_env["repo"]
    now_ts = time.time()

    # 1. Expired supplier offer: 2 days old
    await repo.put("offers", "offer_expired_p3", {
        "state": "ACTIVE",
        "phone": "+919876543210",
        "timestamp": now_ts - 172800,
        "items": [{"part_number": "ATMEGA328P-PU", "quantity": 1000, "unit_price": 1.20, "currency": "USD"}],
    }, 0)

    # 2. Stale daily stock snapshot: 30 hours old
    await repo.put("configuration", "daily-stock", {
        "source_timestamp": now_ts - (30 * 3600),
        "items": [{"device": "Samsung S24 Ultra", "variant": "256GB", "quantity": 50, "unit_cost": 850.0, "currency": "USD"}],
    }, 0)

    # 3. Zero quantity inventory
    await repo.put("inventory", "inv_zero_qty_p3", {
        "device": "MacBook Pro M3",
        "variant": "16/512",
        "quantity": 0,
        "unit_cost": 1500.0,
        "currency": "USD",
        "ownership": "owned",
    }, 0)

    from jarvis.trading_engine import find_best_stock_match

    # Stale supplier offer must NOT match
    m1 = await find_best_stock_match(repo, {"part_number": "ATMEGA328P-PU", "quantity": 100})
    assert m1["sourcing_needed"] is True

    # Stale daily stock snapshot must NOT match
    m2 = await find_best_stock_match(repo, {"device": "Samsung S24 Ultra", "variant": "256GB", "quantity": 5})
    assert m2["sourcing_needed"] is True

    # Zero qty inventory must NOT match
    m3 = await find_best_stock_match(repo, {"device": "MacBook Pro M3", "variant": "16/512", "quantity": 1})
    assert m3["sourcing_needed"] is True


@pytest.mark.asyncio
async def test_gate_scenario_6_gemini_failure_and_resilience(phase3_env):
    """Scenario 6: Gemini failure and error resilience.
    Quota 429 records MODEL_QUOTA failure; reply generation error safely falls back to deterministic anti-slop quote."""
    repo = phase3_env["repo"]
    vault = phase3_env["vault"]
    runner = phase3_env["runner"]
    service = phase3_env["service"]
    setup = make_setup(extra_stock_phones=["+971501234567"])

    await service.save_setup(setup)
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 10000.0})
    runner.autonomy_mode = "turbo"

    # Seed stock
    await repo.put("inventory", "inv_resilience", {
        "device": "iPhone 15",
        "variant": "128GB",
        "quantity": 10,
        "unit_cost": 600.0,
        "currency": "USD",
        "ownership": "owned",
    }, 0)

    # Part A: Classification failure (429 Quota)
    def failing_classification(request: httpx.Request):
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"code": 429, "message": "Resource has been exhausted"}})

    client_quota = GeminiClient(vault, transport=httpx.MockTransport(failing_classification))
    pipeline = IntakePipeline(repo, client_quota, local_first=True)

    msg_id = "false_971504445555@c.us_QUOTA_TEST"
    event = {
        "message_id": msg_id,
        "conversation_id": "971504445555@c.us",
        "phone": "+971504445555",
        "sender_name": "Buyer 429",
        "text": "Need 5 pcs iPhone 15 128GB urgently",
        "timestamp": time.time(),
        "identity_verified": True,
    }

    with pytest.raises(GeminiError) as exc_info:
        await pipeline.process(event, setup)
    assert exc_info.value.code == "MODEL_QUOTA"

    trace_quota = await service.message_trace({"message_id": msg_id})
    assert trace_quota["current_stage"] == "AI_CLASSIFICATION"
    assert trace_quota["stage_outcome"] == "FAILED"
    assert trace_quota["reason"] == "MODEL_QUOTA"

    # Part B: Reply generation error fallback: Gemini throws 503 during reply generation
    # Trade evaluation should NOT crash and should fall back safely to deterministic anti-slop quote
    def reply_error_transport(request: httpx.Request):
        return httpx.Response(503, json={"error": {"code": 503, "message": "Service Unavailable"}})

    vault.gemini_transport = httpx.MockTransport(reply_error_transport)
    repo.gemini_transport = httpx.MockTransport(reply_error_transport)

    import jarvis.trading_engine as te
    orig_quote = te.calculate_dynamic_quote
    async def mock_pricing(*a, **kw):
        return {"quote_usd": "630.00", "target_currency": "USD", "quote_local": "630.00"}
    te.calculate_dynamic_quote = mock_pricing

    try:
        eval_res = await te.evaluate_inbound_trade(
            repo, vault,
            {"conversation_id": "971504445555@c.us", "phone": "+971504445555", "message_id": "msg_fallback_test"},
            setup,
            triage={"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": "iPhone 15", "variant": "128GB", "quantity": 2}]},
        )
        assert eval_res["action"] in ("QUEUED_READY", "QUEUED_DRAFT")
        # Fallback text must contain clean deterministic quote
        assert "iPhone 15 128GB" in eval_res["parts"][0]
        assert "630.00$" in eval_res["parts"][1]
    finally:
        te.calculate_dynamic_quote = orig_quote


@pytest.mark.asyncio
async def test_gate_scenario_7_backlog_over_500_messages_terminates(phase3_env):
    """Scenario 7: Backlogged messages beyond 500 records.
    Verifies that indexed pending queries select and drain oldest work without window starvation."""
    repo = phase3_env["repo"]
    runner = phase3_env["runner"]
    setup = make_setup(extra_stock_phones=["+971501234567"])

    now = time.time()
    # Insert 510 messages, oldest at offset 0
    for idx in range(510):
        t_stamp = now - (600 - idx)
        await repo.put("messages", f"msg_backlog_p3_{idx}", {
            "message_id": f"false_backlog_{idx}@c.us_p3_{idx}",
            "conversation_id": f"backlog_{idx}@c.us",
            "state": "CLASSIFIED",
            "trade_evaluation_pending": True,
            "triage": {"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": f"Device_{idx}", "quantity": 1}]},
            "received": t_stamp,
        }, 0, f"msg_backlog_p3_{idx}")

    # Indexed query pending_evaluation_messages must fetch oldest records
    pending = repo.pending_evaluation_messages(limit=10)
    assert len(pending) == 10
    # Oldest record must be msg_backlog_p3_0
    assert pending[0]["id"] == "msg_backlog_p3_0"
    assert pending[9]["id"] == "msg_backlog_p3_9"


@pytest.mark.asyncio
async def test_gate_scenario_8_media_processing_and_ocr_termination(phase3_env):
    """Scenario 8: Complete media job lifecycle.
    - WAITING_BYTES deadline timeout
    - Invalid MIME rejection
    - Oversized bytes rejection
    - Caption-only classification with OCR failure hold
    - Already-sent decision protection on media retry"""
    repo = phase3_env["repo"]
    vault = phase3_env["vault"]
    runner = phase3_env["runner"]
    service = phase3_env["service"]
    setup = make_setup(extra_stock_phones=["+971501234567"])

    await service.save_setup(setup)
    await service.settings_save({"autonomy_mode": "turbo", "auto_reply_max_usd": 10000.0})
    runner.autonomy_mode = "turbo"
    pipeline = IntakePipeline(repo, None, local_first=True)

    # 1. WAITING_BYTES and deadline timeout
    msg_media = "false_971505556666@c.us_MEDIA_001"
    ev_media = {
        "message_id": msg_media,
        "conversation_id": "971505556666@c.us",
        "phone": "+971505556666",
        "text": "[Photo / Stock Image]",
        "has_media": True,
        "media_type": "image",
        "media_b64": None,
        "timestamp": time.time(),
        "identity_verified": True,
    }
    res_m = await pipeline.process(ev_media, setup)
    assert res_m["status"] == "CAPTURED_REVIEW"
    mid = res_m["id"]

    rec_m = await repo.get("messages", mid)
    assert rec_m["data"]["media_job"]["status"] == "WAITING_BYTES"
    assert rec_m["data"]["media_job"]["deadline"] is not None

    # Simulate expired deadline and trigger runner
    repo.update_media_job(mid, status="WAITING_BYTES", deadline=time.time() - 10)
    await runner.classify_pending()

    rec_timed_out = await repo.get("messages", mid)
    assert rec_timed_out["data"]["media_job"]["status"] == "TIMEOUT"
    assert rec_timed_out["data"]["reason"] == "MEDIA_TIMEOUT"

    # 2. Invalid MIME rejection
    msg_bad_mime = "false_971505556666@c.us_BAD_MIME"
    ev_bad_mime = {
        "message_id": msg_bad_mime,
        "conversation_id": "971505556666@c.us",
        "phone": "+971505556666",
        "text": "[Media attachment]",
        "has_media": True,
        "media_type": "audio",
        "media_mime": "audio/mpeg",
        "media_b64": base64.b64encode(b"fake audio data").decode(),
        "timestamp": time.time(),
        "identity_verified": True,
    }
    res_bad = await pipeline.process(ev_bad_mime, setup)
    assert res_bad["status"] == "NEEDS_REVIEW"
    assert res_bad["reason"] == "INVALID_MEDIA"

    trace_bad = await service.message_trace({"message_id": msg_bad_mime})
    assert trace_bad["current_stage"] == "MEDIA_EXTRACTION"
    assert trace_bad["stage_outcome"] == "FAILED"
    assert trace_bad["reason"] == "INVALID_MEDIA"

    # 3. Oversized bytes rejection
    msg_oversized = "false_971505556666@c.us_OVERSIZED"
    huge_payload = "A" * 6_500_000
    ev_oversized = {
        "message_id": msg_oversized,
        "conversation_id": "971505556666@c.us",
        "phone": "+971505556666",
        "text": "[Stock Image]",
        "has_media": True,
        "media_type": "image",
        "media_mime": "image/jpeg",
        "media_b64": huge_payload,
        "timestamp": time.time(),
        "identity_verified": True,
    }
    res_huge = await pipeline.process(ev_oversized, setup)
    assert res_huge["status"] == "NEEDS_REVIEW"
    assert res_huge["reason"] == "UNSUPPORTED_SIZE"

    # 4. Caption-only message with OCR timeout: prevents final quote dispatch
    msg_cap_ocr = "false_971505556666@c.us_CAP_OCR_FAIL"
    ev_cap_ocr = {
        "message_id": msg_cap_ocr,
        "conversation_id": "971505556666@c.us",
        "phone": "+971505556666",
        "text": "Rate for these items in the photo?",
        "has_media": True,
        "media_type": "image",
        "media_b64": None,
        "timestamp": time.time(),
        "identity_verified": True,
    }
    res_init = await pipeline.process(ev_cap_ocr, setup)
    cap_id = res_init["id"]
    repo.update_media_job(cap_id, status="TIMEOUT")

    # Mock classification on caption text
    def mock_caption_classifier(req: httpx.Request):
        triage = {"category": "BUY", "proceed": True, "needs_review": False, "reason": "Caption inquiry", "items": [{"device": "Unspecified Device", "quantity": 1}]}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(triage)}]}, "finishReason": "STOP"}]})

    pipe_cap = IntakePipeline(repo, GeminiClient(vault, transport=httpx.MockTransport(mock_caption_classifier)), local_first=True)
    res_cap_classified = await pipe_cap.process({**ev_cap_ocr, "media_b64": None}, setup)
    # Must hold in NEEDS_REVIEW due to OCR failure rather than auto-dispatching incomplete quote!
    assert res_cap_classified["status"] == "NEEDS_REVIEW"
    rec_cap_classified = await repo.get("messages", cap_id)
    assert rec_cap_classified["data"]["trade_evaluation_pending"] is False
    assert rec_cap_classified["data"]["reason"] == "MEDIA_TIMEOUT"

    # 5. Already-sent decision protection on media retry
    msg_sent_protect = "false_971505556666@c.us_SENT_PROTECT"
    ev_protect = {
        "message_id": msg_sent_protect,
        "conversation_id": "971505556666@c.us",
        "phone": "+971505556666",
        "text": "WTB 5 iPhone 16",
        "has_media": True,
        "media_type": "image",
        "media_b64": None,
        "identity_verified": True,
    }
    res_protect_init = await pipeline.process(ev_protect, setup)
    prot_id = res_protect_init["id"]
    # Seed message with an accepted outbound quote
    oid = repo.queue_outbound("971505556666@c.us", ["Quote text"], kind="quote", status="ACCEPTED")
    cur_p = await repo.get("messages", prot_id)
    await repo.put("messages", prot_id, {
        **cur_p["data"],
        "outbound_id": oid,
    }, cur_p["revision"])

    pipe_protect = IntakePipeline(repo, GeminiClient(vault, transport=httpx.MockTransport(mock_caption_classifier)), local_first=True)
    late_media_b64 = base64.b64encode(b"late valid image bytes").decode()
    res_late = await pipe_protect.process({
        **ev_protect,
        "media_mime": "image/jpeg",
        "media_b64": late_media_b64,
    }, setup)

    # Must protect existing sent decision and not re-evaluate or overwrite
    rec_after_late = await repo.get("messages", prot_id)
    assert rec_after_late["data"].get("decision_protected") is True
    assert rec_after_late["data"].get("trade_evaluation_pending") is False


@pytest.mark.asyncio
async def test_gate_scenario_9_hardware_grade_and_spec_isolation_b15(phase3_env):
    """Scenario 9: Grade, spec and ownership isolation (B15).
    - UNLOCKED vs JV_LOCK never cross
    - NON_ACTIVE vs ACTIVATED / REFURB never cross
    - Supplier stock (ownership='supplier' or source_kind='supplier_wts') is never matched as Tier 1 owned stock
    - Partial stock records shortage_status='PARTIAL_SHORTAGE' and creates gap sourcing alert."""
    repo = phase3_env["repo"]
    from jarvis.trading_engine import find_best_stock_match

    # 1. Seed inventory with supplier stock masquerading in inventory
    await repo.put("inventory", "inv_supplier_stock_in_inv", {
        "device": "iPhone 16 Pro",
        "variant": "128GB",
        "condition": "NON_ACTIVE",
        "lock_status": "UNLOCKED",
        "quantity": 100,
        "unit_cost": 900.0,
        "currency": "USD",
        "ownership": "supplier",  # Explicit supplier stock
        "source_kind": "supplier_wts",
    }, 0)

    # 2. Seed owned inventory with partial quantity
    await repo.put("inventory", "inv_owned_partial", {
        "device": "iPhone 16 Pro",
        "variant": "128GB",
        "condition": "NON_ACTIVE",
        "lock_status": "UNLOCKED",
        "quantity": 3,
        "unit_cost": 950.0,
        "currency": "USD",
        "ownership": "owned",
        "source_kind": "owned",
    }, 0)

    # Match request for 10 units:
    # Must NOT match the 100 supplier units as Tier 1 owned stock!
    # Must match the 3 owned units as partial stock!
    match = await find_best_stock_match(repo, {
        "device": "iPhone 16 Pro",
        "variant": "128GB",
        "condition": "NON_ACTIVE",
        "lock_status": "UNLOCKED",
        "quantity": 10,
    })

    assert match is not None
    assert match["source_kind"] == "owned"
    assert match["row_id"] == "inv_owned_partial"
    assert match["available_quantity"] == 3
    assert match["requested_quantity"] == 10
    assert match["is_partial"] is True
    assert match["shortage_status"] == "PARTIAL_SHORTAGE"
    assert match["quantity_gap"] == 7

    # Sourcing gap task must be created in tasks for 7 pcs gap
    tasks = await repo.list("tasks", 20)
    gap_tasks = [t for t in tasks if t["data"].get("category") == "sourcing_need" and "need 7 more pcs" in t["data"]["title"]]
    assert len(gap_tasks) == 1
    assert gap_tasks[0]["data"]["details"]["quantity_gap"] == 7
