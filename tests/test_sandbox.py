from __future__ import annotations

from pathlib import Path

from repopilot.sandbox import LocalSandbox


def test_sandbox_runs_test_command(git_repository: Path) -> None:
    result = LocalSandbox(timeout_seconds=30).run(
        git_repository, ["python", "-m", "pytest", "-q"]
    )
    assert result.exit_code == 1
    assert "failed" in result.stdout.casefold()

