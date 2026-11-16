from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "sample"
    repository.mkdir()
    (repository / "maths.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_maths.py").write_text(
        "from maths import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    run_git(repository, "init", "-q")
    run_git(repository, "config", "user.email", "tests@example.invalid")
    run_git(repository, "config", "user.name", "RepoPilot Tests")
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-qm", "fixture")
    return repository

