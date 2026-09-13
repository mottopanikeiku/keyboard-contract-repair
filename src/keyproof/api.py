"""Loopback-only UI and evidence API; candidate pages never share its origin privileges."""

import hashlib
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from keyproof.contracts import RunConfig
from keyproof.service import RunService
from keyproof.storage import default_data_dir
from keyproof.telemetry import Telemetry

WEB_DIR = Path(__file__).parent / "web"


def create_app(data_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        telemetry = Telemetry()
        await telemetry.connect()
        service = RunService(data_dir or default_data_dir(), telemetry)
        app.state.service = service
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="Keyproof", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.netloc != request.url.netloc or parsed.scheme != request.url.scheme:
                    return JSONResponse({"detail": "Cross-origin mutations are not allowed"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site mutations are not allowed"}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
            "connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        return response

    def service(request: Request) -> RunService:
        return request.app.state.service

    def get_record(request: Request, run_id: str):
        try:
            return service(request).store.get_run(run_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(404, "Run not found") from None

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/status")
    async def status(request: Request):
        from keyproof.oracle import TASK_SPEC

        current = service(request)
        return {
            "provider": {
                "codex_available": shutil.which("codex") is not None,
                "openai_configured": bool(os.environ.get("KEYPROOF_API_KEY") or os.environ.get("OPENAI_API_KEY")),
            },
            "weave": current.telemetry.status.model_dump(mode="json"),
            "fixture": {"title": "Harbor workspace settings", "task": TASK_SPEC},
            "busy": current.busy,
        }

    @app.post("/api/telemetry/connect")
    async def connect_weave(request: Request):
        current = service(request)
        if current.busy:
            raise HTTPException(409, "Connect W&B between runs, not while a candidate is changing.")
        return (await current.telemetry.connect()).model_dump(mode="json")

    @app.get("/api/runs")
    async def runs(request: Request):
        return {"runs": [record.model_dump(mode="json") for record in service(request).store.list_runs()]}

    @app.post("/api/runs", status_code=202)
    async def start_run(config: RunConfig, request: Request):
        try:
            return {"run_id": service(request).submit(config)}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str, request: Request):
        return get_record(request, run_id).model_dump(mode="json")

    @app.post("/api/comparisons", status_code=202)
    async def start_comparison(config: RunConfig, request: Request):
        try:
            result = service(request).submit_comparison(config)
            return {key: result[key] for key in ("comparison_id", "run_ids")}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/comparisons/{comparison_id}")
    async def comparison(comparison_id: str, request: Request):
        try:
            return service(request).comparison(comparison_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(404, "Comparison not found") from None

    @app.get("/api/preview/{run_id}/{variant}")
    async def preview(run_id: str, variant: Literal["original", "final"], request: Request):
        record = get_record(request, run_id)
        report = record.initial_report if variant == "original" else record.final_report
        source = record.original_source if variant == "original" else record.final_source
        frozen = any(event.role == "controller" and event.kind == "frozen" for event in record.events)
        if report is None or (variant == "final" and not frozen):
            raise HTTPException(409, "No evaluated capture is available for this candidate")
        if hashlib.sha256(source.encode()).hexdigest() != report.source_hash:
            raise HTTPException(409, "Capture does not correspond to the recorded source")
        root = service(request).store.run_dir(run_id).resolve()
        for artifact in report.artifacts:
            path = Path(artifact).resolve()
            if (
                path.is_relative_to(root) and path.name.endswith("-keyboard.png")
                and path.is_file()
            ):
                return FileResponse(path, media_type="image/png")
        raise HTTPException(409, "The evaluator has not retained a keyboard capture")

    @app.get("/api/artifacts/{run_id}/{filename:path}")
    async def artifact(run_id: str, filename: str, request: Request):
        get_record(request, run_id)
        root = service(request).store.run_dir(run_id).resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "Artifact not found")
        if path.suffix == ".png":
            return FileResponse(path, media_type="image/png")
        if path.suffix not in {".json", ".js", ".txt", ".diff"}:
            raise HTTPException(404, "Artifact is not published")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
