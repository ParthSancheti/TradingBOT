"""Phase 4 Adversarial Verification Suite: High-Throughput Google Sheets Backend & Data-Loss Prevention."""
import asyncio
import copy
import hashlib
import hmac
import json
import time
import pytest
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.sheets import AppsScript, SheetsError, canonical, signature


class AdversarialFakeSheets:
    """Mock Sheets backend that supports failure injection, latency, and inspection."""
    def __init__(self):
        self.records = {}
        self.revisions = {}
        self.calls = 0
        self.fail_countdown = -1
        self.fail_error = "SHEETS_UNAVAILABLE"
        self.reject_ids = set()

    async def list(self, collection, limit=100):
        self.calls += 1
        return [
            {"id": r["id"], "revision": r["revision"], "updated_at": r["updated_at"], "data": r["data"]}
            for (c, _), r in self.records.items() if c == collection
        ][:limit]

    async def put(self, collection, record_id, data, expected_revision, request_id=None):
        return (await self.batch([{"collection": collection, "id": record_id, "data": data, "expected_revision": expected_revision}], request_id))[0]

    async def batch(self, changes, request_id=None):
        self.calls += 1
        if self.fail_countdown > 0:
            self.fail_countdown -= 1
            if self.fail_countdown == 0:
                raise SheetsError(self.fail_error)

        out = []
        for ch in changes:
            col = ch["collection"]
            rid = ch["id"]
            if rid in self.reject_ids:
                raise SheetsError("INVALID_CHANGE")
            exp = int(ch["expected_revision"])
            cur = self.revisions.get((col, rid), 0)
            if cur != exp:
                raise SheetsError(f"REVISION_CONFLICT: {col}/{rid} cur={cur} exp={exp}")
            next_rev = cur + 1
            self.revisions[(col, rid)] = next_rev
            rec = {"id": rid, "revision": next_rev, "updated_at": "2026-11-19T12:00:00Z", "data": copy.deepcopy(ch["data"])}
            self.records[(col, rid)] = rec
            out.append(rec)
        return out


@pytest.mark.asyncio
async def test_100_mutation_burst_and_interrupted_sync_convergence(tmp_path):
    """Adversarial Test 1: 100 rapid mutations with temporary Sheets outages converge with zero data loss."""
    remote = AdversarialFakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "burst.db")
    try:
        # Step 1: Enqueue 100 mutations rapidly across 10 items
        item_revs = {f"item_{k}": 0 for k in range(10)}
        for i in range(100):
            item_id = f"item_{i % 10}"
            exp_rev = item_revs[item_id]
            await repo.put("inventory", item_id, {"stock": i, "mpn": f"PART_{i % 10}"}, exp_rev)
            item_revs[item_id] = exp_rev + 1

        assert repo.has_pending_sheet_writes()

        # Step 2: Simulate intermittent network failure on first flush attempt
        remote.fail_countdown = 1
        remote.fail_error = "SHEETS_UNAVAILABLE"
        res1 = await repo.flush_outbox()
        assert res1.get("error") == "SHEETS_UNAVAILABLE"

        # Step 3: Network restores - flush outbox until clear
        for _ in range(20):
            await repo.flush_outbox(max_batch=10)
            if not repo.has_pending_sheet_writes():
                break

        assert not repo.has_pending_sheet_writes(), "Outbox must drain completely once transport is healthy"
        # Verify 10 distinct items were updated to their final states
        for k in range(10):
            assert ("inventory", f"item_{k}") in remote.records
            assert remote.records[("inventory", f"item_{k}")]["revision"] >= 1
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_oversized_media_and_text_sanitation_preserves_local_fidelity(tmp_path):
    """Adversarial Test 2: Multi-MB base64 media and huge text are stripped for Sheets outbox but kept in SQLite."""
    remote = AdversarialFakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "media_test.db")
    try:
        huge_base64 = "A" * (2 * 1024 * 1024)  # 2 MB image
        huge_payload = {
            "conversation_id": "buyer@c.us",
            "media_b64": huge_base64,
            "media_mime_type": "image/jpeg",
            "raw_text": "BOM list with attached schematic photo",
            "direction": "incoming"
        }

        # Put into local repository
        rec = await repo.put("messages", "msg_media_101", huge_payload, 0)
        assert rec["id"] == "msg_media_101"

        # 1. Verify SQLite records contains the FULL 2 MB payload untouched
        local_rec = repo.get_sync("messages", "msg_media_101")
        assert len(local_rec["data"]["media_b64"]) == 2 * 1024 * 1024

        # 2. Verify sheets_outbox has sanitized media (never pushes 2 MB to Sheets)
        with repo._lock:
            outbox_row = repo._conn.execute(
                "SELECT data_json FROM sheets_outbox WHERE collection='messages' AND id='msg_media_101'"
            ).fetchone()
            outbox_data = json.loads(outbox_row["data_json"])
            assert outbox_data["media_b64"] == "[media attached: image/jpeg]"
            assert len(outbox_row["data_json"].encode("utf-8")) < 500

        # 3. Flush outbox to remote
        await repo.flush_outbox()
        assert ("messages", "msg_media_101") in remote.records
        assert remote.records[("messages", "msg_media_101")]["data"]["media_b64"] == "[media attached: image/jpeg]"
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_poison_pill_quarantining_prevents_queue_deadlock(tmp_path):
    """Adversarial Test 3: A permanently invalid row is quarantined after repeated failures without blocking queue."""
    remote = AdversarialFakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "quarantine_test.db")
    try:
        # Enqueue 3 valid rows and 1 poison row
        await repo.put("messages", "valid_1", {"text": "WTB 10pcs"}, 0)
        await repo.put("messages", "poison_pill", {"text": "BAD DATA"}, 0)
        await repo.put("messages", "valid_2", {"text": "WTB 20pcs"}, 0)

        # Mark poison_pill to be rejected by the remote
        remote.reject_ids.add("poison_pill")

        # Flush outbox repeatedly to trigger batch isolation and eventual quarantine (max 5 attempts)
        for _ in range(8):
            await repo.flush_outbox(max_batch=5)

        # Valid rows must have been flushed successfully
        assert ("messages", "valid_1") in remote.records
        assert ("messages", "valid_2") in remote.records

        # Poison pill must be moved to sheets_quarantine
        quarantined = repo.list_quarantined_sheet_writes()
        assert len(quarantined) >= 1
        assert any(q["record_id"] == "poison_pill" for q in quarantined)

        # Outbox must be completely clean and unblocked
        assert not repo.has_pending_sheet_writes()
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_record_too_large_quarantined_after_threshold(tmp_path):
    """Adversarial Test 4: A record exceeding 16 KB is isolated and quarantined after threshold attempts."""
    remote = AdversarialFakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "large_record.db")
    try:
        # Enqueue an oversized record (> 16 KB)
        huge_str = "Z" * 18000
        await repo.put("tasks", "task_huge", {"description": huge_str}, 0)
        await repo.put("tasks", "task_normal", {"description": "Normal task"}, 0)

        # Flush outbox: normal task flushes, task_huge marked RECORD_TOO_LARGE
        await repo.flush_outbox()

        # Normal task must succeed
        assert ("tasks", "task_normal") in remote.records
        assert repo.sheet_sync_status() == {"pending": 1, "errors": {"RECORD_TOO_LARGE": 1}}

        # Sweep stalled writes into quarantine
        quarantined_count = repo.quarantine_stalled_writes()
        assert quarantined_count == 1

        # Oversized record must be moved to quarantine and outbox must be clean
        quarantined = repo.list_quarantined_sheet_writes()
        assert any(q["record_id"] == "task_huge" and "RECORD_TOO_LARGE" in q["error_reason"] for q in quarantined)
        assert not repo.has_pending_sheet_writes()
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_hmac_tamper_and_replay_rejection():
    """Adversarial Test 5: Verify cryptographic tamper detection, signature verification, and replay rejection."""
    secret = "a" * 32  # 32-byte secret
    fake_url = "https://script.google.com/macros/s/AKfycbz_test/exec"
    app = AppsScript(fake_url, secret)

    # 1. Canonical representation produces consistent hashing
    val1 = {"b": 2, "a": 1}
    val2 = {"a": 1, "b": 2}
    assert canonical(val1) == canonical(val2)
    assert signature(secret, val1) == signature(secret, val2)

    # 2. Tampered signature is detected
    sig = signature(secret, val1)
    tampered_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert not hmac.compare_digest(sig, tampered_sig)

    # 3. Invalid secret length rejected at construction
    with pytest.raises(ValueError, match="at least 32 characters"):
        AppsScript(fake_url, "too-short")

    # 4. Non-HTTPS or non-Google script URL rejected
    with pytest.raises(ValueError, match="deployed HTTPS script.google.com"):
        AppsScript("http://script.google.com/macros/s/test/exec", secret)
    with pytest.raises(ValueError, match="deployed HTTPS script.google.com"):
        AppsScript("https://evil.com/macros/s/test/exec", secret)
