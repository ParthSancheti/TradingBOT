import json
import httpx
import pytest
from conftest import FakeVault,FakeSheets
from jarvis.gemini import GeminiClient,GeminiError
from jarvis.desktop_service import DesktopService
from jarvis.workspace import insights


def vault():
    v=FakeVault();v.set("gemini_connection",json.dumps({"model":"gemini-3.1-flash-lite","keys":["key-one-not-real-123","key-two-not-real-456"]}));return v


async def test_quota_rotates_keys_and_pauses_only_when_all_keys_exhausted():
    calls=[];v=vault()
    def respond(request):
        calls.append(request.headers["x-goog-api-key"])
        return httpx.Response(429,headers={"retry-after":"90"})
    client=GeminiClient(v,httpx.MockTransport(respond))
    with pytest.raises(GeminiError) as error:await client.classify("WTB phone")
    assert error.value.code=="MODEL_QUOTA"
    # Key 1 cooled down, immediately rotated to Key 2, and both exhausted -> pool cooldown active
    assert calls==["key-one-not-real-123","key-two-not-real-456"]
    with pytest.raises(GeminiError):await client.classify("WTB phone")
    assert len(calls)==2 and client.status()["paused_until"]>0
    assert "keys" not in client.status()

async def test_quota_on_first_key_fails_over_to_second_key():
    calls=[];v=vault()
    result_payload={"category":"BUY","proceed":True,"needs_review":False,"reason":"Explicit request","items":[{"device":"iPhone 17","quantity":5}]}
    def respond(request):
        key=request.headers["x-goog-api-key"]
        calls.append(key)
        if key=="key-one-not-real-123":
            return httpx.Response(429,headers={"retry-after":"90"})
        return httpx.Response(200,json={"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":json.dumps(result_payload)}]}}]})
    client=GeminiClient(v,httpx.MockTransport(respond))
    res=await client.classify("WTB 5 iPhone 17")
    assert res.category=="BUY" and calls==["key-one-not-real-123","key-two-not-real-456"]
    assert client.status()["active_key_count"]==1 and client.status()["paused_until"]==0


async def test_gemini_structured_extraction_and_credential_backup():
    calls=[]
    def respond(request):
        calls.append(request)
        body=json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"]=="application/json"
        schema=body["generationConfig"]["responseJsonSchema"]
        assert schema["properties"]["items"]["items"]["properties"]["quantity"]["type"]==["integer","null"]
        assert "$defs" not in schema
        if len(calls)==1:return httpx.Response(403)
        result={"category":"BUY","proceed":True,"needs_review":False,"reason":"Explicit request","items":[{"device":"Samsung S25","quantity":10}]}
        return httpx.Response(200,json={"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":json.dumps(result)}]}}]})
    result=await GeminiClient(vault(),httpx.MockTransport(respond)).classify("WTB S25 10 pcs")
    assert result.items[0].quantity==10 and len(calls)==2
    assert calls[0].url.query==b"" and calls[0].headers["x-goog-api-key"]=="key-one-not-real-123"


async def test_stream_only_publishes_answer_text_and_requires_completion():
    def packet(text, *, thought=False, reason=None):
        candidate={"content":{"parts":[{"text":text,"thought":thought}]}}
        if reason:candidate["finishReason"]=reason
        return "data: "+json.dumps({"candidates":[candidate]})+"\n\n"
    frames=packet("private",thought=True)+packet("Jarvis ")+packet("online.",reason="STOP")
    chunks=[]
    def respond(request):
        assert request.url.path.endswith(":streamGenerateContent")
        assert request.url.params["alt"]=="sse"
        return httpx.Response(200,text=frames)
    client=GeminiClient(vault(),httpx.MockTransport(respond))
    assert await client.generate("Test","Say hello",on_chunk=chunks.append)=="Jarvis online."
    assert chunks==["Jarvis ","online."]
    frames=packet("Partial")
    with pytest.raises(GeminiError,match="complete answer"):
        await client.generate("Test","Say hello",on_chunk=chunks.append)


async def test_gemini_overload_uses_lite_fallback_and_leaves_preferred_model_saved():
    calls=[]
    frames='data: {"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"Jarvis online."}]}}]}\n\n'
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(503) if len(calls)==1 else httpx.Response(200,text=frames)
    retries=[]
    chunks=[]
    client=GeminiClient(vault(),httpx.MockTransport(respond))
    assert await client.generate("Test","Say hello",on_chunk=chunks.append,on_retry=retries.append)=="Jarvis online."
    assert calls==["/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent","/v1beta/models/gemini-3.5-flash-lite:streamGenerateContent"]
    assert retries==["gemini-3.5-flash-lite"] and chunks==["Jarvis online."]
    assert client.status()["model"]=="gemini-3.1-flash-lite"
    assert client.status()["last_model"]=="gemini-3.5-flash-lite"


async def test_empty_stream_retries_once_without_streaming():
    calls=[]
    def respond(request):
        calls.append(request.url.path.rsplit(":",1)[1])
        if len(calls)==1:return httpx.Response(200,text='data: {"candidates":[{"finishReason":"STOP"}]}\n\n')
        return httpx.Response(200,json={"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"Jarvis online."}]}}]})
    chunks=[]
    client=GeminiClient(vault(),httpx.MockTransport(respond))
    assert await client.generate("Test","Say hello",on_chunk=chunks.append)=="Jarvis online."
    assert calls==["streamGenerateContent","generateContent"] and chunks==["Jarvis online."]


async def test_demand_counts_only_classified_buy_records_and_keeps_inventory_separate():
    repo=FakeSheets()
    for i,category in enumerate(("BUY","BUY","SELL")):
        await repo.put("messages",str(i),{"state":"CLASSIFIED","triage":{"category":category,"proceed":True,"items":[{"device":"S25","quantity":10}]}},0)
    await repo.put("inventory","owned",{"device":"S25","quantity":4},0)
    result=await insights(repo)
    assert result["demand"][0]["messages"]==2 and result["demand"][0]["units"]==20
    assert result["inventory"][0]["data"]["quantity"]==4 and not result["complete"]


async def test_provider_limit_and_inventory_revision_gate():
    app=DesktopService(vault=FakeVault(),repository=FakeSheets())
    keys=["fake-key-not-real-"+str(i) for i in range(6)]
    answer=await app.dispatch({"id":1,"method":"provider/save","params":{"keys":keys}})
    assert answer["error"]["code"]=="INVALID_INPUT" and keys[0] not in str(answer)
    item={"id":"stock","device":"S25","quantity":4,"expected_revision":0}
    assert (await app.inventory_save(item))["revision"]==1
    response=await app.dispatch({"id":2,"method":"inventory/save","params":item})
    assert response["error"]["code"]=="REVISION_CONFLICT"


async def test_durable_capture_is_classified_after_a_key_is_added(monkeypatch):
    from jarvis.runner import Runner
    from jarvis.providers import Triage
    repo=FakeSheets();v=FakeVault()
    app=DesktopService(vault=v,repository=repo)
    await app.save_setup({"owner_phone":"+919876543210","stock_phones":["+14155552671"]})
    runner=Runner(v,repo);runner.active=True
    event={"phone":"+14155552671","conversation_id":"14155552671@c.us","sender_id":"14155552671@c.us","message_id":"false_14155552671@c.us_A1","text":"WTB S25 10 pcs","identity_verified":True}
    result=await runner._handle({"operation":"observations","events":[event]})
    assert result["results"][0]["status"]=="CAPTURED_REVIEW"
    class Model:
        def __init__(self,*a):pass
        def status(self):return {"configured":True,"paused_until":0}
        async def classify(self,text):return Triage(category="BUY",proceed=True,needs_review=False,reason="WTB")
    monkeypatch.setattr("jarvis.runner.GeminiClient",Model)
    await runner.classify_pending()
    saved=await repo.list("messages",10)
    assert saved[0]["data"]["state"]=="CLASSIFIED"


async def test_assistant_history_stays_within_sheet_cell_limit_and_request_replay_is_free(monkeypatch):
    from jarvis.workspace import assistant
    calls=[]
    class Model:
        def __init__(self,*a):pass
        def status(self):return {"configured":True}
        async def generate(self,*a,**kw):calls.append(1);return "answer "*1800
    monkeypatch.setattr("jarvis.workspace.GeminiClient",Model)
    repo=FakeSheets();v=FakeVault()
    for i in range(4):await assistant(repo,v,"question "*200,"request-number-000"+str(i))
    history=await repo.get("tasks","desktop-assistant")
    assert len(json.dumps(history["data"],ensure_ascii=False))<16000
    await assistant(repo,v,"question "*200,"request-number-0003")
    assert len(calls)==4


async def test_desktop_assistant_replies_while_sheet_is_unavailable(monkeypatch):
    import asyncio
    class OfflineSheets:
        async def get(self,*args):await asyncio.Event().wait()
        list=get
    class Model:
        def __init__(self,*args):pass
        def status(self):return {"configured":True}
        def reserve_manual_call(self):pass
        async def generate(self,prompt,content,*,max_tokens,on_chunk,on_retry):
            context=json.loads(content)["workspace"]
            assert "inventory" in context["unavailable"]
            on_chunk("Jarvis online.")
            return "Jarvis online."
    monkeypatch.setattr("jarvis.workspace.GeminiClient",Model)
    app=DesktopService(vault=vault(),repository=OfflineSheets())
    events=[]
    app.emit=lambda event,data:events.append(data["stage"])
    try:
        result=await asyncio.wait_for(app.assistant_ask({
            "question":"Are you online?", "request_id":"offline-sheet-test-001",
        }),1)
        assert result["answer"]=="Jarvis online." and result["history_state"]=="pending"
        assert events==["generating","chunk"]
    finally:
        app.assistant_context.clear()


async def test_new_chat_keeps_other_conversation_out_of_model_context(monkeypatch):
    from jarvis.workspace import assistant
    contexts=[]
    class Model:
        def __init__(self,*args):pass
        def status(self):return {"configured":True}
        async def generate(self,prompt,content,**kwargs):
            contexts.append(json.loads(content)["workspace"]["history"])
            return "Reply"
    monkeypatch.setattr("jarvis.workspace.GeminiClient",Model)
    repo=FakeSheets()
    v=vault()
    await assistant(repo,v,"Stock planning","first-conversation-turn",chat_id="chat-a")
    await assistant(repo,v,"FX rates","second-conversation-turn",chat_id="chat-b")
    continued=await assistant(repo,v,"Continue planning","third-conversation-turn",chat_id="chat-a")
    assert contexts==[[],[],[{"question":"Stock planning","answer":"Reply"}]]
    assert continued["chat_id"]=="chat-a" and continued["chat_title"]=="Stock planning"
    with pytest.raises(ValueError,match="Conflicting"):
        await assistant(repo,v,"Stock planning","first-conversation-turn",chat_id="chat-b")
