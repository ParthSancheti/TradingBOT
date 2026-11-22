import json
import httpx
from conftest import FakeSheets, FakeVault
from jarvis.desktop_service import DesktopService
from jarvis.gemini import GeminiClient
from jarvis.runner import Runner


class Bridge:
    ready = True
    last_seen = 0
    def start(self): self.ready = True
    def stop(self): self.ready = False


def make_service():
    vault = FakeVault()
    vault.set("bridge_secret", "test-only-secret")
    vault.set("gemini_connection", json.dumps({"model": "gemini-3.5-flash-lite", "keys": ["test-key-0001"]}))
    repo = FakeSheets()
    runner = Runner(vault, repo)
    runner.bridge = Bridge()
    return DesktopService(vault, repo, runner, opener=lambda: None, cache_ttl=25.0), repo


async def test_bootstrap_loads_workspace_slices_together():
    app, repo = make_service()
    await repo.put("configuration", "business", {
        "business_name": "Desk", "owner_phone": "+919876543210",
        "sources": [{"phone": "+919876543210", "active": True}], "classifier_model": "gemini-3.5-flash-lite",
    }, 0)
    await repo.put("messages", "m1", {"text": "WTB S25 5 pcs", "state": "CLASSIFIED", "triage": {"category": "BUY", "proceed": True, "items": [{"device": "S25", "quantity": 5}]}}, 0)
    await repo.put("inventory", "i1", {"device": "S25", "quantity": 4, "currency": "INR"}, 0)
    app.connected = True

    boot = await app.bootstrap({})
    assert boot["status"]["sheets_connected"] is True
    assert len(boot["activity"]["messages"]) == 1
    assert boot["dashboard"]["captured"] == 1
    assert boot["dashboard"]["inventory"][0]["data"]["quantity"] == 4
    assert isinstance(boot["settings"]["fx_poll_seconds"], int)
    app.close()


async def test_agentic_assistant_executes_sheet_db_and_ui_actions_safely(monkeypatch):
    app, repo = make_service()
    await app.settings_save({"agent_allow_writes": True})
    ui_events = []
    app.emit = lambda ev, data: ui_events.append((ev, data))
    step = {"n": 0}

    def respond(request):
        step["n"] += 1
        body = json.loads(request.content)
        if step["n"] == 1:
            assert "tools" in body
            return httpx.Response(200, json={
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [
                        {"functionCall": {"name": "upsert_inventory", "args": {"device": "iPhone 16 Pro", "variant": "256GB", "quantity": 7, "currency": "INR"}}},
                        {"functionCall": {"name": "dispatch_ui_action", "args": {"action": "navigate", "view": "stock", "msg_filter": "BUY"}}},
                    ]},
                }],
            })
        fn_resps = body["contents"][-1]["parts"]
        assert fn_resps[0]["functionResponse"]["response"]["saved"] is True
        assert fn_resps[1]["functionResponse"]["response"]["dispatched"] is True
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Added **7x iPhone 16 Pro 256GB** and opened Stock & demand."}]}}],
        })

    real_client = GeminiClient
    monkeypatch.setattr("jarvis.workspace.GeminiClient", lambda v: real_client(v, httpx.MockTransport(respond)))

    try:
        res = await app.assistant_ask({
            "question": "Add 7 iPhone 16 Pro 256GB to inventory and show stock",
            "request_id": "agent-tool-request-0001",
            "page": "desk",
            "chat_id": "agent-chat-1",
        })
        assert "7x iPhone 16 Pro 256GB" in res["answer"]
        assert len(res["actions"]) == 1 and res["actions"][0]["view"] == "stock"
        inv = await repo.list("inventory", 10)
        assert inv[0]["data"]["device"] == "iPhone 16 Pro" and inv[0]["data"]["quantity"] == 7
        assert ("ui-action", {"action": "navigate", "view": "stock", "msg_filter": "BUY"}) in ui_events
    finally:
        app.assistant_context.clear()
        app.close()


async def test_voice_turn_accepts_wav_audio_runs_tools_and_enforces_size_guard(monkeypatch):
    app, repo = make_service()
    fake_wav = "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA="

    def respond(request):
        body = json.loads(request.content)
        parts = body["contents"][0]["parts"]
        assert parts[0]["inlineData"]["mimeType"] == "audio/wav"
        assert parts[0]["inlineData"]["data"] == fake_wav
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "HEARD: Summarize my stock\nYou currently have no recorded stock items yet."}]}}],
        })

    real_client = GeminiClient
    monkeypatch.setattr("jarvis.workspace.GeminiClient", lambda v: real_client(v, httpx.MockTransport(respond)))

    try:
        out = await app.dispatch({
            "id": 21,
            "method": "voice/turn",
            "params": {
                "audio_wav_b64": fake_wav,
                "request_id": "voice-turn-request-0001",
                "speak": False,
            },
        })
        assert out["result"]["transcript"] == "Summarize my stock"
        assert "no recorded stock items" in out["result"]["answer"]

        # Normal method >32KB still rejected cleanly
        oversize = await app.dispatch({
            "id": 22,
            "method": "connect",
            "params": {"script_url": "https://script.google.com/macros/s/" + "a" * 34000},
        })
        assert oversize["error"]["code"] == "INVALID_INPUT"
    finally:
        app.assistant_context.clear()
        app.close()


async def test_dynamic_agent_name_behavior_and_unlocked_native_tools(monkeypatch, tmp_path):
    from pathlib import Path
    from jarvis.workspace import AgentToolbox, build_agent_prompt
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, repo = make_service()
    ui_events = []
    app.emit = lambda ev, data: ui_events.append((ev, data))
    try:
        saved = await app.settings_save({
            "agent_name": "Nova",
            "agent_persona": "executive",
            "agent_language": "english",
            "agent_allow_writes": False,
            "agent_max_tool_steps": 10,
            "agent_custom_instructions": "Always prioritize Dubai suppliers.",
        })
        assert saved["agent_name"] == "Nova"
        assert app.runner.agent_name == "Nova"
        st = await app.status({})
        assert st["agent_name"] == "Nova"
        poll = await app.runner._handle({"operation": "poll_config"})
        assert poll["agent_name"] == "Nova"

        prompt = build_agent_prompt(saved, voice_mode=False)
        assert "You are Nova" in prompt
        assert "Always prioritize Dubai suppliers." in prompt
        assert "Write protection is ON" in prompt

        # Write-protected toolbox rejects write tools while allowing notifications & report exports
        ro_box = AgentToolbox(repo, app.vault, service=app, workspace=app.assistant_context, agent_cfg=saved)
        blocked = await ro_box.execute("upsert_inventory", {"device": "S25", "quantity": 2})
        assert "disabled" in blocked["error"].lower()

        notif = await ro_box.execute("send_windows_notification", {"title": "FX Alert", "body": "USD/INR moved"})
        assert notif["sent"] is True
        assert any(ev == "notification" and d["title"] == "FX Alert" for ev, d in ui_events)

        saved_rw = await app.settings_save({**saved, "agent_allow_writes": True})
        rw_box = AgentToolbox(repo, app.vault, service=app, workspace=app.assistant_context, agent_cfg=saved_rw)
        bulk = await rw_box.execute("bulk_upsert_inventory", {"items": [{"device": "Pixel 10", "quantity": 5}]})
        assert "error" not in bulk
        assert (await repo.list("inventory", 10))[0]["data"]["device"] == "Pixel 10"
        report = await rw_box.execute("export_workspace_report", {"title": "EOD Summary"})
        assert report["exported"] is True
        assert Path(report["path"]).is_file()
    finally:
        app.assistant_context.clear()
        app.close()


async def test_runner_watermark_scans_from_start_and_recovers_after_stop():
    app, repo = make_service()
    try:
        await repo.put("configuration", "business", {
            "business_name": "Desk", "owner_phone": "+919876543210", "scan_all": True,
            "sources": [{"phone": "+919876543210", "active": True}], "classifier_model": "not-configured",
        }, 0)
        app.runner.begin_session(now=1000.0)
        app.runner.active = True

        # Ancient message strictly before session start must be skipped
        res_old = await app.runner._handle({
            "operation": "observations",
            "events": [{
                "conversation_id": "919811112222@c.us",
                "sender_id": "919811112222@c.us",
                "phone": "+919811112222",
                "message_id": "false_919811112222@c.us_old1",
                "text": "Old chat before agent start",
                "timestamp": 500,
                "identity_verified": True,
            }],
        })
        assert res_old["results"][0]["status"] == "IGNORED_OLD"
        assert app.runner.observations == 0

        # Fresh message after start is recorded and updates watermark
        res_live = await app.runner._handle({
            "operation": "observations",
            "events": [{
                "conversation_id": "919811112222@c.us",
                "sender_id": "919811112222@c.us",
                "phone": "+919811112222",
                "message_id": "false_919811112222@c.us_live1",
                "text": "WTB 5 S25 Ultra",
                "timestamp": 1100,
                "identity_verified": True,
            }],
        })
        assert res_live["results"][0]["status"] == "CAPTURED_REVIEW"
        assert app.runner.observations == 1

        # Stop at 1200, message arrives at 1500 while stopped, reopen at 1800 -> missed msg MUST scan!
        app.runner.end_session(now=1200.0)
        assert app.runner.active is False
        app.runner.begin_session(now=1800.0)
        app.runner.active = True

        poll = await app.runner._handle({"operation": "poll_config"})
        assert poll["scan_since_ts"] <= 1100.0

        res_catchup = await app.runner._handle({
            "operation": "observations",
            "events": [
                {
                    "conversation_id": "919811112222@c.us",
                    "sender_id": "919811112222@c.us",
                    "phone": "+919811112222",
                    "message_id": "false_919811112222@c.us_veryold",
                    "text": "Way too old",
                    "timestamp": 400,
                    "identity_verified": True,
                },
                {
                    "conversation_id": "919811112222@c.us",
                    "sender_id": "919811112222@c.us",
                    "phone": "+919811112222",
                    "message_id": "false_919811112222@c.us_missed1",
                    "text": "WTB 2 Fold 6 arrived while stopped",
                    "timestamp": 1500,
                    "identity_verified": True,
                },
            ],
        })
        assert res_catchup["results"][0]["status"] == "IGNORED_OLD"
        assert res_catchup["results"][1]["status"] == "CAPTURED_REVIEW"
        assert app.runner.observations == 2

        app.runner.reset_scan_cursor()
        assert app.runner.scan_since_ts is None
    finally:
        app.assistant_context.clear()
        app.close()
