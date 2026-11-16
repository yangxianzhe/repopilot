from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .repository import GitRepository
from .sandbox import LocalSandbox
from .security import resolve_repository, validate_patch
from .tools import ToolRegistry


mcp = FastMCP("RepoPilot", instructions="Safe repository inspection and test tools.")
settings = Settings()


def _registry(repository_path: str) -> ToolRegistry:
    root = resolve_repository(Path(repository_path), settings.allowed_root_paths)
    repository = GitRepository(root, max_file_bytes=settings.max_file_bytes)
    sandbox = LocalSandbox(settings.timeout_seconds, settings.max_output_chars)
    return ToolRegistry(repository, sandbox)


@mcp.tool()
def list_files(repository_path: str, glob: str | None = None, limit: int = 500) -> list[str]:
    """List files in an allowed Git repository."""
    return _registry(repository_path).execute("list_files", {"glob": glob, "limit": limit})


@mcp.tool()
def search_code(
    repository_path: str, query: str, glob: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Search exact text in an allowed Git repository."""
    return _registry(repository_path).execute(
        "search_code", {"query": query, "glob": glob, "limit": limit}
    )


@mcp.tool()
def find_symbol(repository_path: str, symbol: str, limit: int = 30) -> list[dict[str, Any]]:
    """Find declarations using tree-sitter."""
    return _registry(repository_path).execute("find_symbol", {"symbol": symbol, "limit": limit})


@mcp.tool()
def read_file(
    repository_path: str, path: str, start_line: int = 1, end_line: int | None = None
) -> dict[str, Any]:
    """Read a repository file without allowing path traversal."""
    return _registry(repository_path).execute(
        "read_file", {"path": path, "start_line": start_line, "end_line": end_line}
    )


@mcp.tool()
def git_diff(repository_path: str) -> str:
    """Return the current unstaged Git diff."""
    return _registry(repository_path).execute("git_diff", {})


@mcp.tool()
def check_patch(repository_path: str, patch: str) -> list[str]:
    """Validate patch paths without applying the patch."""
    registry = _registry(repository_path)
    return validate_patch(registry.repository.root, patch)


@mcp.tool()
def run_tests(repository_path: str, command: str = "python -m pytest -q") -> dict[str, Any]:
    """Run an allow-listed test command in the configured repository sandbox."""
    return _registry(repository_path).execute("run_tests", {"command": command})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

