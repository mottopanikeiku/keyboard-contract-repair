import asyncio
from pathlib import Path

import pytest

from keyproof.challenges import _candidate
from keyproof.learning_contracts import ProbePlan, ProbeReport, ProbeStep
from keyproof.oracle import TaskBrowser, fixture_source
from keyproof.probes import admit_counterexample, evaluate_probe, is_counterexample


def reference_source():
    return _candidate("reference", fixture_source())


def rapid_probe():
    # A hand-authored regression fixture, not a model-generated discovery.
    return ProbePlan(
        name="Rapid saves followed by an unsaved edit",
        hypothesis="Pending saves must retain each activation's name and never save later edits.",
        steps=[
            ProbeStep(kind="edit", value="Jordan Lee"),
            ProbeStep(kind="save", value="Enter"),
            ProbeStep(kind="edit", value="Taylor Reed"),
            ProbeStep(kind="save", value="Space"),
            ProbeStep(kind="edit", value="Unsaved Draft"),
        ],
    )


def faulty_source(kind):
    handler = {
        "deferred_read": """saveButton.addEventListener('click', () => {
  setTimeout(() => window.harbor.saveDisplayName(), 5000);
  document.querySelector('#save-status').textContent = 'Changes saved';
});""",
        "busy_drop": """let busy = false;
saveButton.addEventListener('click', () => {
  if (busy) return;
  busy = true;
  window.harbor.saveDisplayName();
  setTimeout(() => { busy = false; }, 5000);
});""",
    }[kind]
    return reference_source().replace(
        """saveButton.addEventListener('click', async () => {
  await window.harbor.saveDisplayName();
});""",
        handler,
    )


def admit(source, plan, witness, confirmation):
    return admit_counterexample(
        origin_run_id="0123456789abcdef0123456789abcdef",
        case_id="deferred_read",
        source=source,
        plan=plan,
        witness=witness,
        confirmation=confirmation,
    )


async def test_reference_preserves_rapid_activations_and_does_not_save_later_edit(tmp_path):
    source, plan = reference_source(), rapid_probe()
    report = await evaluate_probe(source, plan, artifact_dir=tmp_path)
    assert report.errors == []
    assert report.passed
    assert [request["payload"]["display_name"] for request in report.observed_requests] == [
        "Jordan Lee",
        "Taylor Reed",
    ]
    assert all(request["accepted"] for request in report.observed_requests)
    assert report.observations[-1]["snapshot"]["persistence"]["display_name"] == "Taylor Reed"
    assert Path(report.artifacts[0]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert not is_counterexample(report)
    with pytest.raises(ValueError, match="failing probe evidence"):
        admit(source, plan, report, report)
    forged = report.model_copy(deep=True)
    forged.passed = False
    forged.gates[2].passed = False
    assert not is_counterexample(forged)


async def test_deferred_read_uses_real_post_payloads_and_requires_two_distinct_replays():
    source, plan = faulty_source("deferred_read"), rapid_probe()
    witness = await evaluate_probe(source, plan)
    confirmation = await evaluate_probe(source, plan)
    assert witness.errors == confirmation.errors == []
    assert is_counterexample(witness) and is_counterexample(confirmation)
    assert "Changes saved" in witness.observations[-1]["snapshot"]["visible_text"]
    assert [request["payload"]["display_name"] for request in witness.observed_requests] == [
        "Unsaved Draft",
        "Unsaved Draft",
    ]
    admit(source, plan, witness, confirmation)
    with pytest.raises(ValueError, match="independent browser replays"):
        admit(source, plan, witness, witness.model_copy(deep=True))
    with pytest.raises(ValueError, match="source-bound"):
        admit(reference_source(), plan, witness, confirmation)

    # Admission reconstructs the gates and ledger: a changed verdict, truncated
    # trace, forged accepted payload, or injected runtime error is not memory.
    verdict = confirmation.model_copy(deep=True)
    verdict.gates[2].actual = []
    truncated = confirmation.model_copy(deep=True)
    truncated.observations.pop()
    # Tamper with the serialized wire evidence, not aliased in-memory snapshots.
    ledger = ProbeReport.model_validate_json(confirmation.model_dump_json())
    ledger.observed_requests[0]["payload"]["display_name"] = "Jordan Lee"
    runtime = confirmation.model_copy(deep=True)
    runtime.observations[-1]["snapshot"]["errors"].append("Uncaught candidate error")
    empty = confirmation.model_copy(deep=True)
    empty.gates = []
    for forged in (verdict, truncated, ledger, runtime, empty):
        assert not is_counterexample(forged)
        with pytest.raises(ValueError, match="failing probe evidence"):
            admit(source, plan, witness, forged)


async def test_busy_drop_loses_second_activation_even_when_first_save_succeeds():
    report = await evaluate_probe(faulty_source("busy_drop"), rapid_probe())
    assert report.errors == []
    assert is_counterexample(report)
    assert [request["payload"]["display_name"] for request in report.observed_requests] == [
        "Jordan Lee"
    ]
    gates = {gate.name: gate for gate in report.gates}
    assert gates["navigation"].passed
    assert not gates["persistence.requests"].passed
    assert not gates["persistence.stored_name"].passed


async def test_unreachable_save_is_navigation_failure_not_request_mismatch():
    source = reference_source() + "\nsaveButton.disabled = true;\n"
    report = await evaluate_probe(source, rapid_probe())
    assert report.errors == []
    gates = {gate.name: gate for gate in report.gates}
    assert not gates["navigation"].passed
    assert gates["persistence.requests"].actual is None
    assert gates["persistence.stored_name"].actual is None
    assert [gate.name for gate in report.gates if not gate.passed] == ["navigation"]


async def test_edit_only_control_exposes_autosave_substituting_for_ignored_activation():
    plan = ProbePlan(
        name="One edit and activation",
        hypothesis="An edit must not persist by itself, even if an ignored activation follows.",
        steps=[
            ProbeStep(kind="edit", value="Jordan Lee"),
            ProbeStep(kind="save", value="Enter"),
        ],
    )
    source = reference_source().replace(
        """saveButton.addEventListener('click', async () => {
  await window.harbor.saveDisplayName();
});""",
        """nameField.addEventListener('input', () => {
  setTimeout(() => window.harbor.saveDisplayName(), 5000);
});""",
    )
    report = await evaluate_probe(source, plan)
    assert report.errors == []
    gates = {gate.name: gate for gate in report.gates}
    assert gates["persistence.requests"].passed
    assert not gates["editing.no_write"].passed
    assert is_counterexample(report)


async def test_candidate_runtime_errors_disqualify_otherwise_real_counterexample():
    source = (
        faulty_source("deferred_read")
        + "\nsetTimeout(() => { throw new Error('probe runtime failure'); }, 30000);\n"
    )
    report = await evaluate_probe(source, rapid_probe())
    assert any("probe runtime failure" in error for error in report.errors)
    assert not is_counterexample(report)


async def test_browser_failure_disqualifies_memory_and_cancellation_propagates(monkeypatch):
    async def broken_enter(self):
        raise RuntimeError("Chromium unavailable")

    monkeypatch.setattr(TaskBrowser, "__aenter__", broken_enter)
    report = await evaluate_probe(reference_source(), rapid_probe())
    assert any("Chromium unavailable" in error for error in report.errors)
    assert not is_counterexample(report)

    async def cancelled_enter(self):
        raise asyncio.CancelledError

    monkeypatch.setattr(TaskBrowser, "__aenter__", cancelled_enter)
    with pytest.raises(asyncio.CancelledError):
        await evaluate_probe(reference_source(), rapid_probe())
