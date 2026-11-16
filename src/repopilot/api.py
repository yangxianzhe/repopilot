from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .agent import RepoPilotEngine
from .config import Settings, get_settings
from .models import ApprovalRequest, RunRecord, RunRequest, TERMINAL_STATUSES
from .security import SecurityError


DASHBOARD = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>RepoPilot</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    body { margin: 0; background: #0b1020; color: #e5e7eb; }
    main { max-width: 980px; margin: 48px auto; padding: 0 24px; }
    .hero, .card { background: #111831; border: 1px solid #263154; border-radius: 18px; padding: 24px; }
    .hero { background: radial-gradient(circle at top right, #1b5f5a, #111831 55%); }
    h1 { margin: 0 0 8px; font-size: 42px; } p { color: #aeb8d4; }
    code { color: #68e0cf; } a { color: #7dd3fc; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); gap: 16px; margin-top: 18px; }
    .badge { display: inline-block; padding: 5px 10px; border: 1px solid #3b4c75; border-radius: 999px; color: #68e0cf; }
  </style>
</head>
<body><main>
  <section class="hero"><span class="badge">Repository engineering agent</span>
    <h1>RepoPilot</h1><p>Inspect → propose diff → approve → sandbox tests → audit.</p>
    <p>Interactive API documentation: <a href="/docs">/docs</a></p>
  </section>
  <section class="grid">
    <div class="card"><h3>Safe by construction</h3><p>Repository boundaries, path validation, command allow-list and explicit patch approval.</p></div>
    <div class="card"><h3>Observable</h3><p>SQLite run records plus event streams at <code>/api/runs/{id}/events/stream</code>.</p></div>
    <div class="card"><h3>Measurable</h3><p>Offline evaluation tracks success, latency, tool calls, tests and changed files.</p></div>
  </section>
</main></body></html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = RepoPilotEngine(runtime_settings)
        yield

    app = FastAPI(
        title="RepoPilot API",
        version="0.1.0",
        description="Auditable repository-level coding agent with human patch approval.",
        lifespan=lifespan,
    )

    def engine(request: Request) -> RepoPilotEngine:
        return request.app.state.engine

    @app.exception_handler(KeyError)
    async def key_error_handler(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": str(exc).strip("'")})

    @app.exception_handler(SecurityError)
    async def security_error_handler(_: Request, exc: SecurityError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:
        return DASHBOARD

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "repopilot"}

    @app.post("/api/runs", response_model=RunRecord, status_code=status.HTTP_202_ACCEPTED)
    async def create_run(payload: RunRequest, request: Request) -> RunRecord:
        try:
            run = engine(request).create_run(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        engine(request).start_background(run.id)
        return run

    @app.get("/api/runs", response_model=list[RunRecord])
    async def list_runs(request: Request, limit: int = Query(50, ge=1, le=500)):
        return engine(request).list_runs(limit)

    @app.get("/api/runs/{run_id}", response_model=RunRecord)
    async def get_run(run_id: str, request: Request):
        return engine(request).get_run(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after: int = Query(0, ge=0)):
        return engine(request).events(run_id, after)

    @app.post("/api/runs/{run_id}/approval", response_model=RunRecord)
    async def approve_run(run_id: str, payload: ApprovalRequest, request: Request):
        try:
            return await engine(request).approve_run(run_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/events/stream")
    async def stream_events(run_id: str, request: Request):
        runtime = engine(request)
        runtime.get_run(run_id)

        async def event_source() -> AsyncIterator[str]:
            cursor = 0
            while True:
                events = runtime.events(run_id, cursor)
                for event in events:
                    cursor = event.sequence
                    yield f"data: {event.model_dump_json()}\n\n"
                run = runtime.get_run(run_id)
                if run.status in TERMINAL_STATUSES:
                    yield f"event: end\ndata: {json.dumps({'status': run.status.value})}\n\n"
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(event_source(), media_type="text/event-stream")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("repopilot.api:app", host="0.0.0.0", port=8000)
