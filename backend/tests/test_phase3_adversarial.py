import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from conftest import FakeSheets, FakeVault

from jarvis.fx import calculate_dynamic_quote
from jarvis.intelligence import match_opportunities
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.trading_engine import (
    _is_grade_compatible,
    compose_dealer_quote,
    compose_multi_item_quote,
    evaluate_inbound_trade,
    evaluate_working_hours,
    find_best_stock_match,
    handle_agent_command,
)


@pytest.mark.asyncio
async def test_gate1_multi_item_mixed_inquiry_and_zero_supplier_leak(tmp_path):
    """Gate 1: Multi-item evaluation in MIXED/BUY messages, supplier unit_cost mapping, and zero internal header leaks."""
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    vault = FakeVault()
    try:
        await repo.put("configuration", "desktop-settings", {
            "autonomy_mode": "draft",
            "default_margin_pct": 5.0,
            "working_hours_start": "00:00",
            "working_hours_end": "23:59",
        }, 0)

        # 1. Owned stock: iPhone 16 Pro Max 256GB (exact match, full qty)
        await repo.put("inventory", "inv_ip16", {
            "device": "iPhone 16 Pro Max", "variant": "256GB", "condition": "NON_ACTIVE",
            "lock_status": "UNLOCKED", "quantity": 15, "unit_cost": 1050.0, "currency": "USD",
        }, 0)

        # 2. Owned stock: STM32F407VGT6 (exact component match, partial qty)
        await repo.put("inventory", "inv_stm32", {
            "part_number": "STM32F407VGT6", "packaging": "Tray", "quantity": 40,
            "unit_cost": 3.50, "currency": "USD",
        }, 0)

        # 3. Owned alt stock: ESP32-WROOM-32E (alt suggestion for ESP32-WROOM-32D)
        await repo.put("inventory", "inv_esp_alt", {
            "device": "ESP32-WROOM-32E", "variant": "Reel", "quantity": 100,
            "unit_cost": 2.20, "currency": "USD",
        }, 0)

        # 4. Fresh supplier offer: IRFZ44N MOSFET (supplier WTS offer with unit_price)
        await repo.put("offers", "offer_irf", {
            "state": "ACTIVE", "phone": "+919876543210", "timestamp": time.time(),
            "items": [{
                "part_number": "IRFZ44N", "packaging": "Tube", "quantity": 1000,
                "unit_price": 0.40, "currency": "USD",
            }],
        }, 0)

        # Test single item match on supplier offer: verify unit_cost is mapped and supplier offer header is NOT leaked
        supp_match = await find_best_stock_match(repo, {"part_number": "IRFZ44N", "quantity": 200})
        assert supp_match is not None
        assert supp_match["source_kind"] == "supplier_wts"
        assert supp_match["item"]["unit_cost"] == 0.40
        supp_quote = compose_dealer_quote({"part_number": "IRFZ44N"}, supp_match, {"quote_usd": "0.45"})
        # CRITICAL ADVERSARIAL ASSERTION: Leaked internal note must NEVER appear in quote
        assert "Supplier offer to confirm:" not in supp_quote[0]
        assert "IRFZ44N" in supp_quote[0]
        assert "0.45$" in supp_quote[1]

        # Mixed inquiry: 4 BUY items and 1 SELL item
        triage_payload = {
            "category": "MIXED",
            "proceed": True,
            "needs_review": False,
            "items": [
                {"direction": "BUY", "device": "iPhone 16 Pro Max", "variant": "256GB", "quantity": 10, "condition": "NON_ACTIVE", "lock_status": "UNLOCKED"},
                {"direction": "BUY", "part_number": "STM32F407VGT6", "packaging": "Tray", "quantity": 100},
                {"direction": "BUY", "device": "ESP32-WROOM", "variant": "Reel", "quantity": 50},
                {"direction": "BUY", "part_number": "IRFZ44N", "quantity": 500},
                {"direction": "SELL", "device": "Used Laptops", "quantity": 20},  # should be excluded from buyer quote
            ],
        }

        # Mock dynamic quote to return clean USD quotes
        async def fake_quote(unit_cost, currency, phone, **kw):
            cost = float(unit_cost)
            return {"quote_usd": f"{cost * 1.05:.2f}", "target_currency": "USD", "quote_local": f"{cost * 1.05:.2f}"}

        import jarvis.trading_engine as te
        orig_quote = te.calculate_dynamic_quote
        te.calculate_dynamic_quote = fake_quote
        try:
            res = await evaluate_inbound_trade(
                repo, vault,
                {"conversation_id": "971501112233@c.us", "phone": "+971501112233", "message_id": "msg_mixed_4items"},
                {},
                triage=triage_payload,
            )
            assert res["action"] in ("QUEUED_DRAFT", "DRAFT_READY")
            outbound = repo.list_outbound(statuses=("DRAFT", "READY", "NEEDS_PRICE_REVIEW"))
            assert len(outbound) >= 1
            quote_text = "\n\n".join(outbound[0]["text_parts"])

            # Verify all 4 buy items are present in the structured quotation
            assert "iPhone 16 Pro Max 256GB" in quote_text
            assert "STM32F407VGT6" in quote_text
            assert "ESP32" in quote_text
            assert "IRFZ44N" in quote_text
            # Verify partial quantity indicator on STM32 (40 pcs ready of 100 req)
            assert "40 pcs ready (of 100 req)" in quote_text or "40 pcs ready" in quote_text
            # Verify alt suggestion flag on ESP32
            assert "Alt available:" in quote_text
            # Verify SELL item was NOT quoted to buyer
            assert "Used Laptops" not in quote_text
            # Verify ZERO internal leaks
            assert "Supplier offer to confirm:" not in quote_text
        finally:
            te.calculate_dynamic_quote = orig_quote
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_gate2_stale_supplier_offer_and_zero_qty_rejection(tmp_path):
    """Gate 2: Expired supplier offers (>TTL) and zero-qty offers are rejected; Tier 4 sourcing alert is fired."""
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    try:
        now_ts = time.time()
        # 1. Stale offer: 3 days old (exceeds 86400s TTL)
        await repo.put("offers", "offer_stale", {
            "state": "ACTIVE", "phone": "+919876543210", "timestamp": now_ts - (3 * 86400),
            "items": [{"part_number": "NE555P", "quantity": 5000, "unit_price": 0.05, "currency": "USD"}],
        }, 0)

        # 2. Zero-qty offer: active and fresh, but quantity is 0
        await repo.put("offers", "offer_zero_qty", {
            "state": "ACTIVE", "phone": "+919876543210", "timestamp": now_ts - 100,
            "items": [{"part_number": "LM358N", "quantity": 0, "unit_price": 0.10, "currency": "USD"}],
        }, 0)

        # Stale offer must NOT match
        match_stale = await find_best_stock_match(repo, {"part_number": "NE555P", "quantity": 100}, offer_ttl_seconds=86400)
        assert match_stale["sourcing_needed"] is True
        assert match_stale["score"] == 0

        # Zero qty offer must NOT match
        match_zero = await find_best_stock_match(repo, {"part_number": "LM358N", "quantity": 50}, offer_ttl_seconds=86400)
        assert match_zero["sourcing_needed"] is True
        assert match_zero["score"] == 0

        # Verify Tier 4 created sourcing alerts in repo tasks
        tasks = await repo.list("tasks", 50)
        sourcing_tasks = [t for t in tasks if t["data"].get("category") == "sourcing_need"]
        assert len(sourcing_tasks) >= 2
        labels = [t["data"]["title"] for t in sourcing_tasks]
        assert any("NE555P" in l for l in labels)
        assert any("LM358N" in l for l in labels)
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_gate3_hardware_grade_iron_wall(tmp_path):
    """Gate 3: Hardware grade isolation: JV_LOCK vs UNLOCKED and NON_ACTIVE vs ACTIVATED/REFURB never cross."""
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    try:
        # Inventory contains JV_LOCK, ACTIVATED, and REFURB devices
        await repo.put("inventory", "inv_jv", {
            "device": "iPhone 16", "variant": "128GB", "condition": "NON_ACTIVE",
            "lock_status": "JV_LOCK", "quantity": 100, "unit_cost": 500.0, "currency": "USD",
        }, 0)
        await repo.put("inventory", "inv_act", {
            "device": "iPhone 16", "variant": "128GB", "condition": "ACTIVATED",
            "lock_status": "UNLOCKED", "quantity": 50, "unit_cost": 600.0, "currency": "USD",
        }, 0)
        await repo.put("inventory", "inv_refurb", {
            "device": "iPhone 16", "variant": "128GB", "condition": "REFURB",
            "lock_status": "UNLOCKED", "quantity": 30, "unit_cost": 450.0, "currency": "USD",
        }, 0)

        # 1. Buyer wants UNLOCKED + NON_ACTIVE -> MUST NOT match JV_LOCK or ACTIVATED or REFURB
        match_clean = await find_best_stock_match(repo, {
            "device": "iPhone 16", "variant": "128GB", "condition": "NON_ACTIVE", "lock_status": "UNLOCKED", "quantity": 10,
        })
        assert match_clean["sourcing_needed"] is True  # No clean unlocked non-active stock exists

        # 2. Direct unit helper tests on _is_grade_compatible
        assert _is_grade_compatible({"lock_status": "JV_LOCK"}, {"lock_status": "UNLOCKED"}) is False
        assert _is_grade_compatible({"lock_status": "UNLOCKED"}, {"lock_status": "JV_LOCK"}) is False
        assert _is_grade_compatible({"condition": "NON_ACTIVE"}, {"condition": "ACTIVATED"}) is False
        assert _is_grade_compatible({"condition": "NEW"}, {"condition": "REFURB"}) is False
        assert _is_grade_compatible({"condition": "NON_ACTIVE"}, {"condition": "USED"}) is False
        assert _is_grade_compatible({"condition": "USED"}, {"condition": "NEW"}) is False
        assert _is_grade_compatible({"condition": "NON_ACTIVE"}, {"condition": "NON_ACTIVE"}) is True
        assert _is_grade_compatible({"lock_status": "UNLOCKED"}, {"lock_status": "UNLOCKED"}) is True

        # 3. Opportunity Matcher compatibility check
        await repo.put("messages", "msg_buy_clean", {
            "state": "CLASSIFIED", "phone": "+919111111111", "updated_at": datetime.now(timezone.utc).isoformat(),
            "triage": {"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": "iPhone 16", "variant": "128GB", "condition": "NON_ACTIVE", "lock_status": "UNLOCKED"}]},
        }, 0)
        await repo.put("messages", "msg_sell_jv", {
            "state": "CLASSIFIED", "phone": "+919222222222", "updated_at": datetime.now(timezone.utc).isoformat(),
            "triage": {"category": "SELL", "proceed": True, "needs_review": False, "items": [{"device": "iPhone 16", "variant": "128GB", "condition": "NON_ACTIVE", "lock_status": "JV_LOCK"}]},
        }, 0)

        # Match opportunities: must NOT match clean buyer with JV seller
        await match_opportunities(repo)
        tasks = await repo.list("tasks", 50)
        matched_alerts = [t for t in tasks if t["data"].get("category") == "match"]
        assert len(matched_alerts) == 0  # Zero false cross-match!
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_gate4_fx_outage_failsafe_and_needs_price_review(tmp_path, monkeypatch):
    """Gate 4: FX outage or pricing failure marks draft as NEEDS_PRICE_REVIEW and allows operator manual approval."""
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    vault = FakeVault()
    try:
        await repo.put("configuration", "desktop-settings", {"autonomy_mode": "auto", "auto_reply_max_usd": 10000.0}, 0)
        await repo.put("inventory", "inv_laptop", {
            "device": "ThinkPad T14", "variant": "16/512", "quantity": 10,
            "unit_cost": 45000.0, "currency": "INR",
        }, 0)

        # Force working hours so it evaluates immediate queueing
        monkeypatch.setattr("jarvis.trading_engine.evaluate_working_hours", lambda *a, **kw: {
            "timezone": "Asia/Kolkata", "local_time_iso": "2026-11-11T14:00:00+05:30",
            "within_hours": True, "wake_at_ts": time.time(), "wake_local_iso": "2026-11-11T14:00:00+05:30",
        })

        # Simulate FX network error
        import jarvis.trading_engine as te
        orig_quote = te.calculate_dynamic_quote
        async def failing_quote(*a, **kw):
            raise httpx.ConnectError("FX service unavailable")
        te.calculate_dynamic_quote = failing_quote

        try:
            res = await evaluate_inbound_trade(
                repo, vault,
                {"conversation_id": "919988776655@c.us", "phone": "+919988776655", "message_id": "msg_fx_fail_1"},
                {},
                triage={"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": "ThinkPad T14", "variant": "16/512", "quantity": 2}]},
            )
            # Must NOT crash, must NOT emit priceless quote, must queue as NEEDS_PRICE_REVIEW
            assert res["action"] == "QUEUED_NEEDS_PRICE_REVIEW"
            assert res["target_status"] == "NEEDS_PRICE_REVIEW"

            # Check outbound list
            outbound = repo.list_outbound(statuses=("NEEDS_PRICE_REVIEW",))
            assert len(outbound) == 1
            draft = outbound[0]
            assert draft["status"] == "NEEDS_PRICE_REVIEW"

            # Verify operator CAN manually approve the draft once price is verified
            repo.upsert_contact("919988776655@c.us", phone="+919988776655", name="Corporate Buyer", category="BUYER")
            approval_err = repo.approve_outbound(
                draft["id"],
                draft["updated_at"],
                account_phone="+919876543210",
            )
            assert approval_err is None  # Cleanly approved!

            # Verify status transitioned to READY and dispatch entry was created
            ready_rows = repo.ready_outbound_ids("+919876543210")
            assert any(r["id"] == draft["id"] for r in ready_rows)
        finally:
            te.calculate_dynamic_quote = orig_quote
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_gate5_night_hold_and_morning_wake_repricing(tmp_path, monkeypatch):
    """Gate 5: Working hours evaluation queues night hold, morning tick reprices and applies autonomy rules."""
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    vault = FakeVault()
    try:
        await repo.put("configuration", "desktop-settings", {"autonomy_mode": "auto", "auto_reply_max_usd": 5000.0, "default_margin_pct": 5.0}, 0)
        await repo.put("inventory", "inv_s25", {
            "device": "Samsung S25 Ultra", "variant": "12/512", "condition": "NON_ACTIVE",
            "lock_status": "UNLOCKED", "quantity": 25, "unit_cost": 800.0, "currency": "USD",
        }, 0)

        # Force night hours
        monkeypatch.setattr("jarvis.trading_engine.evaluate_working_hours", lambda *a, **kw: {
            "timezone": "Asia/Dubai", "local_time_iso": "2026-11-11T03:00:00+04:00",
            "within_hours": False, "wake_at_ts": time.time() - 10, "wake_local_iso": "2026-11-11T08:45:00+04:00",
        })

        async def fake_quote(unit_cost, currency, phone, **kw):
            return {"quote_usd": "840.00", "target_currency": "AED", "quote_local": "3084.90"}
        monkeypatch.setattr("jarvis.trading_engine.calculate_dynamic_quote", fake_quote)

        # 1. Inbound inquiry during night
        res = await evaluate_inbound_trade(
            repo, vault,
            {"conversation_id": "971509998888@c.us", "phone": "+971509998888", "message_id": "night_msg_1"},
            {},
            triage={"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": "Samsung S25 Ultra", "variant": "12/512", "quantity": 3}]},
        )
        assert res["action"] == "SCHEDULED_NIGHT_HOLD"
        due_jobs = repo.due_scheduled_jobs()
        assert len(due_jobs) == 1

        # 2. Morning scheduler tick execution via fake desktop service runner
        from jarvis.desktop_service import DesktopService
        from jarvis.diagnostics import DiagnosticsLog
        from jarvis.runner import Runner

        runner = Runner(vault, repo)
        runner.active = True
        runner.autonomy_mode = "auto"
        service = DesktopService(vault, repo, runner)

        processed = await service.run_scheduler_tick()
        assert processed == 1

        # In auto mode with 3 * 840 = 2520 USD (<= 5000 max_usd limit), status must be READY
        outbox = repo.list_outbound(statuses=("READY",))
        assert len(outbox) == 1
        quote_item = outbox[0]
        assert quote_item["status"] == "READY"
        assert "Samsung S25 Ultra 12/512" in quote_item["text_parts"][0]
        assert "840.00$" in quote_item["text_parts"][1]
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_gate6_status_unification_and_opportunity_loop(tmp_path):
    """Gate 6: Outbound status reconciliation (APPROVED -> READY), claim dispatching, and agent commands."""
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    vault = FakeVault()
    try:
        # 1. Queue outbound with APPROVED status (e.g. from agent broadcast or CLI): must normalize to READY and create dispatch
        oid = repo.queue_outbound(
            "919876543210@c.us",
            ["Special Wholesale Rate Alert"],
            kind="manual",
            recipient_phone="+919876543210",
            status="APPROVED",
        )
        # Verify status is normalized to READY
        row = repo.list_outbound(statuses=("READY",))[0]
        assert row["id"] == oid
        assert row["status"] == "READY"

        # Verify claimable by worker
        ready_ids = repo.ready_outbound_ids("+919876543210")
        assert any(r["id"] == oid for r in ready_ids)

        # 2. Agent command '@Jarvis SEND' queues outbound as READY
        await repo.put("tasks", "pending-banner-campaign", {
            "state": "AWAITING_CONFIRM",
            "recipients": [{"conversation_id": "919111111111@c.us", "phone": "+919111111111", "name": "Buyer 1"}],
            "caption": "Fresh Deals Ready",
            "filename": "banner.png",
            "data_url": "data:image/png;base64,QUJD",
        }, 0)

        cmd_res = await handle_agent_command(repo, vault, "@Jarvis SEND", "self@c.us", runtime_dir=tmp_path)
        assert cmd_res["handled"] == "broadcast_send"
        agent_outbox = repo.list_outbound(statuses=("READY",))
        assert any("Fresh Deals Ready" in part for ob in agent_outbox for part in ob["text_parts"])
    finally:
        repo.close()
