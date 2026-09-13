import asyncio
import hashlib

import pytest

from keyproof import controller
from keyproof.contracts import Contract, EvaluationReport, GateResult, RunConfig, RunRecord, Usage
from keyproof.provider import BudgetExhausted, ModelClient
from keyproof.service import RunService
from keyproof.storage import EvidenceStore
from keyproof.telemetry import Telemetry


class Response(Contract):
    summary: str


@pytest.mark.parametrize(
    "reported",
    [
        {"input_tokens": 10001, "output_tokens": 20, "cached_input_tokens": 0},
        {"input_tokens": None, "output_tokens": 20, "cached_input_tokens": 0},
    ],
    ids=["overspent-response", "unmetered-response"],
)
async def test_unusable_budget_response_cannot_be_consumed(monkeypatch, reported):
    monkeypatch.setattr("keyproof.provider.shutil.which", lambda name: "/bin/false")
    client = ModelClient(RunConfig(max_input_tokens=10000, require_weave=False))
    responses = iter([('{"summary":"proposal"}', reported)])

    async def completion(prompt, schema):
        return next(responses)

    monkeypatch.setattr(client, "_codex", completion)
    with pytest.raises(BudgetExhausted):
        await client.complete("Repair", Response)
    # A subsequent request must stop before consuming another provider response.
    with pytest.raises(BudgetExhausted):
        await client.complete("Try again", Response)


@pytest.mark.parametrize("masked_cleanup", [False, True])
async def test_shutdown_does_not_start_the_next_comparison_member(
    monkeypatch, tmp_path, masked_cleanup
):
    entered = asyncio.Event()
    first = True

    async def evaluation(source, *, phase="development", artifact_dir=None):
        nonlocal first
        if first:
            first = False
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                if masked_cleanup:
                    raise RuntimeError("Driver cleanup failed during cancellation") from None
                raise
        return EvaluationReport(
            phase=phase,
            passed=True,
            gates=[GateResult(name="contract", passed=True)],
            source_hash=hashlib.sha256(source.encode()).hexdigest(),
            elapsed_ms=0,
        )

    monkeypatch.setattr(controller, "evaluate_source", evaluation)
    monkeypatch.setattr("keyproof.oracle.evaluate_source", evaluation)
    monkeypatch.setattr("keyproof.service.shutil.which", lambda name: "/bin/false")
    service = RunService(tmp_path, Telemetry())
    pair = service.submit_comparison(RunConfig(model="unused", require_weave=False))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(service.close(), 3)
    assert service.comparison(pair["comparison_id"])["status"] == "failed"
    assert all(
        service.store.get_run(identifier).status == "failed" for identifier in pair["run_ids"]
    )


def test_interrupted_inflight_usage_is_unknown(tmp_path):
    store = EvidenceStore(tmp_path)
    record = RunRecord(
        run_id="a" * 32,
        config=RunConfig(require_weave=False),
        status="running",
        usage=Usage(calls=2, input_tokens=300, output_tokens=40, complete=False),
    )
    store.save_run(record)
    store.mark_interrupted()
    recovered = store.get_run(record.run_id)
    assert recovered.status == "failed"
    assert not recovered.usage.complete
    assert recovered.usage.input_tokens is None
    assert recovered.usage.output_tokens is None
