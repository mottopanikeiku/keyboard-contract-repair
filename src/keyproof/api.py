"""Loopback-only UI and evidence API; candidate pages never share its origin privileges."""

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

from keyproof.challenges import PresetId, challenge_catalog
from keyproof.contracts import Contract, RunConfig
from keyproof.report import render_report
from keyproof.service import RunService
from keyproof.storage import default_data_dir, verified_capture_path
from keyproof.telemetry import Telemetry

WEB_DIR = Path(__file__).parent / "web"


class ChallengeRequest(Contract):
    preset_id: PresetId
    require_weave: bool = True


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
                    return JSONResponse(
                        {"detail": "Cross-origin mutations are not allowed"}, status_code=403
                    )
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(
                    {"detail": "Cross-site mutations are not allowed"}, status_code=403
                )
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

    def get_comparison(request: Request, comparison_id: str):
        try:
            return service(request).comparison(comparison_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(404, "Comparison not found") from None

    def get_challenge(request: Request, challenge_id: str):
        try:
            return service(request).store.get_challenge(challenge_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(404, "Challenge not found") from None

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
                "openai_configured": bool(
                    os.environ.get("KEYPROOF_API_KEY") or os.environ.get("OPENAI_API_KEY")
                ),
            },
            "weave": current.telemetry.status.model_dump(mode="json"),
            "fixture": {"title": "Harbor workspace settings", "task": TASK_SPEC},
            "busy": current.busy,
        }

    @app.post("/api/telemetry/connect")
    async def connect_weave(request: Request):
        current = service(request)
        try:
            return (await current.reconnect()).model_dump(mode="json")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/runs")
    async def runs(request: Request):
        return {
            "runs": [
                record.model_dump(mode="json") for record in service(request).store.list_runs()
            ]
        }

    @app.post("/api/runs", status_code=202)
    async def start_run(config: RunConfig, request: Request):
        try:
            return {"run_id": service(request).submit(config)}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str, request: Request):
        return get_record(request, run_id).model_dump(mode="json")

    @app.get("/api/comparisons")
    async def comparisons(request: Request):
        return {"comparisons": service(request).store.list_comparisons()}

    @app.post("/api/comparisons", status_code=202)
    async def start_comparison(config: RunConfig, request: Request):
        try:
            result = service(request).submit_comparison(config)
            return {key: result[key] for key in ("comparison_id", "run_ids")}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/comparisons/{comparison_id}")
    async def comparison(comparison_id: str, request: Request):
        return get_comparison(request, comparison_id)

    @app.get("/api/comparisons/{comparison_id}/report", response_class=HTMLResponse)
    async def comparison_report(comparison_id: str, request: Request):
        result = get_comparison(request, comparison_id)
        captures = service(request).store.comparison_screenshots(result)
        return HTMLResponse(
            render_report(result, captures),
            headers={
                "Content-Disposition": f'attachment; filename="keyproof-{comparison_id}.html"'
            },
        )

    @app.get("/api/challenges")
    async def challenges(request: Request):
        fields = {"challenge_id", "preset_id", "status", "verdict", "created_at", "finished_at"}
        return {
            "presets": challenge_catalog(),
            "runs": [
                record.model_dump(mode="json", include=fields)
                for record in service(request).store.list_challenges()
            ],
        }

    @app.post("/api/challenges", status_code=202)
    async def start_challenge(config: ChallengeRequest, request: Request):
        try:
            return (
                service(request)
                .submit_challenge(
                    config.preset_id,
                    require_weave=config.require_weave,
                )
                .model_dump(mode="json")
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/challenges/{challenge_id}")
    async def challenge(challenge_id: str, request: Request):
        return get_challenge(request, challenge_id).model_dump(mode="json")

    @app.get("/api/challenges/{challenge_id}/preview/{phase}")
    async def challenge_preview(
        challenge_id: str,
        phase: Literal["development", "holdout"],
        request: Request,
    ):
        record = get_challenge(request, challenge_id)
        report = record.development_report if phase == "development" else record.holdout_report
        if record.frozen_at is None:
            raise HTTPException(409, "The candidate has not been frozen")
        path = verified_capture_path(
            report,
            record.candidate_source,
            service(request).store.challenge_dir(challenge_id),
            phase=phase,
        )
        if report is not None and report.source_hash == record.source_hash and path is not None:
            return FileResponse(path, media_type="image/png")
        raise HTTPException(409, "No source-matched evaluator capture is available")

    @app.get("/api/preview/{run_id}/{variant}")
    async def preview(run_id: str, variant: Literal["original", "final"], request: Request):
        record = get_record(request, run_id)
        report = record.initial_report if variant == "original" else record.final_report
        source = record.original_source if variant == "original" else record.final_source
        frozen = any(
            event.role == "controller" and event.kind == "frozen" for event in record.events
        )
        if report is None or (variant == "final" and not frozen):
            raise HTTPException(409, "No evaluated capture is available for this candidate")
        path = verified_capture_path(
            report,
            source,
            service(request).store.run_dir(run_id),
            phase="development",
        )
        if path is not None:
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
