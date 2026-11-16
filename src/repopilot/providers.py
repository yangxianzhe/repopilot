from __future__ import annotations

import difflib
import json
import re
from typing import Any, Protocol

import httpx

from .models import Observation, ToolCall
from .tools import ToolDefinition


SYSTEM_PROMPT = """You are RepoPilot, a careful software-engineering agent working in one Git repository.
Use tools to inspect before editing. Make the smallest correct unified diff. Never invent file content.
Every mutation must be proposed through propose_patch. Run relevant tests after applying a patch.
Use finish only when the task is complete or when you can clearly explain why it failed.
Do not request shell commands outside the run_tests allow-list.
"""


class DecisionProvider(Protocol):
    async def decide(
        self,
        issue: str,
        repository_summary: dict[str, Any],
        history: list[Observation],
        tools: list[ToolDefinition],
    ) -> ToolCall: ...


class OfflineHeuristicProvider:
    """Deterministic provider used for demos, CI and air-gapped evaluation.

    It intentionally handles a narrow but real task form:
    `Replace "old" with "new" in path`. The production provider supports
    arbitrary tasks via an OpenAI-compatible tool-calling endpoint.
    """

    REPLACE_PATTERN = re.compile(
        r"replace\s+([\"'`])(?P<old>.*?)\1\s+with\s+([\"'`])(?P<new>.*?)\3\s+in\s+(?P<path>\S+)",
        re.IGNORECASE | re.DOTALL,
    )
    PLAIN_REPLACE_PATTERN = re.compile(
        r"replace\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)\s+in\s+(?P<path>\S+)$",
        re.IGNORECASE | re.DOTALL,
    )

    async def decide(
        self,
        issue: str,
        repository_summary: dict[str, Any],
        history: list[Observation],
        tools: list[ToolDefinition],
    ) -> ToolCall:
        match = self.REPLACE_PATTERN.search(issue.strip()) or self.PLAIN_REPLACE_PATTERN.search(
            issue.strip()
        )
        if not match:
            return ToolCall(
                name="finish",
                arguments={
                    "success": False,
                    "summary": (
                        "Offline mode supports `Replace \"old\" with \"new\" in path`. "
                        "Configure an OpenAI-compatible provider for general issues."
                    ),
                },
                rationale="The offline planner cannot safely infer an arbitrary edit.",
            )
        old, new, path = match.group("old"), match.group("new"), match.group("path").rstrip(".,")
        if not history:
            return ToolCall(
                name="search_code",
                arguments={"query": old, "glob": path, "limit": 20},
                rationale="Locate the exact buggy text before reading or editing.",
            )

        last = history[-1]
        if last.tool_name == "search_code":
            matches = last.output if isinstance(last.output, list) else []
            if not matches:
                return ToolCall(
                    name="finish",
                    arguments={"success": False, "summary": f"Could not find the target text in {path}."},
                )
            located_path = str(matches[0]["path"])
            return ToolCall(
                name="read_file",
                arguments={"path": located_path},
                rationale="Read the complete target file so the patch has exact context.",
            )

        if last.tool_name == "read_file":
            payload = last.output if isinstance(last.output, dict) else {}
            content = str(payload.get("content", ""))
            located_path = str(payload.get("path", path))
            if old not in content:
                return ToolCall(
                    name="finish",
                    arguments={"success": False, "summary": "The target text disappeared before patching."},
                )
            updated = content.replace(old, new, 1)
            patch = "".join(
                difflib.unified_diff(
                    content.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=f"a/{located_path}",
                    tofile=f"b/{located_path}",
                )
            )
            return ToolCall(
                name="propose_patch",
                arguments={"patch": patch, "rationale": f"Replace the requested text in {located_path}."},
                rationale="The requested edit is localized and preserves surrounding code.",
            )

        if last.tool_name == "apply_patch":
            return ToolCall(
                name="run_tests",
                arguments={"command": repository_summary.get("test_command", "python -m pytest -q")},
                rationale="Verify the change with the repository test suite.",
            )

        if last.tool_name == "run_tests":
            output = last.output if isinstance(last.output, dict) else {}
            passed = output.get("exit_code") == 0
            if passed:
                return ToolCall(name="git_diff", arguments={}, rationale="Collect the final auditable diff.")
            return ToolCall(
                name="finish",
                arguments={
                    "success": False,
                    "summary": "The requested edit was applied, but the test suite failed. Review the recorded output.",
                },
            )

        if last.tool_name == "git_diff":
            return ToolCall(
                name="finish",
                arguments={"success": True, "summary": "Applied the requested change and all tests passed."},
            )

        return ToolCall(
            name="finish",
            arguments={"success": False, "summary": f"Unexpected offline workflow state: {last.tool_name}."},
        )


class OpenAICompatibleProvider:
    def __init__(
        self,
        api_base: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.last_usage: dict[str, int] = {}

    async def decide(
        self,
        issue: str,
        repository_summary: dict[str, Any],
        history: list[Observation],
        tools: list[ToolDefinition],
    ) -> ToolCall:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"issue": issue, "repository": repository_summary},
                    ensure_ascii=False,
                ),
            },
        ]
        for observation in history[-12:]:
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "tool": observation.tool_name,
                            "arguments": observation.arguments,
                            "ok": observation.ok,
                            "output": observation.output,
                            "error": observation.error,
                        },
                        ensure_ascii=False,
                        default=str,
                    )[:24_000],
                }
            )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": [tool.openai_schema() for tool in tools],
            "tool_choice": "auto",
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.api_base}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        self.last_usage = data.get("usage", {})
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return ToolCall(
                name="finish",
                arguments={"success": False, "summary": message.get("content") or "Model returned no tool call."},
            )
        function = tool_calls[0]["function"]
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Model returned invalid tool arguments: {exc}") from exc
        return ToolCall(name=function["name"], arguments=arguments, rationale=message.get("content") or "")
