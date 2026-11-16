from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.security import SecurityError, resolve_repo_file, validate_command, validate_patch


def test_rejects_path_traversal(git_repository: Path) -> None:
    with pytest.raises(SecurityError):
        resolve_repo_file(git_repository, "../secret.txt", must_exist=False)


def test_rejects_shell_chaining() -> None:
    with pytest.raises(SecurityError):
        validate_command("python -m pytest; whoami")


def test_accepts_argument_list_without_shell() -> None:
    assert validate_command(["python", "-m", "pytest", "-q"]) == [
        "python",
        "-m",
        "pytest",
        "-q",
    ]


def test_rejects_patch_outside_repository(git_repository: Path) -> None:
    patch = "--- a/../secret.txt\n+++ b/../secret.txt\n@@ -1 +1 @@\n-old\n+new\n"
    with pytest.raises(SecurityError):
        validate_patch(git_repository, patch)

