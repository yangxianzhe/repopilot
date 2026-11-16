from __future__ import annotations

from pathlib import Path

from repopilot.models import AuditEvent, RunRecord
from repopilot.store import RunStore


def test_store_releases_database_file(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    store = RunStore(database)
    run = store.save_run(
        RunRecord(repository_path=str(tmp_path), issue="A valid test issue", test_command="pytest")
    )
    store.add_event(AuditEvent(run_id=run.id, kind="test", message="stored"))
    renamed = tmp_path / "renamed.db"
    database.rename(renamed)
    assert renamed.exists()

