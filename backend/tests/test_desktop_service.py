import pytest

from conftest import FakeSheets, FakeVault
from jarvis.desktop_service import DesktopService
from jarvis.pipeline import IntakePipeline
from jarvis.runner import Runner


class Bridge:
    ready = False
    last_seen = 0
    def start(self): self.ready = True
    def stop(self): self.ready = False


def service():
    vault = FakeVault(); vault.set("bridge_secret", "test-only-secret")
    repo = FakeSheets()
    runner = Runner(vault, repo); runner.bridge = Bridge()
    return DesktopService(vault, repo, runner, opener=lambda: None), repo


async def test_desktop_numbers_only_setup_preserves_rules_and_starts_explicitly():
    app, repo = service()
    await app.initialize()
    assert not (await app.status({}))["running"]
    status = await app.save_setup({"owner_phone": "+919876543210", "stock_phones": ["+14155552671"]})
    assert status["setup"]["stock_phones"] == ["+14155552671"]
    data = repo.records["configuration", "business"]["data"]
    assert not data["scan_all"] and data["sources"][0]["label"] == ""
    assert not app.runner.active
    assert (await app.runner._handle({"operation": "poll_config"}))["active"] is False
    await app.start({})
    config = await app.runner._handle({"operation": "poll_config"})
    assert config["active"] and not config["scan_all"] and not config["can_send"]
    await app.stop({})
    assert (await app.runner._handle({"operation": "observations", "events": []}))["error"] == "OBSERVATION_PAUSED"
    app.close()
    assert not app.runner.bridge.ready


async def test_unknown_method_and_secret_validation_do_not_leak_input():
    app, _ = service()
    assert (await app.dispatch({"id": 8, "method": "exec", "params": {}}))["error"]["code"] == "UNKNOWN_METHOD"
    secret = "not-a-real-secret-do-not-echo"
    result = await app.dispatch({"id": 9, "method": "connect", "params": {"script_url": "invalid", "signing_secret": secret}})
    assert result["error"]["code"] == "INVALID_INPUT"
    assert secret not in str(result)


async def test_manual_send_settings_survive_restart_without_sheets(tmp_path):
    from jarvis.local_repo import SQLiteSyncedRepository
    from jarvis.sheets import UnconfiguredRepository

    path = tmp_path / "workspace.db"
    vault = FakeVault()
    vault.set("bridge_secret", "test-only-secret")
    for attempt in range(2):
        repo = SQLiteSyncedRepository(UnconfiguredRepository(), path)
        runner = Runner(vault, repo)
        runner.bridge = Bridge()
        app = DesktopService(vault, repo, runner, opener=lambda: None)
        try:
            if not attempt:
                await app.settings_save({"autonomy_mode": "send", "agent_name": "Operator"})
                assert not repo.has_pending_sheet_writes()
            else:
                await app.initialize()
                assert (await app.bootstrap({}))["settings"]["autonomy_mode"] == "send"
                assert runner.agent_name == "Operator"
                assert not runner.can_send
                runner.active = True
                assert runner.can_send
                owner = "+919876543210"
                recipient = "+14155552671"
                runner.cached_setup = {"owner_phone": owner, "sources": [{"phone": recipient}]}
                jid = "14155552671@c.us"
                quote = repo.queue_outbound(jid, ["A quote"], recipient_phone=recipient)
                manual = await app.outbound_create({"recipient_phone": recipient, "text": "A manual reply"})
                drafts = {draft["id"]: draft for draft in repo.list_outbound(("DRAFT",))}
                rejected = await app.dispatch({"id": 1, "method": "outbound/action", "params": {
                    "id": quote, "action": "approve", "expected_updated_at": drafts[quote]["updated_at"],
                }})
                assert rejected["error"]["code"] == "MANUAL_SEND_ONLY"
                await app.outbound_action({"id": manual["id"], "action": "approve",
                                           "expected_updated_at": drafts[manual["id"]]["updated_at"]})
                assert repo.ready_outbound_ids(owner, manual_only=True) == [{"id": manual["id"]}]
        finally:
            app.close()


async def test_setup_profile_sync_is_available_with_observation_paused():
    app, repo = service()
    await app.sync_profile({})
    request = await app.runner._handle({"operation": "poll_config"})
    assert request["profile_requested"] and not request["active"]
    await app.runner._handle({"operation": "poll_config", "profile": {"verified": True, "name": "Owner"}})
    assert (await app.status({}))["profile"]["name"] == "Owner"
    assert not app.runner.active and not repo.records


async def test_whatsapp_agent_command_records_progress_without_raw_command(monkeypatch):
    app, repo = service()
    app.runner.active = True
    emitted = []
    app.emit = lambda event, data: emitted.append((event, data))

    async def handle(*args, **kwargs):
        task = next(row["data"] for (collection, _), row in repo.records.items() if collection == "tasks")
        assert task["state"] == "PROCESSING"
        return {"reply": "Prepared"}

    monkeypatch.setattr("jarvis.desktop_service.handle_agent_command", handle)
    await app._process_agent_command({"message_id": "test-message", "text": "@agent private input"})
    task = next(row["data"] for (collection, _), row in repo.records.items() if collection == "tasks")
    assert task["state"] == "COMPLETED" and task["finished_at"] >= task["started_at"]
    assert "private input" not in str(task)
    assert sum(event == "data-updated" for event, _ in emitted) == 2


async def test_saved_gemini_key_requires_an_explicit_reveal_request():
    app, _ = service()
    key = "test-only-key-1234567890"
    status = await app.save_provider({"keys": [key], "model": "gemini-test"})
    assert status["gemini"]["key_count"] == 1 and key not in str(status)
    assert await app.reveal_provider_key({"index": 0}) == {"key": key}
    updated = await app.save_provider({"keys": [0], "model": "gemini-updated"})
    assert updated["gemini"]["model"] == "gemini-updated" and key not in str(updated)
    assert await app.reveal_provider_key({"index": 0}) == {"key": key}
    invalid = await app.dispatch({"id": 10, "method": "provider/save", "params": {"keys": [1]}})
    assert invalid["error"]["code"] == "INVALID_KEY_SLOT"
    with pytest.raises(ValueError):
        await app.reveal_provider_key({"index": -1})


async def test_all_chat_intake_supports_lid_without_assigning_inventory_authority():
    repo = FakeSheets()
    setup = {"sources": [{"phone": "+919876543210", "active": True}], "scan_all": True}
    event = {"conversation_id": "123456789@lid", "sender_id": "123456789@lid", "phone": None,
             "sender_name": "Buyer", "message_id": "false_123456789@lid_ABCD", "text": "WTB S25 12/256", "identity_verified": True}
    result = await IntakePipeline(repo, None).process(event, setup)
    assert result["status"] == "CAPTURED_REVIEW"
    record = repo.records["messages", result["id"]]["data"]
    assert record["phone"] is None and record["sender_name"] == "Buyer" and not record["stock_source"]
    assert (await IntakePipeline(repo, None).process({**event, "phone": "+919876543210"}, setup))["status"] == "NEEDS_REVIEW"
    assert (await IntakePipeline(repo, None).process({**event, "message_id": "false_987654321@lid_ABCD"}, setup))["status"] == "NEEDS_REVIEW"


async def test_group_intake_binds_phone_to_participant_and_keeps_chat_identity():
    repo = FakeSheets()
    setup = {"sources": [{"phone": "+919876543210", "active": True}], "scan_all": True}
    event = {"conversation_id": "1234567890-123456@g.us", "sender_id": "919876543210@c.us", "phone": "+919876543210",
             "message_id": "false_1234567890-123456@g.us_ABCD", "text": "WTS S25 12/256", "identity_verified": True}
    result = await IntakePipeline(repo, None).process(event, setup)
    record = repo.records["messages", result["id"]]["data"]
    assert record["stock_source"] and record["chat_kind"] == "group"
    changed = await IntakePipeline(repo, None).process({**event, "sender_id": "971501234567@c.us"}, setup)
    assert changed["status"] == "NEEDS_REVIEW"


async def test_desktop_swr_cache_coalesces_shares_messages_and_invalidates():
    import asyncio
    from jarvis.sheets import _pooled_client
    assert _pooled_client() is _pooled_client()

    class CountingRepo(FakeSheets):
        def __init__(self):
            super().__init__()
            self.reads = 0
        async def list(self, c, limit=100):
            self.reads += 1
            await asyncio.sleep(0.02)
            return await super().list(c, limit)

    vault = FakeVault(); vault.set("bridge_secret", "test-only-secret")
    repo = CountingRepo()
    await repo.put("messages", "m1", {"text": "WTB S25", "state": "RECEIVED"}, 0)
    await repo.put("inventory", "i1", {"device": "S25", "quantity": 5, "currency": "INR"}, 0)
    runner = Runner(vault, repo); runner.bridge = Bridge()
    app = DesktopService(vault, repo, runner, opener=lambda: None, cache_ttl=30.0)

    # Concurrent dashboard calls coalesce, and activity(limit=50) reuses cached messages(limit=500)
    d1, d2 = await asyncio.gather(app.dashboard({}), app.dashboard({}))
    assert d1["captured"] == 1 and d2["captured"] == 1
    assert repo.reads == 2  # 1 messages + 1 inventory, coalesced across both callers

    act = await app.activity({"limit": 50})
    assert len(act["messages"]) == 1
    assert repo.reads == 2  # Zero extra Sheets read!

    # Write invalidates affected collection immediately
    await app.inventory_save({"id": "i1", "device": "S25", "variant": "", "quantity": 8, "currency": "INR", "expected_revision": 1})
    d3 = await app.dashboard({})
    assert d3["inventory"][0]["data"]["quantity"] == 8
    assert repo.reads == 3  # Only inventory re-fetched; messages still cached

    # Explicit refresh=True bypasses cache
    await app.activity({"limit": 50, "refresh": True})
    assert repo.reads == 4
    app.close()


async def test_start_requests_missing_profile_downloads_pfp_and_new_number_resets_cache(tmp_path, monkeypatch):
    import io
    from PIL import Image
    from jarvis import desktop

    monkeypatch.setattr(desktop, "runtime_directory", lambda: tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (120, 120), color=(3, 105, 161)).save(buf, format="JPEG")
    sample_jpeg = buf.getvalue()

    async def fake_download(url):
        assert url.startswith("https://media-bom5-2.cdn.whatsapp.net/")
        return desktop.save_owner_avatar_bytes(sample_jpeg)

    monkeypatch.setattr("jarvis.runner.download_whatsapp_avatar", fake_download)

    vault = FakeVault(); vault.set("bridge_secret", "test-only-secret")
    repo = FakeSheets()
    runner = Runner(vault, repo); runner.bridge = Bridge()
    cache_file = tmp_path / "workspace-cache.json"
    app = DesktopService(vault, repo, runner, opener=lambda: None, cache_path=cache_file)
    try:
        await app.save_setup({"owner_phone": "+919876543210", "stock_phones": ["+14155552671"]})
        await app.start({})
        cfg = await app.runner._handle({"operation": "poll_config"})
        assert cfg["capture_profile"] is True and cfg["config_version"] == 1

        await app.runner._handle({
            "operation": "poll_config",
            "profile": {
                "verified": True,
                "name": "Parth Sancheti",
                "avatar": None,
                "avatar_url": "https://media-bom5-2.cdn.whatsapp.net/v/t61/pfp.jpg",
            },
        })
        st = await app.status({})
        assert st["profile"]["name"] == "Parth Sancheti"
        assert st["profile"]["avatar"].startswith("data:image/jpeg;base64,")
        assert (tmp_path / "owner-avatar.jpg").is_file()

        # Adding a new owner number resets profile, avatar file, and stored cache
        res = await app.save_setup({"owner_phone": "+919999999999", "stock_phones": ["+14155552671"]})
        assert res["reset_stored_cache"] is True
        assert (await app.status({}))["profile"] is None
        assert not (tmp_path / "owner-avatar.jpg").exists()
        assert app.runner.config_version == 2
    finally:
        app.close()


async def test_profile_persists_once_without_repeating_and_owner_reply_is_blocked_in_observe(tmp_path):
    from jarvis.local_repo import SQLiteSyncedRepository

    vault = FakeVault(); vault.set("bridge_secret", "test-only-secret")
    remote = FakeSheets()
    sql_repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    runner = Runner(vault, sql_repo); runner.bridge = Bridge()
    cache_file = tmp_path / "workspace-cache.json"
    app = DesktopService(vault, sql_repo, runner, opener=lambda: None, cache_path=cache_file)
    try:
        await app.save_setup({"owner_phone": "+919876543210", "stock_phones": ["+919405615135"]})
        await app.start({})
        cfg1 = await app.runner._handle({"operation": "poll_config"})
        assert cfg1["capture_profile"] is True

        # Store verified profile even when WhatsApp account has no profile picture
        await app.runner._handle({
            "operation": "poll_config",
            "profile": {"verified": True, "name": "Trader", "phone": "+919876543210", "avatar": None, "avatar_checked": True},
        })
        cfg2 = await app.runner._handle({"operation": "poll_config"})
        assert cfg2["capture_profile"] is False
        assert cfg2["profile_captured"] is True

        # A previously approved self-chat reply must stay undispatched.
        sql_repo.queue_outbound("919876543210@c.us", ["Hello boss"], kind="agent_reply", status="APPROVED")
        cfg3 = await app.runner._handle({"operation": "poll_config"})
        assert cfg3["outbound"] == [] and cfg3["can_send"] is False
        for mode in ("draft", "auto"):
            runner.autonomy_mode = mode
            poll = await runner._handle({"operation": "poll_config"})
            assert poll["outbound"] == [] and poll["can_send"] is (mode == "draft")
        receipt = await runner._handle({"operation": "dispatch_result", "ok": True})
        assert receipt["error"] == "INVALID_OUTBOUND_ID"
    finally:
        app.close()

    # Restart with fresh DesktopService + SQLite DB -> never repeats profile getter
    vault2 = FakeVault(); vault2.set("bridge_secret", "test-only-secret")
    sql_repo2 = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    runner2 = Runner(vault2, sql_repo2); runner2.bridge = Bridge()
    app2 = DesktopService(vault2, sql_repo2, runner2, opener=lambda: None, cache_path=cache_file)
    try:
        await app2.start({})
        cfg_restarted = await app2.runner._handle({"operation": "poll_config"})
        assert cfg_restarted["capture_profile"] is False
        assert cfg_restarted["profile_captured"] is True
        assert (await app2.status({}))["profile"]["name"] == "Trader"
    finally:
        app2.close()


async def test_activity_retains_social_history_and_paginates(tmp_path):
    import time
    from jarvis.local_repo import SQLiteSyncedRepository

    vault = FakeVault(); vault.set("bridge_secret", "test-only-secret")
    remote = FakeSheets()
    sql_repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    runner = Runner(vault, sql_repo); runner.bridge = Bridge()
    app = DesktopService(vault, sql_repo, runner, opener=lambda: None)
    try:
        now = time.time()
        await sql_repo.put("messages", "social-fresh", {"state": "IGNORED_SOCIAL", "text": "Hi gm", "received": now - 120}, 0)
        await sql_repo.put("messages", "social-expired", {"state": "IGNORED_SOCIAL", "text": "Old casual msg",
                           "phone": "+12025550101", "sender_name": "Test Sender", "received": now - 700}, 0)
        await sql_repo.put("messages", "trade-old", {"state": "CLASSIFIED", "text": "WTB 10 iPhone 17", "received": now - 3600, "triage": {"category": "BUY", "proceed": True, "needs_review": False, "items": []}}, 0)
        await sql_repo.put("messages", "engaged-social", {"state": "IGNORED_SOCIAL", "text": "Replied message", "received": now - 3600}, 0)
        sql_repo.queue_outbound("12345@c.us", ["Sure, noted"], kind="quote", status="SENT", meta={"source_message": "engaged-social"})

        act = await app.activity({"limit": 20})
        ids = {m["id"] for m in act["messages"]}
        assert "social-fresh" in ids
        assert "social-expired" in ids
        assert "trade-old" in ids
        assert "engaged-social" in ids
        assert act["counts"]["captured"] == 4
        first = await app.activity({"limit": 2})
        second = await app.activity({"limit": 2, "offset": 2})
        assert first["has_more"] and not second["has_more"]
        assert {row["id"] for row in first["messages"] + second["messages"]} == ids
        filtered = await app.activity({"limit": 1, "category": "SOCIAL", "person": "+12025550101",
                                       "query": "CASUAL", "include_people": True})
        assert [row["id"] for row in filtered["messages"]] == ["social-expired"]
        assert filtered["total"] == 4 and not filtered["has_more"]
        assert filtered["people"] == [{"person": "+12025550101", "name": "Test Sender"}]
        assert [row["id"] for row in filtered["recent"]] == ["trade-old"]
    finally:
        app.close()


async def test_inventory_parse_xlsx_and_text_to_json_bulk_save(tmp_path, monkeypatch):
    import base64, io, zipfile
    from jarvis.local_repo import SQLiteSyncedRepository

    # Build a minimal valid .xlsx workbook in memory (no external deps needed)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst><si><t>Model</t></si><si><t>Qty</t></si><si><t>Cost</t></si><si><t>iPhone 17 Pro 256GB Non Active Unlocked</t></si></sst>')
        zf.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0"?><worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row><row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2"><v>15</v></c><c r="C2"><v>112500</v></c></row></sheetData></worksheet>')
    xlsx_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    class FakeGemini:
        def __init__(self, *a, **kw): pass
        def status(self): return {"configured": True, "model": "gemini-2.5-flash-lite"}
        async def generate(self, prompt, text, schema, max_tokens):
            assert "iPhone 17 Pro 256GB Non Active Unlocked" in text
            return '{"items":[{"brand":"Apple","model":"iPhone 17 Pro","storage_gb":256,"variant":"256GB","condition":"NON_ACTIVE","lock_status":"UNLOCKED","region_spec":"INDIAN SPEC","tax_status":"WITH_GST","quantity":15,"unit_cost":112500,"currency":"INR"}]}'

    monkeypatch.setattr("jarvis.desktop_service.GeminiClient", FakeGemini)

    vault = FakeVault(); vault.set("bridge_secret", "test-only-secret")
    remote = FakeSheets()
    sql_repo = SQLiteSyncedRepository(remote, tmp_path / "ws.db")
    runner = Runner(vault, sql_repo); runner.bridge = Bridge()
    app = DesktopService(vault, sql_repo, runner, opener=lambda: None)
    try:
        parsed = await app.inventory_parse({"file_b64": xlsx_b64, "file_name": "stock.xlsx"})
        assert parsed["detected_format"] == "xlsx"
        assert len(parsed["rows"]) == 1
        row = parsed["rows"][0]
        assert row["device"] == "Apple iPhone 17 Pro"
        assert row["quantity"] == 15
        assert row["condition"] == "NON_ACTIVE"
        await app.inventory_bulk({"rows": parsed["rows"], "request_id": "req-xlsx-import-0001"})
        inv = await sql_repo.list("inventory", 20)
        assert len(inv) == 1 and inv[0]["data"]["condition"] == "NON_ACTIVE"
    finally:
        app.close()


async def test_extension_pair_rpc_registers_and_syncs(tmp_path, monkeypatch):
    from jarvis import desktop

    app, repo = service()
    await app.initialize()
    monkeypatch.setattr(desktop, "runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(desktop, "auto_detect_extension_ids", lambda: ["ajmiggblbcihebfhcajjlmlimenbneil"])
    fake_exe = tmp_path / "jarvis-native.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(desktop, "find_native_executable", lambda: fake_exe)

    if desktop.sys.platform == "win32":
        class FakeWinreg:
            HKEY_CURRENT_USER = "HKCU"
            REG_SZ = 1
            @staticmethod
            def CreateKey(*args):
                class Ctx:
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                return Ctx()
            @staticmethod
            def SetValueEx(*args): pass
        monkeypatch.setitem(desktop.sys.modules, "winreg", FakeWinreg)

    try:
        # 1. Auto-sync pair call
        res = await app.dispatch({"id": "req-1", "method": "extension/pair", "params": {}})
        assert "result" in res
        paired = res["result"]["paired_ids"]
        assert "ajmiggblbcihebfhcajjlmlimenbneil" in paired

        # 2. Status now includes extension info
        st = await app.dispatch({"id": "req-2", "method": "status", "params": {}})
        assert "extension" in st["result"]
        assert "ajmiggblbcihebfhcajjlmlimenbneil" in st["result"]["extension"]["paired_ids"]

        # 3. Explicit pair with another ID
        new_id = "c" * 32
        res2 = await app.dispatch({"id": "req-3", "method": "extension/pair", "params": {"extension_id": new_id}})
        assert "result" in res2
        assert new_id in res2["result"]["paired_ids"]
    finally:
        app.close()

