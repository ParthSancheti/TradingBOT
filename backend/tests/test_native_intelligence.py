from datetime import datetime,timezone,timedelta
import json
import pytest
from conftest import FakeSheets,FakeVault
from jarvis.intelligence import estimate,alert,match_opportunities,DeskSettings
from jarvis.market_watch import MarketWatch


def series(count=80):
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    return [{"at":(start+timedelta(hours=i)).isoformat(),"rate":85+i*.01} for i in range(count)]


def test_forecast_holdout_cannot_be_reported_as_a_guaranteed_error_bound():
    original=series();a=estimate(original,"1h")
    altered=series();altered[-1]["rate"]=120;b=estimate(altered,"1h")
    assert a["model"]==b["model"]
    assert b["max_error_pct"]>10 and not b["target_met_on_test"]
    assert not a["guaranteed_error_bound"] and a["test_samples"]==20
    assert a["target_at"]>a["as_of"] and a["range_low"]<=a["predicted_rate"]<=a["range_high"]
    assert not estimate(series(20))["available"]


def test_sheet_url_normalization_rejects_host_spoofing_and_keeps_threshold_units():
    s=DeskSettings(sheet_url="https://docs.google.com/spreadsheets/d/test123/edit?gid=4#gid=4")
    assert s.sheet_url.endswith("/test123/edit") and s.fx_threshold_inr==.5
    with pytest.raises(ValueError):DeskSettings(sheet_url="https://docs.google.com.evil.test/spreadsheets/d/a/edit")


async def test_match_is_manual_deduplicated_and_does_not_reserve_or_buy_stock():
    repo=FakeSheets()
    for rid,category,phone in (("buyer","BUY","+14155552671"),("seller","SELL","+919876543210")):
        await repo.put("messages",rid,{"state":"CLASSIFIED","phone":phone,"triage":{"proceed":True,"category":category,"items":[{"device":"iPhone 17","variant":"256GB","quantity":10}]}},0)
    first=await match_opportunities(repo);again=await match_opportunities(repo)
    assert first[0]["id"]==again[0]["id"] and again[0]["revision"]==1
    assert first[0]["data"]["details"]["needs_manual_review"]
    assert not await repo.list("inventory")


async def test_seller_alert_counts_unique_buyers_and_flags_existing_daily_stock():
    import time

    repo = FakeSheets()
    product = {"device": "iPhone 17", "variant": "256GB", "quantity": 10}
    await repo.put("configuration", "daily-stock", {"source_timestamp": time.time(), "items": [product]}, 0)
    for i in range(11):
        phone = f"+120255501{i % 10:02d}"
        await repo.put("messages", f"buyer-{i}", {"state": "CLASSIFIED", "phone": phone, "triage": {
            "proceed": True, "category": "BUY", "items": [product, product],
        }}, 0)
    await repo.put("messages", "seller", {"state": "CLASSIFIED", "phone": "+12025550199", "triage": {
        "proceed": True, "category": "SELL", "items": [product],
    }}, 0)
    alerts = await match_opportunities(repo)
    assert len(alerts) == 1
    assert alerts[0]["data"]["details"]["buyers_count"] == 10
    assert alerts[0]["data"]["details"]["already_in_stock"] is True
    assert (await match_opportunities(repo))[0]["id"] == alerts[0]["id"]


async def test_fx_alert_tracks_accumulated_threshold_and_hourly_news_does_not_repeat(monkeypatch):
    repo=FakeSheets();vault=FakeVault();vault.set("fx_key","not-a-real-key")
    now=datetime.now(timezone.utc)
    await repo.put("news","latest",{"checked_at":now.isoformat(),"articles":[],"seen_urls":[]},0)
    counter=iter((85,85.2,85.4,85.6,85.7))
    class Provider:
        def __init__(self,*a):pass
        async def fx(self):
            price=next(counter)
            return {"rate":price,"source":"test intraday","as_of":str(price),"kind":"indicative"}
        async def news(self):raise AssertionError("Hourly news scan ran early")
    monkeypatch.setattr("jarvis.market_watch.MarketData",Provider)
    watch=MarketWatch(repo,vault)
    for _ in range(5):await watch.refresh()
    notifications=[r for r in await repo.list("tasks",20) if r["data"].get("kind")=="notification"]
    assert len(notifications)==1 and notifications[0]["data"]["details"]["delta_inr"]==pytest.approx(.6)


async def test_sourcing_review_action_does_not_execute_a_purchase():
    from jarvis.desktop_service import DesktopService
    repo=FakeSheets();app=DesktopService(FakeVault(),repo)
    row=await alert(repo,"stock_match","unique","Match","Review",{"device":"S25"})
    result=await app.alert_action({"id":row["id"],"action":"save_task"})
    task=await repo.get("tasks",result["data"]["sourcing_task"])
    assert task["data"]["state"]=="MANUAL_REVIEW" and task["data"]["purchase_executed"] is False
    await app.alert_action({"id":row["id"],"action":"save_task"})
    assert len(await repo.list("tasks",20))==2


async def test_hourly_news_window_excludes_old_future_and_repeated_articles(monkeypatch):
    repo=FakeSheets();v=FakeVault();v.set("news_key","fake-news-key")
    now=datetime.now(timezone.utc)
    def article(name,when):return {"title":name,"url":"https://publisher.test/"+name,"published_at":when.isoformat(),"source":"test"}
    articles=[article("new",now-timedelta(minutes=10)),article("old",now-timedelta(hours=2)),article("future",now+timedelta(minutes=10))]
    calls=[]
    class Provider:
        def __init__(self,*a):pass
        async def news(self):calls.append(1);return articles
    async def quote():return {"rate":85,"source":"test","as_of":now.isoformat(),"kind":"reference"}
    async def past(_):return [{"at":(now-timedelta(days=80-i)).isoformat(),"rate":85+i*.01} for i in range(80)],"1day"
    monkeypatch.setattr("jarvis.market_watch.MarketData",Provider)
    monkeypatch.setattr("jarvis.market_watch.public_reference_fx",quote)
    monkeypatch.setattr("jarvis.market_watch.history",past)
    watch=MarketWatch(repo,v)
    await watch.refresh();await watch.refresh()
    news=await repo.get("news","latest")
    assert [a["title"] for a in news["data"]["articles"]]==["new"] and len(calls)==1
    await watch.refresh(force=True)
    news = await repo.get("news", "latest")
    assert news["data"]["articles"] == []
    assert [a["title"] for a in news["data"]["recent_articles"]] == ["new", "old"]


async def test_contextual_assistant_receives_forecast_alerts_and_current_screen(monkeypatch):
    from jarvis.workspace import assistant
    repo=FakeSheets();v=FakeVault();captured=[]
    await repo.put("fx","forecast",{"predicted_rate":86,"available":True},0)
    await alert(repo,"fx_move","context-test","FX movement","Review")
    class Model:
        def __init__(self,*a):pass
        def status(self):return {"configured":True}
        async def generate(self,prompt,data,**kwargs):captured.append(json.loads(data));return "Saved forecast is 86."
    monkeypatch.setattr("jarvis.workspace.GeminiClient",Model)
    await assistant(repo,v,"What is the forecast?","context-request-12345",page="market")
    context=captured[0]["workspace"]
    assert context["forecast"]["predicted_rate"]==86 and context["current_screen"]=="market"
    assert context["recent_alerts"][0]["category"]=="fx_move"
