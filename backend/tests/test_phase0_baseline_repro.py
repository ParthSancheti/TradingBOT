"""Phase 0 & Phase 1 Contract Verification Tests.

Validates that baseline bugs F01, F05, F12, F24 are resolved:
1. 501-row inventory now hydrates all 501 rows without truncation or pruning.
2. Boot from persisted cache with running=True boots stopped (active=False).
3. Autonomy mode 'auto' migrates to 'turbo' with recorded legacy migration.
4. human_mute_hours=0.25 is accepted and independent quote validity clock is configurable.
5. Signal evaluation for named iPhone inquiry.
"""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.desktop_service import DesktopService
from jarvis.intelligence import DeskSettings
from jarvis.trading_engine import evaluate_inbound_trade, is_worth_effort


class StubRemote:
    def __init__(self, records_by_collection=None):
        self.records = records_by_collection or {}
        self.list_calls = []

    async def list(self, collection, limit=100, offset=0):
        self.list_calls.append({"collection": collection, "limit": limit, "offset": offset})
        items = self.records.get(collection, [])
        slice_items = items[offset:offset + limit]
        return {
            "records": slice_items,
            "total": len(items),
            "has_more": (offset + len(slice_items)) < len(items),
            "next_offset": offset + len(slice_items),
        }

    async def get(self, collection, record_id):
        items = self.records.get(collection, [])
        for item in items:
            if item.get("id") == record_id:
                return item
        return None

    async def health(self):
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_f01_501_inventory_row_preservation(tmp_path):
    """F01 Verified: 501 local rows hydrate completely across pages without losing row 500."""
    db_path = tmp_path / "repo.sqlite3"
    remote = StubRemote()
    repo = SQLiteSyncedRepository(remote, db_path)

    # 1. 501 remote inventory items
    remote_items = [
        {
            "id": f"inv_{i:04d}",
            "data": {"device": f"iPhone {i}", "quantity": 1, "unit_cost_usd": 500.0},
            "revision": 1,
            "updated_at": "2026-11-22T00:00:00Z",
        }
        for i in range(501)
    ]
    remote.records["inventory"] = remote_items

    # 2. Local database already confirmed 501 rows at revision 1
    with repo._lock, repo._conn:
        for item in remote_items:
            repo._conn.execute(
                "INSERT INTO records(collection, id, revision, updated_at, data_json) VALUES(?, ?, ?, ?, ?)",
                ("inventory", item["id"], item["revision"], item["updated_at"], json.dumps(item["data"]))
            )
            repo._conn.execute(
                "INSERT INTO sheets_revisions(collection, id, revision) VALUES(?, ?, ?)",
                ("inventory", item["id"], item["revision"])
            )

    with repo._lock:
        count_before = repo._conn.execute("SELECT COUNT(*) FROM records WHERE collection='inventory'").fetchone()[0]
    assert count_before == 501

    # 3. hydrate_from_remote is called
    pulled = await repo.hydrate_from_remote(("inventory",))
    with repo._lock:
        count_after = repo._conn.execute("SELECT COUNT(*) FROM records WHERE collection='inventory'").fetchone()[0]

    # F01 Fix Verified: All 501 rows remain intact!
    assert count_after == 501
    assert repo.get_sync("inventory", "inv_0500") is not None
    repo.close()


def test_f05_cache_hydration_boots_stopped(tmp_path):
    """F05 Verified: Persisted running=True boots stopped (active=False)."""
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
            "running": True,  # Unclean shutdown / persisted active state
            "scan_mode": "new_only",
            "last_event_ts": 1732270000.0,
        }
    }
    cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

    db_path = tmp_path / "test_f05.sqlite3"
    remote = StubRemote()
    repo = SQLiteSyncedRepository(remote, db_path)
    service = DesktopService(cache_path=cache_file, repository=repo)

    # F05 Fix Verified: Boot invariant active is False!
    assert service.runner.active is False
    service.close()


def test_f12_autonomy_mode_legacy_migration():
    """F12 Verified: auto migrates to turbo with migration reason recorded."""
    cfg = DeskSettings(autonomy_mode="auto")
    assert cfg.autonomy_mode == "turbo"
    assert "auto -> turbo" in cfg.legacy_autonomy_migration


def test_f24_mute_minimum_and_independent_clocks():
    """F24 Verified: human_mute_hours supports 0.25 (15 min) and quote validity clock is configurable."""
    cfg = DeskSettings(human_mute_hours=0.25, quote_validity_minutes=45)
    assert cfg.human_mute_hours == 0.25
    assert cfg.quote_validity_minutes == 45


def test_named_iphone_inquiry_signal():
    """Phase 0 Gate: Signal evaluation for named iPhone inquiry."""
    text = "I want to buy iPhone 17 256GB"
    worth, category = is_worth_effort(text)
    assert worth is True
    assert category == "TRADE_SIGNAL"

    # And bargaining sample dropped by filter
    bargain_text = "825 zyada hai, 790 mein de do"
    worth_b, cat_b = is_worth_effort(bargain_text)
    assert worth_b is False
    assert cat_b == "NO_TRADE_SIGNAL"
