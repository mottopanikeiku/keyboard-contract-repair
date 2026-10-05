"""Replay published candidate sources without invoking a model or cloud tracing."""

import json
from pathlib import Path

import pytest

from keyproof.learning_contracts import ProbePlan, ProbeStep
from keyproof.oracle import evaluate_source
from keyproof.probes import evaluate_probe

COMPARISON = json.loads(
    (Path(__file__).parents[1] / "evidence" / "first-local-comparison.json").read_text(
        encoding="utf-8"
    )
)
RUNS = COMPARISON["runs"]


async def test_recorded_original_reproduces_development_failures():
    report = await evaluate_source(COMPARISON["original_source"])
    recorded = RUNS[0]["initial"]
    assert report.errors == []
    assert report.source_hash == recorded["source_hash"]
    assert sum(gate.passed for gate in report.gates) == recorded["passed_gates"]
    assert len(report.gates) == recorded["total_gates"]
    assert [gate.name for gate in report.gates if not gate.passed] == recorded["failed_gates"]


@pytest.mark.parametrize("run", RUNS, ids=lambda run: run["config"]["mode"])
@pytest.mark.parametrize("phase,record_key", [("development", "final"), ("holdout", "holdout")])
async def test_recorded_repairs_reproduce_browser_scores(run, phase, record_key):
    report = await evaluate_source(run["final_source"], phase=phase)
    recorded = run[record_key]
    assert report.errors == []
    assert report.passed
    assert report.source_hash == recorded["source_hash"]
    assert sum(gate.passed for gate in report.gates) == recorded["passed_gates"]
    assert len(report.gates) == recorded["total_gates"]


@pytest.mark.parametrize("run", RUNS, ids=lambda run: run["config"]["mode"])
async def test_recorded_repairs_preserve_rapid_saves_and_ignore_unsaved_edit(run):
    # Hand-authored extension to the recorded comparison, not a model discovery.
    plan = ProbePlan(
        name="Replay published repair with rapid saves",
        hypothesis="Every activation saves its own value; a later edit stays unsaved.",
        steps=[
            ProbeStep(kind="edit", value="Jordan Lee"),
            ProbeStep(kind="save", value="Enter"),
            ProbeStep(kind="edit", value="Taylor Reed"),
            ProbeStep(kind="save", value="Space"),
            ProbeStep(kind="edit", value="Unsaved Draft"),
        ],
    )
    report = await evaluate_probe(run["final_source"], plan)
    assert report.errors == []
    assert report.passed
    assert report.expected_names == ["Jordan Lee", "Taylor Reed"]
    assert [request["payload"]["display_name"] for request in report.observed_requests] == (
        report.expected_names
    )
    assert all(request["accepted"] for request in report.observed_requests)
    assert report.observations[-1]["snapshot"]["persistence"]["display_name"] == "Taylor Reed"
