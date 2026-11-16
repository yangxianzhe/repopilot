from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .repository import GitRepository
from .sandbox import LocalSandbox


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


TOOL_DEFINITIONS = [
    ToolDefinition(
        "list_files",
        "List tracked and unignored files in the repository.",
        {
            "type": "object",
            "properties": {
                "glob": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
        },
    ),
    ToolDefinition(
        "search_code",
        "Search exact text across repository source files.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "glob": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        "semantic_search",
        "Rank repository chunks using offline lexical embeddings.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "glob": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        "find_symbol",
        "Locate function, method, class and type declarations with tree-sitter.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["symbol"],
        },
    ),
    ToolDefinition(
        "read_file",
        "Read a UTF-8 file or a line range from the repository.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": ["integer", "null"], "minimum": 1},
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        "propose_patch",
        "Submit a unified diff for validation and human approval before application.",
        {
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["patch", "rationale"],
        },
    ),
    ToolDefinition(
        "run_tests",
        "Execute an allow-listed test command in the configured sandbox.",
        {
            "type": "object",
            "properties": {
                "command": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                }
            },
            "required": ["command"],
        },
    ),
    ToolDefinition(
        "git_diff",
        "Show the current repository diff.",
        {"type": "object", "properties": {}},
    ),
    ToolDefinition(
        "finish",
        "Finish the run with a concise summary and success status.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "success": {"type": "boolean"},
            },
            "required": ["summary", "success"],
        },
    ),
]


class ToolRegistry:
    def __init__(self, repository: GitRepository, sandbox: LocalSandbox) -> None:
        self.repository = repository
        self.sandbox = sandbox
        self._handlers: dict[str, Callable[..., Any]] = {
            "list_files": repository.list_files,
            "search_code": repository.search_code,
            "semantic_search": repository.semantic_search,
            "find_symbol": repository.find_symbol,
            "read_file": repository.read_file,
            "run_tests": self._run_tests,
            "git_diff": repository.git_diff,
        }

    @staticmethod
    def definitions() -> list[ToolDefinition]:
        return TOOL_DEFINITIONS

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"Tool cannot be executed directly: {name}")
        return handler(**arguments)

    def _run_tests(self, command: list[str] | str) -> dict[str, Any]:
        return self.sandbox.run(self.repository.root, command).to_dict()

