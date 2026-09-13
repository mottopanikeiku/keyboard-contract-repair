"""Explicit Weave connection and real frozen-source evaluations."""

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import weave

from keyproof.contracts import RunRecord, WeaveStatus


def redact(value: Any) -> Any:
    """Never send configured credentials through a trace or error payload."""
    if isinstance(value, dict):
        sensitive = {"api_key", "authorization", "password", "access_token", "refresh_token", "secret"}
        return {
            key: "[redacted]" if str(key).lower() in sensitive else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for name, secret in os.environ.items():
            if any(marker in name for marker in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")):
                if len(secret) >= 8:
                    value = value.replace(secret, "[redacted]")
    return value


@weave.op()
def connection_probe() -> dict[str, str]:
    call = weave.get_current_call()
    if call is None:
        raise RuntimeError("Weave did not create a trace")
    return {"kind": "connectivity-only", "call_id": call.id}


@weave.op()
def behavioral_contract(output: dict[str, Any]) -> dict[str, Any]:
    gates = output["gates"]
    return {
        "all_contracts_pass": output["passed"],
        "passed_gates": sum(gate["passed"] for gate in gates),
        "total_gates": len(gates),
        "no_runtime_errors": not output["errors"],
        "axe_violations": len(output["axe_violations"]),
    }


class FrozenCandidate(weave.Model):
    """A versioned source snapshot, never another repair opportunity."""

    source: str

    @weave.op()
    async def predict(self, phase: str) -> dict[str, Any]:
        from keyproof.oracle import evaluate_source

        return (await evaluate_source(self.source, phase=phase)).model_dump(mode="json")


def frozen_evaluation(record: RunRecord) -> weave.Evaluation:
    return weave.Evaluation(
        name="keyproof-keyboard-contract-v1",
        evaluation_name=f"{record.config.mode}-{record.run_id[:8]}",
        dataset=weave.Dataset(
            name="keyproof-keyboard-contract-v1",
            rows=[{"phase": "development"}, {"phase": "holdout"}],
        ),
        scorers=[behavioral_contract],
        metadata={
            "run_id": record.run_id,
            "mode": record.config.mode,
            "source_hash": record.final_report.source_hash if record.final_report else None,
            "config": record.config.model_dump(mode="json"),
            "claim": "owned fixture task correctness, not accessibility certification",
        },
    )


class Telemetry:
    def __init__(self) -> None:
        self.client: Any = None
        self.status = WeaveStatus(error="W&B is not connected. Run `uv run wandb login` first.")

    async def connect(self) -> WeaveStatus:
        if self.status.enabled:
            return self.status
        # Inspect existence only. The official SDK owns authentication and credential access.
        has_credentials = bool(os.environ.get("WANDB_API_KEY")) or (Path.home() / ".netrc").exists()
        if not has_credentials:
            return self.status
        return await asyncio.to_thread(self._connect)

    def _connect(self) -> WeaveStatus:
        project = os.environ.get("KEYPROOF_WEAVE_PROJECT", "keyproof")
        os.environ.setdefault("WANDB_HTTP_TIMEOUT", "15")
        try:
            self.client = weave.init(
                project,
                settings={
                    "print_call_link": False,
                    "capture_system_info": False,
                    "implicitly_patch_integrations": False,
                    "http_timeout": 15,
                    "retry_max_attempts": 1,
                },
                postprocess_inputs=redact,
                postprocess_output=redact,
                attributes={"application": "keyproof", "data_scope": "owned-synthetic-fixture"},
            )
            if self.client.entity == "DISABLED":
                raise RuntimeError("Weave returned a disabled client")
            result = connection_probe()
            self.client.flush()
            retrieved = self.client.get_call(result["call_id"])
            if retrieved.id != result["call_id"]:
                raise RuntimeError("Trace delivery was not verified")
            entity = quote(self.client.entity, safe="")
            name = quote(self.client.project, safe="")
            self.status = WeaveStatus(
                enabled=True,
                project=f"{self.client.entity}/{self.client.project}",
                url=f"https://wandb.ai/{entity}/{name}/weave",
            )
        except Exception as exc:
            self.client = None
            weave.finish()
            self.status = WeaveStatus(
                project=project,
                error=(
                    f"W&B connection failed ({type(exc).__name__}); no verified cloud trace. "
                    "Check login, project permissions, and network, then reconnect."
                ),
            )
        return self.status

    async def evaluate_frozen(self, record: RunRecord) -> dict[str, Any]:
        """Execute frozen source again inside a real Weave Dataset/Evaluation.

        These are extra verification executions, not extra repair opportunities or model calls.
        The caller must freeze BOTH comparison candidates before invoking this method.
        """
        if not self.status.enabled:
            raise RuntimeError("Cannot publish an evaluation without a verified Weave connection")
        result = await frozen_evaluation(record).evaluate(
            FrozenCandidate(name="keyproof-frozen-candidate", source=record.final_source)
        )
        await asyncio.to_thread(self.client.flush)
        return result

    async def close(self) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.flush)
        weave.finish()
