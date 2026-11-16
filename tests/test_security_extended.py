"""Extended security tests – path traversal, command injection, patch safety."""
from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.security import (
    SecurityError,
    resolve_repo_file,
    validate_command,
    validate_patch,
)


# ── path traversal (8 cases) ────────────────────────────────────────────────

class TestPathTraversal:
    def test_dotdot_prefix(self, git_repository: Path) -> None:
        with pytest.raises(SecurityError):
            resolve_repo_file(git_repository, "../secret.txt", must_exist=False)

    def test_dotdot_nested(self, git_repository: Path) -> None:
        with pytest.raises(SecurityError):
            resolve_repo_file(git_repository, "sub/../../etc/passwd", must_exist=False)

    def test_absolute_path(self, git_repository: Path) -> None:
        with pytest.raises(SecurityError):
            resolve_repo_file(git_repository, "/etc/passwd", must_exist=False)

    def test_windows_absolute(self, git_repository: Path) -> None:
        with pytest.raises(SecurityError):
            resolve_repo_file(git_repository, "C:\\Windows\\system32\\cmd.exe", must_exist=False)

    def test_dotdot_url_encoded_style(self, git_repository: Path) -> None:
        # literal dots – still a traversal attempt
        with pytest.raises(SecurityError):
            resolve_repo_file(git_repository, "..\\secret.txt", must_exist=False)

    def test_deep_dotdot(self, git_repository: Path) -> None:
        with pytest.raises(SecurityError):
            resolve_repo_file(git_repository, "a/b/c/../../../../../../../etc/shadow", must_exist=False)

    def test_sibling_repo_escape(self, git_repository: Path) -> None:
        sibling = str(git_repository.parent / "other_repo" / "config.py")
        with pytest.raises(SecurityError):
            resolve_repo_file(git_repository, sibling, must_exist=False)

    def test_valid_nested_path_accepted(self, git_repository: Path) -> None:
        # should NOT raise – a normal relative path inside the repo
        result = resolve_repo_file(git_repository, "maths.py", must_exist=True)
        assert result.is_file()


# ── command injection (7 cases) ─────────────────────────────────────────────

class TestCommandInjection:
    def test_semicolon_chain(self) -> None:
        with pytest.raises(SecurityError):
            validate_command("python -m pytest; whoami")

    def test_pipe_chain(self) -> None:
        with pytest.raises(SecurityError):
            validate_command("python -m pytest | cat /etc/passwd")

    def test_ampersand_chain(self) -> None:
        with pytest.raises(SecurityError):
            validate_command("python -m pytest && rm -rf /")

    def test_backtick_injection(self) -> None:
        with pytest.raises(SecurityError):
            validate_command("python -m pytest `whoami`")

    def test_dollar_subshell(self) -> None:
        with pytest.raises(SecurityError):
            validate_command("python -m pytest $(id)")

    def test_disallowed_executable(self) -> None:
        with pytest.raises(SecurityError):
            validate_command("bash -c 'whoami'")

    def test_list_with_metachar(self) -> None:
        with pytest.raises(SecurityError):
            validate_command(["python", "-m", "pytest; whoami"])

    def test_valid_list_accepted(self) -> None:
        result = validate_command(["python", "-m", "pytest", "-q"])
        assert result == ["python", "-m", "pytest", "-q"]

    def test_valid_string_accepted(self) -> None:
        result = validate_command("python -m pytest -q")
        assert result[0] == "python"


# ── patch safety (6 cases) ──────────────────────────────────────────────────

class TestPatchSafety:
    def test_patch_outside_repo(self, git_repository: Path) -> None:
        patch = "--- a/../secret.txt\n+++ b/../secret.txt\n@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(SecurityError):
            validate_patch(git_repository, patch)

    def test_patch_absolute_path(self, git_repository: Path) -> None:
        patch = "--- a//etc/passwd\n+++ b//etc/passwd\n@@ -1 +1 @@\n-root\n+hacked\n"
        with pytest.raises(SecurityError):
            validate_patch(git_repository, patch)

    def test_patch_no_header(self, git_repository: Path) -> None:
        patch = "@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(SecurityError):
            validate_patch(git_repository, patch)

    def test_patch_exceeds_size_limit(self, git_repository: Path) -> None:
        # 2 MB limit is enforced in patch_paths; need > 2_000_000 bytes
        huge = "--- a/maths.py\n+++ b/maths.py\n@@ -1 +1 @@\n" + "+x\n" * 700_000
        with pytest.raises(SecurityError):
            validate_patch(git_repository, huge)

    def test_patch_windows_traversal(self, git_repository: Path) -> None:
        patch = "--- a/..\\secret.txt\n+++ b/..\\secret.txt\n@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(SecurityError):
            validate_patch(git_repository, patch)

    def test_valid_patch_accepted(self, git_repository: Path) -> None:
        patch = "--- a/maths.py\n+++ b/maths.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
        paths = validate_patch(git_repository, patch)
        assert "maths.py" in paths
