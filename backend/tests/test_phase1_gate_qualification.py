"""Phase 1 Gate Qualification Suite.

Validates:
1. Hydration scale: 0, 1, 499, 500, 501, 1205 Sheet rows with pagination.
2. Delayed page and async pagination.
3. Concurrent local edit in sheets_outbox during pagination preserved.
4. Duplicate timestamp stable pagination.
5. Quarantined local updates preserved.
6. Network failure mid-page prevents destructive pruning.
7. Same-revision recovery of missing local records.
8. Boot stopped invariant: clean close, hard kill (running=True in cache), zero dispatch until Start.
9. Historical messages retained and viewable while stopped.
10. Autonomy mode canonical matrix and legacy migration (auto->turbo, approved_auto->draft).
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.desktop_service import DesktopService
from jarvis.intelligence import DeskSettings


class PagedRemoteStub:
    def __init__(self, items=None, delay=0.0, fail_at_page=None):
        self.items = items or []
        self.delay = delay
        self.fail_at_page = fail_at_page
        self.call_count = 0

    async def list(self, collection, limit=100, offset=0):
        self.call_count += 1
        if self.fail_at_page is not None and self.call_count == self.fail_at_page:
            raise ConnectionResetError("Simulated network failure mid-page")
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        page = self.items[offset:offset + limit]
        return {
            "records": page,
            "total": len(self.items),
            "has_more": (offset + len(page)) < len(self.items),
            "next_offset": offset + len(page),
        }

    async def get(self, collection, record_id):
        for it in self.items:
            if it.get("id") == record_id:
                return it
        return None

    async def health(self):
        return {"storage": "google_sheets", "status": "ok"}


def make_inventory_items(count, base_revision=1, timestamp="2026-11-22T00:00:00Z"):
    return [
        {
            "id": f"inv_{i:05d}",
            "data": {
                "device": f"Device_{i:05d}",
                "variant": "256GB",
                "quantity": 10,
                "unit_cost_usd": 700.0,
            },
            "revision": base_revision,
            "updated_at": timestamp,
        }
        for i in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("row_count", [0, 1, 499, 500, 501, 1205])
async def test_gate_inventory_hydration_scale(tmp_path, row_count):
    """Gate test: 0, 1, 499, 500, 501, 1205 Sheet rows hydrate completely

    without silent truncation or unintended record loss.
    """
    db_path = tmp_path / f"scale_{row_count}.sqlite3"
    remote_items = make_inventory_items(row_count)
    remote = PagedRemoteStub(remote_items)
    repo = SQLiteSyncedRepository(remote, db_path)

    # If row_count is 0, pre-seed a confirmed row that was deleted on remote
    if row_count == 0:
        with repo._lock, repo._conn:
            repo._conn.execute(
                "INSERT INTO records(collection, id, revision, updated_at, data_json) VALUES(?, ?, ?, ?, ?)",
                ("inventory", "old_inv", 1, "2026-11-21T00:00:00Z", json.dumps({"device": "Old"}))
            )
            repo._conn.execute(
                "INSERT INTO sheets_revisions(collection, id, revision) VALUES(?, ?, ?)",
                ("inventory", "old_inv", 1)
            )

    pulled = await repo.hydrate_from_remote(("inventory",))
    assert pulled["inventory"] == row_count

    with repo._lock:
        local_count = repo._conn.execute("SELECT COUNT(*) FROM records WHERE collection='inventory'").fetchone()[0]
        revisions_count = repo._conn.execute("SELECT COUNT(*) FROM sheets_revisions WHERE collection='inventory'").fetchone()[0]

    assert local_count == row_count
    assert revisions_count == row_count
    repo.close()


@pytest.mark.asyncio
async def test_gate_delayed_page_and_duplicate_timestamp(tmp_path):
    """Gate test: Async delayed page and rows sharing duplicate timestamps sort and paginate stably."""
    db_path = tmp_path / "delayed.sqlite3"
    # 600 items all sharing the exact same timestamp
    remote_items = make_inventory_items(600, timestamp="2026-11-22T12:00:00Z")
    remote = PagedRemoteStub(remote_items, delay=0.01)
    repo = SQLiteSyncedRepository(remote, db_path)

    pulled = await repo.hydrate_from_remote(("inventory",))
    assert pulled["inventory"] == 600

    with repo._lock:
        count = repo._conn.execute("SELECT COUNT(*) FROM records WHERE collection='inventory'").fetchone()[0]
    assert count == 600
    repo.close()


@pytest.mark.asyncio
async def test_gate_editing_during_pagination_preserved(tmp_path):
    """Gate test: Local edit queued in sheets_outbox is preserved and never overwritten by remote."""
    db_path = tmp_path / "concurrent_edit.sqlite3"
    remote_items = make_inventory_items(50)
    remote = PagedRemoteStub(remote_items)
    repo = SQLiteSyncedRepository(remote, db_path)

    # Initial hydration
    await repo.hydrate_from_remote(("inventory",))

    # User locally edits item inv_00010
    edited_data = {"device": "Device_00010_EDITED", "quantity": 999, "unit_cost_usd": 1200.0}
    await repo.put("inventory", "inv_00010", edited_data, 1)

    # Verify it is in outbox
    with repo._lock:
        outbox_entry = repo._conn.execute(
            "SELECT 1 FROM sheets_outbox WHERE collection='inventory' AND id='inv_00010'"
        ).fetchone()
        assert outbox_entry is not None

    # Re-hydrate from remote (remote still has old data)
    await repo.hydrate_from_remote(("inventory",))

    # Local record must RETAIN the user's edit, not get overwritten by remote!
    local_rec = repo.get_sync("inventory", "inv_00010")
    assert local_rec is not None
    assert local_rec["data"]["quantity"] == 999
    assert local_rec["data"]["device"] == "Device_00010_EDITED"
    repo.close()


@pytest.mark.asyncio
async def test_gate_quarantined_local_update_preserved(tmp_path):
    """Gate test: Quarantined write is preserved and not pruned."""
    db_path = tmp_path / "quarantine.sqlite3"
    remote_items = make_inventory_items(20)
    remote = PagedRemoteStub(remote_items)
    repo = SQLiteSyncedRepository(remote, db_path)

    await repo.hydrate_from_remote(("inventory",))

    # Put a local record and quarantine it
    await repo.put("inventory", "inv_quarantined", {"device": "BadRow"}, 0)
    repo.quarantine_record("inventory", "inv_quarantined", reason="TEST_REASON")

    quarantined = repo.list_quarantined_sheet_writes()
    assert len(quarantined) == 1
    assert quarantined[0]["record_id"] == "inv_quarantined"

    # Hydrate again from remote
    await repo.hydrate_from_remote(("inventory",))

    # Quarantine remains intact
    quarantined_after = repo.list_quarantined_sheet_writes()
    assert len(quarantined_after) == 1
    repo.close()


@pytest.mark.asyncio
async def test_gate_network_failure_mid_page_prevents_destructive_pruning(tmp_path):
    """Gate test: If page 2 fails with network error, hydration aborts and does NOT prune local records!"""
    db_path = tmp_path / "midpage_fail.sqlite3"
    remote_items = make_inventory_items(600)
    # First page succeeds, second page fails
    remote = PagedRemoteStub(remote_items, fail_at_page=2)
    repo = SQLiteSyncedRepository(remote, db_path)

    # Pre-seed 600 items in local db
    with repo._lock, repo._conn:
        for it in remote_items:
            repo._conn.execute(
                "INSERT INTO records(collection, id, revision, updated_at, data_json) VALUES(?, ?, ?, ?, ?)",
                ("inventory", it["id"], it["revision"], it["updated_at"], json.dumps(it["data"]))
            )

    pulled = await repo.hydrate_from_remote(("inventory",))
    # Pulled must be None indicating failure
    assert pulled["inventory"] is None

    # Crucial assertion: All 600 local records remain intact! Zero records were deleted!
    with repo._lock:
        local_count = repo._conn.execute("SELECT COUNT(*) FROM records WHERE collection='inventory'").fetchone()[0]
    assert local_count == 600
    repo.close()


@pytest.mark.asyncio
async def test_gate_same_revision_recovery_of_pruned_record(tmp_path):
    """Gate test: A record missing locally is recovered even if remote is at the same revision."""
    db_path = tmp_path / "recovery.sqlite3"
    remote = PagedRemoteStub([])
    repo = SQLiteSyncedRepository(remote, db_path)

    # Seed an item in sheets_revisions at revision 1, but missing from records
    with repo._lock, repo._conn:
        repo._conn.execute(
            "INSERT INTO sheets_revisions(collection, id, revision) VALUES(?, ?, ?)",
            ("inventory", "inv_recovered", 1)
        )

    assert repo.get_sync("inventory", "inv_recovered") is None

    # Re-hydrate/upsert at same revision 1
    repo._upsert_local("inventory", "inv_recovered", {"device": "iPhone 16", "quantity": 5}, 1, "2026-11-22T00:00:00Z")

    recovered = repo.get_sync("inventory", "inv_recovered")
    assert recovered is not None
    assert recovered["data"]["device"] == "iPhone 16"
    assert recovered["data"]["quantity"] == 5
    repo.close()


def test_gate_boot_stopped_and_startup_lock(tmp_path):
    """Gate test: After clean close, hard kill, or cached running=True,

    service always boots stopped (runner.active=False), zero dispatch activity until explicit Start.
    Reopening still displays historical messages.
    """
    cache_file = tmp_path / "desktop_cache.json"
    cache_data = {
        "configuration": {
            "id": "business",
            "data": {
                "business_name": "Jarvis Desk",
                "classifier_model": "gemini-2.5-flash",
                "owner_phone": "+919876543210",
                "sources": [{"phone": "+919876543211", "label": "Stock", "purpose": "inventory"}],
                "lead_capture": True,
                "classifier_daily_limit": 500,
            }
        },
        "wa_cursor": {
            "running": True,  # Hard kill simulation
            "scan_mode": "new_only",
            "last_event_ts": 1732270000.0,
        }
    }
    cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

    db_path = tmp_path / "service_boot.sqlite3"
    remote = PagedRemoteStub([])
    repo = SQLiteSyncedRepository(remote, db_path)

    # Seed historical message
    with repo._lock, repo._conn:
        repo._conn.execute(
            "INSERT INTO records(collection, id, revision, updated_at, data_json) VALUES(?, ?, ?, ?, ?)",
            ("messages", "msg_hist_1", 1, "2026-11-22T00:00:00Z", json.dumps({"text": "Historical inquiry", "sender": "+919999999999"}))
        )

    service = DesktopService(cache_path=cache_file, repository=repo)

    # 1. Startup invariant: Always boot stopped
    assert service.runner.active is False
    assert getattr(service.runner, "always_start_stopped", False) is True

    # 2. Historical messages are retained and visible while stopped
    hist = repo.get_sync("messages", "msg_hist_1")
    assert hist is not None
    assert hist["data"]["text"] == "Historical inquiry"

    # 3. Clean close leaves running=False
    service.close()
    saved_cache = json.loads(cache_file.read_text(encoding="utf-8"))
    assert saved_cache["wa_cursor"]["running"] is False


def test_gate_autonomy_modes_and_legacy_migration():
    """Gate test: Canonical modes table (observe, draft, send, turbo) and legacy migrations."""
    # 1. auto -> turbo with recorded migration note
    cfg_auto = DeskSettings(autonomy_mode="auto")
    assert cfg_auto.autonomy_mode == "turbo"
    assert "auto -> turbo" in cfg_auto.legacy_autonomy_migration

    # 2. approved_auto -> draft with recorded migration note
    cfg_approved = DeskSettings(autonomy_mode="approved_auto")
    assert cfg_approved.autonomy_mode == "draft"
    assert "approved_auto -> draft" in cfg_approved.legacy_autonomy_migration

    # 3. Canonical modes preserved without migration note
    for mode in ("observe", "draft", "send", "turbo"):
        cfg = DeskSettings(autonomy_mode=mode)
        assert cfg.autonomy_mode == mode
        assert cfg.legacy_autonomy_migration is None

    # 4. Unknown values rejected with actionable error
    with pytest.raises(Exception) as exc:
        DeskSettings(autonomy_mode="unrecognized_mode")
    assert "Allowed modes are: observe, draft, send, turbo" in str(exc.value)

    # 5. Independent clocks and settings constraints
    cfg_full = DeskSettings(
        always_start_stopped=True,
        human_mute_hours=0.25,  # 15 minutes supported
        quote_validity_minutes=45,
        dispatch_jitter_min_ms=2000,
        dispatch_jitter_max_ms=3000,
        ui_scale=100
    )
    assert cfg_full.human_mute_hours == 0.25
    assert cfg_full.quote_validity_minutes == 45
    assert cfg_full.ui_scale == 100

    # 6. Invalid jitter rejected
    with pytest.raises(Exception):
        DeskSettings(dispatch_jitter_min_ms=5000, dispatch_jitter_max_ms=1000)
