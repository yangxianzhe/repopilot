from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

from .agent import RepoPilotEngine
from .config import Settings
from .models import EvaluationResult, EvaluationTask, RunRequest, RunStatus
from .repository import GitRepository


def load_tasks(path: Path) -> list[EvaluationTask]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_tasks = payload.get("tasks", payload if isinstance(payload, list) else [])
    tasks: list[EvaluationTask] = []
    for raw in raw_tasks:
        item = dict(raw)
        repository = Path(item["repository_path"])
        if not repository.is_absolute():
            repository = (path.parent / repository).resolve()
        item["repository_path"] = repository
        tasks.append(EvaluationTask.model_validate(item))
    return tasks


def _initialize_git(repository: Path) -> None:
    if (repository / ".git").exists():
        return
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "repopilot@example.invalid"],
        ["git", "config", "user.name", "RepoPilot Eval"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "evaluation fixture"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=repository, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)


async def evaluate(path: Path, output: Path | None = None) -> dict[str, Any]:
    tasks = load_tasks(path)
    results: list[EvaluationResult] = []
    with tempfile.TemporaryDirectory(prefix="repopilot-eval-") as temporary:
        workspace = Path(temporary)
        settings = Settings(
            allowed_roots=str(workspace),
            database_path=workspace / "evaluation.db",
            provider="offline",
            sandbox_mode="local",
        )
        engine = RepoPilotEngine(settings)
        for task in tasks:
            repository = workspace / task.id
            shutil.copytree(task.repository_path, repository, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            _initialize_git(repository)
            run = engine.create_run(
                RunRequest(
                    repository_path=repository,
                    issue=task.issue,
                    test_command=task.test_command,
                    auto_approve=True,
                    provider="offline",
                )
            )
            run = await engine.execute_run(run.id)
            changed = [line[3:] for line in GitRepository(repository).status() if len(line) > 3]
            expected_ok = not task.expected_files or set(task.expected_files).issubset(changed)
            results.append(
                EvaluationResult(
                    task_id=task.id,
                    run_id=run.id,
                    success=run.status == RunStatus.SUCCEEDED and expected_ok,
                    status=run.status,
                    elapsed_ms=run.metrics.elapsed_ms,
                    tool_calls=run.metrics.tool_calls,
                    tests_run=run.metrics.tests_run,
                    changed_files=changed,
                    error=run.error if expected_ok else "Expected files were not changed",
                )
            )
    successful = sum(result.success for result in results)
    report = {
        "task_count": len(results),
        "successful": successful,
        "success_rate": successful / len(results) if results else 0.0,
        "average_elapsed_ms": mean([result.elapsed_ms for result in results]) if results else 0.0,
        "average_tool_calls": mean([result.tool_calls for result in results]) if results else 0.0,
        "results": [result.model_dump(mode="json") for result in results],
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report

