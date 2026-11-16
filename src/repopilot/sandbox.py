from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .security import truncate_output, validate_command


@dataclass(slots=True)
class ExecutionResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalSandbox:
    """Constrained local runner for development and CI.

    It does not invoke a shell, accepts only allow-listed test commands, limits
    runtime/output, and fixes the working directory to the repository.
    """

    def __init__(self, timeout_seconds: int = 120, max_output_chars: int = 30_000) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def run(self, repository: Path, command: list[str] | str) -> ExecutionResult:
        argv = validate_command(command)
        executable = Path(argv[0]).name.casefold()
        if executable in {"python", "python.exe", "python3", "python3.exe"}:
            argv[0] = sys.executable
        elif executable in {"pytest", "pytest.exe", "pytest.cmd"}:
            argv = [sys.executable, "-m", "pytest", *argv[1:]]
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CI": "1",
        }
        started = time.perf_counter()
        try:
            result = subprocess.run(
                argv,
                cwd=repository,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
                env=environment,
            )
            return ExecutionResult(
                command=argv,
                exit_code=result.returncode,
                stdout=truncate_output(result.stdout, self.max_output_chars),
                stderr=truncate_output(result.stderr, self.max_output_chars),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                command=argv,
                exit_code=124,
                stdout=truncate_output(exc.stdout or "", self.max_output_chars),
                stderr=truncate_output(exc.stderr or "", self.max_output_chars),
                duration_ms=int((time.perf_counter() - started) * 1000),
                timed_out=True,
            )


class DockerSandbox(LocalSandbox):
    """Networkless, resource-limited Docker runner for untrusted test suites."""

    def __init__(
        self,
        image: str,
        timeout_seconds: int = 120,
        max_output_chars: int = 30_000,
    ) -> None:
        super().__init__(timeout_seconds, max_output_chars)
        self.image = image

    def run(self, repository: Path, command: list[str] | str) -> ExecutionResult:
        if not shutil.which("docker"):
            raise RuntimeError("Docker sandbox requested, but docker is not installed")
        argv = validate_command(command)
        mount = f"{repository.resolve()}:/workspace"
        docker_command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "768m",
            "--cpus",
            "1.5",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            mount,
            "-w",
            "/workspace",
            self.image,
            *argv,
        ]
        started = time.perf_counter()
        try:
            result = subprocess.run(
                docker_command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
            return ExecutionResult(
                command=argv,
                exit_code=result.returncode,
                stdout=truncate_output(result.stdout, self.max_output_chars),
                stderr=truncate_output(result.stderr, self.max_output_chars),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                command=argv,
                exit_code=124,
                stdout=truncate_output(exc.stdout or "", self.max_output_chars),
                stderr=truncate_output(exc.stderr or "", self.max_output_chars),
                duration_ms=int((time.perf_counter() - started) * 1000),
                timed_out=True,
            )
