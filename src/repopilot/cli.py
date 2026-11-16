from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

from .agent import RepoPilotEngine
from .api import create_app
from .config import Settings
from .evaluation import evaluate
from .models import ApprovalRequest, RunRequest, RunStatus


def _settings_for_repository(repository: Path, database: Path | None = None) -> Settings:
    resolved = repository.resolve()
    return Settings(
        allowed_roots=str(resolved.parent),
        database_path=database or Path(".repopilot/repopilot.db"),
    )


async def _run_agent(args: argparse.Namespace) -> int:
    repository = Path(args.repository).resolve()
    issue = " ".join(args.issue) if isinstance(args.issue, list) else args.issue
    settings = _settings_for_repository(repository, Path(args.database))
    if args.provider:
        settings.provider = args.provider
    if args.model:
        settings.model = args.model
    if args.api_base:
        settings.api_base = args.api_base
    engine = RepoPilotEngine(settings)
    run = engine.create_run(
        RunRequest(
            repository_path=repository,
            issue=issue,
            test_command=args.test_command,
            auto_approve=args.auto_approve,
            max_steps=args.max_steps,
            provider=args.provider,
        )
    )
    run = await engine.execute_run(run.id)
    if run.status == RunStatus.WAITING_APPROVAL:
        print("\nProposed patch:\n")
        print(run.pending_patch)
        if args.non_interactive:
            print(json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2))
            return 2
        approved = input("Apply this patch? [y/N] ").strip().casefold() in {"y", "yes"}
        run = await engine.approve_run(
            run.id,
            ApprovalRequest(approved=approved, comment="CLI reviewer decision"),
        )
    print(json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if run.status == RunStatus.SUCCEEDED else 1


def _prepare_demo(destination: Path, force: bool = False) -> Path:
    source = Path(__file__).resolve().parents[2] / "examples" / "buggy_calculator"
    destination = destination.resolve()
    if destination.exists():
        if not force:
            raise FileExistsError(f"Destination already exists: {destination}")
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "demo@repopilot.local"],
        ["git", "config", "user.name", "RepoPilot Demo"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "buggy calculator fixture"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=destination, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repopilot", description="Auditable repository coding agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run the agent against one Git repository")
    run.add_argument("repository")
    run.add_argument(
        "issue",
        nargs="+",
        help="Issue text; multiple native-shell arguments are joined to preserve quotes on Windows",
    )
    run.add_argument("--test-command", default="python -m pytest -q")
    run.add_argument("--provider", choices=["offline", "openai_compatible"], default="offline")
    run.add_argument("--model")
    run.add_argument("--api-base")
    run.add_argument("--database", default=".repopilot/repopilot.db")
    run.add_argument("--max-steps", type=int)
    run.add_argument("--auto-approve", action="store_true")
    run.add_argument("--non-interactive", action="store_true")

    serve = subparsers.add_parser("serve", help="Start the FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    evaluation = subparsers.add_parser("eval", help="Run an offline task suite")
    evaluation.add_argument("task_file")
    evaluation.add_argument("--output", default=".repopilot/evaluation.json")

    demo = subparsers.add_parser("prepare-demo", help="Create a fresh demo Git repository")
    demo.add_argument("destination", nargs="?", default=".demo/buggy_calculator")
    demo.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run":
        raise SystemExit(asyncio.run(_run_agent(args)))
    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(Settings()), host=args.host, port=args.port)
        return
    if args.command == "eval":
        report = asyncio.run(evaluate(Path(args.task_file), Path(args.output)))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["success_rate"] == 1.0 else 1)
    if args.command == "prepare-demo":
        destination = _prepare_demo(Path(args.destination), args.force)
        print(destination)


if __name__ == "__main__":
    main()
