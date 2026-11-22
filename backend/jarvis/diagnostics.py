"""Bounded operational log with a fixed schema and no request content."""

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


MAX_ENTRIES = 1000
MAX_AGE_SECONDS = 7 * 24 * 60 * 60
EVENTS = frozenset({
    "engine/start", "engine/stop", "diagnostic", "connect", "setup/save", "start", "stop",
    "provider/save", "provider/test", "provider/discover", "inventory/save", "inventory/bulk",
    "inventory/parse", "assistant/ask", "voice/turn", "live/start", "live/stop",
    "profile/sync", "bridge/open", "bridge/restarted", "wa/sync", "outbound/create", "outbound/action",
    "outbound/dispatch", "whatsapp/dispatch", "whatsapp/intake", "whatsapp/receipt", "worker/market",
    "worker/classifier", "worker/sheets", "worker/scheduler", "worker/connections", "worker/notifications",
})
CODES = frozenset({
    "ACTION_FAILED", "INVALID_INPUT", "UNKNOWN_METHOD", "UNSUPPORTED", "BRIDGE_UNAVAILABLE", "BRIDGE_RESTARTED",
    "SHEETS_UNAVAILABLE", "SHEETS_NOT_CONNECTED", "SHEETS_NOT_CONFIGURED", "SHEETS_CONNECTION_ERROR",
    "RESPONSE_NOT_VERIFIED", "REVISION_CONFLICT", "WORKBOOK_SWITCH_BLOCKED", "ACCOUNT_SWITCH_BLOCKED",
    "WORKBOOK_BINDING_REQUIRED", "MODEL_COOLDOWN", "MODEL_DAILY_LIMIT", "MODEL_KEY_REQUIRED",
    "MODEL_OVERLOADED", "MODEL_QUOTA", "MODEL_UNAVAILABLE", "MODEL_OUTPUT_INVALID",
    "APPROVED", "CLAIMED", "ACCEPTED", "SENT", "DELIVERED", "CANCELLED", "DECLINED",
    "DISPATCH_DISABLED", "DELIVERY_UNKNOWN", "UNKNOWN", "FAILED", "FAILED_BEFORE_SEND",
    "ACCOUNT_MISMATCH", "RECIPIENT_MISMATCH", "RETRY_REQUIRED", "OBSERVATION_PAUSED",
    "EXPIRED", "BRIDGE_TIMEOUT", "BUSY_RETRY", "RECORD_TOO_LARGE", "BATCH_TOO_LARGE",
    "NOT_AUTHORIZED", "LOG_UNAVAILABLE",
})


class DiagnosticsLog:
    def __init__(self, path: Path | None = None):
        self._path = path
        self._connection = None
        self._lock = threading.Lock()

    def _database(self):
        if self._connection is None:
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self._path) if self._path else ":memory:", check_same_thread=False, timeout=1)
            try:
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY, at INTEGER NOT NULL, event TEXT NOT NULL,
                        level TEXT NOT NULL, code TEXT, duration_ms INTEGER, count INTEGER
                    )
                """)
            except sqlite3.Error:
                connection.close()
                raise
            connection.row_factory = sqlite3.Row
            self._connection = connection
        return self._connection

    @staticmethod
    def _prune(connection, now):
        connection.execute("DELETE FROM events WHERE at < ?", (now - MAX_AGE_SECONDS,))
        connection.execute("DELETE FROM events WHERE id <= (SELECT max(id) FROM events) - ?", (MAX_ENTRIES,))

    def record(self, event, *, level="info", code=None, duration_ms=None, count=None):
        if event not in EVENTS:
            return
        level = level if level in {"info", "warning", "error"} else "warning"
        code = (code if isinstance(code, str) and code in CODES else "OTHER") if code is not None else None
        duration_ms = min(max(duration_ms, 0), 86_400_000) if type(duration_ms) is int else None
        count = min(max(count, 0), 1_000_000) if type(count) is int else None
        now = int(time.time())
        try:
            with self._lock, self._database() as connection:
                connection.execute(
                    "INSERT INTO events(at,event,level,code,duration_ms,count) VALUES(?,?,?,?,?,?)",
                    (now, event, level, code, duration_ms, count),
                )
                self._prune(connection, now)
        except (OSError, sqlite3.Error):
            # Diagnostics cannot prevent the trading workflow from running.
            return

    def snapshot(self):
        with self._lock, self._database() as connection:
            self._prune(connection, int(time.time()))
            rows = connection.execute(
                "SELECT at,event,level,code,duration_ms,count FROM events ORDER BY id DESC"
            ).fetchall()
        entries = []
        for row in rows:
            item = dict(row)
            item["at"] = datetime.fromtimestamp(item["at"], timezone.utc).isoformat(timespec="seconds")
            entries.append({key: value for key, value in item.items() if value is not None})
        return {"entries": entries, "max_entries": MAX_ENTRIES, "retention_days": 7}

    def clear(self):
        with self._lock, self._database() as connection:
            connection.execute("DELETE FROM events")

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
