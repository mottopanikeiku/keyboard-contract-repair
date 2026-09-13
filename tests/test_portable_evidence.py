import hashlib
import json
from copy import deepcopy
from html.parser import HTMLParser

import pytest

from keyproof.assessment import assess_comparison
from keyproof.report import render_report


def paired_evidence():
    source = "document.querySelector('button').disabled = false;\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    development = {
        "phase": "development",
        "passed": True,
        "source_hash": digest,
        "elapsed_ms": 1,
        "gates": [
            {"name": "keyboard.persist_exactly_once", "passed": True, "expected": 1, "actual": 1}
        ],
        "errors": [],
        "artifacts": [],
    }
    holdout = deepcopy(development)
    holdout["phase"] = "holdout"
    holdout["gates"][0]["name"] = "keyboard.space.persist_exactly_once"
    runs = []
    for mode, identifier in (("team", "a" * 32), ("single", "b" * 32)):
        runs.append(
            {
                "run_id": identifier,
                "config": {
                    "mode": mode,
                    "provider": "codex",
                    "model": "explicit-model",
                    "max_iterations": 3,
                    "max_model_calls": 12,
                    "max_input_tokens": 180000,
                    "max_output_tokens": 6000,
                    "require_weave": False,
                },
                "status": "completed",
                "started_at": "2026-09-12T10:00:00+00:00",
                "finished_at": "2026-09-12T10:02:00+00:00",
                "original_source": source,
                "final_source": source,
                "initial_report": deepcopy(development),
                "final_report": deepcopy(development),
                "holdout_report": deepcopy(holdout),
                "errors": [],
                "iterations": [],
                "usage": {
                    "calls": 2,
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": None,
                    "elapsed_ms": 10,
                    "complete": True,
                },
                "events": [
                    {
                        "role": "controller",
                        "kind": "frozen",
                        "sequence": 1,
                        "timestamp": "2026-09-12T10:01:00+00:00",
                        "summary": "Candidate frozen",
                        "data": {},
                    }
                ],
                "weave": {"enabled": False, "url": None},
            }
        )
    return {
        "comparison_id": "c" * 32,
        "status": "completed",
        "errors": [],
        "run_ids": [run["run_id"] for run in runs],
        "started_at": "2026-09-12T10:00:00+00:00",
        "frozen_at": "2026-09-12T10:03:00+00:00",
        "finished_at": "2026-09-12T10:04:00+00:00",
        "results": runs,
    }


def fail_contract(run, phase="holdout_report"):
    run[phase]["gates"][0]["passed"] = False
    run[phase]["passed"] = False


def test_correctness_tie_is_not_broken_by_lower_spend():
    pair = paired_evidence()
    pair["results"][1]["usage"].update(calls=1, input_tokens=50, output_tokens=20)
    assert assess_comparison(pair)["verdict"] == "tie"


def test_development_and_holdout_must_both_pass_for_a_winner():
    pair = paired_evidence()
    fail_contract(pair["results"][0], "final_report")
    assert assess_comparison(pair)["verdict"] == "single"
    fail_contract(pair["results"][1])
    assert assess_comparison(pair)["verdict"] == "neither"


def test_metered_budget_exhaustion_is_valid_but_execution_errors_are_not():
    pair = paired_evidence()
    pair["results"][0]["status"] = "budget_exhausted"
    fail_contract(pair["results"][0])
    assert assess_comparison(pair)["verdict"] == "single"
    pair["results"][0]["errors"] = ["Model request failed"]
    assert assess_comparison(pair)["verdict"] == "unavailable"


@pytest.mark.parametrize(
    "forgery",
    [
        "hash",
        "phase",
        "passed",
        "missing-gate",
        "usage",
        "freeze",
        "post-freeze",
        "event-error",
        "membership",
        "config",
        "weave",
    ],
)
def test_completion_label_cannot_override_broken_evidence(forgery):
    pair = paired_evidence()
    run = pair["results"][0]
    if forgery == "hash":
        run["final_source"] = "// substituted candidate"
    elif forgery == "phase":
        run["holdout_report"]["phase"] = "development"
    elif forgery == "passed":
        run["holdout_report"]["gates"][0]["passed"] = False
    elif forgery == "missing-gate":
        run["holdout_report"]["gates"] = []
    elif forgery == "usage":
        run["usage"]["input_tokens"] = None
    elif forgery == "freeze":
        run["events"] = []
    elif forgery == "post-freeze":
        run["events"].append({"role": "engineer", "kind": "patch", "sequence": 2})
    elif forgery == "event-error":
        run["events"].append({"role": "provider", "kind": "error", "sequence": 2})
    elif forgery == "membership":
        pair["run_ids"][1] = pair["run_ids"][0]
    elif forgery == "config":
        run["config"]["model"] = "different-model"
    elif forgery == "weave":
        for candidate in pair["results"]:
            candidate["config"]["require_weave"] = True
    assert assess_comparison(pair)["verdict"] == "unavailable"


class EvidenceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.json = ""
        self.in_evidence = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag == "script" and attributes.get("id") == "keyproof-evidence":
            self.in_evidence = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_evidence = False

    def handle_data(self, data):
        if self.in_evidence:
            self.json += data


def test_untrusted_evidence_cannot_forge_verdict_or_active_content():
    pair = paired_evidence()
    pair["assessment"] = {"comparable": True, "verdict": "team"}
    attack = '</script><img src=x onerror="alert(1)"><script>alert(2)</script>'
    pair["comparison_id"] = attack
    pair["results"][0]["final_source"] = attack
    pair["results"][0]["weave"] = {
        "enabled": True,
        "url": "javascript:alert(3)",
        "trace_url": "https://wandb.ai.evil.example/trace",
        "evaluation_url": "https://user:password@wandb.ai/trace",
    }
    report = render_report(pair, {"team:final": 'data:image/svg+xml,<svg onload="alert(4)"/>'})
    parser = EvidenceParser()
    parser.feed(report)
    scripts = [attrs for tag, attrs in parser.tags if tag == "script"]
    assert scripts == [{"id": "keyproof-evidence", "type": "application/json"}]
    assert not any(tag in ("img", "iframe", "object", "embed") for tag, _ in parser.tags)
    assert not any(key.startswith("on") for _, attrs in parser.tags for key in attrs)
    assert not any("href" in attrs for _, attrs in parser.tags)
    exported = json.loads(parser.json)
    assert exported["results"][0]["final_source"] == attack
    assert exported["assessment"]["verdict"] == "unavailable"


def test_export_omits_local_paths_and_credentials_without_needing_cloud_or_images():
    pair = paired_evidence()
    pair["results"][0]["final_report"]["artifacts"] = ["/home/private-person/evidence/final.png"]
    pair["results"][0]["events"][0]["data"] = {
        "api_key": "private-secret",
        "detail": "Error in /tmp/private-run/source.js; authorization=token-value",
    }
    report = render_report(pair)
    assert "/home/private-person" not in report
    assert "/tmp/private-run" not in report
    assert "private-secret" not in report
    assert "token-value" not in report
    parser = EvidenceParser()
    parser.feed(report)
    assert json.loads(parser.json)["assessment"]["verdict"] == "tie"
