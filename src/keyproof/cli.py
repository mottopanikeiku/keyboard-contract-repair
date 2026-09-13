"""CLI for real browser checks, bounded repair, and matched comparisons."""

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from keyproof.contracts import RunConfig
from keyproof.storage import default_data_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keyproof",
        description="Repair owned keyboard tasks; verify behavior independently. Not a WCAG certificate.",
        epilog=(
            "Linux with bubblewrap required. Setup: uv sync; uv run playwright install chromium; "
            "codex login; uv run wandb login. "
            "Set KEYPROOF_WEAVE_PROJECT=team/project to select a Weave project. "
            "API provider: KEYPROOF_API_KEY, KEYPROOF_BASE_URL, KEYPROOF_MODEL. "
            "No keys belong in source control."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Launch the local evidence dashboard")
    serve.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--data-dir", type=Path, default=default_data_dir())
    doctor = commands.add_parser("doctor", help="Launch Chromium and inspect model/W&B readiness")
    doctor.add_argument("--local-only", action="store_true", help="Do not attempt W&B connection")
    check = commands.add_parser("check", help="Execute the immutable oracle against source")
    check.add_argument("--source", type=Path, help="Behavior JavaScript; defaults to original fixture")
    check.add_argument("--phase", choices=["development", "holdout"], default="development")
    check.add_argument("--artifacts", type=Path)
    for name in ("run", "compare"):
        command = commands.add_parser(name, help="Run one repair" if name == "run" else "Run a matched team/single pair")
        command.add_argument("--mode", choices=["team", "single"], default="team")
        command.add_argument("--provider", choices=["codex", "openai"], default="codex")
        command.add_argument("--model", help="Explicit provider model ID (recommended for comparisons)")
        command.add_argument("--iterations", type=int, default=3)
        command.add_argument("--max-calls", type=int, default=12)
        command.add_argument("--max-input-tokens", type=int, default=180000)
        command.add_argument("--max-output-tokens", type=int, default=6000, help="Aggregate run output cap; Codex usage is checked after each call")
        command.add_argument("--local-only", action="store_true", help="Explicitly allow untraced local execution; not sponsor-integrated")
        command.add_argument("--data-dir", type=Path, default=default_data_dir())
        command.add_argument("--json", action="store_true", help="Print complete evidence JSON")
    return parser


def _summary(record) -> dict:
    def phase(report):
        if report is None:
            return None
        return {
            "passed": report.passed,
            "gates_passed": sum(gate.passed for gate in report.gates),
            "gates_total": len(report.gates),
            "failed_gates": [gate.name for gate in report.gates if not gate.passed],
            "errors": report.errors,
        }

    return {
        "run_id": record.run_id,
        "status": record.status,
        "mode": record.config.mode,
        "model": record.config.model,
        "initial": phase(record.initial_report),
        "final": phase(record.final_report),
        "holdout": phase(record.holdout_report),
        "iterations": len(record.iterations),
        "accepted_patches": sum(item.accepted for item in record.iterations),
        "usage": record.usage.model_dump(),
        "weave": record.weave.model_dump(),
        "errors": record.errors,
    }


async def _doctor(local_only: bool) -> int:
    from keyproof.oracle import evaluate_source, fixture_source
    from keyproof.telemetry import Telemetry

    telemetry = Telemetry()
    try:
        if not local_only:
            await telemetry.connect()
        report = await evaluate_source(fixture_source())
        result = {
            "browser": {"runtime_errors": report.errors, "axe_violations": report.axe_violations},
            "original_fixture": {
                "passed": report.passed,
                "failed_gates": [gate.name for gate in report.gates if not gate.passed],
                "note": "The original fixture intentionally has keyboard defects; this is not a repair result.",
            },
            "provider": {"codex_installed": shutil.which("codex") is not None, "authentication": "verified by an actual run, not binary presence"},
            "weave": telemetry.status.model_dump(),
        }
        print(json.dumps(result, indent=2))
        return 1 if report.errors or (not local_only and not telemetry.status.enabled) else 0
    finally:
        await telemetry.close()


async def _run(args) -> int:
    from keyproof.service import RunService
    from keyproof.telemetry import Telemetry

    telemetry = Telemetry()
    if not args.local_only:
        await telemetry.connect()
    service = RunService(args.data_dir, telemetry)
    config = RunConfig(
        mode=args.mode,
        provider=args.provider,
        model=args.model,
        max_iterations=args.iterations,
        max_model_calls=args.max_calls,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        require_weave=not args.local_only,
    )
    try:
        if args.command == "compare":
            comparison = service.submit_comparison(config)
            print(f"Started comparison {comparison['comparison_id']}", file=sys.stderr, flush=True)
            await service.wait()
            result = service.comparison(comparison["comparison_id"])
            records = [service.store.get_run(run_id) for run_id in result["run_ids"]]
            payload = result if args.json else {
                "comparison_id": result["comparison_id"],
                "status": result["status"],
                "protocol": result["protocol"],
                "errors": result.get("errors", []),
                "results": [_summary(record) for record in records],
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0 if result["status"] == "completed" else 1
        run_id = service.submit(config)
        print(f"Started run {run_id}", file=sys.stderr, flush=True)
        await service.wait()
        record = service.store.get_run(run_id)
        print(json.dumps(record.model_dump(mode="json") if args.json else _summary(record), indent=2, ensure_ascii=False))
        return 0 if (
            record.status == "completed" and record.holdout_report is not None
            and record.holdout_report.passed
        ) else 1
    finally:
        await service.close()


async def _check(args) -> int:
    from keyproof.oracle import evaluate_source, fixture_source

    source = args.source.read_text(encoding="utf-8") if args.source else fixture_source()
    report = await evaluate_source(source, phase=args.phase, artifact_dir=args.artifacts)
    print(report.model_dump_json(indent=2))
    return 0 if report.passed else 1


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "serve":
            import uvicorn

            from keyproof.api import create_app

            uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port)
            return
        if args.command == "doctor":
            code = asyncio.run(_doctor(args.local_only))
        elif args.command == "check":
            code = asyncio.run(_check(args))
        else:
            code = asyncio.run(_run(args))
    except (ValueError, RuntimeError) as exc:
        print(f"keyproof: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)
