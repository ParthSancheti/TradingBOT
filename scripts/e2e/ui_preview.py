"""Serve the real desktop renderer against the real DesktopService.

The renderer's window.jarvis IPC is replaced by an HTTP shim; every call goes
to DesktopService.dispatch exactly as Electron would send it. WhatsApp traffic
is injected through Runner.handle (the same entry the native host uses).
Google Sheets is an in-memory stand-in. Usage: python ui_preview.py PORT
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp(prefix="jarvis-ui-"))
from common import EXTENSION_ID, FileVault, ROOT  # noqa: E402
from backend_server import MemorySheet  # noqa: E402  (does not run main on import)
from jarvis.desktop_service import DesktopService  # noqa: E402
from jarvis.local_repo import SQLiteSyncedRepository  # noqa: E402

RENDERER = ROOT / "desktop" / "renderer"
EVENTS = []
OWNER = "+919800000001"


async def seed(service):
    repo = service.repo
    await repo.put("configuration", "business", {
        "business_name": "E2E Traders", "owner_phone": OWNER, "classifier_model": "gemini-3.5-flash-lite",
        "sources": [{"phone": "+971500000002", "label": "Dubai supplier"}], "scan_all": True, "lead_capture": True,
    }, 0)
    inventory = [
        ("inv-1", "iPhone 16 Pro", "256GB", "25", 905, "USD"),
        ("inv-2", "Samsung S25 Ultra", "12/512", 18, 820, "USD"),
        ("inv-3", "Pixel 9 Pro", "128GB", "1,200 pcs", 540, "USD"),
    ]
    for rid, device, variant, qty, cost, cur in inventory:
        await repo.put("inventory", rid, {"device": device, "variant": variant, "quantity": qty, "unit_cost": cost,
                                          "currency": cur, "condition": "NON_ACTIVE", "owned_stock": True}, 0)
    service.configuration = await repo.get("configuration", "business")
    runner = service.runner
    runner.cached_setup = service.configuration["data"]
    runner.active = True
    runner.begin_session()
    now = int(time.time())
    events = [
        ("971500000002@c.us", "Dubai supplier", "S1", "Ready stock iPhone 16 Pro 256GB 40 pcs USD 905", now - 300),
        ("447700900003@c.us", "London buyer", "B1", "WTB Samsung S25 Ultra 512GB 20 pcs", now - 200),
        ("14155550104@c.us", "Archived dealer", "A1", "WTS Pixel 9 Pro 128GB 15 pcs", now - 120),
        ("14155550104@c.us", "Archived dealer", "A2", "Price USD 540 each", now - 60),
    ]
    body = {"operation": "observations", "session_id": "uipreviewsession0001", "events": [
        {"conversation_id": cid, "sender_id": cid, "phone": "+" + cid.split("@")[0], "chat_name": name, "sender_name": name,
         "chat_kind": "direct", "message_id": f"false_{cid}_{mid}", "text": text, "timestamp": ts, "identity_verified": True}
        for cid, name, mid, text, ts in events],
        "activity": {"phase": "waiting", "detail": "4 new messages sent to Jarvis"},
        "coverage": {"discovered": 4, "scanned": 1, "failed": 0}}
    await asyncio.to_thread(runner.handle, body)
    runner.extension_last_seen = time.time()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    vault = FileVault()
    vault.set("bridge_secret", "e2e-secret-" + "x" * 32)
    vault.set("extension_id", EXTENSION_ID)
    vault.set("sheets_connection", json.dumps({"url": "https://script.google.com/macros/s/E2E/exec"}))
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    def run_loop():
        asyncio.set_event_loop(loop)
        repo = SQLiteSyncedRepository(MemorySheet(), Path(os.environ["LOCALAPPDATA"]) / "workspace.db")
        service = DesktopService(vault=vault, repository=repo, cache_path=Path(os.environ["LOCALAPPDATA"]) / "cache.json")
        service.emit = lambda event, data: EVENTS.append({"event": event, "data": data})
        holder["service"] = service

        async def boot():
            service._loop = asyncio.get_running_loop()
            service.runner.service_loop = service._loop
            await seed(service)
            service.connected = True
            service.initializing = False
            service.start_bridge()
            ready.set()
        loop.run_until_complete(boot())
        loop.run_forever()

    threading.Thread(target=run_loop, daemon=True).start()
    ready.wait(30)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw): super().__init__(*a, directory=str(RENDERER), **kw)
        def log_message(self, *a): pass
        def _json(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            if self.path == "/events":
                out = EVENTS[:]; del EVENTS[:len(out)]; return self._json(out)
            return super().do_GET()
        def do_POST(self):
            raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fut = asyncio.run_coroutine_threadsafe(holder["service"].dispatch(raw), loop)
            try: return self._json(fut.result(30))
            except Exception as error: return self._json({"id": raw.get("id"), "error": {"code": "TIMEOUT", "message": str(error)}})

    print(f"READY http://127.0.0.1:{port}/index.html", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
