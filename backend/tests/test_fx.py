from datetime import datetime, timezone

import httpx
import pytest
from conftest import FakeSheets
from jarvis.fx import FXUnavailable, convert_reference, preferred_currency
from jarvis.pipeline import IntakePipeline
from jarvis.providers import Triage


async def test_reference_conversion_checks_pair_and_rounds_by_currency():
    quote="JPY"
    def response(request):
        assert request.url.path=="/v2/rate/USD/JPY"
        return httpx.Response(200,json={"base":"USD","quote":quote,"rate":158.21,
            "date":datetime.now(timezone.utc).date().isoformat()})
    transport=httpx.MockTransport(response)
    assert (await convert_reference("825","USD","JPY",transport=transport))["converted"]=="130523"
    quote="AED"
    with pytest.raises(FXUnavailable):
        await convert_reference("825","USD","JPY",transport=transport)


async def test_whatsapp_buy_keeps_original_budget_and_adds_review_conversions(monkeypatch):
    async def convert(amount,base,quote):
        return {"base":base,"quote":quote,"amount":str(amount),"converted":"100","kind":"reference"}
    monkeypatch.setattr("jarvis.fx.convert_reference",convert)
    class Model:
        async def classify(self,text):
            return Triage.model_validate({"category":"BUY","proceed":True,"needs_review":False,"reason":"WTB",
                "reply_currency":"EUR","items":[{"device":"iPhone","quantity":10,"unit_price":825.0,"currency":"USD"}]})
    repo=FakeSheets()
    phone="+971501234567"
    conversation="971501234567@c.us"
    assert preferred_currency(phone)["currency"]=="AED"
    assert preferred_currency(None)["currency"]=="USD"
    event={"phone":phone,"conversation_id":conversation,"message_id":"false_"+conversation+"_fx",
           "text":"WTB 10 iPhone, USD 825 each, quote in EUR","identity_verified":True}
    result=await IntakePipeline(repo,Model()).process(event,{"sources":[],"scan_all":True})
    request=(await repo.get("requests",result["id"]))["data"]
    assert request["state"]=="NEEDS_REVIEW" and result["sent"] is False
    assert request["items"][0]["unit_price"]==825.0
    pricing=request["buyer_pricing"]
    assert pricing["currency"]=="EUR" and pricing["source"]=="buyer_requested"
    assert [r["quote"] for r in pricing["conversions"]]==["USD","EUR"]


async def test_world_rates_and_pair_history_compute_sorted_deltas_and_ma():
    from jarvis.fx import pair_history, world_currencies, world_rates

    def respond(request):
        if request.url.path == "/v2/currencies":
            return httpx.Response(200, json=[
                {"iso_code": "INR", "name": "Indian Rupee", "symbol": "₹", "type": "currency"},
                {"iso_code": "AED", "name": "UAE Dirham", "symbol": "د.إ", "type": "currency"},
                {"iso_code": "EUR", "name": "Euro", "symbol": "€", "type": "currency"},
                {"iso_code": "XAU", "name": "Gold", "symbol": "XAU", "type": "metal"},
                {"iso_code": "AAVE", "name": "Aave", "symbol": "AAVE", "type": "crypto"},
            ])
        if request.url.path == "/v2/rates" and "from" not in request.url.params:
            return httpx.Response(200, json=[
                {"date": "2026-11-10", "base": "USD", "quote": "INR", "rate": 94.0},
                {"date": "2026-11-10", "base": "USD", "quote": "AED", "rate": 3.6725},
                {"date": "2026-11-10", "base": "USD", "quote": "EUR", "rate": 0.92},
                {"date": "2026-11-10", "base": "USD", "quote": "XAU", "rate": 0.00038},
            ])
        if request.url.path == "/v2/rates" and "from" in request.url.params:
            rows = []
            for idx, r in enumerate([92.0, 92.5, 93.0, 93.5, 94.0]):
                rows.append({"date": f"2026-11-0{idx+1}", "base": "USD", "quote": "INR", "rate": r})
                rows.append({"date": f"2026-11-0{idx+1}", "base": "USD", "quote": "EUR", "rate": 0.90 + idx * 0.005})
            return httpx.Response(200, json=rows)
        return httpx.Response(404)

    transport = httpx.MockTransport(respond)
    catalog = await world_currencies(include_metals=True, transport=transport)
    assert [c["code"] for c in catalog] == ["INR", "AED", "EUR", "XAU"]

    board = await world_rates("USD", symbols=["INR", "EUR"], days=30, compare_quotes=["INR", "EUR"], transport=transport)
    assert board["base"] == "USD"
    assert [r["code"] for r in board["rates"]] == ["INR", "EUR", "AED", "XAU"]
    inr = board["rates"][0]
    assert inr["pinned"] is True
    assert inr["change_1d_pct"] == round((94.0 / 93.5 - 1) * 100, 3)
    assert len(inr["sparkline"]) == 5
    assert [c["code"] for c in board["comparison"]] == ["INR", "EUR"]

    hist = await pair_history("USD", "INR", days=30, transport=transport)
    assert hist["latest_rate"] == 94.0
    assert hist["low"] == 92.0 and hist["high"] == 94.0
    assert len(hist["points"]) == 5
    assert hist["points"][-1]["ma7"] == 93.0
