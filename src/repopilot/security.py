from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


class SecurityError(ValueError):
    """Raised when an operation escapes the configured security boundary."""


SHELL_META = re.compile(r"[;&|><`$\r\n]")
PATCH_HEADER = re.compile(r"^(?:---|\+\+\+)\s+(?:a/|b/)?(.+?)(?:\t.*)?$")
DEFAULT_ALLOWED_EXECUTABLES = {
    "python",
    "python3",
    "pytest",
    "npm",
    "pnpm",
    "yarn",
    "go",
    "cargo",
    "dotnet",
}


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_repository(path: Path, allowed_roots: list[Path], require_git: bool = True) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise SecurityError(f"Repository path is not a directory: {resolved}")
    roots = [root.expanduser().resolve(strict=True) for root in allowed_roots]
    if not any(is_relative_to(resolved, root) for root in roots):
        raise SecurityError(f"Repository is outside allowed roots: {resolved}")
    if require_git and not (resolved / ".git").exists():
        raise SecurityError(f"Repository is not a Git worktree: {resolved}")
    return resolved


def resolve_repo_file(repo: Path, relative_path: str, *, must_exist: bool = True) -> Path:
    candidate_input = Path(relative_path)
    if candidate_input.is_absolute():
        raise SecurityError("Absolute file paths are not allowed")
    candidate = (repo / candidate_input).resolve(strict=must_exist)
    if not is_relative_to(candidate, repo.resolve()):
        raise SecurityError(f"Path escapes repository: {relative_path}")
    if must_exist and not candidate.is_file():
        raise SecurityError(f"Path is not a file: {relative_path}")
    return candidate


def validate_command(command: str | list[str], allowed: set[str] | None = None) -> list[str]:
    if isinstance(command, list):
        if not command or not all(isinstance(item, str) and item for item in command):
            raise SecurityError("Command arguments must be non-empty strings")
        if any(SHELL_META.search(item) for item in command):
            raise SecurityError("Shell metacharacters and command chaining are not allowed")
        args = command[:]
    else:
        if not command.strip():
            raise SecurityError("Command cannot be empty")
        if SHELL_META.search(command):
            raise SecurityError("Shell metacharacters and command chaining are not allowed")
        try:
            args = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            raise SecurityError(f"Invalid command: {exc}") from exc
    if not args:
        raise SecurityError("Command cannot be empty")
    executable = Path(args[0].strip('"')).name.lower()
    executable = executable.removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
    if executable not in (allowed or DEFAULT_ALLOWED_EXECUTABLES):
        raise SecurityError(f"Executable is not allowed: {executable}")
    return [arg.strip('"') for arg in args]


def patch_paths(patch: str) -> list[str]:
    if len(patch) > 2_000_000:
        raise SecurityError("Patch exceeds 2 MB limit")
    paths: list[str] = []
    for line in patch.splitlines():
        match = PATCH_HEADER.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if value == "/dev/null":
            continue
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise SecurityError(f"Unsafe patch path: {value}")
        paths.append(value.replace("\\", "/"))
    if not paths:
        raise SecurityError("Patch does not contain file headers")
    return sorted(set(paths))


def validate_patch(repo: Path, patch: str) -> list[str]:
    paths = patch_paths(patch)
    for value in paths:
        resolve_repo_file(repo, value, must_exist=False)
    return paths


def truncate_output(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(1, limit // 2)
    return f"{value[:half]}\n... <truncated {len(value) - limit} chars> ...\n{value[-half:]}"
