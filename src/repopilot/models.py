from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.REJECTED,
    RunStatus.CANCELLED,
}


class RunRequest(BaseModel):
    repository_path: Path
    issue: str = Field(min_length=5, max_length=20_000)
    test_command: str = "python -m unittest discover -s tests"
    auto_approve: bool = False
    max_steps: int | None = Field(default=None, ge=2, le=100)
    provider: str | None = None

    @field_validator("repository_path", mode="before")
    @classmethod
    def expand_repository_path(cls, value: Any) -> Path:
        return Path(value).expanduser()


class ApprovalRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=2000)


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""

    def signature(self) -> str:
        import json

        return f"{self.name}:{json.dumps(self.arguments, sort_keys=True, ensure_ascii=False)}"


class Observation(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0


class RunMetrics(BaseModel):
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_ms: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    repeated_actions: int = 0
    tests_run: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class RunRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    repository_path: str
    issue: str
    test_command: str
    provider: str = "offline"
    auto_approve: bool = False
    max_steps: int = 16
    status: RunStatus = RunStatus.PENDING
    current_step: int = 0
    pending_patch: str | None = None
    result: str | None = None
    error: str | None = None
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditEvent(BaseModel):
    run_id: str
    sequence: int = 0
    kind: str
    message: str
    tool_name: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class EvaluationTask(BaseModel):
    id: str
    repository_path: Path
    issue: str
    test_command: str = "python -m unittest discover -s tests"
    expected_files: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class EvaluationResult(BaseModel):
    task_id: str
    run_id: str
    success: bool
    status: RunStatus
    elapsed_ms: float
    tool_calls: int
    tests_run: int
    changed_files: list[str] = Field(default_factory=list)
    error: str | None = None
