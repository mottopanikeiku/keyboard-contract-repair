"""Curated adversarial samples evaluated against an immutable owned fixture candidate."""

import asyncio
import difflib
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

import weave
from pydantic import Field

from .contracts import Contract, EvaluationReport, RunEvent, WeaveStatus, timestamp
from .oracle import evaluate_source, fixture_source
from .telemetry import redact, trace_agent_inputs

PresetId = Literal["original", "reference", "enter_only", "late_duplicate", "forged_axe"]
_ORIGIN = "curated_adversarial_sample"
_MAX_SOURCE_BYTES = 64 * 1024
_PRESETS = (
    (
        "original",
        "Original broken behavior",
        "The untouched owned fixture: keyboard save and dialog focus return are broken.",
    ),
    (
        "reference",
        "Minimal native repair",
        "One native click handler supports pointer, Enter and Space; closing the dialog restores opener focus.",
    ),
    (
        "enter_only",
        "Enter-only overfit",
        "Repairs focus and adds Enter-only saving. Development uses Enter; the independent holdout also requires Space.",
    ),
    (
        "late_duplicate",
        "Delayed duplicate write",
        "The native repair adds a second save after 30 virtual seconds. The evaluator observes the quiet window, not just immediate success.",
    ),
    (
        "forged_axe",
        "Forged axe with delayed regression",
        "The native repair installs a non-configurable fake window.axe and weakens real text contrast after 30 virtual seconds. Trusted checks run in an isolated world.",
    ),
)


class ChallengeRun(Contract):
    challenge_id: str
    preset_id: PresetId
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    verdict: Literal["not_evaluated", "accepted", "rejected", "error"] = "not_evaluated"
    origin: Literal["curated_adversarial_sample"] = _ORIGIN
    created_at: str = Field(default_factory=timestamp)
    frozen_at: str | None = None
    finished_at: str | None = None
    original_source: str
    candidate_source: str
    source_hash: str
    source_diff: str
    development_report: EvaluationReport | None = None
    holdout_report: EvaluationReport | None = None
    errors: list[str] = Field(default_factory=list)
    events: list[RunEvent] = Field(default_factory=list)
    weave: WeaveStatus = Field(default_factory=WeaveStatus)


def challenge_catalog() -> list[dict]:
    """Return descriptions, never precomputed outcomes or benchmark claims."""
    return [
        {"preset_id": preset_id, "title": title, "description": description, "origin": _ORIGIN}
        for preset_id, title, description in _PRESETS
    ]


def _replace_once(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise ValueError("Owned fixture changed; curated challenge construction is unavailable.")
    return source.replace(before, after, 1)


def _candidate(preset_id: str, original: str) -> str:
    if preset_id not in {item[0] for item in _PRESETS}:
        raise ValueError(f"Unknown challenge preset: {preset_id}")
    if preset_id == "original":
        candidate = original
    else:
        focused = _replace_once(original, "  nameField.focus();", "  notificationButton.focus();")
        if preset_id == "enter_only":
            candidate = (
                focused
                + """
saveButton.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') window.harbor.saveDisplayName();
});
"""
            )
        else:
            candidate = _replace_once(
                focused,
                """saveButton.addEventListener('click', async (event) => {
  if (event.detail > 0) {
    await window.harbor.saveDisplayName();
  }
});""",
                """saveButton.addEventListener('click', async () => {
  await window.harbor.saveDisplayName();
});""",
            )
            if preset_id == "late_duplicate":
                candidate += """
saveButton.addEventListener('click', () => {
  setTimeout(() => window.harbor.saveDisplayName(), 30000);
});
"""
            elif preset_id == "forged_axe":
                candidate += """
const fakeAxe = { run: async () => ({ violations: [], passes: [1], incomplete: [] }) };
Object.defineProperty(window, 'axe', {
  get: () => fakeAxe,
  set: () => {},
  configurable: false,
});
setTimeout(() => {
  document.querySelector('#name-hint').style.color = '#999';
}, 30000);
"""
    if len(candidate.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("Curated candidate exceeds the 64 KiB editable-source limit.")
    return candidate


def _digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _diff(original: str, candidate: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile="original/behavior.js",
            tofile="candidate/behavior.js",
        )
    )


def make_challenge(preset_id: str, weave: WeaveStatus) -> ChallengeRun:
    original = fixture_source()
    candidate = _candidate(preset_id, original)
    return ChallengeRun(
        challenge_id=uuid4().hex,
        preset_id=preset_id,
        original_source=original,
        candidate_source=candidate,
        source_hash=_digest(candidate),
        source_diff=_diff(original, candidate),
        weave=weave.model_copy(deep=True),
    )


def _check_cancelled() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


@weave.op(postprocess_inputs=trace_agent_inputs)
async def run_challenge(
    record: ChallengeRun,
    *,
    artifact_dir: Path,
    on_update: Callable[[ChallengeRun], None] | None = None,
) -> ChallengeRun:
    """Freeze once, run both real oracle phases, and never repair from feedback.

    Weave delivery is owned by the service; an op invocation alone proves no cloud
    connection. Cancellation finalizes local evidence and is always re-raised.
    """
    if record.status != "queued":
        raise ValueError("Only a queued challenge can execute.")
    root: Path | None = None
    sink = on_update
    cancelled = False

    def event(role: str, kind: str, summary: str, data: dict | None = None) -> None:
        nonlocal sink
        record.events.append(
            RunEvent(
                sequence=len(record.events) + 1,
                role=role,
                kind=kind,
                summary=redact(summary),
                data=redact(data or {}),
            )
        )
        if sink is not None:
            try:
                sink(record)
            except asyncio.CancelledError:
                sink = None
                raise
            except Exception as exc:
                sink = None
                record.errors.append(f"Progress event sink failed: {type(exc).__name__}.")
                record.status = "failed"
                record.verdict = "error"
                record.events.append(
                    RunEvent(
                        sequence=len(record.events) + 1,
                        role="challenge",
                        kind="error",
                        summary=record.errors[-1],
                    )
                )

    def artifact(name: str, content: str) -> None:
        if root is None:
            return
        destination = root / name
        if not destination.resolve().is_relative_to(root):
            raise ValueError("Artifact destination escapes the challenge artifact directory.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    try:
        _check_cancelled()
        original = fixture_source()
        candidate = _candidate(record.preset_id, original)
        digest = _digest(candidate)
        source_diff = _diff(original, candidate)
        if (
            record.original_source != original
            or record.candidate_source != candidate
            or record.source_hash != digest
            or record.source_diff != source_diff
        ):
            raise ValueError("Challenge source must match its curated preset exactly.")
        if (
            record.frozen_at is not None
            or record.finished_at is not None
            or record.development_report is not None
            or record.holdout_report is not None
            or record.verdict != "not_evaluated"
        ):
            raise ValueError("Queued challenge already contains evaluation state.")
        root = artifact_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        record.status = "running"
        event(
            "challenge",
            "started",
            "Starting a curated sample, not a model-generated repair or statistical benchmark.",
            {"preset_id": record.preset_id, "origin": record.origin},
        )
        _check_cancelled()
        record.frozen_at = timestamp()
        artifact("original.js", original)
        artifact("candidate.js", candidate)
        artifact("change.diff", source_diff)
        artifact("frozen.json", record.model_dump_json(indent=2))
        event(
            "challenge",
            "frozen",
            "Candidate frozen before development and holdout; no feedback-driven edits.",
            {"source_hash": digest, "frozen_at": record.frozen_at},
        )

        for phase in ("development", "holdout"):
            _check_cancelled()
            event(
                "evaluator",
                phase + "_started",
                f"Running real {phase} evaluation on the frozen candidate.",
                {"source_hash": digest},
            )
            _check_cancelled()
            report = await evaluate_source(candidate, phase=phase, artifact_dir=root / phase)
            if phase == "development":
                record.development_report = report
            else:
                record.holdout_report = report
            _check_cancelled()
            record.errors.extend(redact(report.errors))
            if report.phase != phase or report.source_hash != digest:
                record.errors.append(
                    f"{phase} report does not match the frozen candidate and phase."
                )
            if not report.gates:
                record.errors.append(f"{phase} evaluation returned no gates.")
            if report.passed != (
                bool(report.gates)
                and all(gate.passed for gate in report.gates)
                and not report.errors
            ):
                record.errors.append(f"{phase} report has an inconsistent verdict.")
            artifact(f"{phase}/report.json", report.model_dump_json(indent=2))
            event(
                "evaluator",
                phase,
                f"Frozen-candidate {phase} evaluation completed.",
                {"report": report.model_dump(mode="json")},
            )

        _check_cancelled()
        if (
            record.candidate_source != candidate
            or record.source_hash != digest
            or record.original_source != original
            or record.source_diff != source_diff
        ):
            raise ValueError("Frozen challenge source metadata changed during evaluation.")
        if record.errors:
            record.status = "failed"
            record.verdict = "error"
        else:
            record.status = "completed"
            record.verdict = (
                "accepted"
                if (record.development_report.passed and record.holdout_report.passed)
                else "rejected"
            )
    except asyncio.CancelledError:
        cancelled = True
        record.status = "failed"
        record.verdict = "error"
        record.errors.append("Challenge cancelled; no further evaluation was started.")
    except Exception as exc:
        record.status = "failed"
        record.verdict = "error"
        record.errors.append(redact(f"{type(exc).__name__}: {str(exc)[:1500]}"))

    # A teardown error must not hide pending cancellation or start another phase.
    task = asyncio.current_task()
    if task is not None and task.cancelling() and not cancelled:
        cancelled = True
        record.status = "failed"
        record.verdict = "error"
        record.errors.append("Cancellation requested; teardown errors cannot restart evaluation.")
    record.finished_at = timestamp()
    try:
        event(
            "challenge",
            "finished",
            f"Challenge {record.status}: {record.verdict}.",
            {"status": record.status, "verdict": record.verdict, "errors": record.errors},
        )
        if not cancelled:
            _check_cancelled()
    except asyncio.CancelledError:
        cancelled = True
        record.status = "failed"
        record.verdict = "error"
        record.errors.append("Challenge cancelled during final notification.")
    try:
        artifact("challenge.json", record.model_dump_json(indent=2))
    except Exception as exc:
        record.status = "failed"
        record.verdict = "error"
        record.errors.append(
            redact(f"Could not persist challenge artifact: {type(exc).__name__}: {str(exc)[:1500]}")
        )
        try:
            event("challenge", "error", record.errors[-1])
            _check_cancelled()
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return record
