from __future__ import annotations

import asyncio
from pathlib import Path

from repopilot.agent import RepoPilotEngine
from repopilot.config import Settings
from repopilot.models import ApprovalRequest, RunRequest, RunStatus


def make_engine(tmp_path: Path, repository: Path) -> RepoPilotEngine:
    settings = Settings(
        allowed_roots=str(repository.parent),
        database_path=tmp_path / "runs.db",
        provider="offline",
        timeout_seconds=30,
    )
    return RepoPilotEngine(settings)


def test_end_to_end_auto_approved_repair(git_repository: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = make_engine(tmp_path, git_repository)
        run = engine.create_run(
            RunRequest(
                repository_path=git_repository,
                issue='Replace "return a - b" with "return a + b" in maths.py',
                test_command="python -m pytest -q",
                auto_approve=True,
            )
        )
        finished = await engine.execute_run(run.id)
        assert finished.status == RunStatus.SUCCEEDED
        assert finished.metrics.tests_run == 1
        assert "return a + b" in (git_repository / "maths.py").read_text(encoding="utf-8")
        assert any(event.kind == "patch_proposed" for event in engine.events(run.id))

    asyncio.run(scenario())


def test_manual_patch_approval(git_repository: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = make_engine(tmp_path, git_repository)
        run = engine.create_run(
            RunRequest(
                repository_path=git_repository,
                issue='Replace "return a - b" with "return a + b" in maths.py',
                test_command="python -m pytest -q",
                auto_approve=False,
            )
        )
        waiting = await engine.execute_run(run.id)
        assert waiting.status == RunStatus.WAITING_APPROVAL
        assert waiting.pending_patch and "return a + b" in waiting.pending_patch
        finished = await engine.approve_run(
            run.id, ApprovalRequest(approved=True, comment="Reviewed in test")
        )
        assert finished.status == RunStatus.SUCCEEDED

    asyncio.run(scenario())


def test_patch_approval_recovers_after_engine_restart(
    git_repository: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        first_engine = make_engine(tmp_path, git_repository)
        run = first_engine.create_run(
            RunRequest(
                repository_path=git_repository,
                issue='Replace "return a - b" with "return a + b" in maths.py',
                test_command="python -m pytest -q",
                auto_approve=False,
            )
        )
        waiting = await first_engine.execute_run(run.id)
        assert waiting.status == RunStatus.WAITING_APPROVAL

        restarted_engine = make_engine(tmp_path, git_repository)
        finished = await restarted_engine.approve_run(
            run.id, ApprovalRequest(approved=True, comment="Approved after restart")
        )
        assert finished.status == RunStatus.SUCCEEDED
        assert finished.metrics.tests_run == 1

    asyncio.run(scenario())
