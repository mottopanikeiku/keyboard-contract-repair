"""Re-execute one frozen learned dataset against before/after source versions."""

import asyncio
import hashlib
from typing import Any

import weave
from pydantic import PrivateAttr

from .learning_contracts import LearningRun, ProbePlan, ProbeReport, memory_digest, probe_digest
from .telemetry import trace_agent_inputs


@weave.op()
def learned_keyboard_contract(output: dict[str, Any]) -> dict[str, Any]:
    from .probes import is_counterexample

    report = ProbeReport.model_validate(output)
    return {
        "all_contracts_pass": report.passed,
        "counterexample_detected": is_counterexample(report),
        "execution_valid": not report.errors,
        "failed_contracts": sum(not gate.passed for gate in report.gates),
    }


class FrozenCurriculum(weave.Model):
    sources: dict[str, str]
    variant: str
    memory_hash: str
    _browser_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _reports: list[tuple[str, ProbeReport]] = PrivateAttr(default_factory=list)

    @weave.op()
    async def predict(self, case_id: str, probe: dict[str, Any]) -> dict[str, Any]:
        from .probes import evaluate_probe

        # Weave schedules dataset rows concurrently; do not fan out Chromium processes.
        async with self._browser_lock:
            report = await evaluate_probe(self.sources[case_id], ProbePlan.model_validate(probe))
            self._reports.append((case_id, report))
            return report.model_dump(mode="json")


@weave.op(postprocess_inputs=trace_agent_inputs)
async def execute_learning_evaluation(record: LearningRun) -> dict[str, Any]:
    from .probes import is_counterexample

    if (
        not record.memory
        or not record.memory_frozen_at
        or record.memory_hash != memory_digest(record.memory)
        or not record.transfer_source_revealed_at
        or record.memory_frozen_at > record.transfer_source_revealed_at
        or not record.cases
        or any(not case.frozen_at or not case.final_source for case in record.cases)
    ):
        raise ValueError("Learning evaluation requires frozen memory and frozen case sources")
    rows = [
        {"case_id": case.case_id, "probe": entry.plan.model_dump(mode="json")}
        for case in record.cases
        for entry in record.memory
    ]
    dataset = weave.Dataset(name="keyproof-learned-keyboard-regressions", rows=rows)
    evaluation = weave.Evaluation(
        name="keyproof-learned-keyboard-regressions",
        dataset=dataset,
        scorers=[learned_keyboard_contract],
        metadata={
            "learning_id": record.learning_id,
            "memory_hash": record.memory_hash,
            "memory_frozen_at": record.memory_frozen_at,
            "transfer_source_revealed_at": record.transfer_source_revealed_at,
            "claim": "same frozen generated probes, before/after owned workflow implementations",
            "scope": "three synthetic profile-workflow variants; not a statistical benchmark",
        },
    )
    results: dict[str, Any] = {
        "memory_hash": record.memory_hash,
        "dataset_rows": len(rows),
        "evaluations": {},
    }
    expected_pairs = {
        (case.case_id, probe_digest(entry.plan)) for case in record.cases for entry in record.memory
    }
    for variant in ("before", "after"):
        sources = {
            case.case_id: case.original_source if variant == "before" else case.final_source
            for case in record.cases
        }
        model = FrozenCurriculum(
            name=f"keyproof-{variant}-curriculum",
            sources=sources,
            variant=variant,
            memory_hash=record.memory_hash,
        )
        summary, call = await evaluation.evaluate.call(model=model)
        actual_pairs = {(case_id, report.probe_hash) for case_id, report in model._reports}
        if len(model._reports) != len(rows) or actual_pairs != expected_pairs:
            raise RuntimeError("Weave did not execute every frozen memory row exactly once")
        if any(
            report.errors
            or report.source_hash != hashlib.sha256(sources[case_id].encode()).hexdigest()
            or report.probe_hash != probe_digest(report.plan)
            or not report.gates
            or report.passed != all(gate.passed for gate in report.gates)
            for case_id, report in model._reports
        ):
            raise RuntimeError(
                "A published memory replay contains incomplete or mismatched evidence"
            )
        detected_cases = {
            case_id for case_id, report in model._reports if is_counterexample(report)
        }
        expected_detections = {
            case.case_id
            for case in record.cases
            if any(
                is_counterexample(report)
                for report in case.initial_memory_reports + case.discoveries
            )
        }
        if variant == "before" and not expected_detections.issubset(detected_cases):
            raise RuntimeError("A previously witnessed counterexample did not reproduce in Weave")
        if variant == "after" and any(not report.passed for _, report in model._reports):
            raise RuntimeError("A frozen repair failed the learned Weave regression dataset")
        results["evaluations"][variant] = {
            "summary": summary,
            "call_id": call.id if call else None,
            "passed_rows": sum(report.passed for _, report in model._reports),
            "detected_cases": sorted(detected_cases),
        }
    results["dataset_reference"] = dataset.ref.uri if dataset.ref is not None else None
    return results
