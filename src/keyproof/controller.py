"""Own candidate state, independent verdicts and budgets outside model control."""

import asyncio
import difflib
from pathlib import Path
from typing import Callable
from uuid import uuid4

import weave

from .agents import audit_task, engineer_patch, single_patch
from .contracts import EvaluationReport, Iteration, RunConfig, RunEvent, RunRecord, SourcePatch, timestamp
from .oracle import TaskBrowser, evaluate_source, fixture_source
from .provider import BudgetExhausted, ModelClient

MAX_SOURCE_BYTES = 64 * 1024


class PatchRejected(ValueError):
    pass


def apply_patch(source: str, patch: SourcePatch) -> str:
    """Match all edits against the immutable input, then splice once in source order."""
    spans: list[tuple[int, int, str]] = []
    for edit in patch.edits:
        if not edit.before:
            raise PatchRejected("An edit has an empty before string.")
        start = source.find(edit.before)
        if start < 0:
            raise PatchRejected("An edit's before string does not match the current source.")
        # Search one character later, not str.count: overlapping occurrences are ambiguous too.
        if source.find(edit.before, start + 1) >= 0:
            raise PatchRejected("An edit's before string is ambiguous.")
        if edit.before == edit.after:
            raise PatchRejected("No-op edits are not allowed.")
        spans.append((start, start + len(edit.before), edit.after))
    if not spans:
        raise PatchRejected("A patch must contain an edit.")
    spans.sort(key=lambda span: span[0])
    cursor = 0
    parts: list[str] = []
    for start, end, replacement in spans:
        if start < cursor:
            raise PatchRejected("Edits overlap in the original source.")
        parts.extend((source[cursor:start], replacement))
        cursor = end
    parts.append(source[cursor:])
    candidate = "".join(parts)
    if candidate == source:
        raise PatchRejected("The patch has no net effect.")
    if len(candidate.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise PatchRejected("Candidate exceeds the 64 KiB editable-source limit.")
    return candidate


def acceptance(previous: EvaluationReport, candidate: EvaluationReport) -> tuple[bool, str]:
    if candidate.phase != "development" or previous.phase != "development":
        return False, "Only development evidence can drive repair decisions."
    if candidate.errors:
        return False, "Candidate evaluation raised errors; rolling back."
    old_names = [gate.name for gate in previous.gates]
    new_names = [gate.name for gate in candidate.gates]
    if len(set(new_names)) != len(new_names) or set(old_names) != set(new_names):
        return False, "Candidate did not preserve the complete development gate set."
    passing_before = {gate.name for gate in previous.gates if gate.passed}
    passing_after = {gate.name for gate in candidate.gates if gate.passed}
    regressions = passing_before - passing_after
    if regressions:
        return False, "Regressed passing gates: " + ", ".join(sorted(regressions))
    if set(candidate.axe_violations) - set(previous.axe_violations):
        return False, "Introduced new axe accessibility violations; rolling back."
    old_failures = sum(not gate.passed for gate in previous.gates)
    new_failures = sum(not gate.passed for gate in candidate.gates)
    if new_failures >= old_failures:
        return False, "No meaningful reduction in failing development gates; rolling back."
    return True, f"Accepted: failing development gates reduced from {old_failures} to {new_failures}, with no regressions."


def _write_artifact(root: Path | None, name: str, content: str) -> None:
    if root is None:
        return
    destination = root / name
    if not destination.resolve().is_relative_to(root):
        raise ValueError("Artifact destination escapes the run artifact directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _history(iterations: list[Iteration]) -> list[dict]:
    return [
        {"iteration": item.number, "patch": item.patch.model_dump() if item.patch else None,
         "decision": item.decision, "accepted": item.accepted,
         "development_report": item.report.model_dump() if item.report else None}
        for item in iterations
    ]


@weave.op(postprocess_inputs=lambda inputs: {key: value for key, value in inputs.items() if key != "on_update"})
async def run_repair(
    config: RunConfig,
    *,
    run_id: str | None = None,
    on_update: Callable[[RunRecord], None] | None = None,
    artifact_dir: Path | None = None,
    evaluate_holdout: bool = True,
) -> RunRecord:
    record = RunRecord(run_id=run_id or uuid4().hex, config=config, status="running")
    client = ModelClient(config)
    record.usage = client.usage
    root: Path | None = None
    cancelled = False
    sink = on_update

    def event(role: str, kind: str, summary: str, data: dict | None = None) -> None:
        nonlocal sink
        item = RunEvent(sequence=len(record.events) + 1, role=role, kind=kind,
                        summary=summary, data=data or {})
        record.events.append(item)
        if sink is not None:
            try:
                sink(record)
            except Exception as exc:
                record.errors.append(f"Progress event sink failed: {type(exc).__name__}.")
                sink = None

    try:
        if artifact_dir is not None:
            root = artifact_dir.resolve()
            root.mkdir(parents=True, exist_ok=True)
        event("controller", "started", f"Starting {config.mode} repair with independent keyboard gates.")
        event("provider", "identity", "Real model provider and aggregate run limits.", client.identity)
        original = fixture_source()
        record.original_source = original
        record.final_source = original
        _write_artifact(root, "original.js", original)
        record.initial_report = await evaluate_source(
            original, phase="development", artifact_dir=root / "initial" if root else None,
        )
        record.final_report = record.initial_report
        event("evaluator", "development", "Original-source development evaluation completed.",
              {"report": record.initial_report.model_dump()})
        if record.initial_report.errors:
            raise RuntimeError("Original-source development evaluation failed: " + "; ".join(record.initial_report.errors))

        for number in range(1, config.max_iterations + 1):
            assert record.final_report is not None
            if record.final_report.passed:
                break
            client.check_budget()
            item = Iteration(number=number)
            history = _history(record.iterations)
            record.iterations.append(item)
            event("controller", "iteration", f"Starting repair attempt {number}.", {"iteration": number})
            async with TaskBrowser(record.final_source, artifact_dir=root / f"audit-{number}" if root else None) as browser:
                if config.mode == "team":
                    event("auditor", "started", "Auditor selecting keyboard actions from the live page.")
                    item.audit = await audit_task(client, browser, record.final_report, event)
                    event("engineer", "started", "Repair engineer reviewing source and independent auditor evidence.")
                    item.patch = await engineer_patch(client, record.final_source, record.final_report, item.audit, history)
                else:
                    event("single", "started", "Single agent exploring and repairing with the same keyboard tools.")
                    item.patch, item.audit = await single_patch(
                        client, browser, record.final_source, record.final_report, history, event,
                    )
            event("provider", "usage", "Observed cumulative model usage.", client.usage.model_dump())
            event("engineer" if config.mode == "team" else "single", "patch", item.patch.summary,
                  {"iteration": number, "patch": item.patch.model_dump()})
            try:
                candidate = apply_patch(record.final_source, item.patch)
            except PatchRejected as exc:
                item.decision = str(exc)
                event("controller", "rejected", item.decision, {"iteration": number})
                _write_artifact(root, f"iteration-{number}/attempt.json", item.model_dump_json(indent=2))
                continue
            item.candidate_source = candidate
            item.diff = "".join(difflib.unified_diff(
                record.final_source.splitlines(keepends=True), candidate.splitlines(keepends=True),
                fromfile="accepted/behavior.js", tofile=f"candidate-{number}/behavior.js",
            ))
            _write_artifact(root, f"iteration-{number}/candidate.js", candidate)
            _write_artifact(root, f"iteration-{number}/change.diff", item.diff)
            item.report = await evaluate_source(
                candidate, phase="development", artifact_dir=root / f"iteration-{number}" if root else None,
            )
            item.accepted, item.decision = acceptance(record.final_report, item.report)
            if item.accepted:
                record.final_source = candidate
                record.final_report = item.report
            event("controller", "accepted" if item.accepted else "rejected", item.decision,
                  {"iteration": number, "diff": item.diff, "report": item.report.model_dump()})
            _write_artifact(root, f"iteration-{number}/attempt.json", item.model_dump_json(indent=2))

        record.status = "completed" if record.final_report and record.final_report.passed else "budget_exhausted"
        if record.status == "budget_exhausted":
            event("controller", "budget", "Iteration or model budget ended before all development gates passed.")
    except BudgetExhausted as exc:
        record.status = "budget_exhausted"
        event("controller", "budget", str(exc), {"usage": client.usage.model_dump()})
    except asyncio.CancelledError:
        cancelled = True
        record.status = "failed"
        record.errors.append("Run cancelled; any active model process group was terminated.")
        event("controller", "error", record.errors[-1])
    except Exception as exc:
        record.status = "failed"
        message = f"{type(exc).__name__}: {str(exc)[:1500]}"
        record.errors.append(message)
        event("controller", "error", message)

    # Freeze first. No agent call exists below this boundary; holdout is never repair feedback.
    try:
        if record.final_report is not None and not cancelled:
            event("controller", "frozen", "Final candidate frozen; no further model calls.",
                  {"development_passed": record.final_report.passed})
            _write_artifact(root, "final.js", record.final_source)
            if evaluate_holdout:
                record.holdout_report = await evaluate_source(
                    record.final_source, phase="holdout", artifact_dir=root / "holdout" if root else None,
                )
                event("evaluator", "holdout", "Independent frozen-candidate holdout evaluation completed.",
                      {"report": record.holdout_report.model_dump()})
                if record.holdout_report.errors:
                    record.status = "failed"
                    record.errors.extend(record.holdout_report.errors)
    except asyncio.CancelledError:
        cancelled = True
        record.status = "failed"
        record.errors.append("Run cancelled during frozen-candidate evaluation.")
    except Exception as exc:
        record.status = "failed"
        record.errors.append(f"Final evaluation/artifact failure: {type(exc).__name__}: {str(exc)[:1500]}")
    record.finished_at = timestamp()
    event("provider", "usage", "Final observed usage; null token counts mean unknown, not zero.", client.usage.model_dump())
    if record.errors:
        record.status = "failed"
    event("controller", "finished", f"Run {record.status}.", {
        "status": record.status, "development_passed": record.final_report.passed if record.final_report else None,
        "holdout_passed": record.holdout_report.passed if record.holdout_report else None,
        "errors": record.errors,
    })
    try:
        _write_artifact(root, "run.json", record.model_dump_json(indent=2))
    except Exception as exc:
        record.status = "failed"
        record.errors.append(f"Could not persist run artifact: {type(exc).__name__}.")
    if cancelled:
        raise asyncio.CancelledError
    return record
