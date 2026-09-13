"""Conservative, pure assessment of a recorded resource-matched experiment."""

import hashlib
import math
from datetime import datetime
from typing import Any

CAVEAT = (
    "One paired experiment is not a statistical benchmark or evidence of general team "
    "superiority. Gate counts are diagnostics, not a quality ranking. This owned synthetic "
    "keyboard fixture is not WCAG certification or validation by disabled users."
)
_LIMITS = ("max_iterations", "max_model_calls", "max_input_tokens", "max_output_tokens")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _duration(value: Any) -> bool:
    return _count(value) or (type(value) is float and math.isfinite(value) and value >= 0)


def _report_names(report: Any, source: str, phase: str) -> set[str] | None:
    if not isinstance(report, dict) or report.get("errors") != []:
        return None
    if (
        report.get("phase") != phase
        or report.get("source_hash") != hashlib.sha256(source.encode()).hexdigest()
    ):
        return None
    gates = report.get("gates")
    if not isinstance(gates, list) or not gates or not _duration(report.get("elapsed_ms")):
        return None
    names = set()
    for gate in gates:
        if not isinstance(gate, dict) or type(gate.get("passed")) is not bool:
            return None
        name = gate.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            return None
        names.add(name)
    if type(report.get("passed")) is not bool or report["passed"] != all(
        gate["passed"] for gate in gates
    ):
        return None
    return names


def assess_comparison(comparison: dict) -> dict:
    """Return an outcome only when the recorded evidence supports a fair full-contract pair.

    Hashes bind report snapshots, not authorship: this is an integrity check of trusted
    controller records, not a cryptographic attestation of an arbitrary uploaded JSON file.
    """

    def result(verdict: str, reason: str) -> dict:
        return {
            "comparable": verdict != "unavailable",
            "verdict": verdict,
            "reason": reason,
            "caveat": CAVEAT,
        }

    def unavailable(reason: str) -> dict:
        return result("unavailable", reason)

    if (
        not isinstance(comparison, dict)
        or comparison.get("status") != "completed"
        or comparison.get("errors") != []
    ):
        return unavailable(
            "Comparison is incomplete or contains execution errors; no quality outcome is available."
        )
    runs, ids = comparison.get("results"), comparison.get("run_ids")
    if (
        not isinstance(runs, list)
        or len(runs) != 2
        or not all(isinstance(run, dict) for run in runs)
    ):
        return unavailable(
            "Exactly one complete team record and one complete single record are required."
        )
    if (
        not isinstance(ids, list)
        or len(ids) != 2
        or not all(isinstance(value, str) and value for value in ids)
        or ids[0] == ids[1]
    ):
        return unavailable("Comparison membership is missing or duplicated.")
    if any(not isinstance(run.get("run_id"), str) for run in runs) or {
        run["run_id"] for run in runs
    } != set(ids):
        return unavailable("The supplied run records do not match the comparison membership.")
    if any(not isinstance(run.get("config"), dict) for run in runs):
        return unavailable("Explicit matched run configurations are required.")
    if sorted(str(run["config"].get("mode")) for run in runs) != ["single", "team"]:
        return unavailable("Comparison must contain exactly one team run and one single run.")
    team = next(run for run in runs if run["config"]["mode"] == "team")
    single = next(run for run in runs if run["config"]["mode"] == "single")
    for run in runs:
        config = run["config"]
        if (
            not all(
                isinstance(config.get(key), str) and config[key].strip()
                for key in ("provider", "model")
            )
            or type(config.get("require_weave")) is not bool
            or not all(_count(config.get(key)) and config[key] > 0 for key in _LIMITS)
        ):
            return unavailable(
                "Provider, model, integration setting and positive configured ceilings must be explicit."
            )
    fields = ("provider", "model", "require_weave", *_LIMITS)
    if any(team["config"][key] != single["config"][key] for key in fields):
        return unavailable(
            "Provider, model, integration setting or configured resource ceilings differ."
        )
    if (
        not isinstance(team.get("original_source"), str)
        or not team["original_source"]
        or team["original_source"] != single.get("original_source")
    ):
        return unavailable("The pair did not start from the same known original source.")
    started, frozen, finished = (
        _time(comparison.get(key)) for key in ("started_at", "frozen_at", "finished_at")
    )
    if started is None or frozen is None or finished is None or not started <= frozen <= finished:
        return unavailable(
            "Comparison start, freeze and completion provenance is missing or inconsistent."
        )
    schemas = []
    for run in runs:
        if run.get("status") not in ("completed", "budget_exhausted") or run.get("errors") != []:
            return unavailable(
                "A run is interrupted, incomplete or contains execution errors; budget exhaustion alone is allowed."
            )
        weave = run.get("weave")
        if run["config"]["require_weave"] and (
            not isinstance(weave, dict)
            or weave.get("enabled") is not True
            or weave.get("error")
            or not isinstance(weave.get("evaluation_url"), str)
            or not weave["evaluation_url"].strip()
        ):
            return unavailable(
                "Required Weave evaluation delivery has not been verified for both runs."
            )
        usage = run.get("usage")
        if (
            not isinstance(usage, dict)
            or usage.get("complete") is not True
            or not all(_count(usage.get(key)) for key in ("calls", "input_tokens", "output_tokens"))
            or not _duration(usage.get("elapsed_ms"))
            or (
                usage.get("cached_input_tokens") is not None
                and not _count(usage["cached_input_tokens"])
            )
        ):
            return unavailable(
                "Actual model-call and token usage must be known, complete and finite; unknown is not zero."
            )
        if not isinstance(run.get("final_source"), str):
            return unavailable("A recorded final candidate source is required.")
        report_names = [
            _report_names(run.get("initial_report"), run["original_source"], "development"),
            _report_names(run.get("final_report"), run["final_source"], "development"),
            _report_names(run.get("holdout_report"), run["final_source"], "holdout"),
        ]
        if any(names is None for names in report_names):
            return unavailable(
                "Evaluation evidence has missing gates, errors, a phase/hash mismatch or an inconsistent pass claim."
            )
        if report_names[0] != report_names[1]:
            return unavailable(
                "Original and final development reports do not cover compatible contract gates."
            )
        schemas.append(report_names)
        events = run.get("events")
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            return unavailable("Controller freeze provenance is missing.")
        freeze_events = [
            event
            for event in events
            if event.get("role") == "controller" and event.get("kind") == "frozen"
        ]
        run_start, run_end = _time(run.get("started_at")), _time(run.get("finished_at"))
        if len(freeze_events) != 1 or run_start is None or run_end is None:
            return unavailable(
                "Each terminal run needs exactly one recorded controller freeze and valid timestamps."
            )
        event = freeze_events[0]
        freeze_time = _time(event.get("timestamp"))
        if (
            freeze_time is None
            or not run_start <= freeze_time <= run_end <= finished
            or freeze_time > frozen
        ):
            return unavailable(
                "Freeze timing does not establish both candidates were sealed before paired holdout."
            )
        freeze_data = event.get("data", {})
        if not isinstance(freeze_data, dict):
            return unavailable("Controller freeze metadata is invalid.")
        if (
            "development_passed" in freeze_data
            and freeze_data["development_passed"] is not run["final_report"]["passed"]
            or "source_hash" in freeze_data
            and freeze_data["source_hash"] != run["final_report"]["source_hash"]
        ):
            return unavailable(
                "Controller freeze metadata does not match the final evaluated source."
            )
        sequence = event.get("sequence")
        if not _count(sequence) or sequence < 1:
            return unavailable("Controller freeze event sequence is invalid.")
        previous = 0
        for item in events:
            position = item.get("sequence")
            if not _count(position) or position <= previous:
                return unavailable("Run event ordering is invalid.")
            previous = position
            if item.get("kind") in ("error", "interrupted", "cancelled"):
                return unavailable(
                    "Run events retain execution errors or interruption despite the terminal status."
                )
            data = item.get("data")
            if (
                isinstance(data, dict)
                and isinstance(data.get("report"), dict)
                and data["report"].get("errors")
            ):
                return unavailable(
                    "An evaluator execution error remains in the recorded run events."
                )
            if position > sequence and (
                item.get("role") in ("auditor", "engineer", "single")
                or item.get("kind") in ("patch", "accepted", "rejected", "iteration")
            ):
                return unavailable(
                    "Candidate-changing activity was recorded after the controller freeze."
                )
            if item.get("kind") == "holdout" and (
                position <= sequence or (_time(item.get("timestamp")) or run_start) < frozen
            ):
                return unavailable("Holdout evidence was exposed before both candidates froze.")
    if schemas[0] != schemas[1]:
        return unavailable("The pair was evaluated against incompatible contract gate sets.")
    team_pass = team["final_report"]["passed"] and team["holdout_report"]["passed"]
    single_pass = single["final_report"]["passed"] and single["holdout_report"]["passed"]
    if team_pass and single_pass:
        return result(
            "tie",
            "Both frozen candidates pass every development and holdout gate. Lower spend is not a correctness win.",
        )
    if team_pass or single_pass:
        mode = "team" if team_pass else "single"
        return result(
            mode,
            f"Only the {mode} candidate passes every development and holdout gate in this pair.",
        )
    return result(
        "neither",
        "Neither frozen candidate passes both full contracts. Small gate-count differences do not establish a quality winner.",
    )
