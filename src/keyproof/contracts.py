"""Shared wire contracts. Agents may propose edits, never verdicts."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrowserAction(Contract):
    kind: Literal["press", "type", "finish"]
    value: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=1000)


class GateResult(Contract):
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""


class EvaluationReport(Contract):
    phase: Literal["development", "holdout"]
    passed: bool
    gates: list[GateResult]
    source_hash: str
    elapsed_ms: float
    actions: list[dict[str, Any]] = Field(default_factory=list)
    axe_violations: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class TextEdit(Contract):
    before: str = Field(min_length=1, max_length=32000)
    after: str = Field(max_length=32000)


class SourcePatch(Contract):
    summary: str = Field(min_length=1, max_length=2000)
    edits: list[TextEdit] = Field(min_length=1, max_length=8)


class AuditReport(Contract):
    summary: str
    observations: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class Usage(Contract):
    calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    elapsed_ms: float = 0


class RunConfig(Contract):
    mode: Literal["team", "single"] = "team"
    provider: Literal["codex", "openai"] = "codex"
    model: str | None = Field(default=None, max_length=120)
    max_iterations: int = Field(default=3, ge=1, le=5)
    max_model_calls: int = Field(default=12, ge=2, le=30)
    max_output_tokens: int = Field(default=6000, ge=500, le=16000)
    max_input_tokens: int = Field(default=180000, ge=10000, le=1000000)
    require_weave: bool = True


class RunEvent(Contract):
    sequence: int
    timestamp: str = Field(default_factory=timestamp)
    role: str
    kind: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class Iteration(Contract):
    number: int
    audit: AuditReport | None = None
    patch: SourcePatch | None = None
    diff: str = ""
    candidate_source: str | None = None
    report: EvaluationReport | None = None
    accepted: bool = False
    decision: str = ""


class WeaveStatus(Contract):
    enabled: bool = False
    project: str | None = None
    url: str | None = None
    error: str | None = None


class RunRecord(Contract):
    run_id: str
    config: RunConfig
    status: Literal["queued", "running", "completed", "failed", "budget_exhausted"] = "queued"
    started_at: str = Field(default_factory=timestamp)
    finished_at: str | None = None
    original_source: str = ""
    final_source: str = ""
    initial_report: EvaluationReport | None = None
    iterations: list[Iteration] = Field(default_factory=list)
    final_report: EvaluationReport | None = None
    events: list[RunEvent] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    weave: WeaveStatus = Field(default_factory=WeaveStatus)
    errors: list[str] = Field(default_factory=list)
