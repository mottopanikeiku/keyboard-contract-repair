"""Single-owner execution with durable events and sealed comparison evaluation."""

import asyncio
import fcntl
import json
import os
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from keyproof.contracts import RunConfig, RunEvent, RunRecord, timestamp
from keyproof.storage import EvidenceStore
from keyproof.telemetry import Telemetry, redact


class RunService:
    def __init__(self, root: Path, telemetry: Telemetry):
        self.store = EvidenceStore(root)
        self.telemetry = telemetry
        self._lock = (self.store.root / "execution.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise RuntimeError("Another Keyproof process owns this evidence directory") from None
        self.store.mark_interrupted()
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def check_config(self, config: RunConfig) -> None:
        if self.busy:
            raise RuntimeError("A run is active. Wait for it to finish before starting another.")
        if config.require_weave and not self.telemetry.status.enabled:
            raise RuntimeError(
                "A verified Weave connection is required. Connect W&B, or explicitly allow "
                "an untraced local experiment; that experiment is not sponsor-integrated."
            )
        if config.provider == "codex" and shutil.which("codex") is None:
            raise ValueError("Codex CLI is not installed. Install/login or configure the API provider.")
        if config.provider == "openai" and not (
            os.environ.get("KEYPROOF_API_KEY") or os.environ.get("OPENAI_API_KEY")
        ):
            raise ValueError("The OpenAI-compatible provider has no configured API key.")

    def _new_record(self, config: RunConfig) -> RunRecord:
        from keyproof.oracle import fixture_source
        if config.model is None:
            model = os.environ.get("KEYPROOF_MODEL")
            if config.provider == "openai":
                model = model or os.environ.get("OPENAI_MODEL")
            config = config.model_copy(update={"model": model})

        source = fixture_source()
        record = RunRecord(
            run_id=uuid4().hex,
            config=config,
            original_source=source,
            final_source=source,
            weave=self.telemetry.status.model_copy(deep=True),
        )
        self.store.save_run(record)
        return record

    def submit(self, config: RunConfig) -> str:
        self.check_config(config)
        record = self._new_record(config)
        self._task = asyncio.create_task(self._execute(record, holdout=True, publish=True))
        return record.run_id

    def submit_comparison(self, config: RunConfig) -> dict[str, Any]:
        self.check_config(config)
        if not (config.model or os.environ.get("KEYPROOF_MODEL") or (
            config.provider == "openai" and os.environ.get("OPENAI_MODEL")
        )):
            raise ValueError("Comparisons require an explicit model ID. Set Model or KEYPROOF_MODEL.")
        records = [self._new_record(config.model_copy(update={"mode": mode})) for mode in ("team", "single")]
        comparison = {
            "comparison_id": uuid4().hex,
            "status": "queued",
            "errors": [],
            "run_ids": [record.run_id for record in records],
            "started_at": timestamp(),
            "protocol": (
                "Same original source, model, public contracts, tools and resource ceilings. "
                "Both candidates freeze before holdout. Actual spend may differ; one pair "
                "does not establish superiority."
            ),
        }
        self.store.save_comparison(comparison)
        self._task = asyncio.create_task(self._compare(comparison, records))
        return comparison

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def _execute(self, placeholder: RunRecord, *, holdout: bool, publish: bool) -> None:
        from keyproof.controller import run_repair

        placeholder.status = "running"
        self.store.save_run(placeholder)

        def on_update(snapshot: RunRecord) -> None:
            snapshot.weave = self.telemetry.status.model_copy(deep=True)
            self.store.save_run(snapshot)

        try:
            result = await run_repair(
                placeholder.config,
                run_id=placeholder.run_id,
                on_update=on_update,
                artifact_dir=self.store.run_dir(placeholder.run_id),
                evaluate_holdout=holdout,
            )
            result.weave = self.telemetry.status.model_copy(deep=True)
            self.store.save_run(result)
            if publish and result.final_report is not None and not result.final_report.errors:
                await self._publish(result)
        except asyncio.CancelledError:
            interrupted = self.store.get_run(placeholder.run_id)
            interrupted.status = "failed"
            interrupted.errors.append("Run cancelled; no completed result is claimed.")
            interrupted.finished_at = timestamp()
            self.store.save_run(interrupted)
            raise
        except Exception as exc:
            failed = self.store.get_run(placeholder.run_id)
            failed.status = "failed"
            failed.errors.append(redact(f"{type(exc).__name__}: {exc}"))
            failed.finished_at = timestamp()
            self.store.save_run(failed)

    async def _publish(self, record: RunRecord) -> None:
        if not self.telemetry.status.enabled:
            return
        try:
            summary = await self.telemetry.evaluate_frozen(record)
            self.store._write(self.store.run_dir(record.run_id) / "weave-evaluation.json", summary)
            record.events.append(
                RunEvent(
                    sequence=len(record.events) + 1,
                    role="weave",
                    kind="evaluation_published",
                    summary="Frozen source re-executed in a Weave Dataset/Evaluation; no further repair.",
                    data={"project": self.telemetry.status.project, "url": self.telemetry.status.url},
                )
            )
        except Exception as exc:
            record.weave.error = f"Cloud evaluation failed ({type(exc).__name__}); local evidence retained."
            record.errors.append(record.weave.error)
            if record.config.require_weave:
                record.status = "failed"
        self.store.save_run(record)

    async def _compare(self, comparison: dict[str, Any], records: list[RunRecord]) -> None:
        from keyproof.oracle import evaluate_source

        comparison["status"] = "running"
        self.store.save_comparison(comparison)
        try:
            # Neither agent can receive holdout feedback while either candidate remains mutable.
            for record in records:
                await self._execute(record, holdout=False, publish=False)
            frozen = [self.store.get_run(record.run_id) for record in records]
            comparison["frozen_at"] = timestamp()
            self.store.save_comparison(comparison)
            for record in frozen:
                if record.final_report is None:
                    continue
                record.holdout_report = await evaluate_source(
                    record.final_source,
                    phase="holdout",
                    artifact_dir=self.store.run_dir(record.run_id) / "holdout",
                )
                if record.holdout_report.errors:
                    record.status = "failed"
                    record.errors.extend(record.holdout_report.errors)
                self.store.save_run(record)
                if not record.final_report.errors and not record.holdout_report.errors:
                    await self._publish(record)
            final = [self.store.get_run(record.run_id) for record in records]
            comparison["status"] = (
                "completed" if all(
                    record.final_report is not None and record.holdout_report is not None
                    and not record.errors
                    for record in final
                ) else "failed"
            )
        except asyncio.CancelledError:
            comparison["status"] = "failed"
            comparison["errors"].append("Comparison cancelled; no complete comparison is claimed.")
            for pending in records:
                interrupted = self.store.get_run(pending.run_id)
                if interrupted.status in {"queued", "running"}:
                    interrupted.status = "failed"
                    interrupted.finished_at = timestamp()
                    interrupted.errors.append("Comparison cancelled before this run completed.")
                    self.store.save_run(interrupted)
            raise
        except Exception as exc:
            comparison["status"] = "failed"
            comparison["errors"].append(redact(f"{type(exc).__name__}: {exc}"))
        finally:
            comparison["finished_at"] = timestamp()
            self.store.save_comparison(comparison)

    def comparison(self, identifier: str) -> dict[str, Any]:
        result = self.store.get_comparison(identifier)
        result["results"] = [
            self.store.get_run(run_id).model_dump(mode="json") for run_id in result["run_ids"]
        ]
        return result

    async def close(self) -> None:
        try:
            if self.busy:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            await self.telemetry.close()
        finally:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
