import json
import httpx
from conftest import FakeSheets, FakeVault
from jarvis import voice
from jarvis.desktop_service import DesktopService
from jarvis.gemini import GeminiClient
from jarvis.live import LiveSession
from jarvis.runner import Runner


def make_service():
    vault = FakeVault()
    vault.set("bridge_secret", "test-only-secret")
    vault.set("gemini_connection", json.dumps({"model": "gemini-3.5-flash-lite", "keys": ["test-key-0001"]}))
    repo = FakeSheets()
    runner = Runner(vault, repo)
    return DesktopService(vault, repo, runner, opener=lambda: None, cache_ttl=25.0), repo


def test_clean_speech_text_formats_currency_and_trade_acronyms():
    raw = "HEARD: price check\n**WTB** S25: ₹93,800 → $1,000 [Sheet](https://example.com) ```py\nx=1\n```"
    spoken = voice.clean_speech_text(raw)
    assert "HEARD:" not in spoken
    assert "93,800 rupees" in spoken
    assert "1,000 dollars" in spoken
    assert "buy request" in spoken
    assert " to " in spoken
    assert "*" not in spoken and "https://" not in spoken


async def test_speak_audio_prefers_google_tts_hindlish_and_falls_back_to_wav(monkeypatch):
    voice.reset_tts_cooldown()
    assert voice.has_hindlish("Bhai aaj dollar ka rate kya hai yaar?") is True
    assert voice.has_hindlish("Check Samsung S25 stock levels") is False

    async def ok_gem(text, voice_name, *, vault=None, rate=1):
        assert voice_name == "Charon"
        return b"RIFF-google-studio-wav", "audio/wav"

    gtts_called = []

    async def fail_if_gtts(text, lang="auto"):
        gtts_called.append((text, lang))
        return b"ID3-robotic-gtts"

    monkeypatch.setattr(voice, "_gemini_tts_wav", ok_gem)
    monkeypatch.setattr(voice, "_google_tts_classic_mp3", fail_if_gtts)
    monkeypatch.setattr(voice, "_edge_tts_mp3", lambda *a, **kw: None)
    monkeypatch.setattr(voice, "_synthesize_wav_sync", lambda *a, **kw: b"RIFF-fallback")

    b64, mime = await voice.speak_audio_base64("Bhai S25 rate batao.", voice_id="default", rate=1, vault=FakeVault())
    assert mime == "audio/wav" and b64
    assert not gtts_called

    async def no_gem(*a, **kw):
        return None

    async def ok_edge(text, v, rate=1):
        assert v == "hi-IN-MadhurNeural"
        return b"ID3-edge-hindlish"

    monkeypatch.setattr(voice, "_gemini_tts_wav", no_gem)
    monkeypatch.setattr(voice, "_edge_tts_mp3", ok_edge)
    # Default Studio voice must NEVER switch to robotic translate.google.com when Edge Neural works
    b64_e, mime_e = await voice.speak_audio_base64("Yaar kitna stock bacha hai?", voice_id="default", rate=1)
    assert mime_e == "audio/mpeg" and b64_e
    assert not gtts_called

    # Explicit gtts:auto still works only when manually chosen
    b64_g, mime_g = await voice.speak_audio_base64("Bhai S25 rate batao.", voice_id="gtts:auto", rate=1)
    assert mime_g == "audio/mpeg" and b64_g and len(gtts_called) == 1

    async def no_edge(text, v, rate=1):
        return None

    monkeypatch.setattr(voice, "_edge_tts_mp3", no_edge)
    b64_fb, mime_fb = await voice.speak_audio_base64("Offline fallback check.", voice_id="default", rate=1)
    assert mime_fb == "audio/wav" and b64_fb


async def test_voice_turn_renderer_prevents_dual_voice_switching(monkeypatch):
    voice.reset_tts_cooldown()
    used_edge_voices = []

    async def no_gem(*a, **kw):
        return None

    async def edge_bytes(text, v, rate=1):
        used_edge_voices.append(v)
        return b"ID3-edge-chunk"

    monkeypatch.setattr(voice, "_gemini_tts_wav", no_gem)
    monkeypatch.setattr(voice, "_edge_tts_mp3", edge_bytes)

    renderer = voice.VoiceTurnRenderer(voice_id="default", rate=1, vault=FakeVault())
    # Clause 1 is English -> locks en-IN-PrabhatNeural; Clause 2 has Hindlish -> MUST keep exact same speaker!
    c1, m1 = await renderer.synthesize("Total owned stock is 14 devices.")
    c2, m2 = await renderer.synthesize("Bhai dollar rate 93.80 hai.")
    assert c1 and m1 == "audio/mpeg"
    assert c2 and m2 == "audio/mpeg"
    assert used_edge_voices == ["en-IN-PrabhatNeural", "en-IN-PrabhatNeural"]


async def test_gemini_tts_key_rotation_and_cooldown(monkeypatch):
    # Wait-on-429 key rotation + cooldown lock avoids rapid turn-to-turn oscillation
    voice.reset_tts_cooldown()
    calls = []
    vault = FakeVault()
    vault.set("gemini_connection", json.dumps({"model": "gemini-3.5-flash-lite", "keys": ["key-1-000000000001", "key-2-000000000002"]}))

    class FakeResp:
        def __init__(self, code, data=None):
            self.status_code = code
            self.is_success = 200 <= code < 300
            self._data = data or {}
        def json(self):
            return self._data

    class FakeHttp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers=None, json=None):
            calls.append(headers["x-goog-api-key"])
            if headers["x-goog-api-key"] == "key-1-000000000001":
                return FakeResp(429)
            return FakeResp(200, {"candidates": [{"content": {"parts": [{"inlineData": {"data": "AAAA", "mimeType": "audio/L16;rate=24000"}}]}}]})

    monkeypatch.setattr("jarvis.gemini.GeminiClient._http_client", lambda self, t=10: FakeHttp())
    wav = await voice._gemini_tts_wav("Hello world", "Charon", vault=vault)
    assert wav is not None and wav[1] == "audio/wav"
    assert calls == ["key-1-000000000001", "key-2-000000000002"]

    # If all keys return 429, cooldown skips further Gemini TTS bursts smoothly
    vault_rl = FakeVault()
    vault_rl.set("gemini_connection", json.dumps({"model": "gemini-3.5-flash-lite", "keys": ["only-key-000000001"]}))

    class RateLimitedHttp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers=None, json=None):
            calls.append("rl")
            return FakeResp(429)

    monkeypatch.setattr("jarvis.gemini.GeminiClient._http_client", lambda self, t=10: RateLimitedHttp())
    calls.clear()
    assert await voice._gemini_tts_wav("First hit", "Charon", vault=vault_rl) is None
    assert await voice._gemini_tts_wav("Second hit during cooldown", "Charon", vault=vault_rl) is None
    assert calls == ["rl"]
    voice.reset_tts_cooldown()


async def test_voice_turn_streams_ordered_sentence_audio_chunks(monkeypatch):
    app, repo = make_service()
    events = []
    app.emit = lambda ev, data: events.append((ev, data))

    async def fake_speak(text, *, voice_id=None, rate=1, vault=None):
        return ("bW9ja0F1ZGlv:" + text[:12]), "audio/mpeg"

    monkeypatch.setattr("jarvis.workspace.speak_audio_base64", fake_speak)

    sse = (
        'data: {"candidates":[{"content":{"parts":[{"text":"HEARD: Check dollar rate\\nUSD is 93.80 rupees right now. "}]}}]}\n\n'
        'data: {"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"Buy demand is active for Samsung S25."}]}}]}\n\n'
    )

    def respond(request):
        assert ":streamGenerateContent" in request.url.path
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    real_client = GeminiClient
    monkeypatch.setattr("jarvis.workspace.GeminiClient", lambda v: real_client(v, httpx.MockTransport(respond)))

    try:
        out = await app.dispatch({
            "id": 91,
            "method": "voice/turn",
            "params": {
                "audio_wav_b64": "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=",
                "request_id": "voice-stream-req-0001",
                "voice_id": "en-GB-RyanNeural",
                "speak": True,
            },
        })
        res = out["result"]
        assert res["transcript"] == "Check dollar rate"
        assert res["streamed_audio"] is True
        assert res["wav_b64"] is None
        voice_evs = [d for ev, d in events if ev == "voice"]
        heard_evs = [d for d in voice_evs if d["type"] == "transcript_heard"]
        audio_evs = [d for d in voice_evs if d["type"] == "audio_chunk"]
        assert heard_evs and heard_evs[0]["text"] == "Check dollar rate"
        assert [a["seq"] for a in audio_evs] == [0, 1]
        assert audio_evs[0]["mime_type"] == "audio/mpeg"
        assert voice_evs[-1]["type"] == "audio_done"
    finally:
        app.assistant_context.clear()
        app.close()


async def test_live_configures_speech_voice_and_accepts_50ms_frames(monkeypatch):
    sent = []

    class Socket:
        async def send(self, data):
            sent.append(json.loads(data))

        async def recv(self):
            return '{"setupComplete":{}}'

        def __aiter__(self):
            return self

        async def __anext__(self):
            await __import__("asyncio").Event().wait()

        async def close(self):
            pass

    async def connect(*_args, **_kwargs):
        return Socket()

    monkeypatch.setattr("jarvis.live.connect", connect)
    vault = FakeVault()
    vault.set("gemini_connection", json.dumps({"keys": ["fake-gemini-key"]}))
    live = LiveSession(None, vault, lambda *_a: None)
    info = await live.start("assistant", voice_name="Orus")
    try:
        assert info["voice"] == "Orus"
        assert sent[0]["setup"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Orus"
        import base64
        pcm50ms = base64.b64encode(b"\x00\x01" * 800).decode("ascii")
        for _ in range(25):
            r = await live.audio(pcm50ms)
            assert r["accepted"] is True
    finally:
        await live.stop()


async def test_runtime_cache_persists_non_secret_workspace_and_history(tmp_path):
    cache_file = tmp_path / "workspace-cache.json"
    vault = FakeVault()
    vault.set("bridge_secret", "test-only-bridge-secret")
    vault.set("gemini_connection", json.dumps({"model": "gemini-3.5-flash-lite", "keys": ["secret-gemini-api-key-12345"]}))
    vault.set("sheets_connection", json.dumps({"url": "https://script.google.com/macros/s/test/exec", "secret": "secret-signing-token"}))
    repo = FakeSheets()
    await repo.put("configuration", "business", {
        "business_name": "Acme Phones",
        "owner_phone": "+919820000001",
        "sources": [{"phone": "+919820000002", "active": True}],
        "classifier_model": "gemini-3.5-flash-lite",
        "scan_all": False,
    }, 0)
    await repo.put("inventory", "inv-1", {"device": "iPhone 17", "variant": "256GB", "quantity": 5, "price": 85000}, 0)

    app1 = DesktopService(vault, repo, Runner(vault, repo), opener=lambda: None, cache_ttl=25.0, cache_path=cache_file)
    try:
        await app1.check_connections()
        assert app1.connected is True
        app1.assistant_context.snapshot = {"snapshot_at": "2026-11-10T10:00:00Z", "summary": {"messages": 3}, "unavailable": []}
        app1.assistant_context.queue_history({
            "request_id": "req-cached-0001",
            "chat_id": "chat-alpha-001",
            "chat_title": "Inventory Check",
            "question": "What stock do we have?",
            "answer": "You have 5 units of iPhone 17 256GB.",
            "at": "2026-11-10T10:01:00Z",
        })
        await app1.bootstrap({})
    finally:
        app1.close()
        app1.assistant_context.clear()

    raw_cache = cache_file.read_text(encoding="utf-8")
    assert "secret-gemini-api-key-12345" not in raw_cache
    assert "secret-signing-token" not in raw_cache
    assert "test-only-bridge-secret" not in raw_cache
    assert "iPhone 17" in raw_cache

    app2 = DesktopService(FakeVault(), FakeSheets(), cache_ttl=25.0, cache_path=cache_file)
    try:
        assert app2.connected is False
        assert app2.configuration["data"]["owner_phone"] == "+919820000001"
        assert app2.assistant_context.current() is not None
        assert app2.assistant_context.history_expires == 0
        turns = app2.assistant_context.local_turns()
        assert len(turns) == 1 and turns[0]["chat_id"] == "chat-alpha-001"

        boot = (await app2.dispatch({"id": 1, "method": "bootstrap", "params": {}}))["result"]
        assert boot["dashboard"]["inventory"][0]["data"]["device"] == "iPhone 17"
        assert boot["history"]["synced"] is False
        assert len(boot["history"]["turns"]) == 1
    finally:
        app2.assistant_context.clear()
        app2.close()
