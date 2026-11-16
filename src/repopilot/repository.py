from __future__ import annotations

import difflib
import fnmatch
import math
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from tree_sitter_language_pack import get_parser

from .security import SecurityError, patch_paths, resolve_repo_file, validate_patch


LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

DECLARATION_NODES = {
    "function_definition",
    "function_declaration",
    "method_definition",
    "method_declaration",
    "class_definition",
    "class_declaration",
    "interface_declaration",
    "struct_item",
    "enum_item",
    "type_alias_declaration",
}

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+")


class RepositoryError(RuntimeError):
    pass


class GitRepository:
    """Read, search and patch one Git working tree with strict path containment."""

    def __init__(self, root: Path, max_file_bytes: int = 1_000_000) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir() or not (self.root / ".git").exists():
            raise SecurityError(f"Not a Git worktree: {self.root}")
        self.max_file_bytes = max_file_bytes
        self.git = shutil.which("git") or "git"

    def _git(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [self.git, *args],
            cwd=self.root,
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            text=False,
            check=False,
        )
        result.stdout = result.stdout.decode("utf-8", "replace")
        result.stderr = result.stderr.decode("utf-8", "replace")
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RepositoryError(f"git {' '.join(args)} failed: {detail}")
        return result

    def list_files(self, glob: str | None = None, limit: int = 500) -> list[str]:
        result = self._git(["ls-files", "-co", "--exclude-standard", "-z"])
        files = sorted({item for item in result.stdout.split("\0") if item})
        if glob:
            files = [path for path in files if fnmatch.fnmatch(path, glob)]
        return files[:limit]

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        target = resolve_repo_file(self.root, path)
        if not target.is_file():
            raise RepositoryError(f"File does not exist: {path}")
        if target.stat().st_size > self.max_file_bytes:
            raise RepositoryError(f"File exceeds {self.max_file_bytes} bytes: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line if end_line is not None else len(lines))
        selected = lines[start - 1 : end]
        numbered = "\n".join(f"{number:>5} | {line}" for number, line in enumerate(selected, start))
        return {
            "path": target.relative_to(self.root).as_posix(),
            "start_line": start,
            "end_line": end,
            "line_count": len(lines),
            "content": "\n".join(selected) + ("\n" if selected else ""),
            "numbered_content": numbered,
        }

    def search_code(
        self,
        query: str,
        glob: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not query:
            raise RepositoryError("Search query must not be empty")
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for relative in self.list_files(glob=glob, limit=10_000):
            target = resolve_repo_file(self.root, relative)
            if not target.is_file() or target.stat().st_size > self.max_file_bytes:
                continue
            try:
                lines = target.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, 1):
                if needle in line.casefold():
                    matches.append(
                        {"path": relative, "line": line_number, "text": line.strip()[:300]}
                    )
                    if len(matches) >= limit:
                        return matches
        return matches

    def find_symbol(self, symbol: str, limit: int = 30) -> list[dict[str, Any]]:
        """Find declarations using tree-sitter; unsupported files are skipped."""
        query = symbol.casefold()
        results: list[dict[str, Any]] = []
        parsers: dict[str, Any] = {}
        for relative in self.list_files(limit=10_000):
            suffix = Path(relative).suffix.lower()
            language = LANGUAGES.get(suffix)
            if not language:
                continue
            target = resolve_repo_file(self.root, relative)
            if not target.is_file() or target.stat().st_size > self.max_file_bytes:
                continue
            source = target.read_bytes()
            try:
                parser = parsers.setdefault(language, get_parser(language))
                tree = parser.parse(source)
            except Exception:
                continue
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                stack.extend(reversed(node.children))
                if node.type not in DECLARATION_NODES:
                    continue
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    continue
                name = source[name_node.start_byte : name_node.end_byte].decode("utf-8", "replace")
                if query not in name.casefold():
                    continue
                first_line = source[node.start_byte : node.end_byte].decode("utf-8", "replace").splitlines()[0]
                results.append(
                    {
                        "path": relative,
                        "line": node.start_point[0] + 1,
                        "kind": node.type,
                        "name": name,
                        "signature": first_line[:300],
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    def semantic_search(
        self,
        query: str,
        glob: str | None = None,
        limit: int = 10,
        chunk_lines: int = 40,
    ) -> list[dict[str, Any]]:
        """Offline lexical embeddings for zero-service semantic-ish retrieval."""
        query_vector = self._vector(query)
        if not query_vector:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for relative in self.list_files(glob=glob, limit=10_000):
            target = resolve_repo_file(self.root, relative)
            if not target.is_file() or target.stat().st_size > self.max_file_bytes:
                continue
            try:
                lines = target.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for index in range(0, len(lines), chunk_lines):
                chunk = "\n".join(lines[index : index + chunk_lines])
                score = self._cosine(query_vector, self._vector(chunk))
                if score:
                    scored.append(
                        (
                            score,
                            {
                                "path": relative,
                                "start_line": index + 1,
                                "end_line": min(index + chunk_lines, len(lines)),
                                "score": round(score, 4),
                                "preview": chunk[:400],
                            },
                        )
                    )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in scored[:limit]]

    @staticmethod
    def _vector(text: str) -> Counter[str]:
        normalized = text.replace("_", " ")
        tokens = [token.casefold() for token in TOKEN_RE.findall(normalized)]
        return Counter(tokens)

    @staticmethod
    def _cosine(left: Counter[str], right: Counter[str]) -> float:
        if not left or not right:
            return 0.0
        common = set(left) & set(right)
        dot = sum(left[token] * right[token] for token in common)
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0

    def apply_patch(self, patch: str) -> list[str]:
        validate_patch(self.root, patch)
        check = self._git(
            ["apply", "--check", "--whitespace=nowarn", "-"],
            input_text=patch,
            check=False,
        )
        if check.returncode != 0:
            raise RepositoryError(f"Patch check failed: {(check.stderr or check.stdout).strip()}")
        self._git(["apply", "--whitespace=nowarn", "-"], input_text=patch)
        return patch_paths(patch)

    def git_diff(self, staged: bool = False) -> str:
        args = ["diff", "--no-ext-diff", "--"]
        if staged:
            args.insert(1, "--cached")
        return self._git(args).stdout

    def status(self) -> list[str]:
        return [line for line in self._git(["status", "--short"]).stdout.splitlines() if line]

    def create_patch(self, path: str, old_content: str, new_content: str) -> str:
        resolve_repo_file(self.root, path, must_exist=False)
        return "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )

    def repository_summary(self) -> dict[str, Any]:
        files = self.list_files(limit=10_000)
        counts = Counter(Path(path).suffix.lower() or "[no extension]" for path in files)
        return {
            "root": str(self.root),
            "files": len(files),
            "languages": dict(counts.most_common(12)),
            "status": self.status(),
        }


def format_search_results(results: Iterable[dict[str, Any]]) -> str:
    return "\n".join(
        f"{item['path']}:{item.get('line', item.get('start_line', 1))}: "
        f"{item.get('text', item.get('signature', item.get('preview', '')))}"
        for item in results
    )
