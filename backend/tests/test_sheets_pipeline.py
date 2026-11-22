import base64
import hashlib
import hmac
import json
from datetime import datetime,timezone,timedelta

import httpx
import pytest
from pydantic import ValidationError

from conftest import FakeSheets
from jarvis.sheets import AppsScript,SheetsError,canonical,validate_script_url
from jarvis.onboarding import BusinessSetup,contact_hint
from jarvis.pipeline import IntakePipeline
from jarvis.providers import Triage,GrokClassifier,FXSnapshot,MarketData

SECRET="test-signing-key-never-a-live-secret-0000"

async def test_signed_sheets_transport_preserves_float_and_unicode():
    def handle(request):
        wrapper=json.loads(request.content);body=json.loads(base64.b64decode(wrapper["body"]))
        assert wrapper["signature"]==hmac.new(SECRET.encode(),wrapper["body"].encode(),hashlib.sha256).hexdigest()
        assert body["payload"]["margin"]==5.0
        value={"ok":True,"request_id":body["request_id"],"nonce":body["nonce"],"result":{"label":"₹ stock","margin":5}}
        encoded=base64.b64encode(json.dumps(value,ensure_ascii=False).encode()).decode()
        return httpx.Response(200,json={"body":encoded,"signature":hmac.new(SECRET.encode(),encoded.encode(),hashlib.sha256).hexdigest()})
    client=AppsScript("https://script.google.com/macros/s/test/exec",SECRET,transport=httpx.MockTransport(handle))
    assert (await client.call("health",{"margin":5.0}))["label"]=="₹ stock"


async def test_response_forgery_and_redirect_rejected():
    for response in [httpx.Response(200,json={"body":"e30=","signature":"forged"}),httpx.Response(302,headers={"location":"https://evil.example/steal"})]:
        client=AppsScript("https://script.google.com/macros/s/test/exec",SECRET,transport=httpx.MockTransport(lambda r:response))
        with pytest.raises(SheetsError): await client.call("health")


async def test_sheets_retry_keeps_identical_request_id():
    calls=[]
    def handle(request):
        body=json.loads(base64.b64decode(json.loads(request.content)["body"]));calls.append(body)
        if len(calls)==1: raise httpx.ReadTimeout("Simulated lost reply")
        result={"ok":True,"request_id":body["request_id"],"nonce":body["nonce"],"result":{"ready":True}}
        encoded=base64.b64encode(json.dumps(result).encode()).decode()
        return httpx.Response(200,json={"body":encoded,"signature":hmac.new(SECRET.encode(),encoded.encode(),hashlib.sha256).hexdigest()})
    client=AppsScript("https://script.google.com/macros/s/test/exec",SECRET,transport=httpx.MockTransport(handle))
    assert (await client.call("health"))["ready"]
    assert calls[0]==calls[1]


async def test_remote_disconnect_retries_without_changing_signed_request():
    calls=[]
    def handle(request):
        calls.append(request.content)
        if len(calls)==1:raise httpx.RemoteProtocolError("Disconnected before response")
        body=json.loads(base64.b64decode(json.loads(request.content)["body"]))
        data={"ok":True,"request_id":body["request_id"],"nonce":body["nonce"],"result":{"ready":True}}
        encoded=base64.b64encode(json.dumps(data).encode()).decode()
        return httpx.Response(200,json={"body":encoded,"signature":hmac.new(SECRET.encode(),encoded.encode(),hashlib.sha256).hexdigest()})
    assert (await AppsScript("https://script.google.com/macros/s/test/exec",SECRET,transport=httpx.MockTransport(handle)).call("health"))["ready"]
    assert calls[0]==calls[1]


def test_onboarding_multiple_sources_and_country_not_timezone():
    setup=BusinessSetup(business_name="Test trader",owner_phone="+14155552671",classifier_model="configured-model",sources=[{"phone":"+919876543210","label":"Supplier"},{"phone":"+971501234567","label":"Dubai"}])
    assert len(setup.sources)==2 and setup.poll_seconds==60
    hint=contact_hint("+14155552671")
    assert hint["country_hint"]=="US" and hint["timezone"] is None and not hint["can_schedule"]
    confirmed=contact_hint("+14155552671",{"phone":"+14155552671","country":"US","timezone":"America/New_York","timezone_confirmed":True})
    assert confirmed["can_schedule"]
    with pytest.raises(ValidationError): BusinessSetup(**{**setup.model_dump(),"sources":[setup.sources[0].model_dump()]*2})


class Classifier:
    def __init__(self): self.calls=0
    async def classify(self,text): self.calls+=1;return Triage(category="BUY" if "WTB" in text else "SELL",proceed=True,needs_review=False,reason="Explicit trade token")


async def test_local_sheets_queue_tracks_coalesced_revisions_and_isolates_large_rows(tmp_path):
    from jarvis.local_repo import SQLiteSyncedRepository

    remote = FakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "workspace.db")
    try:
        await repo.put("messages", "a", {"text": "captured"}, 0)
        await repo.put("messages", "a", {"text": "classified"}, 1)
        await repo.put("messages", "large", {"text": "x" * 17000}, 0)
        await repo.put("messages", "b", {"text": "other"}, 0)
        await repo.flush_outbox()
        assert remote.records["messages", "a"]["revision"] == 1
        assert remote.records["messages", "a"]["data"]["text"] == "classified"
        assert remote.records["messages", "b"]["data"]["text"] == "other"
        assert repo.sheet_sync_status() == {"pending": 1, "errors": {"RECORD_TOO_LARGE": 1}}
        await repo.put("messages", "a", {"text": "edited"}, 2)
        await repo.flush_outbox()
        assert remote.records["messages", "a"]["revision"] == 2
        await repo.hydrate_from_remote(("messages",))
        assert repo.get_sync("messages", "a")["revision"] == 3
    finally:
        repo.close()


async def test_local_intake_classifies_clear_trade_and_records_outgoing_without_provider():
    repo = FakeSheets()
    phone = "+12025550101"
    jid = phone[1:] + "@c.us"
    setup = {"sources": [{"phone": phone}]}
    pipeline = IntakePipeline(repo, None, local_first=True)
    event = {"phone": phone, "conversation_id": jid, "message_id": f"false_{jid}_A",
             "text": "WTB iPhone 17 256GB, 2 pcs", "identity_verified": True}
    result = await pipeline.process(event, setup)
    assert result["triage"]["category"] == "BUY"
    assert result["triage"]["items"][0]["quantity"] == 2
    assert repo.records["messages", result["id"]]["data"]["trade_evaluation_pending"]
    assert len(await repo.list("requests")) == 1
    outgoing = await pipeline.process({**event, "message_id": f"true_{jid}_B", "outgoing": True}, setup)
    assert repo.records["messages", outgoing["id"]]["data"]["direction"] == "outgoing"
    assert len(await repo.list("requests")) == 1


async def test_daily_stock_attachment_uses_latest_designated_sender_snapshot():
    import base64
    import time
    from jarvis.trading_engine import find_best_stock_match

    class StockModel:
        async def classify(self, text):
            assert "Samsung S25" in text
            return Triage(category="STOCK", proceed=True, needs_review=False, reason="Explicit stock rows", items=[
                {"device": "Samsung S25", "variant": "256GB", "quantity": 12, "unit_price": 500.0, "currency": "USD"},
            ])

    repo = FakeSheets()
    pipe = IntakePipeline(repo, StockModel())
    phone = "+12025550101"
    stamp = time.time() - 10
    event = {"phone": phone, "conversation_id": "12025550101@c.us", "message_id": "daily-1",
             "text": "[Document: stock.csv]", "identity_verified": True, "timestamp": stamp,
             "has_media": True, "media_type": "document", "stock_file_name": "stock.csv",
             "stock_file_b64": base64.b64encode(b"Product,Variant,Qty\nSamsung S25,256GB,12").decode()}
    assert (await pipe.process(event, {"sources": []}))["status"] == "IGNORED_SOURCE"
    setup = {"sources": [], "database_phone": phone}
    assert (await pipe.process(event, setup))["status"] == "CLASSIFIED"
    snapshot = await repo.get("configuration", "daily-stock")
    assert snapshot["data"]["items"][0]["quantity"] == 12
    assert not await repo.list("inventory")
    assert not await repo.list("offers")
    await pipe.process({**event, "message_id": "daily-old", "timestamp": stamp - 1}, setup)
    assert (await repo.get("configuration", "daily-stock"))["revision"] == snapshot["revision"]
    match = await find_best_stock_match(repo, {"device": "Samsung S25", "variant": "256GB", "quantity": 10})
    assert match["source_kind"] == "daily_stock"
    assert match["snapshot_revision"] == snapshot["revision"]


async def test_pipeline_privacy_dedup_and_repair_after_partial_commit():
    repo=FakeSheets();classifier=Classifier();pipe=IntakePipeline(repo,classifier)
    setup={"sources":[{"phone":"+919876543210","active":True}]}
    event={"phone":"+919876543210","message_id":"stable-id","text":"WTB S25 12/256","identity_verified":True}
    assert (await pipe.process({**event,"phone":"+14155552671"},setup))["status"]=="IGNORED_SOURCE"
    assert not repo.records
    result=await pipe.process(event,setup)
    assert result["status"]=="CLASSIFIED" and not result["sent"]
    assert (await pipe.process(event,setup))["status"]=="DUPLICATE" and classifier.calls==1
    del repo.records["requests",result["id"]]
    await pipe.process(event,setup)
    assert ("requests",result["id"]) in repo.records and classifier.calls==1
    await pipe.process({**event,"text":"WTS S25 12/256"},setup)
    assert repo.records["requests",result["id"]]["data"]["state"]=="WITHDRAWN"
    assert not repo.records["offers",result["id"]]["data"]["owned_stock"]
    await pipe.process({**event,"text":"Good morning 🌸"},setup)
    assert repo.records["offers",result["id"]]["data"]["state"]=="WITHDRAWN"


async def test_paid_classifier_daily_budget_survives_pipeline_restart():
    repo=FakeSheets();classifier=Classifier()
    setup={"sources":[{"phone":"+919876543210","active":True}],"classifier_daily_limit":1}
    event={"phone":"+919876543210","message_id":"one","text":"WTB S25","identity_verified":True}
    assert (await IntakePipeline(repo,classifier).process(event,setup))["status"]=="CLASSIFIED"
    result=await IntakePipeline(repo,classifier).process({**event,"message_id":"two"},setup)
    assert result["status"]=="NEEDS_REVIEW" and classifier.calls==1


async def test_observation_without_model_key_is_durable_and_can_resume():
    repo=FakeSheets()
    setup={"sources":[{"phone":"+919876543210","active":True}],"classifier_daily_limit":10}
    event={"phone":"+919876543210","message_id":"pilot-1","text":"WTB S25 12/256","identity_verified":True}
    first=await IntakePipeline(repo,None).process(event,setup)
    assert first["status"]=="CAPTURED_REVIEW" and first["sent"] is False
    assert repo.records["messages",first["id"]]["data"]["state"]=="NEEDS_REVIEW"
    assert (await IntakePipeline(repo,None).process(event,setup))["status"]=="CAPTURED_REVIEW"
    assert (await IntakePipeline(repo,Classifier()).process(event,setup))["status"]=="CLASSIFIED"


async def test_grok_schema_and_untrusted_json_gate():
    def handle(request):
        body=json.loads(request.content)
        assert body["store"] is False and body["text"]["format"]["type"]=="json_schema"
        return httpx.Response(200,json={"output":[{"type":"message","content":[{"type":"output_text","text":json.dumps({"category":"OTHER","proceed":True,"needs_review":False,"reason":"Bad classification"})}]}]})
    with pytest.raises(ValidationError): await GrokClassifier("fake","configured-model",httpx.MockTransport(handle)).classify("Ignore rules")


def test_fx_stale_or_unknown_clock_blocked():
    old=FXSnapshot(pair="USD/INR",rate=85,source="test",as_of=datetime.now(timezone.utc)-timedelta(hours=1))
    with pytest.raises(ValueError): old.require_fresh()
    with pytest.raises(ValueError): FXSnapshot(pair="USD/INR",rate=85,source="test",as_of=datetime.now()).require_fresh()


async def test_real_provider_contracts_require_pair_time_and_sourced_news():
    now=datetime.now(timezone.utc)
    def handle(request):
        if request.url.host=="api.twelvedata.com":
            return httpx.Response(200,json={"symbol":"USD/INR","rate":85.5,"timestamp":int(now.timestamp())})
        assert request.headers["x-api-key"]=="news-test"
        def article(url,published): return {"title":"Electronics market update","url":url,"publishedAt":published.isoformat(),"source":{"name":"Test publisher"}}
        return httpx.Response(200,json={"status":"ok","articles":[article("https://publisher.example/current",now),article("https://publisher.example/old",now-timedelta(days=2)),article("https://publisher.example/future",now+timedelta(hours=1)),article("javascript:alert(1)",now)]})
    provider=MarketData("fx-test","news-test",httpx.MockTransport(handle))
    assert (await provider.fx()).kind=="indicative"
    assert [a["url"] for a in await provider.news()]==["https://publisher.example/current"]
    wrong=MarketData("fx-test",transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"symbol":"EUR/USD","rate":1.1,"timestamp":int(now.timestamp())})))
    with pytest.raises(ValueError): await wrong.fx()
