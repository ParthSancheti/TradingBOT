import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from conftest import FakeSheets, FakeVault

from jarvis.fx import calculate_dynamic_quote, compute_aa_buffer_pct
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.runner import Runner
from jarvis.trading_engine import (
    compose_dealer_quote,
    evaluate_inbound_trade,
    evaluate_working_hours,
    find_best_stock_match,
    generate_promo_banner_png,
    handle_agent_command,
    is_worth_effort,
)


def test_one_percent_fast_gate_and_family_isolation():
    assert is_worth_effort("Good morning 🌸")[0] is False
    assert is_worth_effort("Dinner at 8pm?", "FAMILY_PERSONAL") == (False, "FAMILY_PERSONAL")
    assert is_worth_effort("WTB iPhone 17 256 Non Act")[0] is True
    assert is_worth_effort("Samsung A17 8/128 @400 @ 23300/-")[0] is True
    assert is_worth_effort("Call me back on +919876543210")[0] is True


def test_geo_timezone_night_queue_holds_usa_night_and_allows_day():
    # 07:00 UTC == 23:00 PST (previous day) -> USA Night Hold!
    usa_night = datetime(2026, 11, 11, 7, 0, tzinfo=timezone.utc)
    res = evaluate_working_hours("+14155552671", now_utc=usa_night)
    assert res["timezone"] == "America/Los_Angeles"
    assert res["within_hours"] is False
    assert res["wake_at_ts"] > usa_night.timestamp()

    # 18:00 UTC == 10:00 PST -> Working hours!
    usa_day = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
    day_res = evaluate_working_hours("+14155552671", now_utc=usa_day)
    assert day_res["within_hours"] is True


def test_upward_only_aa_buffer_and_multicurrency_quote():
    assert compute_aa_buffer_pct(fx_change_1h_pct=-1.2, tech_drop_pct=2.0, adverse_news_count=0) == 0.0
    shock = compute_aa_buffer_pct(fx_change_1h_pct=0.8, tech_drop_pct=-2.0, adverse_news_count=2, aa_cap_pct=2.0)
    assert shock == 2.0


async def test_dynamic_quote_multi_currency_and_upward_aa_ratchet():
    def respond(request):
        if request.url.path == "/v2/rate/INR/USD":
            return httpx.Response(200, json={"base": "INR", "quote": "USD", "rate": 0.0117647, "date": datetime.now(timezone.utc).date().isoformat()})
        if request.url.path == "/v2/rate/USD/AED":
            return httpx.Response(200, json={"base": "USD", "quote": "AED", "rate": 3.6725, "date": datetime.now(timezone.utc).date().isoformat()})
        return httpx.Response(404)

    q = await calculate_dynamic_quote(
        70000, "INR", "+971501234567",
        margin_pct=5.0, aa_pct=1.0,
        transport=httpx.MockTransport(respond),
    )
    assert q["target_currency"] == "AED"
    assert float(q["quote_usd"]) > 850.0
    assert float(q["quote_local"]) > 3100.0


async def test_strict_hardware_grade_matching_and_anti_slop_quote(tmp_path):
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    try:
        await repo.put("inventory", "jv1", {
            "device": "iPhone 17", "variant": "256", "condition": "ACTIVATED",
            "lock_status": "JV_LOCK", "quantity": 50, "unit_cost": 550, "currency": "USD",
        }, 0)
        await repo.put("inventory", "good1", {
            "device": "iPhone 17", "variant": "256", "condition": "NON_ACTIVE",
            "lock_status": "UNLOCKED", "color_breakdown": {"Black": 73, "Lavender": 10},
            "quantity": 83, "unit_cost": 790, "currency": "USD",
        }, 0)

        match = await find_best_stock_match(repo, {
            "device": "iPhone 17", "variant": "256", "condition": "NON_ACTIVE", "lock_status": "UNLOCKED",
        })
        assert match is not None and match["row_id"] == "good1"
        parts = compose_dealer_quote({"device": "iPhone 17"}, match, {"quote_usd": "825.00", "target_currency": "AED", "quote_local": "3029.81"})
        assert "Non Act" in parts[0] and "Black-73, Lavender-10" in parts[0]
        assert "825.00$" in parts[1] and "AED" in parts[1]
    finally:
        repo.close()


async def test_sqlite_coalesced_sheets_sync_and_human_mute_and_agent_banner(tmp_path):
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    vault = FakeVault()
    try:
        await repo.put("configuration", "business", {"business_name": "Desk", "owner_phone": "+919876543210", "classifier_model": "gemini-2.5-flash-lite", "sources": [{"phone": "+919876543210", "active": True}], "scan_all": True}, 0)
        await repo.put("configuration", "desktop-settings", {"autonomy_mode": "draft"}, 0)
        await repo.flush_outbox()
        assert ("configuration", "desktop-settings") in remote.records

        # Contact + 4h human manual reply mute
        repo.upsert_contact("919811112222@c.us", phone="+919811112222", name="Buyer", category="VIP_BUYER", last_contacted_at=time.time())
        repo.upsert_contact("919800000000@c.us", phone="+919800000000", name="Mom", category="FAMILY_PERSONAL", last_contacted_at=time.time())
        assert repo.is_chat_muted("919800000000@c.us") is True
        assert repo.is_chat_muted("919811112222@c.us") is False

        runner = Runner(vault, repo)
        runner.active = True
        runner.autonomy_mode = "draft"
        await runner._handle({
            "operation": "observations",
            "events": [],
            "human_replies": [{"conversation_id": "919811112222@c.us", "timestamp": time.time()}],
            "lid_map": {"99999999@lid": "+919811112222"},
        })
        assert repo.is_chat_muted("919811112222@c.us") is True
        assert repo.resolve_lid("99999999@lid")["phone"] == "+919811112222"

        # @Agent Diwali banner preview + SEND excludes FAMILY_PERSONAL
        res = await handle_agent_command(repo, vault, "@Agent make a Diwali banner and send to contacts from last 5 days", "self@c.us", runtime_dir=tmp_path)
        assert res["handled"] == "banner_preview" and res["recipients"] == 1
        sent = await handle_agent_command(repo, vault, "@Agent SEND", "self@c.us", runtime_dir=tmp_path)
        assert sent["handled"] == "broadcast_send" and "1 verified trade contacts" in sent["reply"]

        banner = generate_promo_banner_png("DIWALI DEALS", "Ready Stock", "DM for Live Rate", tmp_path / "banners")
        assert banner["data_url"].startswith("data:image/png;base64,")
    finally:
        repo.close()


async def test_night_hold_schedules_and_evaluates_inbound_trade(tmp_path, monkeypatch):
    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    vault = FakeVault()
    try:
        await repo.put("configuration", "desktop-settings", {"autonomy_mode": "auto", "auto_reply_max_usd": 10000.0}, 0)
        await repo.put("inventory", "s25", {"device": "Samsung S25", "variant": "12/256", "condition": "NON_ACTIVE", "lock_status": "UNLOCKED", "quantity": 20, "unit_cost": 700, "currency": "USD"}, 0)

        monkeypatch.setattr("jarvis.trading_engine.evaluate_working_hours", lambda *a, **kw: {
            "timezone": "America/New_York", "local_time_iso": "2026-11-11T02:00:00-05:00",
            "within_hours": False, "wake_at_ts": time.time() + 3600, "wake_local_iso": "2026-11-11T08:45:00-05:00",
        })
        async def fake_quote(*a, **kw):
            return {"quote_usd": "735.00", "target_currency": "USD", "quote_local": "735.00"}
        monkeypatch.setattr("jarvis.trading_engine.calculate_dynamic_quote", fake_quote)

        out = await evaluate_inbound_trade(
            repo, vault,
            {"conversation_id": "12125550199@c.us", "phone": "+12125550199", "message_id": "false_12125550199@c.us_m1"},
            {},
            triage={"category": "BUY", "proceed": True, "needs_review": False, "items": [{"device": "Samsung S25", "variant": "12/256", "quantity": 5}]},
        )
        assert out["action"] == "SCHEDULED_NIGHT_HOLD"
        assert len(repo.list_scheduled_jobs("PENDING")) == 1
    finally:
        repo.close()


async def test_image_media_routes_to_multimodal_classifier_without_base64_text(tmp_path):
    from jarvis.pipeline import IntakePipeline
    from jarvis.providers import Triage

    assert is_worth_effort("[Photo / Stock Image]", "UNKNOWN", has_media=True, media_type="image") == (True, "MEDIA_OCR_CHECK")
    assert is_worth_effort("/9j/" + "A" * 140, "UNKNOWN")[0] is False

    seen_calls = []

    class FakeVisionClassifier:
        async def classify(self, text, *, media_b64=None, media_mime=None):
            seen_calls.append((text, media_b64, media_mime))
            if media_b64 == "U09DSUFM":
                return Triage(category="OTHER", proceed=False, needs_review=False, reason="Unrelated photo")
            return Triage(
                category="STOCK",
                proceed=True,
                needs_review=False,
                reason="Stock sheet photo OCR",
                items=[{"device": "iPhone 17 Pro", "variant": "256GB", "quantity": 12, "unit_price": 115000.0, "currency": "INR"}],
            )

    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    try:
        setup = {"owner_phone": "+919876543210", "scan_all": True, "sources": []}
        pipeline = IntakePipeline(repo, FakeVisionClassifier())
        event = {
            "conversation_id": "179126040305838@lid",
            "sender_id": "179126040305838@lid",
            "phone": None,
            "message_id": "false_179126040305838@lid_img1",
            "text": "/9j/" + "QUJD" * 40,
            "identity_verified": True,
        }
        res = await pipeline.process(event, setup)
        assert res["status"] == "CLASSIFIED"
        assert seen_calls and seen_calls[0][2] == "image/jpeg"
        saved = await repo.get("messages", res["id"])
        assert "media_b64" not in saved["data"]
        assert saved["data"]["text"].startswith("[Image OCR]")
        assert (await pipeline.process(event, setup))["status"] == "DUPLICATE"
        assert len(seen_calls) == 1
        missing = {**event, "message_id": "false_179126040305838@lid_img2", "text": "[Media attachment]", "has_media": True, "media_type": "image"}
        pending = await pipeline.process(missing, setup)
        assert pending["status"] == "CAPTURED_REVIEW"
        assert (await repo.get("messages", pending["id"]))["data"]["reason"] == "MEDIA_BYTES_REQUIRED"
        assert await repo.get("offers", pending["id"]) is None
        assert len(seen_calls) == 1
        confirmed = await pipeline.process({**missing, "media_b64": "QUJD", "media_mime": "image/jpeg"}, setup)
        assert confirmed["status"] == "CLASSIFIED"
        assert confirmed["id"] == pending["id"]
        social = await pipeline.process({**missing, "message_id": "false_179126040305838@lid_img3", "media_b64": "U09DSUFM", "media_mime": "image/jpeg"}, setup)
        assert social["status"] == "IGNORED_SOCIAL"
        assert await repo.get("offers", social["id"]) is None
    finally:
        repo.close()


async def test_resolved_lid_phone_backfills_contact_profile_and_allows_country_timezone(tmp_path):
    from jarvis.pipeline import IntakePipeline

    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    try:
        setup = {"owner_phone": "+919876543210", "scan_all": True, "sources": []}
        ev_unmapped = {
            "conversation_id": "179126040305838@lid",
            "sender_id": "179126040305838@lid",
            "phone": None,
            "sender_name": "PR. RAHUL AJIT BURAD",
            "message_id": "false_179126040305838@lid_m1",
            "text": "WTB 5 iPhone 17 Pro",
            "identity_verified": True,
        }
        await IntakePipeline(repo, None).process(ev_unmapped, setup)
        assert repo.get_contact("179126040305838@lid")["phone"] is None

        # Now Contact Info panel resolves +919028794222 -> backfills contact_profiles and accepts bound event
        repo.save_lid_phone("179126040305838@lid", "+919028794222")
        assert repo.get_contact("179126040305838@lid")["phone"] == "+919028794222"
        assert evaluate_working_hours("+919028794222")["timezone"] in ("Asia/Kolkata", "Asia/Calcutta")

        ev_mapped = {
            **ev_unmapped,
            "phone": "+919028794222",
            "message_id": "false_179126040305838@lid_m2",
        }
        res2 = await IntakePipeline(repo, None).process(ev_mapped, setup)
        assert res2["status"] == "CAPTURED_REVIEW"
    finally:
        repo.close()
