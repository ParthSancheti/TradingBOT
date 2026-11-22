from jarvis import diagnostics
from jarvis.diagnostics import DiagnosticsLog


def test_log_retention_redaction_and_clear_survive_restart(tmp_path, monkeypatch):
    path = tmp_path / "diagnostics.db"
    now = 1_800_000_000
    monkeypatch.setattr(diagnostics.time, "time", lambda: now)
    monkeypatch.setattr(diagnostics, "MAX_ENTRIES", 3)
    log = DiagnosticsLog(path)
    log.record("engine/start")
    now += diagnostics.MAX_AGE_SECONDS + 1
    assert log.snapshot()["entries"] == []

    log.record("engine/start")
    log.record("whatsapp/dispatch", code="ACCEPTED", count=1)
    log.record("worker/sheets", level="warning", code="https://private.invalid/test?secret=hidden")
    log.record("customer text that must never be logged")
    log.record("stop", duration_ms=123)
    log.close()

    reopened = DiagnosticsLog(path)
    try:
        entries = reopened.snapshot()["entries"]
        assert [entry["event"] for entry in entries] == ["stop", "worker/sheets", "whatsapp/dispatch"]
        assert entries[0]["duration_ms"] == 123
        assert entries[1]["code"] == "OTHER"
        assert entries[2]["code"] == "ACCEPTED" and entries[2]["count"] == 1
        assert "secret=hidden" not in repr(entries)
        reopened.clear()
    finally:
        reopened.close()

    final = DiagnosticsLog(path)
    try:
        assert final.snapshot()["entries"] == []
    finally:
        final.close()
