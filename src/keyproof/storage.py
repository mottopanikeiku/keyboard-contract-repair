"""Local, atomic evidence storage; no credentials or arbitrary client paths."""

import json
import os
import re
from pathlib import Path
from typing import Any

from keyproof.contracts import RunRecord, timestamp

_ID = re.compile(r"^[a-f0-9]{32}$")


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.runs = self.root / "runs"
        self.comparisons = self.root / "comparisons"
        self.runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.comparisons.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def validate_id(identifier: str) -> str:
        if not _ID.fullmatch(identifier):
            raise ValueError("Invalid evidence identifier")
        return identifier

    def run_dir(self, run_id: str) -> Path:
        return self.runs / self.validate_id(run_id)

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def save_run(self, record: RunRecord) -> None:
        self._write(self.run_dir(record.run_id) / "run.json", record.model_dump(mode="json"))

    def get_run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate_json(
            (self.run_dir(run_id) / "run.json").read_text(encoding="utf-8")
        )

    def list_runs(self) -> list[RunRecord]:
        records = []
        for path in self.runs.glob("*/run.json"):
            records.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
        return sorted(records, key=lambda record: record.started_at, reverse=True)

    def save_comparison(self, comparison: dict[str, Any]) -> None:
        identifier = self.validate_id(comparison["comparison_id"])
        self._write(self.comparisons / f"{identifier}.json", comparison)

    def get_comparison(self, identifier: str) -> dict[str, Any]:
        path = self.comparisons / f"{self.validate_id(identifier)}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def mark_interrupted(self) -> None:
        for record in self.list_runs():
            if record.status in {"queued", "running"}:
                record.status = "failed"
                record.finished_at = timestamp()
                record.errors.append("Execution was interrupted; partial evidence is preserved.")
                self.save_run(record)
        for path in self.comparisons.glob("*.json"):
            comparison = json.loads(path.read_text(encoding="utf-8"))
            if comparison["status"] in {"queued", "running"}:
                comparison["status"] = "failed"
                comparison.setdefault("errors", []).append("Comparison interrupted before completion.")
                self.save_comparison(comparison)


def default_data_dir() -> Path:
    return Path(os.environ.get("KEYPROOF_DATA_DIR", ".keyproof"))
