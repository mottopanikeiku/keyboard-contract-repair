"""Execute model-proposed keyboard macros; only the Python oracle supplies verdicts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .contracts import BrowserAction, GateResult
from .learning_contracts import MemoryEntry, ProbePlan, ProbeReport, probe_digest
from .oracle import INITIAL_NAME, TaskBrowser

_ACTION_LIMIT = 40
_NAVIGATION_LIMIT = 12
_QUIET_MS = 60_000
_GATE_NAMES = (
    "navigation",
    "editing.no_write",
    "persistence.requests",
    "persistence.stored_name",
    "runtime",
)


def _expected_names(plan: ProbePlan) -> list[str]:
    names: list[str] = []
    current = INITIAL_NAME
    for step in plan.steps:
        if step.kind == "edit":
            current = step.value
        elif step.kind == "save":
            names.append(current)
    return names


def _trace_evidence(
    plan: ProbePlan, observations: list[dict[str, Any]], *, edit_only: bool = False
) -> dict[str, Any]:
    """Check the complete macro expansion, not assertions or labels from a report."""
    if len(observations) < 2 or observations[0].get("kind") != "initial":
        raise ValueError("Missing initial browser observation")
    replay_id = observations[0]["replay_id"]
    if UUID(replay_id).hex != replay_id:
        raise ValueError("Invalid replay identity")
    previous: list[dict[str, Any]] = []
    runtime_errors: list[str] = []
    for observation in observations:
        snapshot = observation["snapshot"]
        persistence = snapshot["persistence"]
        requests = persistence["requests"]
        if (
            persistence["synthetic"] is not True
            or type(persistence["request_count"]) is not int
            or persistence["request_count"] != len(requests)
            or requests[: len(previous)] != previous
        ):
            raise ValueError("Inconsistent Python request ledger")
        stored = INITIAL_NAME
        for request in requests:
            if set(request) != {"method", "payload", "accepted"} or request["method"] != "POST":
                raise ValueError("Invalid ledger request")
            payload = request["payload"]
            accepted = (
                isinstance(payload, dict)
                and set(payload) == {"display_name"}
                and isinstance(payload["display_name"], str)
                and 0 < len(payload["display_name"]) <= 80
            )
            if request["accepted"] is not accepted:
                raise ValueError("Inconsistent request acceptance")
            if accepted:
                stored = payload["display_name"]
        if persistence["display_name"] != stored:
            raise ValueError("Stored value contradicts the Python ledger")
        if not isinstance(snapshot["errors"], list):
            raise ValueError("Invalid runtime evidence")
        runtime_errors.extend(snapshot["errors"])
        runtime_errors.extend(f"Blocked request: {url}" for url in snapshot.get("blocked", []))
        previous = requests

    index = 1
    action_count = 0
    current = observations[0]["snapshot"]
    before_activation = list(current["persistence"]["requests"])
    activated = False
    intended_name = INITIAL_NAME
    navigation: dict[str, Any] = {"completed": True}

    def consume(kind: str, macro_index: int) -> dict[str, Any]:
        nonlocal index, current, before_activation
        observation = observations[index]
        if observation.get("kind") != kind or observation.get("macro_index") != macro_index:
            raise ValueError("Trace does not expand the supplied probe")
        index += 1
        current = observation["snapshot"]
        if not activated:
            before_activation = list(current["persistence"]["requests"])
        return observation

    def keyboard(
        kind: str, value: str, macro_index: int, activation_name: str | None = None
    ) -> None:
        nonlocal action_count
        observation = consume("keyboard", macro_index)
        action = observation["snapshot"]["action"]
        action_count += 1
        if (
            action_count > _ACTION_LIMIT
            or observation["snapshot"]["step"] != action_count
            or action["kind"] != kind
            or action["value"] != value
            or observation.get("activation_name") != activation_name
        ):
            raise ValueError("Unexpected expanded keyboard action")

    for macro_index, step in enumerate(plan.steps):
        if edit_only and step.kind == "save":
            continue
        if step.kind == "wait":
            observation = consume("wait", macro_index)
            if observation.get("virtual_duration_ms") != int(step.value):
                raise ValueError("Explicit wait differs from the probe")
            continue
        target = "display-name" if step.kind == "edit" else "save-name"
        tabs = 0
        while (
            current["focus"]["id"] != target
            and tabs < _NAVIGATION_LIMIT
            and action_count < _ACTION_LIMIT
        ):
            keyboard("press", "Tab", macro_index)
            tabs += 1
        needed = 2 if step.kind == "edit" else 1
        failed = current["focus"]["id"] != target or action_count + needed > _ACTION_LIMIT
        if not failed and step.kind == "edit":
            keyboard("press", "Control+A", macro_index)
            keyboard("type", step.value, macro_index)
            failed = current["focus"]["id"] != target or current["focus"]["value"] != step.value
            intended_name = step.value
        elif not failed:
            activated = True
            keyboard("press", step.value, macro_index, activation_name=intended_name)
        if failed:
            observation = consume("navigation_failed", macro_index)
            if observation.get("target") != target:
                raise ValueError("Navigation failure has the wrong target")
            navigation = {
                "completed": False,
                "macro_index": macro_index,
                "target": target,
                "focus": current["focus"],
            }
            break

    final = observations[index]
    if (
        index != len(observations) - 1
        or final.get("kind") != "quiet_window"
        or final.get("virtual_duration_ms") != _QUIET_MS
    ):
        raise ValueError("Missing final bounded quiet window")
    if final["snapshot"].get("action") is not None:
        raise ValueError("Final observation is not a settled snapshot")
    editing_requests: list[dict[str, Any]] = []
    if not edit_only:
        control = _trace_evidence(plan, observations[0]["edit_only_observations"], edit_only=True)
        if control["replay_id"] == replay_id:
            raise ValueError("Edit-only control requires a fresh browser session")
        editing_requests = control["requests"]
        runtime_errors.extend(control["runtime_errors"])
        if not control["navigation"]["completed"]:
            navigation = {"completed": False, "edit_only_control": control["navigation"]}
    return {
        "replay_id": replay_id,
        "navigation": navigation,
        "before_activation": before_activation,
        "editing_requests": editing_requests,
        "requests": final["snapshot"]["persistence"]["requests"],
        "stored_name": final["snapshot"]["persistence"]["display_name"],
        "runtime_errors": list(dict.fromkeys(runtime_errors)),
    }


def _gates(plan: ProbePlan, evidence: dict[str, Any], errors: list[str]) -> list[GateResult]:
    names = _expected_names(plan)
    navigated = evidence["navigation"]["completed"]
    requests = [
        {"method": "POST", "payload": {"display_name": name}, "accepted": True} for name in names
    ]
    values = [
        ("navigation", {"completed": True}, evidence["navigation"]),
        (
            "editing.no_write",
            {"before_activation": [], "edit_only_control": []},
            {
                "before_activation": evidence["before_activation"],
                "edit_only_control": evidence["editing_requests"],
            },
        ),
        ("persistence.requests", requests, evidence["requests"]),
        ("persistence.stored_name", names[-1], evidence["stored_name"]),
        ("runtime", [], list(dict.fromkeys(errors + evidence["runtime_errors"]))),
    ]
    gates = []
    for name, expected, actual in values:
        skipped = not navigated and name in _GATE_NAMES[1:4]
        gates.append(
            GateResult(
                name=name,
                passed=True if skipped else expected == actual,
                expected=None if skipped else expected,
                actual=None if skipped else actual,
                detail="Not evaluated: keyboard macro navigation failed" if skipped else "",
            )
        )
    return gates


async def _execute_trace(
    browser: TaskBrowser,
    plan: ProbePlan,
    observations: list[dict[str, Any]],
    *,
    replay_id: str,
    edit_only: bool = False,
) -> None:
    action_count = 0
    intended_name = INITIAL_NAME

    def record(kind: str, snapshot: dict[str, Any], **metadata: Any) -> None:
        observations.append({"kind": kind, **metadata, "snapshot": copy.deepcopy(snapshot)})

    snapshot = await browser.observe()
    record("initial", snapshot, replay_id=replay_id)

    async def keyboard(
        kind: str, value: str, macro_index: int, activation_name: str | None = None
    ) -> None:
        nonlocal snapshot, action_count
        snapshot = await browser.act(BrowserAction(kind=kind, value=value))
        action_count += 1
        record("keyboard", snapshot, macro_index=macro_index, activation_name=activation_name)

    for macro_index, step in enumerate(plan.steps):
        if edit_only and step.kind == "save":
            continue
        if step.kind == "wait":
            snapshot = await browser.settle(int(step.value))
            record("wait", snapshot, macro_index=macro_index, virtual_duration_ms=int(step.value))
            continue
        target = "display-name" if step.kind == "edit" else "save-name"
        tabs = 0
        while (
            snapshot["focus"]["id"] != target
            and tabs < _NAVIGATION_LIMIT
            and action_count < _ACTION_LIMIT
        ):
            await keyboard("press", "Tab", macro_index)
            tabs += 1
        needed = 2 if step.kind == "edit" else 1
        failed = snapshot["focus"]["id"] != target or action_count + needed > _ACTION_LIMIT
        if not failed and step.kind == "edit":
            await keyboard("press", "Control+A", macro_index)
            await keyboard("type", step.value, macro_index)
            failed = snapshot["focus"]["id"] != target or snapshot["focus"]["value"] != step.value
            intended_name = step.value
        elif not failed:
            await keyboard("press", step.value, macro_index, activation_name=intended_name)
        if failed:
            record("navigation_failed", snapshot, macro_index=macro_index, target=target)
            break
    snapshot = await browser.settle(_QUIET_MS)
    record("quiet_window", snapshot, virtual_duration_ms=_QUIET_MS)


async def evaluate_probe(
    source: str, plan: ProbePlan, *, artifact_dir: Path | None = None
) -> ProbeReport:
    """Replay a bounded DSL in a fresh Chromium session with Python-owned expectations."""
    plan = ProbePlan.model_validate(plan.model_dump())
    started = time.perf_counter()
    observations: list[dict[str, Any]] = []
    errors: list[str] = []
    artifacts: list[str] = []
    replay_id = uuid4().hex
    try:
        async with asyncio.timeout(90):
            # Each replay has a distinct capture path, including confirmation replays.
            directory = artifact_dir / replay_id if artifact_dir is not None else None
            async with TaskBrowser(source, artifact_dir=directory) as browser:
                await _execute_trace(browser, plan, observations, replay_id=replay_id)
                capture = await browser.capture()
                if capture is not None:
                    artifacts.append(capture)
            # Do not insert a long wait into the generated sequence: an isolated
            # control omits activations but retains edits/waits, exposing autosaves
            # that could otherwise substitute for an ignored Save activation.
            control_observations: list[dict[str, Any]] = []
            observations[0]["edit_only_observations"] = control_observations
            async with TaskBrowser(source) as browser:
                await _execute_trace(
                    browser,
                    plan,
                    control_observations,
                    replay_id=uuid4().hex,
                    edit_only=True,
                )
    except Exception as exc:
        # CancelledError is a BaseException: cancellation escapes, after browser cleanup.
        errors.append(f"{type(exc).__name__}: {str(exc)[-1800:]}")

    try:
        evidence = _trace_evidence(plan, observations)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        if not errors:
            errors.append(f"Incomplete oracle evidence: {exc}")
        evidence = {
            "navigation": {"completed": False, "reason": "execution incomplete"},
            "before_activation": [],
            "editing_requests": [],
            "requests": [],
            "stored_name": INITIAL_NAME,
            "runtime_errors": [],
        }
        if observations:
            evidence["requests"] = observations[-1]["snapshot"]["persistence"]["requests"]
    errors = list(dict.fromkeys(errors + evidence["runtime_errors"]))
    gates = _gates(plan, evidence, errors)
    return ProbeReport(
        plan=plan,
        probe_hash=probe_digest(plan),
        source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        passed=all(gate.passed for gate in gates) and not errors,
        gates=gates,
        expected_names=_expected_names(plan),
        observed_requests=evidence["requests"],
        observations=observations,
        errors=errors,
        artifacts=artifacts,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )


def _validated_evidence(report: ProbeReport) -> dict[str, Any] | None:
    try:
        checked = ProbeReport.model_validate(report.model_dump())
        plan = checked.plan
        if checked.errors or checked.probe_hash != probe_digest(plan):
            return None
        if len(checked.source_hash) != 64 or any(
            c not in "0123456789abcdef" for c in checked.source_hash
        ):
            return None
        evidence = _trace_evidence(plan, checked.observations)
        gates = _gates(plan, evidence, [])
        if (
            evidence["runtime_errors"]
            or checked.expected_names != _expected_names(plan)
            or checked.observed_requests != evidence["requests"]
            or checked.gates != gates
            or checked.passed != all(gate.passed for gate in gates)
        ):
            return None
        return evidence
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def is_counterexample(report: ProbeReport) -> bool:
    """Only complete, internally consistent, error-free failing oracle evidence qualifies."""
    return _validated_evidence(report) is not None and any(not gate.passed for gate in report.gates)


def admit_counterexample(
    *,
    origin_run_id: str,
    case_id: str,
    source: str,
    plan: ProbePlan,
    witness: ProbeReport,
    confirmation: ProbeReport,
) -> MemoryEntry:
    """Admit matching failures from two distinct replays, never a model-authored assertion."""
    plan = ProbePlan.model_validate(plan.model_dump())
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    evidence = []
    for report in (witness, confirmation):
        checked = _validated_evidence(report)
        if (
            checked is None
            or not any(not gate.passed for gate in report.gates)
            or report.source_hash != source_hash
            or report.probe_hash != probe_digest(plan)
            or report.plan != plan
        ):
            raise ValueError("Memory requires source-bound, error-free failing probe evidence")
        evidence.append(checked)
    first, second = evidence
    if first["replay_id"] == second["replay_id"]:
        raise ValueError("Memory requires two independent browser replays")
    if witness.gates != confirmation.gates or {
        key: value for key, value in first.items() if key != "replay_id"
    } != {key: value for key, value in second.items() if key != "replay_id"}:
        raise ValueError("Independent replays did not reproduce the same failing evidence")
    return MemoryEntry(
        entry_id=uuid4().hex,
        origin_run_id=origin_run_id,
        discovered_case_id=case_id,
        probe_hash=probe_digest(plan),
        plan=plan,
        failing_source=source,
        witness=witness.model_copy(deep=True),
        confirmation=confirmation.model_copy(deep=True),
    )
