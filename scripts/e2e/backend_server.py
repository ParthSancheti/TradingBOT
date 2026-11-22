"""Starts the real LocalBridge + Runner + IntakePipeline + SQLite outbox."""
import asyncio
import json
import secrets
import sys
import threading

from common import EXTENSION_ID, FileVault
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.runner import Runner
from pathlib import Path
import os

OWNER = "+919800000001"


class MemorySheet:
    def __init__(self): self.rows = {}
    async def get(self, c, i): return self.rows.get((c, i))
    async def list(self, c, limit=100): return [r for (k, _), r in self.rows.items() if k == c][:limit]
    async def batch(self, changes, request_id=None):
        out = []
        for ch in changes:
            key = (ch["collection"], ch["id"])
            cur = self.rows.get(key, {}).get("revision", 0)
            if cur != ch["expected_revision"]:
                from jarvis.sheets import SheetsError
                raise SheetsError("REVISION_CONFLICT")
            self.rows[key] = {"id": ch["id"], "revision": cur + 1, "updated_at": "2026-11-20T00:00:00Z", "data": ch["data"]}
            out.append(self.rows[key])
        return {"records": out}
    async def health(self): return {"storage": "memory"}


async def main():
    vault = FileVault()
    vault.set("bridge_secret", secrets.token_urlsafe(32))
    vault.set("extension_id", EXTENSION_ID)
    sheet = MemorySheet()
    repo = SQLiteSyncedRepository(sheet, Path(os.environ["LOCALAPPDATA"]) / "workspace.db")
    await repo.put("configuration", "business", {
        "business_name": "E2E Traders", "owner_phone": OWNER, "classifier_model": "gemini-3.5-flash-lite",
        "sources": [{"phone": "+971500000002", "label": "Dubai supplier"}], "scan_all": True, "lead_capture": True,
    }, 0)
    runner = Runner(vault, repo)
    runner.service_loop = asyncio.get_running_loop()
    commands = []
    runner.on_agent_command = commands.append
    runner.begin_session()
    runner.active = True
    runner.bridge.start()
    print("READY", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await repo.flush_outbox()
    messages = [{"text": r["data"].get("text"), "state": r["data"].get("state"), "phone": r["data"].get("phone"),
                 "direction": r["data"].get("direction", "incoming"), "chat": r["data"].get("chat_name")}
                for r in repo.list_sync("messages", 100)]
    print(json.dumps({
        "messages": messages,
        "sheet_rows": sorted(f"{c}/{i}" for (c, i) in sheet.rows if c == "messages"),
        "sheet_pending": repo.sheet_sync_status(),
        "agent_commands": [c["text"] for c in commands],
        "runner": {k: runner.status()[k] for k in ("state", "observations", "coverage")},
    }), flush=True)
    runner.bridge.stop()
    repo.close()


if __name__ == "__main__":
    asyncio.run(main())
