"""Local-first SQLite WAL operational repository with coalesced background Google Sheets sync."""
import asyncio
import copy
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .sheets import SheetsError


class SQLiteSyncedRepository:
    _PERMANENT_SHEET_ERRORS = frozenset({"REVISION_CONFLICT", "RECORD_TOO_LARGE", "BATCH_TOO_LARGE",
                                         "INVALID_CHANGE", "IDEMPOTENCY_CONFLICT"})
    """Sub-millisecond local SQLite WAL store backed by serialized async Google Sheets sync."""

    def __init__(self, remote_repo, db_path, *, on_sync_error=None):
        self.remote = remote_repo
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.on_sync_error = on_sync_error
        self._lock = threading.RLock()
        self._flush_lock = asyncio.Lock()
        self._flush_task = None
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            c = self._conn
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute(
                """CREATE TABLE IF NOT EXISTS records (
                    collection TEXT NOT NULL,
                    id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (collection, id)
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_records_col_updated ON records(collection, updated_at DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_records_col_updated_asc ON records(collection, updated_at ASC)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS message_stage_events (
                    event_id TEXT PRIMARY KEY,
                    source_message_id TEXT NOT NULL,
                    conversation_id TEXT,
                    account_phone TEXT,
                    message_revision INTEGER NOT NULL DEFAULT 0,
                    sequence INTEGER NOT NULL,
                    attempt_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    occurred_at REAL NOT NULL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_stage_events_msg ON message_stage_events(source_message_id, sequence ASC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_stage_events_conv ON message_stage_events(conversation_id, occurred_at DESC)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS sheets_outbox (
                    collection TEXT NOT NULL,
                    id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    queued_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    PRIMARY KEY (collection, id)
                )"""
            )
            c.execute("""CREATE TABLE IF NOT EXISTS sheets_revisions (
                collection TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY (collection, id)
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS sheets_quarantine (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                record_id TEXT NOT NULL,
                expected_revision INTEGER NOT NULL,
                error_reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                quarantined_at REAL NOT NULL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sheets_quarantine_col ON sheets_quarantine(collection, record_id)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS chat_turns (
                    request_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    model TEXT,
                    at TEXT NOT NULL,
                    meta_json TEXT NOT NULL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_chat ON chat_turns(chat_id, at ASC)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    recipient_jid TEXT NOT NULL,
                    buyer_phone TEXT,
                    buyer_tz TEXT NOT NULL,
                    run_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    recalculate_fx INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_jobs(status, run_at)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS contact_profiles (
                    conversation_id TEXT PRIMARY KEY,
                    phone TEXT,
                    name TEXT,
                    category TEXT NOT NULL DEFAULT 'UNKNOWN',
                    bot_mode TEXT NOT NULL DEFAULT 'DRAFT',
                    manual_mute_until REAL NOT NULL DEFAULT 0,
                    last_scanned_ts REAL NOT NULL DEFAULT 0,
                    last_contacted_at REAL NOT NULL DEFAULT 0,
                    is_announcement INTEGER NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS lid_phone_map (
                    lid TEXT PRIMARY KEY,
                    phone TEXT NOT NULL,
                    name TEXT,
                    updated_at REAL NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS outbound_queue (
                    id TEXT PRIMARY KEY,
                    recipient_jid TEXT NOT NULL,
                    recipient_phone TEXT,
                    kind TEXT NOT NULL,
                    text_parts_json TEXT NOT NULL,
                    attachment_path TEXT,
                    caption TEXT,
                    status TEXT NOT NULL DEFAULT 'DRAFT',
                    delay_ms INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS sku_sightings (
                    sig TEXT PRIMARY KEY,
                    supplier_key TEXT,
                    seen_at REAL NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1
                )"""
            )
            c.execute("""CREATE TABLE IF NOT EXISTS outbound_dispatch (
                id TEXT PRIMARY KEY,
                account_phone TEXT NOT NULL,
                expires_at REAL NOT NULL,
                session_id TEXT,
                claim_token TEXT
            )""")
            c.execute(
                """CREATE TABLE IF NOT EXISTS kv_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            # Startup crash recovery: IN_FLIGHT -> UNKNOWN, CLAIMED -> READY
            c.execute(
                """UPDATE outbound_queue
                   SET status=CASE status WHEN 'IN_FLIGHT' THEN 'UNKNOWN' ELSE 'READY' END, updated_at=?
                   WHERE status IN ('CLAIMED', 'IN_FLIGHT')""",
                (time.time(),),
            )
            c.execute("DELETE FROM outbound_dispatch WHERE id IN (SELECT id FROM outbound_queue WHERE status='UNKNOWN')")
            c.commit()

    def get_local_state(self, key):
        with self._lock:
            row = self._conn.execute("SELECT value_json FROM kv_state WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            try:
                return json.loads(row["value_json"])
            except Exception:
                return None

    def set_local_state(self, key, value):
        with self._lock:
            self._conn.execute(
                """INSERT INTO kv_state(key, value_json, updated_at) VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def delete_local_state(self, key):
        with self._lock:
            self._conn.execute("DELETE FROM kv_state WHERE key=?", (key,))
            self._conn.commit()

    def invalidate(self, *collections):
        # Local SQLite is always immediately consistent; kept for interface compatibility.
        return None

    def close(self):
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    @staticmethod
    def _row_to_record(row):
        return {
            "id": row["id"],
            "revision": int(row["revision"]),
            "updated_at": row["updated_at"],
            "data": json.loads(row["data_json"]),
        }

    def get_sync(self, collection, record_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT id, revision, updated_at, data_json FROM records WHERE collection=? AND id=?",
                (collection, record_id),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def list_sync(self, collection, limit=100, *, offset=0):
        lim = max(1, min(int(limit or 100), 1000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, revision, updated_at, data_json FROM records WHERE collection=? ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (collection, lim, offset),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def record_stage_event(self, source_message_id, stage, outcome, reason_code, *,
                           conversation_id=None, account_phone=None, message_revision=0,
                           attempt_id=None, retryable=False, metadata=None, occurred_at=None):
        now = float(occurred_at if occurred_at is not None else time.time())
        meta = metadata or {}
        att_id = str(attempt_id or uuid.uuid4().hex[:12])
        event_id = f"evt_{str(source_message_id)[:16]}_{int(now*1000)}_{secrets.token_hex(4)}"
        with self._lock, self._conn:
            c = self._conn
            # Deduplicate identical event attempt idempotently
            existing = c.execute(
                """SELECT event_id, sequence, occurred_at FROM message_stage_events
                   WHERE source_message_id=? AND stage=? AND outcome=? AND reason_code=? AND attempt_id=?""",
                (source_message_id, stage, outcome, reason_code, att_id),
            ).fetchone()
            if existing:
                return {
                    "schema_version": 1,
                    "event_id": existing["event_id"],
                    "source_message_id": source_message_id,
                    "conversation_id": conversation_id,
                    "account_phone": account_phone,
                    "message_revision": message_revision,
                    "sequence": int(existing["sequence"]),
                    "attempt_id": att_id,
                    "stage": stage,
                    "outcome": outcome,
                    "reason_code": reason_code,
                    "retryable": bool(retryable),
                    "metadata": meta,
                    "occurred_at": float(existing["occurred_at"]),
                }

            row_seq = c.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM message_stage_events WHERE source_message_id=?",
                (source_message_id,),
            ).fetchone()
            seq = int(row_seq["next_seq"]) if row_seq else 1

            c.execute(
                """INSERT INTO message_stage_events(
                    event_id, source_message_id, conversation_id, account_phone,
                    message_revision, sequence, attempt_id, stage, outcome,
                    reason_code, retryable, metadata_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, source_message_id, conversation_id, account_phone,
                    message_revision, seq, att_id, stage, outcome,
                    reason_code, 1 if retryable else 0, json.dumps(meta, ensure_ascii=False), now,
                ),
            )

            # Keep message record fields synchronized in records table
            row_msg = c.execute(
                "SELECT id, revision, data_json FROM records WHERE collection='messages' AND (id=? OR json_extract(data_json, '$.message_id')=?)",
                (source_message_id, source_message_id),
            ).fetchone()
            if row_msg:
                try:
                    data = json.loads(row_msg["data_json"])
                    data["current_stage"] = stage
                    data["stage_outcome"] = outcome
                    data["reason"] = reason_code
                    data["last_progress_at"] = now
                    if "model_requested" in meta and meta["model_requested"] is not None:
                        data["model_requested"] = meta["model_requested"]
                    if "model_served" in meta and meta["model_served"] is not None:
                        data["model_served"] = meta["model_served"]
                    c.execute(
                        "UPDATE records SET data_json=?, updated_at=? WHERE collection='messages' AND id=?",
                        (json.dumps(data, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), row_msg["id"]),
                    )
                except Exception:
                    pass

            return {
                "schema_version": 1,
                "event_id": event_id,
                "source_message_id": source_message_id,
                "conversation_id": conversation_id,
                "account_phone": account_phone,
                "message_revision": message_revision,
                "sequence": seq,
                "attempt_id": att_id,
                "stage": stage,
                "outcome": outcome,
                "reason_code": reason_code,
                "retryable": bool(retryable),
                "metadata": meta,
                "occurred_at": now,
            }

    def get_message_trace(self, source_message_id):
        with self._lock:
            c = self._conn
            row_msg = c.execute(
                "SELECT id, revision, updated_at, data_json FROM records WHERE collection='messages' AND (id=? OR json_extract(data_json, '$.message_id')=?)",
                (source_message_id, source_message_id),
            ).fetchone()

            canonical_id = row_msg["id"] if row_msg else source_message_id
            data = json.loads(row_msg["data_json"]) if row_msg else {}
            msg_id = data.get("message_id") or source_message_id
            conv_id = data.get("conversation_id")

            rows_events = c.execute(
                """SELECT event_id, source_message_id, conversation_id, account_phone,
                          message_revision, sequence, attempt_id, stage, outcome,
                          reason_code, retryable, metadata_json, occurred_at
                   FROM message_stage_events
                   WHERE source_message_id=? OR source_message_id=?
                   ORDER BY sequence ASC""",
                (canonical_id, msg_id),
            ).fetchall()

            if not row_msg and not rows_events:
                return {"schema_version": 1, "found": False, "message_id": source_message_id}

            events = []
            for r in rows_events:
                try:
                    meta = json.loads(r["metadata_json"])
                except Exception:
                    meta = {}
                events.append({
                    "schema_version": 1,
                    "event_id": r["event_id"],
                    "source_message_id": r["source_message_id"],
                    "conversation_id": r["conversation_id"],
                    "account_phone": r["account_phone"],
                    "message_revision": int(r["message_revision"]),
                    "sequence": int(r["sequence"]),
                    "attempt_id": r["attempt_id"],
                    "stage": r["stage"],
                    "outcome": r["outcome"],
                    "reason_code": r["reason_code"],
                    "retryable": bool(r["retryable"]),
                    "metadata": meta,
                    "occurred_at": float(r["occurred_at"]),
                })

            if not conv_id and events:
                conv_id = events[0].get("conversation_id")

            last_evt = events[-1] if events else None

            decisions = []
            row_req = c.execute(
                "SELECT id, revision, data_json FROM records WHERE collection='requests' AND (id=? OR json_extract(data_json, '$.source_message')=?)",
                (canonical_id, canonical_id),
            ).fetchone()
            if row_req:
                try:
                    decisions.append({"collection": "requests", "id": row_req["id"], "data": json.loads(row_req["data_json"])})
                except Exception:
                    pass

            row_off = c.execute(
                "SELECT id, revision, data_json FROM records WHERE collection='offers' AND (id=? OR json_extract(data_json, '$.source_message')=?)",
                (canonical_id, canonical_id),
            ).fetchone()
            if row_off:
                try:
                    decisions.append({"collection": "offers", "id": row_off["id"], "data": json.loads(row_off["data_json"])})
                except Exception:
                    pass

            outbounds = []
            rows_out = c.execute(
                """SELECT id, recipient_jid, recipient_phone, kind, status, created_at, updated_at, meta_json
                   FROM outbound_queue
                   WHERE json_extract(meta_json, '$.source_message')=? OR json_extract(meta_json, '$.source_message')=?""",
                (canonical_id, msg_id),
            ).fetchall()
            for r in rows_out:
                try:
                    meta = json.loads(r["meta_json"])
                except Exception:
                    meta = {}
                outbounds.append({
                    "id": r["id"],
                    "recipient_jid": r["recipient_jid"],
                    "recipient_phone": r["recipient_phone"],
                    "kind": r["kind"],
                    "status": r["status"],
                    "created_at": float(r["created_at"]),
                    "updated_at": float(r["updated_at"]),
                    "meta": meta,
                })

            media_job = data.get("media_job") or {
                "status": "NONE" if not data.get("has_media") else ("WAITING_BYTES" if data.get("reason") == "MEDIA_BYTES_REQUIRED" else "ENRICHED"),
                "attempts": 0,
                "deadline": None,
                "last_error": None,
                "retryable": True,
            }

            return {
                "schema_version": 1,
                "found": True,
                "id": canonical_id,
                "message_id": msg_id,
                "conversation_id": conv_id,
                "phone": data.get("phone"),
                "direction": data.get("direction", "incoming"),
                "text": data.get("text", ""),
                "state": data.get("state"),
                "current_stage": data.get("current_stage") or (last_evt["stage"] if last_evt else ("AI_CLASSIFICATION" if data.get("state") == "CLASSIFIED" else "CAPTURE")),
                "stage_outcome": data.get("stage_outcome") or (last_evt["outcome"] if last_evt else ("SUCCESS" if data.get("state") in ("CLASSIFIED", "OUTGOING") else "PENDING")),
                "reason": data.get("reason") or (last_evt["reason_code"] if last_evt else None),
                "processing_stage": data.get("processing_stage"),
                "last_progress_at": data.get("last_progress_at") or (last_evt["occurred_at"] if last_evt else float(data.get("received") or 0.0)),
                "classification_source": data.get("classification_source"),
                "model_requested": data.get("model_requested"),
                "model_served": data.get("model_served"),
                "triage": data.get("triage"),
                "media_job": media_job,
                "trade_action": data.get("trade_action"),
                "outbound_id": data.get("outbound_id"),
                "events": events,
                "decisions": decisions,
                "outbound_attempts": outbounds,
            }

    def pending_classification_messages(self, limit=20):
        lim = max(1, min(int(limit or 20), 100))
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, revision, updated_at, data_json FROM records
                   WHERE collection='messages'
                     AND json_extract(data_json, '$.state') IN ('RECEIVED', 'NEEDS_REVIEW')
                     AND COALESCE(json_extract(data_json, '$.reason'), '') NOT IN (
                         'MEDIA_BYTES_REQUIRED', 'MEDIA_STOCK_UNCONFIRMED', 'MEDIA_TIMEOUT',
                         'IGNORED_OWNER', 'IGNORED_SOURCE', 'IGNORED_FAMILY', 'IGNORED_SOCIAL',
                         'INVALID_STOCK_FILE', 'STOCK_FILE_BYTES_REQUIRED'
                     )
                     AND COALESCE(json_extract(data_json, '$.classifier_attempts'), 0) < 3
                   ORDER BY updated_at ASC, id ASC
                   LIMIT ?""",
                (lim,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def pending_evaluation_messages(self, limit=20):
        lim = max(1, min(int(limit or 20), 100))
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, revision, updated_at, data_json FROM records
                   WHERE collection='messages'
                     AND json_extract(data_json, '$.state') = 'CLASSIFIED'
                     AND json_extract(data_json, '$.trade_evaluation_pending') = 1
                   ORDER BY updated_at ASC, id ASC
                   LIMIT ?""",
                (lim,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def pending_media_messages(self, limit=20):
        lim = max(1, min(int(limit or 20), 100))
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, revision, updated_at, data_json FROM records
                   WHERE collection='messages'
                     AND json_extract(data_json, '$.reason') = 'MEDIA_BYTES_REQUIRED'
                   ORDER BY updated_at ASC, id ASC
                   LIMIT ?""",
                (lim,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def update_media_job(self, message_id, *, status, deadline=None, last_error=None, retryable=True):
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id, revision, data_json FROM records WHERE collection='messages' AND (id=? OR json_extract(data_json, '$.message_id')=?)",
                (message_id, message_id),
            ).fetchone()
            if not row:
                return None
            data = json.loads(row["data_json"])
            job = data.get("media_job") or {}
            attempts = job.get("attempts", 0) + 1
            first_requested = job.get("first_requested_at") or now
            dl = deadline if deadline is not None else job.get("deadline")
            job_update = {
                "status": status,
                "attempts": attempts,
                "first_requested_at": first_requested,
                "last_attempt_at": now,
                "deadline": dl,
                "bytes_hash": job.get("bytes_hash"),
                "has_caption": job.get("has_caption", False),
                "caption_text": job.get("caption_text"),
                "ocr_status": "FAILED" if status in ("TIMEOUT", "FAILED") else job.get("ocr_status", "PENDING"),
                "last_error": last_error,
                "retryable": retryable,
            }
            data["media_job"] = job_update
            if status == "TIMEOUT":
                data["reason"] = "MEDIA_TIMEOUT"
                data["current_stage"] = "MEDIA_EXTRACTION"
                data["stage_outcome"] = "BLOCKED"
                data["processing_stage"] = "Waiting for media bytes timed out"
                data["last_progress_at"] = now
                if job.get("has_caption"):
                    data["trade_evaluation_pending"] = False
            self._conn.execute(
                "UPDATE records SET data_json=?, updated_at=? WHERE collection='messages' AND id=?",
                (json.dumps(data, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            return job_update

    def message_counts(self):
        with self._lock:
            row = self._conn.execute("""SELECT count(*) AS captured,
                count(*) FILTER (WHERE json_extract(data_json, '$.state')='CLASSIFIED') AS classified,
                count(*) FILTER (WHERE json_extract(data_json, '$.state') IN ('NEEDS_REVIEW','RECEIVED')
                    OR json_extract(data_json, '$.triage.needs_review')=1) AS needs_review,
                count(*) FILTER (WHERE json_extract(data_json, '$.triage.category')='BUY') AS buy,
                count(*) FILTER (WHERE json_extract(data_json, '$.triage.category')='SELL') AS sell,
                count(*) FILTER (WHERE json_extract(data_json, '$.triage.category')='STOCK') AS stock,
                count(*) FILTER (WHERE json_extract(data_json, '$.stock_source')=1) AS sources,
                count(*) FILTER (WHERE json_extract(data_json, '$.state')='IGNORED_SOCIAL'
                    OR json_extract(data_json, '$.triage.category')='OTHER') AS social,
                count(*) FILTER (WHERE json_extract(data_json, '$.direction')='outgoing'
                    OR json_extract(data_json, '$.state')='OUTGOING') AS outgoing
                FROM records WHERE collection='messages'""").fetchone()
            return dict(row)

    def message_page(self, limit, offset, *, category="ALL", person="", query=""):
        category_sql = """CASE
            WHEN json_extract(data_json, '$.direction')='outgoing' OR json_extract(data_json, '$.state')='OUTGOING' THEN 'OUTGOING'
            WHEN json_extract(data_json, '$.state')='IGNORED_SOCIAL' OR json_extract(data_json, '$.triage.category')='OTHER' THEN 'SOCIAL'
            ELSE json_extract(data_json, '$.triage.category') END"""
        person_sql = """COALESCE(NULLIF(json_extract(data_json, '$.phone'), ''),
            NULLIF(json_extract(data_json, '$.sender_id'), ''),
            NULLIF(json_extract(data_json, '$.conversation_id'), ''))"""
        conditions = ["collection='messages'"]
        args = []
        if category == "REVIEW":
            conditions.append("""(json_extract(data_json, '$.state') IN ('NEEDS_REVIEW','RECEIVED')
                OR json_extract(data_json, '$.triage.needs_review')=1)""")
        elif category == "WORK":
            conditions.append(f"COALESCE(({category_sql}), '') != 'SOCIAL'")
        elif category == "IMPORTANT":
            conditions.append(f"""(({category_sql}) IN ('BUY','SELL','STOCK') OR
                (json_extract(data_json, '$.state')='NEEDS_REVIEW' AND COALESCE(({category_sql}), '')!='SOCIAL'))""")
        elif category == "SOURCE":
            conditions.append("json_extract(data_json, '$.stock_source')=1")
        elif category != "ALL":
            conditions.append(f"({category_sql})=?")
            args.append(category)
        if person:
            conditions.append(f"({person_sql})=?")
            args.append(person)
        if query:
            fields = ("sender_name", "sender_label", "chat_name", "phone", "text", "triage.category")
            searchable = " || ' ' || ".join(f"COALESCE(json_extract(data_json, '$.{field}'), '')" for field in fields)
            conditions.append(f"instr(lower({searchable}), lower(?)) > 0")
            args.append(query)
        where = " AND ".join(conditions)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, revision, updated_at, data_json FROM records WHERE {where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit + 1, offset),
            ).fetchall()
            return [self._row_to_record(row) for row in rows[:limit]], len(rows) > limit

    def message_people(self):
        with self._lock:
            rows = self._conn.execute("""SELECT person, max(name) AS name FROM (
                SELECT COALESCE(NULLIF(json_extract(data_json, '$.phone'), ''),
                    NULLIF(json_extract(data_json, '$.sender_id'), ''),
                    NULLIF(json_extract(data_json, '$.conversation_id'), '')) AS person,
                    COALESCE(NULLIF(json_extract(data_json, '$.sender_name'), ''),
                        NULLIF(json_extract(data_json, '$.sender_label'), ''),
                        json_extract(data_json, '$.chat_name')) AS name
                FROM records WHERE collection='messages')
                WHERE person IS NOT NULL GROUP BY person ORDER BY person LIMIT 1000""").fetchall()
            return [dict(row) for row in rows]

    def _upsert_local(self, collection, record_id, data, revision, updated_at):
        encoded = json.dumps(data, ensure_ascii=False)
        with self._lock, self._conn:
            if self._conn.execute(
                "SELECT 1 FROM sheets_outbox WHERE collection=? AND id=?", (collection, record_id)
            ).fetchone():
                return
            remote_revision = self._sheet_revision(collection, record_id)
            if remote_revision is not None and revision <= remote_revision:
                return
            existing = self._conn.execute("SELECT revision FROM records WHERE collection=? AND id=?",
                                          (collection, record_id)).fetchone()
            local_revision = max(int(revision), int(existing["revision"]) + 1 if existing else 0)
            self._conn.execute(
                """INSERT INTO records(collection, id, revision, updated_at, data_json)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(collection, id) DO UPDATE SET
                     revision=excluded.revision,
                     updated_at=excluded.updated_at,
                     data_json=excluded.data_json""",
                (collection, record_id, local_revision, updated_at, encoded),
            )
            self._remember_sheet_revision(collection, record_id, revision)

    def _sheet_revision(self, collection, record_id):
        row = self._conn.execute("SELECT revision FROM sheets_revisions WHERE collection=? AND id=?",
                                 (collection, record_id)).fetchone()
        return int(row["revision"]) if row else None

    def _remember_sheet_revision(self, collection, record_id, revision):
        self._conn.execute(
            """INSERT INTO sheets_revisions(collection,id,revision) VALUES(?,?,?)
               ON CONFLICT(collection,id) DO UPDATE SET revision=excluded.revision""",
            (collection, record_id, revision),
        )

    async def get(self, collection, record_id, *, force=False):
        local = None if force else self.get_sync(collection, record_id)
        if local is not None:
            return local
        try:
            remote = await self.remote.get(collection, record_id)
        except Exception:
            return self.get_sync(collection, record_id)
        if remote is not None:
            self._upsert_local(collection, record_id, remote["data"], remote.get("revision", 1), remote.get("updated_at") or datetime.now(timezone.utc).isoformat())
        return self.get_sync(collection, record_id)

    async def list(self, collection, limit=100, *, force=False):
        local = [] if force else self.list_sync(collection, limit)
        if local:
            return local
        try:
            rows = await self.remote.list(collection, limit)
        except Exception:
            return self.list_sync(collection, limit)
        for row in rows:
            self._upsert_local(collection, row["id"], row["data"], row.get("revision", 1), row.get("updated_at") or datetime.now(timezone.utc).isoformat())
        return self.list_sync(collection, limit)

    async def put(self, collection, record_id, data, expected_revision, request_id=None):
        records = await self.batch([{"collection": collection, "id": record_id, "expected_revision": expected_revision, "data": data}], request_id)
        return records[0]

    @staticmethod
    def _prepare_sheets_payload(payload):
        """Sanitize payload for Google Sheets sync (strip heavy binary media blobs)."""
        if not isinstance(payload, dict):
            return payload
        clean = copy.deepcopy(payload)
        # Strip raw binary media (media_b64, raw_media)
        if "media_b64" in clean and clean["media_b64"]:
            mime = clean.get("media_mime_type", "image")
            clean["media_b64"] = f"[media attached: {mime}]"
        if "raw_media" in clean and clean["raw_media"]:
            clean["raw_media"] = "[raw media stripped]"
        return clean

    async def batch(self, changes, request_id=None):
        now_iso = datetime.now(timezone.utc).isoformat()
        now_ts = time.time()
        out = []
        with self._lock:
            c = self._conn
            try:
                c.execute("BEGIN IMMEDIATE")
                for ch in changes:
                    col = ch["collection"]
                    rid = ch["id"]
                    exp = int(ch["expected_revision"])
                    payload = copy.deepcopy(ch["data"])
                    row = c.execute(
                        "SELECT revision FROM records WHERE collection=? AND id=?",
                        (col, rid),
                    ).fetchone()
                    cur_rev = int(row["revision"]) if row else 0
                    if cur_rev != exp:
                        c.execute("ROLLBACK")
                        # Plain code: callers retry on error.code == "REVISION_CONFLICT".
                        raise SheetsError("REVISION_CONFLICT")
                    next_rev = cur_rev + 1
                    encoded = json.dumps(payload, ensure_ascii=False)
                    c.execute(
                        """INSERT INTO records(collection, id, revision, updated_at, data_json)
                           VALUES(?, ?, ?, ?, ?)
                           ON CONFLICT(collection, id) DO UPDATE SET
                             revision=excluded.revision,
                             updated_at=excluded.updated_at,
                             data_json=excluded.data_json""",
                        (col, rid, next_rev, now_iso, encoded),
                    )
                    # Track lowest expected revision not yet acknowledged by Google Sheets
                    existing_out = c.execute(
                        "SELECT expected_revision FROM sheets_outbox WHERE collection=? AND id=?",
                        (col, rid),
                    ).fetchone()
                    known_remote = self._sheet_revision(col, rid)
                    remote_exp = int(existing_out["expected_revision"]) if existing_out else (
                        known_remote if known_remote is not None else exp
                    )
                    sheets_payload = self._prepare_sheets_payload(payload)
                    sheets_encoded = json.dumps(sheets_payload, ensure_ascii=False)
                    c.execute(
                        """INSERT INTO sheets_outbox(collection, id, expected_revision, data_json, queued_at, attempts)
                           VALUES(?, ?, ?, ?, ?, 0)
                           ON CONFLICT(collection, id) DO UPDATE SET
                             data_json=excluded.data_json,
                             queued_at=excluded.queued_at,
                             attempts=0,
                             last_error=NULL""",
                        (col, rid, remote_exp, sheets_encoded, now_ts),
                    )
                    out.append({"id": rid, "revision": next_rev, "updated_at": now_iso, "data": payload})
                c.commit()
            except Exception:
                try:
                    if c.in_transaction:
                        c.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        self.schedule_flush()
        return out

    def schedule_flush(self):
        try:
            loop = asyncio.get_running_loop()
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = loop.create_task(self.flush_outbox())
        except RuntimeError:
            pass

    def has_pending_sheet_writes(self):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM sheets_outbox LIMIT 1").fetchone() is not None

    def sheet_sync_status(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT last_error, COUNT(*) AS count FROM sheets_outbox GROUP BY last_error"
            ).fetchall()
        return {"pending": sum(row["count"] for row in rows),
                "errors": {row["last_error"]: row["count"] for row in rows if row["last_error"]}}

    async def flush_outbox(self, max_batch=15):
        """Sync valid rows while keeping conflicts and oversized rows visible for review."""
        async with self._flush_lock:
            flushed = 0
            permanent = {"REVISION_CONFLICT", "RECORD_TOO_LARGE", "BATCH_TOO_LARGE",
                         "INVALID_CHANGE", "IDEMPOTENCY_CONFLICT"}
            while True:
                with self._lock:
                    rows = self._conn.execute(
                        """SELECT collection,id,expected_revision,data_json FROM sheets_outbox
                           WHERE last_error IS NULL OR last_error NOT IN
                             ('REVISION_CONFLICT','RECORD_TOO_LARGE','BATCH_TOO_LARGE','INVALID_CHANGE','IDEMPOTENCY_CONFLICT')
                           ORDER BY queued_at ASC LIMIT ?""", (max_batch,),
                    ).fetchall()
                if not rows:
                    return {"flushed": flushed}
                batch = []
                size = 0
                for row in rows:
                    row_size = len(row["data_json"].encode("utf-8"))
                    if row_size > 16000:
                        self._mark_sheet_error([row], "RECORD_TOO_LARGE")
                        continue
                    # Leave room for IDs, mutation metadata, and the signed transport envelope.
                    if size + row_size + 512 > 34000:
                        break
                    batch.append(row)
                    size += row_size + 512
                if not batch:
                    continue
                if not hasattr(self.remote, "batch"):
                    batch = batch[:1]
                try:
                    await self._sync_sheet_rows(batch)
                    flushed += len(batch)
                except SheetsError as error:
                    error.code = str(error.code).split(":", 1)[0].strip()
                    if error.code in permanent and len(batch) > 1:
                        # A rejected Sheets batch applies no rows. Isolate the failing row.
                        for row in batch:
                            try:
                                await self._sync_sheet_rows([row])
                                flushed += 1
                            except SheetsError as row_error:
                                row_error.code = str(row_error.code).split(":", 1)[0].strip()
                                if row_error.code == "REVISION_CONFLICT" and await self._resolve_conflict(row):
                                    flushed += 1
                                    continue
                                self._mark_sheet_error([row], row_error.code)
                                if row_error.code not in permanent:
                                    return {"flushed": flushed, "error": row_error.code}
                    elif error.code == "REVISION_CONFLICT" and len(batch) == 1 and await self._resolve_conflict(batch[0]):
                        flushed += 1
                    else:
                        self._mark_sheet_error(batch, error.code)
                        if error.code not in permanent:
                            return {"flushed": flushed, "error": error.code}
                except Exception:
                    self._mark_sheet_error(batch, "SHEETS_UNAVAILABLE")
                    return {"flushed": flushed, "error": "SHEETS_UNAVAILABLE"}

    async def _resolve_conflict(self, row):
        """Rebase a conflicting write on the Sheet's current revision.

        Before this, any revision drift (a manual Sheet edit, a lost
        acknowledgement) left the row stuck forever and the Sheet stopped
        receiving that record. Newest change wins: for operator-editable
        collections a newer Sheet edit is pulled into the app; otherwise the
        local record is written over the Sheet row."""
        collection, record_id = row["collection"], row["id"]
        try:
            if hasattr(self.remote, "get"):
                remote = await self.remote.get(collection, record_id)
            else:
                remote = next((r for r in await self.remote.list(collection, 1000) if r.get("id") == record_id), None)
        except Exception:
            return False
        remote_rev = int(remote.get("revision", 0)) if remote else 0
        with self._lock:
            queued = self._conn.execute(
                "SELECT queued_at FROM sheets_outbox WHERE collection=? AND id=? AND data_json=?",
                (collection, record_id, row["data_json"]),
            ).fetchone()
        if not queued:
            return True
        if remote and collection in ("inventory", "configuration"):
            try:
                remote_ts = datetime.fromisoformat(str(remote.get("updated_at")).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                remote_ts = 0
            if remote_ts > float(queued["queued_at"]):
                with self._lock, self._conn:
                    self._conn.execute("DELETE FROM sheets_outbox WHERE collection=? AND id=? AND data_json=?",
                                       (collection, record_id, row["data_json"]))
                    self._conn.execute("DELETE FROM sheets_revisions WHERE collection=? AND id=?", (collection, record_id))
                self._upsert_local(collection, record_id, remote["data"], remote_rev,
                                   remote.get("updated_at") or datetime.now(timezone.utc).isoformat())
                return True
        rebased = {**dict(row), "expected_revision": remote_rev}
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sheets_outbox SET expected_revision=? WHERE collection=? AND id=? AND data_json=?",
                (remote_rev, collection, record_id, row["data_json"]),
            )
        try:
            await self._sync_sheet_rows([rebased])
            return True
        except Exception:
            return False

    async def _sync_sheet_rows(self, rows):
        changes = [{"collection": row["collection"], "id": row["id"],
                    "expected_revision": int(row["expected_revision"]), "data": json.loads(row["data_json"])}
                   for row in rows]
        identity = json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if hasattr(self.remote, "batch"):
            await self.remote.batch(changes, request_id=request_id)
        else:
            change = changes[0]
            await self.remote.put(change["collection"], change["id"], change["data"],
                                  change["expected_revision"], request_id=request_id)
        with self._lock, self._conn:
            for row in rows:
                key = (row["collection"], row["id"])
                acknowledged = int(row["expected_revision"]) + 1
                self._remember_sheet_revision(*key, acknowledged)
                self._conn.execute(
                    "DELETE FROM sheets_outbox WHERE collection=? AND id=? AND data_json=?",
                    (*key, row["data_json"]),
                )
                # The operator may have edited a record while the Sheets write was in flight.
                self._conn.execute(
                    "UPDATE sheets_outbox SET expected_revision=? WHERE collection=? AND id=?",
                    (acknowledged, *key),
                )

    def _quarantine_sheet_row(self, row, reason):
        with self._lock, self._conn:
            qid = f"{row['collection']}:{row['id']}:{int(time.time() * 1000)}"
            self._conn.execute(
                """INSERT INTO sheets_quarantine(id, collection, record_id, expected_revision, error_reason, payload_json, quarantined_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (qid, row["collection"], row["id"], int(row["expected_revision"]), str(reason), row["data_json"], time.time()),
            )
            self._conn.execute(
                "DELETE FROM sheets_outbox WHERE collection=? AND id=?",
                (row["collection"], row["id"]),
            )

    def _mark_sheet_error(self, rows, code):
        to_quarantine = []
        to_update = []
        with self._lock, self._conn:
            for row in rows:
                current = self._conn.execute(
                    "SELECT attempts FROM sheets_outbox WHERE collection=? AND id=?",
                    (row["collection"], row["id"]),
                ).fetchone()
                attempts = (int(current["attempts"]) + 1) if current else 1
                # Network/quota failures are temporary. Quarantining them after
                # five 4-second retries silently stopped Sheet sync after any
                # short outage. Only permanent rejections are quarantined.
                # A row the Sheet rejects as invalid can never succeed; park it in
                # quarantine (kept for review) so it never blocks the queue.
                if (attempts >= 5 and code in self._PERMANENT_SHEET_ERRORS) or (
                        len(rows) == 1 and code in ("INVALID_CHANGE", "IDEMPOTENCY_CONFLICT")):
                    to_quarantine.append((row, f"{code} (attempts={attempts})"))
                else:
                    to_update.append((code, row["collection"], row["id"], row["data_json"]))

            if to_update:
                self._conn.executemany(
                    """UPDATE sheets_outbox SET attempts=attempts+1,last_error=?
                       WHERE collection=? AND id=? AND data_json=?""",
                    to_update,
                )
            for row, reason in to_quarantine:
                qid = f"{row['collection']}:{row['id']}:{int(time.time() * 1000)}"
                self._conn.execute(
                    """INSERT INTO sheets_quarantine(id, collection, record_id, expected_revision, error_reason, payload_json, quarantined_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (qid, row["collection"], row["id"], int(row["expected_revision"]), str(reason), row["data_json"], time.time()),
                )
                self._conn.execute(
                    "DELETE FROM sheets_outbox WHERE collection=? AND id=?",
                    (row["collection"], row["id"]),
                )
        if self.on_sync_error:
            self.on_sync_error(SheetsError(code))

    def quarantine_record(self, collection, record_id, reason="MANUAL_QUARANTINE"):
        """Explicitly move an outbox record into sheets_quarantine."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT collection, id, expected_revision, data_json, attempts, last_error FROM sheets_outbox WHERE collection=? AND id=?",
                (collection, record_id),
            ).fetchone()
            if not row:
                return False
            qid = f"{collection}:{record_id}:{int(time.time() * 1000)}"
            err_reason = f"{reason} (last_error={row['last_error']}, attempts={row['attempts']})"
            self._conn.execute(
                """INSERT INTO sheets_quarantine(id, collection, record_id, expected_revision, error_reason, payload_json, quarantined_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (qid, collection, record_id, int(row["expected_revision"]), err_reason, row["data_json"], time.time()),
            )
            self._conn.execute(
                "DELETE FROM sheets_outbox WHERE collection=? AND id=?",
                (collection, record_id),
            )
            return True

    def quarantine_stalled_writes(self, max_attempts=3):
        """Quarantine records in sheets_outbox that have permanent errors or exceeded retry limits."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                """SELECT collection, id, expected_revision, data_json, last_error, attempts
                   FROM sheets_outbox
                   WHERE last_error IN ('RECORD_TOO_LARGE', 'BATCH_TOO_LARGE', 'INVALID_CHANGE', 'IDEMPOTENCY_CONFLICT')
                      OR attempts >= ?""",
                (max_attempts,),
            ).fetchall()
            for row in rows:
                qid = f"{row['collection']}:{row['id']}:{int(time.time() * 1000)}"
                reason = f"{row['last_error'] or 'MAX_ATTEMPTS_EXCEEDED'} (attempts={row['attempts']})"
                self._conn.execute(
                    """INSERT INTO sheets_quarantine(id, collection, record_id, expected_revision, error_reason, payload_json, quarantined_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (qid, row["collection"], row["id"], int(row["expected_revision"]), reason, row["data_json"], time.time()),
                )
                self._conn.execute(
                    "DELETE FROM sheets_outbox WHERE collection=? AND id=?",
                    (row["collection"], row["id"]),
                )
            return len(rows)

    def list_quarantined_sheet_writes(self, limit=100):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, collection, record_id, expected_revision, error_reason, payload_json, quarantined_at FROM sheets_quarantine ORDER BY quarantined_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    async def hydrate_from_remote(self, collections=("configuration", "inventory", "messages", "requests", "offers", "tasks", "fx", "news")):
        """Pull confirmed remote revisions without replacing pending local edits."""
        pulled = {}
        for collection in collections:
            try:
                rows = await self.remote.list(collection, 500)
            except Exception:
                pulled[collection] = None
                continue
            remote_ids = set()
            for row in rows:
                remote_ids.add(row["id"])
                self._upsert_local(collection, row["id"], row["data"], row["revision"], row["updated_at"])
            # Prune authoritative records (inventory, offers) deleted on remote that have no pending local edits
            if collection in ("inventory", "offers") and rows:
                with self._lock, self._conn:
                    local_rows = self._conn.execute(
                        "SELECT id FROM records WHERE collection=?", (collection,)
                    ).fetchall()
                    for lr in local_rows:
                        lid = lr["id"]
                        if lid not in remote_ids:
                            is_pending = self._conn.execute(
                                "SELECT 1 FROM sheets_outbox WHERE collection=? AND id=?", (collection, lid)
                            ).fetchone()
                            if not is_pending:
                                self._conn.execute("DELETE FROM records WHERE collection=? AND id=?", (collection, lid))
            pulled[collection] = len(rows)
        return pulled

    async def health(self):
        return await self.remote.health()

    # --- Chat History (Unlimited Local Threads) ---
    def save_chat_turn(self, turn):
        with self._lock:
            self._conn.execute(
                """INSERT INTO chat_turns(request_id, chat_id, question, answer, model, at, meta_json)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                     answer=excluded.answer, model=excluded.model, at=excluded.at, meta_json=excluded.meta_json""",
                (
                    turn["request_id"],
                    turn.get("chat_id") or "legacy",
                    turn.get("question") or "",
                    turn.get("answer") or "",
                    turn.get("model") or "",
                    turn.get("at") or datetime.now(timezone.utc).isoformat(),
                    json.dumps(turn, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def list_chat_turns(self, limit=250):
        with self._lock:
            rows = self._conn.execute(
                "SELECT meta_json FROM chat_turns ORDER BY at DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
            out = [json.loads(r["meta_json"]) for r in rows]
            out.reverse()
            return out

    # --- Contact Profiles, Category Tagging, Watermarks & Auto-Mute ---
    def upsert_contact(self, conversation_id, *, phone=None, name=None, category=None, is_announcement=None, last_scanned_ts=None, manual_mute_seconds=None, last_contacted_at=None):
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT * FROM contact_profiles WHERE conversation_id=?", (conversation_id,)).fetchone()
            cur_phone = phone or (row["phone"] if row else None)
            cur_name = name or (row["name"] if row else None)
            cur_cat = category or (row["category"] if row else "UNKNOWN")
            cur_mode = row["bot_mode"] if row else "DRAFT"
            cur_mute = float(row["manual_mute_until"]) if row else 0.0
            if manual_mute_seconds is not None:
                cur_mute = max(cur_mute, now + float(manual_mute_seconds))
            cur_scan = float(row["last_scanned_ts"]) if row else 0.0
            if last_scanned_ts is not None:
                cur_scan = max(cur_scan, float(last_scanned_ts))
            cur_contacted = float(row["last_contacted_at"]) if row else 0.0
            if last_contacted_at is not None:
                cur_contacted = max(cur_contacted, float(last_contacted_at))
            cur_ann = int(bool(is_announcement)) if is_announcement is not None else (int(row["is_announcement"]) if row else 0)
            self._conn.execute(
                """INSERT INTO contact_profiles(conversation_id, phone, name, category, bot_mode, manual_mute_until, last_scanned_ts, last_contacted_at, is_announcement, meta_json, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                     phone=COALESCE(excluded.phone, contact_profiles.phone),
                     name=COALESCE(excluded.name, contact_profiles.name),
                     category=excluded.category,
                     manual_mute_until=excluded.manual_mute_until,
                     last_scanned_ts=excluded.last_scanned_ts,
                     last_contacted_at=excluded.last_contacted_at,
                     is_announcement=excluded.is_announcement,
                     updated_at=excluded.updated_at""",
                (conversation_id, cur_phone, cur_name, cur_cat, cur_mode, cur_mute, cur_scan, cur_contacted, cur_ann, now),
            )
            self._conn.commit()

    def get_contact(self, conversation_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM contact_profiles WHERE conversation_id=?", (conversation_id,)).fetchone()
            return dict(row) if row else None

    def list_contacts(self, *, category=None, days=None):
        with self._lock:
            q = "SELECT * FROM contact_profiles WHERE 1=1"
            args = []
            if category:
                q += " AND category=?"
                args.append(category)
            if days is not None:
                q += " AND last_contacted_at >= ?"
                args.append(time.time() - float(days) * 86400.0)
            q += " ORDER BY updated_at DESC"
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def is_chat_muted(self, conversation_id):
        with self._lock:
            row = self._conn.execute("SELECT manual_mute_until, category FROM contact_profiles WHERE conversation_id=?", (conversation_id,)).fetchone()
            if not row:
                return False
            if row["category"] in ("FAMILY_PERSONAL", "IGNORED"):
                return True
            return float(row["manual_mute_until"] or 0) > time.time()

    # --- LID <-> Phone Mapping ---
    def save_lid_phone(self, lid, phone, name=None):
        if not lid or not phone:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO lid_phone_map(lid, phone, name, updated_at) VALUES(?, ?, ?, ?)
                   ON CONFLICT(lid) DO UPDATE SET phone=excluded.phone, name=COALESCE(excluded.name, lid_phone_map.name), updated_at=excluded.updated_at""",
                (lid, phone, name, now),
            )
            self._conn.execute(
                "UPDATE contact_profiles SET phone=COALESCE(phone, ?), updated_at=? WHERE conversation_id=?",
                (phone, now, lid),
            )
            self._conn.commit()

    def resolve_lid(self, lid):
        with self._lock:
            row = self._conn.execute("SELECT phone, name FROM lid_phone_map WHERE lid=?", (lid,)).fetchone()
            return dict(row) if row else None

    # --- Geo-Timezone Scheduled Jobs ---
    def enqueue_scheduled_job(self, job_id, recipient_jid, buyer_phone, buyer_tz, run_at, payload, *, recalculate_fx=True):
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO scheduled_jobs(id, recipient_jid, buyer_phone, buyer_tz, run_at, payload_json, recalculate_fx, status, attempts, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     run_at=excluded.run_at, payload_json=excluded.payload_json, status='PENDING', updated_at=excluded.updated_at""",
                (job_id, recipient_jid, buyer_phone, buyer_tz, float(run_at), json.dumps(payload, ensure_ascii=False), int(bool(recalculate_fx)), now, now),
            )
            self._conn.commit()

    def due_scheduled_jobs(self, now=None, limit=20):
        t = float(now if now is not None else time.time())
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled_jobs WHERE status='PENDING' AND run_at <= ? ORDER BY run_at ASC LIMIT ?",
                (t, limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json"))
                out.append(d)
            return out

    def list_scheduled_jobs(self, status=None, limit=50):
        with self._lock:
            q = "SELECT * FROM scheduled_jobs"
            args = []
            if status:
                q += " WHERE status=?"
                args.append(status)
            q += " ORDER BY run_at ASC LIMIT ?"
            args.append(limit)
            rows = self._conn.execute(q, args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json"))
                out.append(d)
            return out

    def update_scheduled_job(self, job_id, status, error=None):
        with self._lock:
            self._conn.execute(
                "UPDATE scheduled_jobs SET status=?, attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
                (status, error, time.time(), job_id),
            )
            self._conn.commit()

    # --- Verified 2-Phase WhatsApp Outbound Queue ---
    def queue_outbound(self, recipient_jid, text_parts, *, kind="quote", recipient_phone=None, account_phone=None, attachment_path=None, caption=None, status="DRAFT", delay_ms=0, meta=None, dedupe_key=None):
        norm_status = "READY" if status in ("APPROVED", "READY") else status
        oid = "out-" + (uuid.uuid5(uuid.NAMESPACE_OID, dedupe_key).hex[:16] if dedupe_key else uuid.uuid4().hex[:16])
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO outbound_queue(id, recipient_jid, recipient_phone, kind, text_parts_json, attachment_path, caption, status, delay_ms, created_at, updated_at, meta_json)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (oid, recipient_jid, recipient_phone, kind, json.dumps(list(text_parts), ensure_ascii=False), attachment_path, caption, norm_status, int(delay_ms), now, now, json.dumps(meta or {}, ensure_ascii=False)),
            )
            if dedupe_key:
                existing = self._conn.execute("SELECT id FROM outbound_queue WHERE id=?", (oid,)).fetchone()
                if existing:
                    oid = existing["id"]
            if norm_status == "READY":
                bound_account = account_phone or recipient_phone
                if bound_account:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO outbound_dispatch(id, account_phone, expires_at) VALUES(?, ?, ?)",
                        (oid, bound_account, now + 300),
                    )
            self._conn.commit()
        return oid

    def get_outbound(self, oid):
        with self._lock:
            row = self._conn.execute("SELECT * FROM outbound_queue WHERE id=?", (oid,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["text_parts"] = json.loads(d.pop("text_parts_json"))
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
            return d

    def list_outbound(self, statuses=("DRAFT", "READY", "APPROVED", "NEEDS_PRICE_REVIEW"), limit=30, *, newest_first=False):
        placeholders = ",".join("?" for _ in statuses)
        order = "DESC" if newest_first else "ASC"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM outbound_queue WHERE status IN ({placeholders}) ORDER BY created_at {order} LIMIT ?",
                (*statuses, limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["text_parts"] = json.loads(d.pop("text_parts_json"))
                d["meta"] = json.loads(d.pop("meta_json") or "{}")
                out.append(d)
            return out

    def approve_outbound(self, oid, expected_updated_at, account_phone, allowed_phones=None, *, manual_only=False):
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT * FROM outbound_queue WHERE id=?", (oid,)).fetchone()
            if not row or row["status"] not in ("DRAFT", "NEEDS_PRICE_REVIEW"):
                return "DRAFT_CHANGED"
            if expected_updated_at is not None:
                try:
                    if float(row["updated_at"]) != float(expected_updated_at):
                        return "DRAFT_CHANGED"
                except (ValueError, TypeError):
                    if row["updated_at"] != expected_updated_at:
                        return "DRAFT_CHANGED"
            if manual_only and row["kind"] != "manual":
                return "MANUAL_SEND_ONLY"
            phone = row["recipient_phone"]
            def _digits(p):
                return re.sub(r"\D", "", str(p or ""))

            phone_digits = _digits(phone)
            account_digits = _digits(account_phone)
            is_agent_reply = (row["kind"] == "agent_reply")
            if not phone_digits or not account_digits or (phone_digits == account_digits and not is_agent_reply):
                return "RECIPIENT_NOT_ALLOWED"
            jid = row["recipient_jid"]
            jid_digits = _digits(jid.split("@")[0]) if isinstance(jid, str) and "@" in jid else ""
            valid_direct = bool(jid_digits and jid_digits == phone_digits and isinstance(jid, str) and jid.endswith(("@c.us", "@s.whatsapp.net")))
            valid_lid = False
            if isinstance(jid, str) and jid.endswith("@lid") and hasattr(self, "resolve_lid"):
                mapped = self.resolve_lid(jid)
                valid_lid = bool(mapped and _digits(mapped.get("phone")) == phone_digits)
            if not valid_direct and not valid_lid:
                return "RECIPIENT_NOT_ALLOWED"
            cprof = self.get_contact(jid) if hasattr(self, "get_contact") else None
            if cprof and cprof.get("category") in ("FAMILY_PERSONAL", "IGNORED"):
                return "RECIPIENT_BLOCKED"
            if allowed_phones is not None and not is_agent_reply:
                allowed_digits_set = {_digits(p) for p in allowed_phones}
                if phone_digits not in allowed_digits_set:
                    return "RECIPIENT_NOT_ALLOWED"
            parts = json.loads(row["text_parts_json"])
            metadata = json.loads(row["meta_json"])
            att_path = row["attachment_path"]
            joined_text = "\n\n".join(parts) if parts else ""
            if not joined_text.strip() and not att_path:
                return "UNSUPPORTED_MESSAGE"
            if len(joined_text) > 3000:
                return "UNSUPPORTED_MESSAGE"
            if row["caption"] and len(row["caption"]) > 3000:
                return "UNSUPPORTED_MESSAGE"
            if row["kind"] not in ("manual", "quote", "scheduled_quote", "seller_followup", "agent_reply"):
                return "UNSUPPORTED_MESSAGE"
            if row["kind"] in ("quote", "scheduled_quote") and not self._quote_is_current(row, metadata):
                return "QUOTE_REQUIRES_REVIEW"
            if (self.is_chat_muted(phone[1:] + "@c.us")
                    or self.is_chat_muted(phone[1:] + "@s.whatsapp.net")
                    or (isinstance(jid, str) and self.is_chat_muted(jid))):
                return "RECIPIENT_MUTED"
            now = time.time()
            self._conn.execute(
                "INSERT OR REPLACE INTO outbound_dispatch(id, account_phone, expires_at) VALUES(?, ?, ?)",
                (oid, account_phone, now + 120),
            )
            self._conn.execute("UPDATE outbound_queue SET status='READY', updated_at=? WHERE id=?", (now, oid))
            return None

    def _quote_is_current(self, row, metadata):
        if row["status"] in ("NEEDS_PRICE_REVIEW", "DRAFT"):
            return True
        if not (metadata.get("pricing") or {}).get("quote_usd") or time.time() - row["created_at"] > 600:
            return False
        revision = metadata.get("snapshot_revision")
        if revision is None:
            return True
        snapshot = self.get_sync("configuration", "daily-stock")
        return bool(snapshot and snapshot["revision"] == revision
                    and 0 <= time.time() - snapshot["data"]["source_timestamp"] < 86400)

    def dismiss_outbound(self, oid):
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE outbound_queue SET status='DISMISSED', updated_at=? WHERE id=? AND status IN ('DRAFT','READY','CLAIMED')",
                (time.time(), oid),
            ).rowcount == 1

    def has_outbound_receipt(self, message_id):
        if not isinstance(message_id, str) or not message_id:
            return False
        with self._lock:
            return self._conn.execute(
                """SELECT 1 FROM outbound_queue q, json_each(q.meta_json, '$.sent_ids') receipt
                   WHERE q.status='ACCEPTED' AND receipt.value=? LIMIT 1""",
                (message_id,),
            ).fetchone() is not None

    def revoke_outbound(self):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE outbound_queue SET status='CANCELLED', updated_at=? WHERE status IN ('READY','CLAIMED')",
                (time.time(),),
            )

    def _expire_outbound(self, now):
        self._conn.execute(
            """UPDATE outbound_queue
               SET status=CASE status WHEN 'IN_FLIGHT' THEN 'UNKNOWN' ELSE 'EXPIRED' END, updated_at=?
               WHERE status IN ('READY','CLAIMED','IN_FLIGHT')
                 AND id IN (SELECT id FROM outbound_dispatch WHERE expires_at<=?)""",
            (now, now),
        )

    def ready_outbound_ids(self, account_phone, *, manual_only=False):
        with self._lock, self._conn:
            self._expire_outbound(time.time())
            rows = self._conn.execute(
                """SELECT q.id FROM outbound_queue q JOIN outbound_dispatch d ON q.id=d.id
                   WHERE q.status='READY' AND d.account_phone=? AND (?=0 OR q.kind='manual')
                   ORDER BY q.updated_at LIMIT 4""",
                (account_phone, int(manual_only)),
            ).fetchall()
            return [{"id": row["id"]} for row in rows]

    def dispatch_outbound(self, operation, body, account_phone, allowed_phones, enabled=True, *, manual_only=False, **kwargs):
        oid = body.get("outbound_id") or body.get("id")
        session = body.get("session_id") or "worker_session_0123456789"
        if not isinstance(oid, str) or not re.fullmatch(r"out-[0-9a-f]{16}", oid):
            return {"error": "INVALID_OUTBOUND_ID"}
        if not isinstance(session, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", session):
            return {"error": "INVALID_BRIDGE_SESSION"}
        now = time.time()
        with self._lock, self._conn:
            self._expire_outbound(now)
            row = self._conn.execute(
                """SELECT q.*, d.account_phone, d.expires_at, d.session_id, d.claim_token
                   FROM outbound_queue q JOIN outbound_dispatch d ON q.id=d.id WHERE q.id=?""",
                (oid,),
            ).fetchone()
            if not row:
                return {"error": "APPROVAL_UNAVAILABLE"}
            status = row["status"]
            token = row["claim_token"]
            owns_claim = token and row["session_id"] == session and body.get("claim_token") == token
            if operation == "dispatch_result":
                outcome = body.get("outcome")
                ids = body.get("sent_ids")
                if not owns_claim or outcome not in ("ACCEPTED", "FAILED_BEFORE_SEND", "UNKNOWN"):
                    return {"error": "INVALID_DISPATCH_RESULT"}
                if outcome == "ACCEPTED" and (
                    status not in ("IN_FLIGHT", "UNKNOWN", "ACCEPTED") or not isinstance(ids, list)
                    or len(ids) != 1 or not isinstance(ids[0], str) or not ids[0].strip() or len(ids[0]) > 300
                ):
                    return {"error": "MISSING_PROVIDER_RECEIPT"}
                if status == "ACCEPTED" and outcome == "ACCEPTED" and ids != json.loads(row["meta_json"]).get("sent_ids"):
                    return {"error": "RECEIPT_CHANGED"}
                if status in ("ACCEPTED", "FAILED_BEFORE_SEND", "UNKNOWN") and status == outcome:
                    return {"ok": True}
                if status not in ("CLAIMED", "IN_FLIGHT", "UNKNOWN") or (status == "UNKNOWN" and outcome != "ACCEPTED"):
                    return {"error": "DISPATCH_ALREADY_CLOSED"}
                meta = json.loads(row["meta_json"])
                if outcome == "ACCEPTED":
                    meta["sent_ids"] = ids
                self._conn.execute(
                    "UPDATE outbound_queue SET status=?, updated_at=?, meta_json=? WHERE id=?",
                    (outcome, now, json.dumps(meta), oid),
                )
                return {"ok": True}
            if not enabled:
                return {"error": "DISPATCH_DISABLED"}
            if manual_only and row["kind"] != "manual":
                return {"error": "MANUAL_SEND_ONLY"}
            if not account_phone or account_phone != row["account_phone"] or (body.get("account_phone") and body.get("account_phone") != account_phone):
                return {"error": "ACCOUNT_MISMATCH"}
            phone = row["recipient_phone"]
            jid = row["recipient_jid"]
            is_manual = (row["kind"] == "manual")
            is_agent_reply = (row["kind"] == "agent_reply")

            def _digits(p):
                return re.sub(r"\D", "", str(p or ""))

            if (self.is_chat_muted(phone[1:] + "@c.us")
                    or self.is_chat_muted(phone[1:] + "@s.whatsapp.net")
                    or (isinstance(jid, str) and self.is_chat_muted(jid))):
                return {"error": "RECIPIENT_MUTED"}

            cprof = self.get_contact(jid) if hasattr(self, "get_contact") else None
            if cprof and cprof.get("category") in ("FAMILY_PERSONAL", "IGNORED"):
                return {"error": "RECIPIENT_BLOCKED"}

            if not is_manual and not is_agent_reply and allowed_phones is not None:
                allowed_digits_set = {_digits(p) for p in allowed_phones}
                if _digits(phone) not in allowed_digits_set:
                    return {"error": "RECIPIENT_UNAVAILABLE"}

            if row["kind"] in ("quote", "scheduled_quote") and not self._quote_is_current(row, json.loads(row["meta_json"])):
                return {"error": "QUOTE_REQUIRES_REVIEW"}

            target_jid = jid if jid else (phone[1:] + "@c.us")
            if operation == "dispatch_claim" and status == "READY":
                token = secrets.token_urlsafe(32)
                self._conn.execute(
                    "UPDATE outbound_dispatch SET session_id=?, claim_token=? WHERE id=?", (session, token, oid)
                )
                self._conn.execute("UPDATE outbound_queue SET status='CLAIMED', updated_at=? WHERE id=?", (now, oid))
                return {
                    "item": {
                        "id": oid,
                        "account_phone": account_phone,
                        "recipient_phone": phone,
                        "recipient_jid": target_jid,
                        "claim_token": token,
                        "text_parts": ["\n\n".join(json.loads(row["text_parts_json"]))],
                        "attachment_path": row["attachment_path"],
                        "caption": row["caption"],
                        "meta": json.loads(row["meta_json"]),
                    }
                }
            if operation == "dispatch_authorize" and status == "CLAIMED" and owns_claim:
                expires_at = min(row["expires_at"], now + 8)
                self._conn.execute(
                    "UPDATE outbound_dispatch SET expires_at=? WHERE id=?", (now + 90, oid)
                )
                self._conn.execute("UPDATE outbound_queue SET status='IN_FLIGHT', updated_at=? WHERE id=?", (now, oid))
                return {"ok": True, "expires_at": expires_at}
            return {"error": "APPROVAL_UNAVAILABLE"}

    def set_outbound_status(self, oid, status, meta_patch=None):
        with self._lock:
            if meta_patch:
                row = self._conn.execute("SELECT meta_json FROM outbound_queue WHERE id=?", (oid,)).fetchone()
                cur = json.loads(row["meta_json"] or "{}") if row else {}
                cur.update(meta_patch)
                self._conn.execute(
                    "UPDATE outbound_queue SET status=?, meta_json=?, updated_at=? WHERE id=?",
                    (status, json.dumps(cur, ensure_ascii=False), time.time(), oid),
                )
            else:
                self._conn.execute("UPDATE outbound_queue SET status=?, updated_at=? WHERE id=?", (status, time.time(), oid))
            self._conn.commit()

    # --- Forwarded Stock List Deduplication ---
    def record_sku_sighting(self, sig, supplier_key, window_seconds=21600):
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT seen_at, count FROM sku_sightings WHERE sig=?", (sig,)).fetchone()
            if row and (now - float(row["seen_at"])) < window_seconds:
                self._conn.execute("UPDATE sku_sightings SET count=count+1, seen_at=? WHERE sig=?", (now, sig))
                self._conn.commit()
                return True, int(row["count"]) + 1
            self._conn.execute(
                """INSERT INTO sku_sightings(sig, supplier_key, seen_at, count) VALUES(?, ?, ?, 1)
                   ON CONFLICT(sig) DO UPDATE SET supplier_key=excluded.supplier_key, seen_at=excluded.seen_at, count=1""",
                (sig, supplier_key, now),
            )
            self._conn.commit()
            return False, 1

    # --- Phase 4 Notification History & Status Management ---
    def list_notifications_paginated(self, limit=50, offset=0, state=None, category=None):
        """Paginated and filtered query for notification tasks."""
        with self._lock:
            where_clauses = ["collection='tasks'", "json_extract(data_json, '$.kind')='notification'"]
            params = []

            if state:
                if state == "ACTIVE":
                    where_clauses.append("json_extract(data_json, '$.state') IN ('NEW', 'READ', 'SAVED_FOR_REVIEW')")
                elif state != "ALL":
                    where_clauses.append("json_extract(data_json, '$.state') = ?")
                    params.append(state)
            else:
                # Default: active notifications only (excludes ARCHIVED and DISMISSED)
                where_clauses.append("json_extract(data_json, '$.state') NOT IN ('ARCHIVED', 'DISMISSED')")

            if category:
                where_clauses.append("json_extract(data_json, '$.category') = ?")
                params.append(category)

            where_sql = " AND ".join(where_clauses)

            total_row = self._conn.execute(
                f"SELECT COUNT(*) AS total FROM records WHERE {where_sql}",
                params,
            ).fetchone()
            total = int(total_row["total"]) if total_row else 0

            unread_row = self._conn.execute(
                """SELECT COUNT(*) AS unread FROM records
                   WHERE collection='tasks'
                     AND json_extract(data_json, '$.kind')='notification'
                     AND json_extract(data_json, '$.state')='NEW'"""
            ).fetchone()
            unread_count = int(unread_row["unread"]) if unread_row else 0

            query_sql = f"""SELECT id, revision, updated_at, data_json FROM records
                            WHERE {where_sql}
                            ORDER BY updated_at DESC, id DESC
                            LIMIT ? OFFSET ?"""
            rows = self._conn.execute(query_sql, [*params, limit, offset]).fetchall()
            items = [
                {
                    "id": r["id"],
                    "revision": int(r["revision"]),
                    "updated_at": r["updated_at"],
                    "data": json.loads(r["data_json"]),
                }
                for r in rows
            ]
            return {
                "items": items,
                "total": total,
                "unread_count": unread_count,
            }

    def archive_all_notifications(self):
        """Archive all active notifications while preserving history in SQLite."""
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            rows = self._conn.execute(
                """SELECT id, revision, data_json FROM records
                   WHERE collection='tasks'
                     AND json_extract(data_json, '$.kind')='notification'
                     AND json_extract(data_json, '$.state') IN ('NEW', 'READ', 'SAVED_FOR_REVIEW', 'DISMISSED')"""
            ).fetchall()
            for r in rows:
                d = json.loads(r["data_json"])
                d["state"] = "ARCHIVED"
                d["archived_at"] = now
                self._conn.execute(
                    "UPDATE records SET revision=revision+1, updated_at=?, data_json=? WHERE collection='tasks' AND id=?",
                    (now_iso, json.dumps(d, ensure_ascii=False), r["id"]),
                )
            self._conn.commit()
            return len(rows)

    def mark_all_notifications_read(self):
        """Mark all NEW notifications as READ."""
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            rows = self._conn.execute(
                """SELECT id, revision, data_json FROM records
                   WHERE collection='tasks'
                     AND json_extract(data_json, '$.kind')='notification'
                     AND json_extract(data_json, '$.state')='NEW'"""
            ).fetchall()
            for r in rows:
                d = json.loads(r["data_json"])
                d["state"] = "READ"
                d["read_at"] = now
                if not d.get("notified_at"):
                    d["notified_at"] = now
                self._conn.execute(
                    "UPDATE records SET revision=revision+1, updated_at=?, data_json=? WHERE collection='tasks' AND id=?",
                    (now_iso, json.dumps(d, ensure_ascii=False), r["id"]),
                )
            self._conn.commit()
            return len(rows)

    def resolve_superseded_notifications(self, referenced_message_id: str, newer_revision: int = 1):
        """Resolve active notifications associated with a superseded message revision."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                """SELECT id, revision, data_json FROM records
                   WHERE collection='tasks'
                     AND json_extract(data_json, '$.kind')='notification'
                     AND json_extract(data_json, '$.state') IN ('NEW', 'READ')"""
            ).fetchall()
            now = time.time()
            now_iso = datetime.now(timezone.utc).isoformat()
            resolved_count = 0
            for r in rows:
                data = json.loads(r["data_json"])
                details = data.get("details", {})
                buyer_msg = details.get("buyer_message")
                seller_msg = details.get("seller_message")
                src_msg = details.get("source_message_id") or details.get("source_id") or details.get("buy_item", {}).get("source_id")
                if referenced_message_id and referenced_message_id in (buyer_msg, seller_msg, src_msg):
                    data["state"] = "RESOLVED"
                    data["resolved_at"] = now
                    data["resolution_reason"] = "SUPERSEDED_BY_NEWER_REVISION"
                    new_rev = int(r["revision"]) + 1
                    self._conn.execute(
                        "UPDATE records SET revision=?, updated_at=?, data_json=? WHERE collection='tasks' AND id=?",
                        (new_rev, now_iso, json.dumps(data, ensure_ascii=False), r["id"]),
                    )
                    resolved_count += 1
            if resolved_count:
                self._conn.commit()
            return resolved_count
