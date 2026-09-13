"""Local, atomic evidence storage; no credentials or arbitrary client paths."""

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from keyproof.contracts import EvaluationReport, RunRecord, timestamp

if TYPE_CHECKING:
    from keyproof.challenges import ChallengeRun

_ID = re.compile(r"^[a-f0-9]{32}$")


def verified_capture_path(
    report: EvaluationReport | None,
    source: str,
    root: Path,
    *,
    phase: str,
) -> Path | None:
    """Bind retained PNG evidence to its phase, exact source and owned directory."""
    if (
        report is None
        or report.phase != phase
        or report.source_hash != hashlib.sha256(source.encode("utf-8")).hexdigest()
    ):
        return None
    root = root.resolve()
    for artifact in report.artifacts:
        path = Path(artifact).resolve()
        if (
            path.is_relative_to(root)
            and path.name.endswith("-keyboard.png")
            and path.is_file()
            and path.stat().st_size <= 4 * 1024 * 1024
        ):
            with path.open("rb") as image:
                if image.read(8) == b"\x89PNG\r\n\x1a\n":
                    return path
    return None


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.runs = self.root / "runs"
        self.comparisons = self.root / "comparisons"
        self.challenges = self.root / "challenges"
        self.runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.comparisons.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.challenges.mkdir(parents=True, exist_ok=True, mode=0o700)

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

    def list_comparisons(self) -> list[dict[str, Any]]:
        records = [
            json.loads(path.read_text(encoding="utf-8")) for path in self.comparisons.glob("*.json")
        ]
        return sorted(records, key=lambda record: record["started_at"], reverse=True)

    def comparison_screenshots(self, comparison: dict[str, Any]) -> dict[str, str]:
        screenshots = {}
        for run_id in comparison["run_ids"]:
            record = self.get_run(run_id)
            frozen = any(
                event.role == "controller" and event.kind == "frozen" for event in record.events
            )
            for variant, source, report in (
                ("original", record.original_source, record.initial_report),
                ("final", record.final_source, record.final_report),
            ):
                if variant == "final" and not frozen:
                    continue
                path = verified_capture_path(
                    report,
                    source,
                    self.run_dir(run_id),
                    phase="development",
                )
                if path is not None:
                    screenshots[f"{record.config.mode}:{variant}"] = (
                        "data:image/png;base64,"
                        + base64.b64encode(path.read_bytes()).decode("ascii")
                    )
        return screenshots

    def challenge_dir(self, challenge_id: str) -> Path:
        return self.challenges / self.validate_id(challenge_id)

    def save_challenge(self, record: "ChallengeRun") -> None:
        self._write(
            self.challenge_dir(record.challenge_id) / "challenge.json",
            record.model_dump(mode="json"),
        )

    def get_challenge(self, challenge_id: str) -> "ChallengeRun":
        from keyproof.challenges import ChallengeRun

        return ChallengeRun.model_validate_json(
            (self.challenge_dir(challenge_id) / "challenge.json").read_text(encoding="utf-8")
        )

    def list_challenges(self) -> list["ChallengeRun"]:
        from keyproof.challenges import ChallengeRun

        records = [
            ChallengeRun.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.challenges.glob("*/challenge.json")
        ]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def mark_interrupted(self) -> None:
        for record in self.list_runs():
            if record.status in {"queued", "running"}:
                if record.status == "running":
                    record.usage.complete = False
                    record.usage.input_tokens = None
                    record.usage.output_tokens = None
                    record.usage.cached_input_tokens = None
                record.status = "failed"
                record.finished_at = timestamp()
                record.errors.append(
                    "Execution was interrupted; partial evidence is preserved and in-flight usage is unknown."
                )
                self.save_run(record)
        for path in self.comparisons.glob("*.json"):
            comparison = json.loads(path.read_text(encoding="utf-8"))
            if comparison["status"] in {"queued", "running"}:
                comparison["status"] = "failed"
                comparison.setdefault("errors", []).append(
                    "Comparison interrupted before completion."
                )
                self.save_comparison(comparison)
        for record in self.list_challenges():
            if record.status in {"queued", "running"}:
                record.status = "failed"
                record.verdict = "error"
                record.finished_at = timestamp()
                record.errors.append(
                    "Challenge interrupted before completion; no verdict is claimed."
                )
                self.save_challenge(record)


def default_data_dir() -> Path:
    return Path(os.environ.get("KEYPROOF_DATA_DIR", ".keyproof"))
