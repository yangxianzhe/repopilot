from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .config import Settings
from .models import (
    ApprovalRequest,
    AuditEvent,
    Observation,
    RunRecord,
    RunRequest,
    RunStatus,
    TERMINAL_STATUSES,
    ToolCall,
    utc_now,
)
from .providers import OfflineHeuristicProvider, OpenAICompatibleProvider
from .repository import GitRepository
from .sandbox import DockerSandbox, LocalSandbox
from .security import resolve_repository, validate_patch
from .store import RunStore
from .tools import ToolRegistry


class AgentState(TypedDict, total=False):
    run_id: str
    repository_path: str
    issue: str
    test_command: str
    provider: str
    auto_approve: bool
    max_steps: int
    step: int
    history: list[dict[str, Any]]
    current_call: dict[str, Any] | None
    signatures: dict[str, int]
    pending_patch: str | None
    proposal_valid: bool
    done: bool


class RepoPilotEngine:
    """Durable run service around a LangGraph inspect/patch/test loop."""

    def __init__(self, settings: Settings | None = None, store: RunStore | None = None) -> None:
        self.settings = settings or Settings()
        self.settings.ensure_directories()
        self.store = store or RunStore(self.settings.database_path)
        self._checkpointer = InMemorySaver()
        self.graph = self._build_graph()
        self._tasks: dict[str, asyncio.Task[RunRecord]] = {}

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("decide", self._decide)
        graph.add_node("execute", self._execute)
        graph.add_node("propose", self._propose)
        graph.add_node("approve", self._approve)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "decide")
        graph.add_conditional_edges(
            "decide",
            self._route_decision,
            {"execute": "execute", "propose": "propose", "finalize": "finalize"},
        )
        graph.add_edge("execute", "decide")
        graph.add_conditional_edges(
            "propose",
            lambda state: "approve" if state.get("proposal_valid") else "decide",
            {"approve": "approve", "decide": "decide"},
        )
        graph.add_conditional_edges(
            "approve",
            lambda state: "end" if state.get("done") else "decide",
            {"end": END, "decide": "decide"},
        )
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self._checkpointer)

    def create_run(self, request: RunRequest) -> RunRecord:
        repository = resolve_repository(request.repository_path, self.settings.allowed_root_paths)
        provider = request.provider or self.settings.provider
        if provider not in {"offline", "openai_compatible"}:
            raise ValueError(f"Unsupported provider: {provider}")
        if provider == "openai_compatible" and not self.settings.model:
            raise ValueError("An API model is required for the openai_compatible provider")
        run = RunRecord(
            repository_path=str(repository),
            issue=request.issue,
            test_command=request.test_command,
            auto_approve=request.auto_approve,
            max_steps=request.max_steps or self.settings.max_steps,
            provider=provider,
        )
        self.store.save_run(run)
        self._event(run.id, "run_created", "Run created", data={"request": request.model_dump(mode="json")})
        return run

    async def execute_run(self, run_id: str) -> RunRecord:
        run = self._require_run(run_id)
        if run.status in TERMINAL_STATUSES or run.status == RunStatus.WAITING_APPROVAL:
            return run
        run.status = RunStatus.RUNNING
        run.metrics.started_at = run.metrics.started_at or utc_now()
        self.store.save_run(run)
        self._event(run.id, "run_started", "Agent loop started")
        state: AgentState = {
            "run_id": run.id,
            "repository_path": run.repository_path,
            "issue": run.issue,
            "test_command": run.test_command,
            "provider": run.provider,
            "auto_approve": run.auto_approve,
            "max_steps": run.max_steps,
            "step": 0,
            "history": [],
            "current_call": None,
            "signatures": {},
            "pending_patch": None,
            "proposal_valid": False,
            "done": False,
        }
        try:
            await self.graph.ainvoke(
                state,
                config={
                    "configurable": {"thread_id": run.id},
                    "recursion_limit": run.max_steps * 4 + 12,
                },
            )
        except Exception as exc:
            self._fail_run(run.id, exc)
        return self._require_run(run.id)

    async def approve_run(self, run_id: str, request: ApprovalRequest) -> RunRecord:
        run = self._require_run(run_id)
        if run.status != RunStatus.WAITING_APPROVAL or not run.pending_patch:
            raise ValueError("Run is not waiting for patch approval")
        self._event(
            run.id,
            "approval_received",
            "Patch approved" if request.approved else "Patch rejected",
            data=request.model_dump(),
        )
        graph_config = {
            "configurable": {"thread_id": run.id},
            "recursion_limit": run.max_steps * 4 + 12,
        }
        try:
            snapshot = await self.graph.aget_state(graph_config)
            if not snapshot.values or not snapshot.next:
                await self._resume_persisted_patch(run, request)
                return self._require_run(run.id)
            await self.graph.ainvoke(
                Command(resume=request.model_dump()),
                config=graph_config,
            )
        except Exception as exc:
            # A process restart loses the in-memory LangGraph cursor, but the
            # pending patch is persisted. Resume it from the SQLite run record.
            if "checkpoint" in str(exc).casefold() or "resume" in str(exc).casefold():
                await self._resume_persisted_patch(run, request)
            else:
                self._fail_run(run.id, exc)
        return self._require_run(run.id)

    def start_background(self, run_id: str) -> asyncio.Task[RunRecord]:
        existing = self._tasks.get(run_id)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self.execute_run(run_id), name=f"repopilot-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return task

    def get_run(self, run_id: str) -> RunRecord:
        return self._require_run(run_id)

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        return self.store.list_runs(limit)

    def events(self, run_id: str, after: int = 0) -> list[AuditEvent]:
        self._require_run(run_id)
        return self.store.events(run_id, after)

    async def _decide(self, state: AgentState) -> dict[str, Any]:
        run = self._require_run(state["run_id"])
        step = int(state.get("step", 0)) + 1
        if step > int(state["max_steps"]):
            call = ToolCall(
                name="finish",
                arguments={"success": False, "summary": "Maximum agent steps exceeded."},
            )
        else:
            repository = self._repository(state["repository_path"])
            summary = repository.repository_summary()
            summary["test_command"] = state["test_command"]
            history = [Observation.model_validate(item) for item in state.get("history", [])]
            provider = self._provider(state["provider"])
            call = await provider.decide(state["issue"], summary, history, ToolRegistry.definitions())
            known_tools = {definition.name for definition in ToolRegistry.definitions()}
            if call.name not in known_tools:
                call = ToolCall(
                    name="finish",
                    arguments={"success": False, "summary": f"Provider requested unknown tool: {call.name}"},
                )
            usage = getattr(provider, "last_usage", {})
            run.metrics.input_tokens += int(usage.get("prompt_tokens", 0))
            run.metrics.output_tokens += int(usage.get("completion_tokens", 0))

        signatures = dict(state.get("signatures", {}))
        signature = call.signature()
        signatures[signature] = signatures.get(signature, 0) + 1
        if signatures[signature] > self.settings.max_repeat_actions:
            run.metrics.repeated_actions += 1
            call = ToolCall(
                name="finish",
                arguments={
                    "success": False,
                    "summary": f"Loop guard stopped repeated action: {signature[:180]}",
                },
            )
        run.current_step = step
        run.metrics.steps = step
        self.store.save_run(run)
        self._event(
            run.id,
            "decision",
            call.rationale or f"Selected {call.name}",
            tool_name=call.name,
            data={"arguments": call.arguments, "step": step},
        )
        return {"step": step, "current_call": call.model_dump(), "signatures": signatures}

    @staticmethod
    def _route_decision(state: AgentState) -> str:
        call = ToolCall.model_validate(state["current_call"])
        if call.name == "finish":
            return "finalize"
        if call.name == "propose_patch":
            return "propose"
        return "execute"

    def _execute(self, state: AgentState) -> dict[str, Any]:
        call = ToolCall.model_validate(state["current_call"])
        run = self._require_run(state["run_id"])
        started = time.perf_counter()
        try:
            arguments = dict(call.arguments)
            if call.name == "run_tests":
                arguments["command"] = state["test_command"]
            output = self._registry(state["repository_path"]).execute(call.name, arguments)
            observation = Observation(
                tool_name=call.name,
                arguments=arguments,
                ok=True,
                output=output,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            observation = Observation(
                tool_name=call.name,
                arguments=call.arguments,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        run.metrics.tool_calls += 1
        if call.name == "run_tests":
            run.metrics.tests_run += 1
        self.store.save_run(run)
        self._record_observation(run.id, observation)
        history = [*state.get("history", []), observation.model_dump(mode="json")]
        return {"history": history, "current_call": None}

    def _propose(self, state: AgentState) -> dict[str, Any]:
        call = ToolCall.model_validate(state["current_call"])
        run = self._require_run(state["run_id"])
        patch = str(call.arguments.get("patch", ""))
        try:
            paths = validate_patch(self._repository(state["repository_path"]).root, patch)
        except Exception as exc:
            observation = Observation(
                tool_name="propose_patch",
                arguments={"rationale": call.arguments.get("rationale", "")},
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            run.metrics.tool_calls += 1
            self.store.save_run(run)
            self._record_observation(run.id, observation)
            return {
                "history": [*state.get("history", []), observation.model_dump(mode="json")],
                "proposal_valid": False,
                "current_call": None,
            }
        run.pending_patch = patch
        run.status = RunStatus.RUNNING if state.get("auto_approve") else RunStatus.WAITING_APPROVAL
        run.metrics.tool_calls += 1
        self.store.save_run(run)
        self._event(
            run.id,
            "patch_proposed",
            str(call.arguments.get("rationale", "Patch proposed")),
            tool_name="propose_patch",
            data={"paths": paths, "patch": patch},
        )
        return {"pending_patch": patch, "proposal_valid": True}

    def _approve(self, state: AgentState) -> dict[str, Any]:
        run = self._require_run(state["run_id"])
        if state.get("auto_approve"):
            decision = {"approved": True, "comment": "auto-approved by run configuration"}
        else:
            decision = interrupt(
                {
                    "run_id": run.id,
                    "message": "Review the proposed unified diff before it is applied.",
                    "patch": state["pending_patch"],
                }
            )
        if not decision.get("approved"):
            run.status = RunStatus.REJECTED
            run.result = decision.get("comment") or "Patch rejected by reviewer."
            run.pending_patch = None
            self._finish_metrics(run)
            self.store.save_run(run)
            self._event(run.id, "run_rejected", run.result)
            return {"done": True, "pending_patch": None}
        started = time.perf_counter()
        try:
            paths = self._repository(state["repository_path"]).apply_patch(str(state["pending_patch"]))
            observation = Observation(
                tool_name="apply_patch",
                arguments={"paths": paths, "comment": decision.get("comment", "")},
                ok=True,
                output={"paths": paths},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            run.status = RunStatus.RUNNING
        except Exception as exc:
            observation = Observation(
                tool_name="apply_patch",
                arguments={},
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        run.pending_patch = None
        run.metrics.tool_calls += 1
        self.store.save_run(run)
        self._record_observation(run.id, observation)
        return {
            "history": [*state.get("history", []), observation.model_dump(mode="json")],
            "pending_patch": None,
            "proposal_valid": False,
            "current_call": None,
        }

    def _finalize(self, state: AgentState) -> dict[str, Any]:
        call = ToolCall.model_validate(state["current_call"])
        run = self._require_run(state["run_id"])
        success = bool(call.arguments.get("success"))
        run.status = RunStatus.SUCCEEDED if success else RunStatus.FAILED
        run.result = str(call.arguments.get("summary", "Run finished."))
        run.pending_patch = None
        self._finish_metrics(run)
        self.store.save_run(run)
        self._event(
            run.id,
            "run_finished",
            run.result,
            data={"status": run.status.value, "metrics": run.metrics.model_dump(mode="json")},
        )
        return {"done": True, "pending_patch": None}

    async def _resume_persisted_patch(self, run: RunRecord, request: ApprovalRequest) -> None:
        if not request.approved:
            run.status = RunStatus.REJECTED
            run.result = request.comment or "Patch rejected by reviewer."
            run.pending_patch = None
            self._finish_metrics(run)
            self.store.save_run(run)
            self._event(run.id, "run_rejected", run.result)
            return
        patch = run.pending_patch or ""
        try:
            paths = self._repository(run.repository_path).apply_patch(patch)
        except Exception as exc:
            self._fail_run(run.id, exc)
            return
        observation = Observation(
            tool_name="apply_patch",
            arguments={"paths": paths, "comment": request.comment},
            ok=True,
            output={"paths": paths},
        )
        run.status = RunStatus.RUNNING
        run.pending_patch = None
        run.metrics.tool_calls += 1
        self.store.save_run(run)
        self._record_observation(run.id, observation)
        state: AgentState = {
            "run_id": run.id,
            "repository_path": run.repository_path,
            "issue": run.issue,
            "test_command": run.test_command,
            "provider": run.provider,
            "auto_approve": True,
            "max_steps": run.max_steps,
            "step": run.current_step,
            "history": [observation.model_dump(mode="json")],
            "current_call": None,
            "signatures": {},
            "pending_patch": None,
            "proposal_valid": False,
            "done": False,
        }
        await self.graph.ainvoke(
            state,
            config={
                "configurable": {"thread_id": f"{run.id}-recovered"},
                "recursion_limit": run.max_steps * 4 + 12,
            },
        )

    def _provider(self, name: str):
        if name == "offline":
            return OfflineHeuristicProvider()
        return OpenAICompatibleProvider(
            api_base=self.settings.api_base,
            model=self.settings.model,
            api_key=self.settings.api_key or None,
            timeout_seconds=self.settings.timeout_seconds,
        )

    def _repository(self, path: str) -> GitRepository:
        return GitRepository(
            root=resolve_repository(
                Path(path), self.settings.allowed_root_paths
            ),
            max_file_bytes=self.settings.max_file_bytes,
        )

    def _registry(self, path: str) -> ToolRegistry:
        if self.settings.sandbox_mode == "docker":
            sandbox = DockerSandbox(
                image=self.settings.docker_image,
                timeout_seconds=self.settings.timeout_seconds,
                max_output_chars=self.settings.max_output_chars,
            )
        else:
            sandbox = LocalSandbox(
                timeout_seconds=self.settings.timeout_seconds,
                max_output_chars=self.settings.max_output_chars,
            )
        return ToolRegistry(self._repository(path), sandbox)

    def _record_observation(self, run_id: str, observation: Observation) -> None:
        message = (
            f"{observation.tool_name} completed"
            if observation.ok
            else f"{observation.tool_name} failed: {observation.error}"
        )
        self._event(
            run_id,
            "tool_result",
            message,
            tool_name=observation.tool_name,
            data={"observation": observation.model_dump(mode="json")},
        )

    def _event(
        self,
        run_id: str,
        kind: str,
        message: str,
        *,
        tool_name: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return self.store.add_event(
            AuditEvent(
                run_id=run_id,
                kind=kind,
                message=message,
                tool_name=tool_name,
                data=data or {},
            )
        )

    def _require_run(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run: {run_id}")
        return run

    def _fail_run(self, run_id: str, exc: Exception) -> None:
        run = self._require_run(run_id)
        run.status = RunStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        self._finish_metrics(run)
        self.store.save_run(run)
        self._event(run.id, "run_failed", run.error)

    @staticmethod
    def _finish_metrics(run: RunRecord) -> None:
        run.metrics.finished_at = utc_now()
        if run.metrics.started_at:
            run.metrics.elapsed_ms = (
                run.metrics.finished_at - run.metrics.started_at
            ).total_seconds() * 1000
