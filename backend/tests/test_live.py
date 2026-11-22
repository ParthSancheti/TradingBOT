import asyncio
import json
import pytest
from conftest import FakeSheets,FakeVault
from jarvis.live import LiveSession
from jarvis.workspace import AssistantContext


async def test_live_keeps_standard_key_backend_only_and_closes_transport(monkeypatch):
    sent=[];events=[];connections=[]
    class Socket:
        async def send(self,data):sent.append(json.loads(data))
        async def recv(self):return '{"setupComplete":{}}'
        def __aiter__(self):return self
        async def __anext__(self):await asyncio.Event().wait()
        async def close(self):self.closed=True
    socket=Socket()
    async def connect(url,**options):connections.append((url,options));return socket
    monkeypatch.setattr("jarvis.live.connect",connect)
    vault=FakeVault();key="fake-key-do-not-expose-123456789"
    vault.set("gemini_connection",json.dumps({"keys":[key]}))
    class SlowSheets:
        async def get(self,*args):await asyncio.Event().wait()
        list=get
    sheets=SlowSheets()
    workspace=AssistantContext(sheets)
    live=LiveSession(sheets,vault,lambda e,d:events.append((e,d)),workspace)
    response=await asyncio.wait_for(live.start("stock"),1)
    assert not response["workspace_context"]
    assert key not in str(response) and key not in connections[0][0]
    assert connections[0][1]["additional_headers"]["x-goog-api-key"]==key
    assert sent[0]["setup"]["generationConfig"]["responseModalities"]==["AUDIO"]
    declarations = sent[0]["setup"]["tools"][0]["functionDeclarations"]
    assert [tool["name"] for tool in declarations] == ["lookup_inventory"]
    await live.text("Summarize demand")
    assert sent[-1]["realtimeInput"]["text"]=="Summarize demand"
    with pytest.raises(ValueError):await live.audio("invalid")
    await live.stop();assert socket.closed and live.socket is None
    workspace.clear()


async def test_live_resumes_on_goaway_until_explicit_stop(monkeypatch):
    setups=[]
    events=[]
    reconnected=asyncio.Event()

    class Socket:
        async def send(self,data):setups.append(json.loads(data)["setup"])
        async def recv(self):return '{"setupComplete":{}}'
        def __aiter__(self):return self
        async def __anext__(self):
            if len(setups)==1:
                return '{"sessionResumptionUpdate":{"resumable":true,"newHandle":"resume-token"},"goAway":{"timeLeft":"5s"}}'
            await asyncio.Event().wait()
        async def close(self):pass

    async def connect(*args,**kwargs):return Socket()
    def emit(event,data):
        events.append(data)
        if data["type"]=="reconnected":reconnected.set()

    monkeypatch.setattr("jarvis.live.connect",connect)
    vault=FakeVault()
    vault.set("gemini_connection",json.dumps({"keys":["fake-gemini-key"]}))
    live=LiveSession(None,vault,emit)
    await live.start()
    try:
        await asyncio.wait_for(reconnected.wait(),2)
        assert setups[0]["sessionResumption"]=={}
        assert setups[1]["sessionResumption"]=={"handle":"resume-token"}
        assert setups[1]["contextWindowCompression"]=={"slidingWindow":{}}
        assert [x["type"] for x in events]==["reconnecting","reconnected"]
    finally:
        await live.stop()
    assert live.socket is None and live.receiver is None
    assert events[-1]["type"]=="closed"
