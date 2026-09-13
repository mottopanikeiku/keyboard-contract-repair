"""Bounded generated probes and independently witnessed regression memory."""

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import (
    Contract,
    EvaluationReport,
    GateResult,
    RunConfig,
    RunEvent,
    SourcePatch,
    Usage,
    WeaveStatus,
    timestamp,
)

PROBE_VERSION = "keyboard-profile-probe-v1"


class ProbeStep(Contract):
    kind: Literal["edit", "save", "wait"]
    value: str = Field(max_length=80)

    @model_validator(mode="after")
    def valid_value(self) -> "ProbeStep":
        if self.kind == "edit" and (
            not self.value or any(ord(char) < 32 or ord(char) == 127 for char in self.value)
        ):
            raise ValueError("edit requires 1–80 printable characters")
        if self.kind == "save" and self.value not in {"Enter", "Space"}:
            raise ValueError("save requires Enter or Space")
        if self.kind == "wait" and (
            not self.value.isascii() or not self.value.isdecimal() or int(self.value) > 10000
        ):
            raise ValueError("wait requires milliseconds between 0 and 10000")
        return self


class ProbePlan(Contract):
    name: str = Field(min_length=1, max_length=120)
    hypothesis: str = Field(min_length=1, max_length=2000)
    steps: list[ProbeStep] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def executable(self) -> "ProbePlan":
        kinds = {step.kind for step in self.steps}
        if not {"edit", "save"}.issubset(kinds):
            raise ValueError("A probe must edit a name and activate Save")
        if self.steps[0].kind != "edit":
            raise ValueError("A probe starts by establishing its own display name")
        if sum(int(step.value) for step in self.steps if step.kind == "wait") > 20000:
            raise ValueError("Explicit waits are limited to 20000 virtual milliseconds")
        return self


def probe_digest(plan: ProbePlan) -> str:
    payload = {"version": PROBE_VERSION, "steps": [step.model_dump() for step in plan.steps]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ProbeReport(Contract):
    version: Literal["keyboard-profile-probe-v1"] = PROBE_VERSION
    plan: ProbePlan
    probe_hash: str
    source_hash: str
    passed: bool
    gates: list[GateResult]
    expected_names: list[str] = Field(default_factory=list)
    observed_requests: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0


class MemoryEntry(Contract):
    entry_id: str
    origin_run_id: str
    discovered_case_id: str
    verified_at: str = Field(default_factory=timestamp)
    probe_hash: str
    plan: ProbePlan
    failing_source: str
    witness: ProbeReport
    confirmation: ProbeReport


def memory_digest(entries: list[MemoryEntry]) -> str:
    """Version executable content, independently of labels, paths, or recording times."""
    return hashlib.sha256(
        json.dumps(sorted(probe_digest(entry.plan) for entry in entries)).encode()
    ).hexdigest()


class LearningConfig(RunConfig):
    mode: Literal["team"] = "team"
    memory_run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class LearningRepair(Contract):
    number: int
    patch: SourcePatch | None = None
    candidate_source: str = ""
    source_diff: str = ""
    development_report: EvaluationReport | None = None
    probe_reports: list[ProbeReport] = Field(default_factory=list)
    accepted: bool = False
    decision: str = ""


class LearningCase(Contract):
    case_id: str
    title: str
    partition: Literal["training", "transfer"]
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    original_source: str
    original_source_hash: str
    initial_development_report: EvaluationReport | None = None
    initial_holdout_report: EvaluationReport | None = None
    initial_memory_reports: list[ProbeReport] = Field(default_factory=list)
    discoveries: list[ProbeReport] = Field(default_factory=list)
    repairs: list[LearningRepair] = Field(default_factory=list)
    final_source: str = ""
    frozen_at: str | None = None
    final_development_report: EvaluationReport | None = None
    final_probe_reports: list[ProbeReport] = Field(default_factory=list)
    final_holdout_report: EvaluationReport | None = None
    errors: list[str] = Field(default_factory=list)


class LearningRun(Contract):
    learning_id: str
    config: LearningConfig
    status: Literal["queued", "running", "completed", "failed", "budget_exhausted"] = "queued"
    verdict: Literal[
        "not_evaluated", "transfer_verified", "transfer_missed", "repair_rejected", "error"
    ] = "not_evaluated"
    created_at: str = Field(default_factory=timestamp)
    finished_at: str | None = None
    cases: list[LearningCase] = Field(default_factory=list)
    memory: list[MemoryEntry] = Field(default_factory=list)
    memory_frozen_at: str | None = None
    memory_hash: str | None = None
    transfer_source_revealed_at: str | None = None
    events: list[RunEvent] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    weave: WeaveStatus = Field(default_factory=WeaveStatus)
    errors: list[str] = Field(default_factory=list)
