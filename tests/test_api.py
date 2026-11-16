from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from repopilot.api import create_app
from repopilot.config import Settings


def test_health_and_run_creation(git_repository: Path, tmp_path: Path) -> None:
    settings = Settings(
        allowed_roots=str(git_repository.parent),
        database_path=tmp_path / "api.db",
        provider="offline",
        timeout_seconds=30,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["status"] == "ok"
        response = client.post(
            "/api/runs",
            json={
                "repository_path": str(git_repository),
                "issue": 'Replace "return a - b" with "return a + b" in maths.py',
                "test_command": "python -m pytest -q",
                "auto_approve": True,
                "provider": "offline",
            },
        )
        assert response.status_code == 202
        run_id = response.json()["id"]
        for _ in range(120):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.1)
        assert run["status"] == "succeeded"
