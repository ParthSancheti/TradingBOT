import pytest
from test_phase4_adversarial import AdversarialFakeSheets

from jarvis.local_repo import SQLiteSyncedRepository
from jarvis.sheets import SheetsError
from jarvis.workspace import _quantity


class OfflineSheets(AdversarialFakeSheets):
    def __init__(self):
        super().__init__()
        self.offline = True

    async def batch(self, changes, request_id=None):
        if self.offline:
            raise SheetsError("SHEETS_UNAVAILABLE")
        return await super().batch(changes, request_id)


@pytest.mark.asyncio
async def test_long_sheets_outage_does_not_drop_rows_from_sync(tmp_path):
    remote = OfflineSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "outage.db")
    try:
        await repo.put("messages", "m1", {"text": "WTB 10pcs"}, 0)
        for _ in range(12):  # ~48 s of failed 4-second sync loops
            await repo.flush_outbox()
        assert repo.list_quarantined_sheet_writes() == []
        assert repo.sheet_sync_status()["pending"] == 1
        remote.offline = False
        await repo.flush_outbox()
        assert ("messages", "m1") in remote.records
        assert not repo.has_pending_sheet_writes()
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_revision_drift_is_rebased_instead_of_stuck(tmp_path):
    remote = AdversarialFakeSheets()
    repo = SQLiteSyncedRepository(remote, tmp_path / "drift.db")
    try:
        await repo.put("tasks", "t1", {"title": "a"}, 0)
        await repo.flush_outbox()
        # The Sheet row was changed elsewhere (revision moved on) and the ack was lost.
        remote.revisions[("tasks", "t1")] = 3
        remote.records[("tasks", "t1")]["revision"] = 3
        await repo.put("tasks", "t1", {"title": "b"}, 1)
        await repo.flush_outbox()
        assert remote.records[("tasks", "t1")]["data"] == {"title": "b"}
        assert not repo.has_pending_sheet_writes()
    finally:
        repo.close()


def test_sheet_quantities_are_numbers():
    assert [_quantity(v) for v in ("25", "1,200 pcs", 7, None, "n/a", -3, True)] == [25, 1200, 7, 0, 0, 0, 0]
