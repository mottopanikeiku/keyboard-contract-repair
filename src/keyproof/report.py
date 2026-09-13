"""Offline, inert evidence presentation; candidate JavaScript is always text."""

import base64
import binascii
import difflib
import hashlib
import html
import json
import math
import re
from urllib.parse import urlsplit

from keyproof.assessment import assess_comparison

_SENSITIVE = re.compile(
    r"api[_-]?key|authorization|password|access[_-]?token|refresh[_-]?token|secret|credential", re.I
)
_PATH = re.compile(
    r"(?:file://[^\s<>\"']+|(?:/home/|/Users/|/tmp/|/root/|/var/|/private/|/mnt/|/opt/)[^\s<>\"']+|[A-Za-z]:\\[^\s<>\"']+)"
)
_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"',;<>]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]{8,}")
_RUN_FIELDS = (
    "run_id",
    "config",
    "status",
    "started_at",
    "finished_at",
    "original_source",
    "final_source",
    "initial_report",
    "final_report",
    "holdout_report",
    "iterations",
    "events",
    "usage",
    "weave",
    "errors",
)
_COMPARISON_FIELDS = (
    "comparison_id",
    "status",
    "run_ids",
    "started_at",
    "frozen_at",
    "finished_at",
    "protocol",
    "errors",
)


def _safe_url(value: object) -> str | None:
    """Match the cockpit's W&B-only HTTPS policy; never export signed/credential URLs."""
    if not isinstance(value, str) or any(ord(char) < 33 for char in value) or "\\" in value:
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.port not in (None, 443)
            or not (url.hostname == "wandb.ai" or url.hostname.endswith(".wandb.ai"))
        ):
            return None
        return value
    except ValueError:
        return None


def _sanitize(value: object) -> object:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key = str(key)
            if key.lower() in ("artifacts", "artifact_dir", "path", "file_path"):
                continue
            if _SENSITIVE.search(key):
                clean[key] = "[redacted]"
            elif key.lower().endswith("url"):
                clean[key] = _safe_url(item)
            else:
                clean[key] = _sanitize(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        value = _SECRET.sub(r"\1[redacted]", value)
        value = _BEARER.sub("[redacted credential]", value)
        value = re.sub(
            r"https?://[^\s<>\"']+",
            lambda match: _safe_url(match.group()) or "[external URL omitted]",
            value,
        )
        return _PATH.sub("[local path omitted]", value)
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return "[unsupported value omitted]"


def _text(value: object) -> str:
    if value is None:
        return "Unknown / not reported"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return html.escape(str(value), quote=True)


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _png(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("data:image/png;base64,"):
        return None
    try:
        image = base64.b64decode(value[22:], validate=True)
    except (ValueError, binascii.Error):
        return None
    return value if image.startswith(b"\x89PNG\r\n\x1a\n") else None


def _gates(report: object, title: str) -> str:
    report = _mapping(report)
    gates = [_mapping(gate) for gate in _list(report.get("gates"))]
    if not report or not gates:
        return f'<section class="evaluation"><h3>{title}</h3><p class="missing">Unavailable · no complete gate report.</p></section>'
    passed = sum(gate.get("passed") is True for gate in gates)
    valid = (
        type(report.get("passed")) is bool
        and report.get("errors") == []
        and report["passed"] == (passed == len(gates))
    )
    state = ("PASS" if report["passed"] else "FAIL") if valid else "UNAVAILABLE"
    rows = []
    for gate in gates:
        ok = gate.get("passed")
        label = "Pass" if ok is True else "Fail" if ok is False else "Unknown"
        rows.append(
            f'<tr><td class="{label.lower()}">{label}</td><th scope="row">{_text(gate.get("name"))}</th>'
            f"<td><pre>{_text(gate.get('expected'))}</pre></td><td><pre>{_text(gate.get('actual'))}</pre>"
            f"<p>{_text(gate.get('detail', ''))}</p></td></tr>"
        )
    failed = [gate.get("name", "Unnamed gate") for gate in gates if gate.get("passed") is not True]
    failures = _text(", ".join(str(name) for name in failed)) if failed else "None recorded"
    return (
        f'<section class="evaluation"><h3>{title}</h3><p class="gate-score">{state} · {passed}/{len(gates)} gates passed</p>'
        f'<p><strong>Failed or unknown gates:</strong> {failures}</p><p class="mono">Source SHA-256: {_text(report.get("source_hash"))}</p>'
        f"<p>Recorded phase: {_text(report.get('phase'))} · Evaluator time: {_text(report.get('elapsed_ms'))} ms</p>"
        f"<p>Evaluator errors: {_text(report.get('errors'))}</p>"
        '<details><summary>Every gate · expected versus observed</summary><div class="table-scroll"><table>'
        "<thead><tr><th>Result</th><th>Gate</th><th>Expected</th><th>Observed</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></details></section>"
    )


def _run(run: dict, screenshots: dict, *, comparable: bool) -> str:
    config, usage, weave = (_mapping(run.get(key)) for key in ("config", "usage", "weave"))
    mode = config.get("mode", "unknown")
    mode_label = "Team" if mode == "team" else "Single" if mode == "single" else "Unidentified run"
    parts = [
        f'<article class="run"><header><p class="eyebrow">Recorded execution</p><h2>{mode_label}</h2>'
        f'<p class="mono">Run {_text(run.get("run_id"))}</p><p>Status: {_text(run.get("status"))} · '
        f"{'Pair integrity checks passed' if comparable else 'Pair outcome unavailable; observations below are not a verified win'}</p></header>"
    ]
    parts.append(
        '<dl class="provenance">'
        + "".join(
            f"<div><dt>{label}</dt><dd>{_text(run.get(key))}</dd></div>"
            for key, label in (("started_at", "Started"), ("finished_at", "Execution finished"))
        )
        + "</dl>"
    )
    freezes = [
        event
        for event in _list(run.get("events"))
        if isinstance(event, dict)
        and event.get("role") == "controller"
        and event.get("kind") == "frozen"
    ]
    parts.append(
        "<p><strong>Controller freeze:</strong> "
        + (
            "; ".join(
                f"{_text(event.get('timestamp'))} · event #{_text(event.get('sequence'))}"
                for event in freezes
            )
            or "Unavailable"
        )
        + "</p>"
    )
    parts.append(f"<p><strong>Run errors:</strong> {_text(run.get('errors'))}</p>")
    if weave.get("enabled") is True:
        parts.append(
            "<p>Weave connection recorded. A project link alone is not proof of a delivered trace or evaluation.</p>"
        )
        for key, label in (
            ("url", "Open recorded Weave project"),
            ("trace_url", "Open recorded trace"),
            ("evaluation_url", "Open recorded evaluation"),
        ):
            link = _safe_url(weave.get(key))
            if link:
                parts.append(
                    f'<p><a href="{_text(link)}" rel="noopener noreferrer">{label}</a></p>'
                )
    else:
        parts.append(
            '<p class="missing">'
            + (
                "Explicit local-only execution · not sponsor-integrated."
                if config.get("require_weave") is False
                else "No verified Weave connection; no sponsor-integration claim."
            )
            + "</p>"
        )
    parts.append(
        f"<p>Actual usage complete: {_text(usage.get('complete'))}. Token counts are not dollars; no price or cost saving is inferred.</p>"
    )
    for key, title in (
        ("initial_report", "Original · development"),
        ("final_report", "Frozen final · development"),
        ("holdout_report", "Frozen final · holdout"),
    ):
        parts.append(_gates(run.get(key), title))
    parts.append(
        '<section><h3>Evaluator captures · inert PNG only</h3><p>Captures show appearance, not keyboard correctness. Gates carry the behavioral evidence.</p><div class="captures">'
    )
    for phase in ("original", "final"):
        image = _png(screenshots.get(f"{mode}:{phase}"))
        # Supplied screenshots have already been source-hash-verified by the caller.
        parts.append(
            f"<figure><figcaption>{phase.title()} · evaluator PNG</figcaption>"
            + (
                f'<img src="{image}" alt="{mode_label} {phase} source captured by the isolated evaluator">'
                if image
                else '<p class="missing">No verified capture supplied. No substitute preview is generated.</p>'
            )
            + "</figure>"
        )
    parts.append(
        "</div></section><section><h3>Source evidence</h3><p>Source is escaped, never executed. Sensitive text may be redacted; recorded hashes refer to the original unredacted bytes.</p>"
    )
    original, final = run.get("original_source"), run.get("final_source")
    for title, source in (
        ("Before · original source", original),
        ("After · recorded final source", final),
    ):
        parts.append(
            f'<details><summary>{title}</summary><pre class="source">{_text(source)}</pre></details>'
        )
    diff = (
        "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                final.splitlines(keepends=True),
                fromfile="original/behavior.js",
                tofile="final/behavior.js",
            )
        )
        if isinstance(original, str) and isinstance(final, str)
        else None
    )
    parts.append(
        f'<details><summary>Net source patch</summary><pre class="source">{_text(diff if diff is None or diff else "No net source change.")}</pre></details>'
    )
    for iteration in _list(run.get("iterations")):
        if not isinstance(iteration, dict):
            continue
        parts.append(
            f"<details><summary>Attempt {_text(iteration.get('number'))} · accepted: {_text(iteration.get('accepted'))}</summary>"
            f'<p>{_text(iteration.get("decision"))}</p><pre class="source">{_text(iteration.get("patch"))}</pre>'
            f'<pre class="source">{_text(iteration.get("diff"))}</pre></details>'
        )
    parts.append("</section></article>")
    return "".join(parts)


_STYLE = """
:root{color-scheme:light;--ink:#172332;--muted:#536171;--line:#ccd5de;--blue:#194ee7;--paper:#f2f5f8}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1180px;margin:auto;padding:44px 28px}h1,h2,h3,p{margin:0 0 16px}h1{font-size:clamp(2.6rem,7vw,5rem);letter-spacing:-.065em;line-height:1.05}h2{font-size:2.4rem;letter-spacing:-.04em}h3{font-size:1.15rem}a{color:#1542bd;overflow-wrap:anywhere}.eyebrow{text-transform:uppercase;font-size:.76rem;font-weight:800;letter-spacing:.16em;color:var(--blue)}.hero{border-top:6px solid var(--ink);padding-top:28px}.lede{max-width:760px;color:var(--muted);font-size:1.1rem}.verdict{background:var(--ink);color:white;padding:28px;margin:30px 0}.verdict h2{font-size:2rem}.verdict.unavailable{background:#693715}.verdict p:last-child{margin-bottom:0}.caveat{font-size:.9rem;opacity:.88}.section,.run{background:white;border:1px solid var(--line);padding:28px;margin:24px 0}.run{border-top:4px solid var(--blue)}.run header{border-bottom:1px solid var(--line);margin-bottom:20px}.mono{font-family:ui-monospace,monospace;font-size:.82rem;overflow-wrap:anywhere}.provenance{display:flex;gap:32px;flex-wrap:wrap}.provenance dt{font-weight:700}.provenance dd{margin:0;color:var(--muted);overflow-wrap:anywhere}.evaluation{border-top:1px solid var(--line);padding-top:22px;margin-top:24px}.gate-score{font-size:1.2rem;font-weight:800}.missing,.fail{color:#8b351e}.pass{color:#155c44}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:.86rem}caption{text-align:left;font-weight:700;margin-bottom:12px}th,td{padding:11px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);overflow-wrap:anywhere}thead{background:#edf1f6}th{font-weight:650}td pre{margin:0;font-size:.75rem;max-width:440px}pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}.source{background:#f3f5f8;padding:18px;font-size:.8rem;line-height:1.5}details{margin:16px 0;border:1px solid var(--line);padding:12px 16px}summary{cursor:pointer;font-weight:650}.captures{display:grid;grid-template-columns:1fr 1fr;gap:20px}figure{margin:0;background:var(--paper);padding:12px}figcaption{font-weight:650;font-size:.85rem;margin-bottom:10px}img{display:block;width:100%;height:auto;border:1px solid var(--line)}footer{font-size:.85rem;color:var(--muted);border-top:1px solid var(--line);padding-top:20px}.limits li{margin-bottom:10px}
@media(max-width:650px){main{padding:24px 14px}.section,.run,.verdict{padding:20px}.captures{grid-template-columns:1fr}th,td{padding:8px}}
@media print{@page{margin:16mm}body{background:white;font-size:10pt}main{max-width:none;padding:0}.hero{padding-top:12px}h1{font-size:38pt}.section,.run{border:0;padding:0;margin:22px 0}.run{break-before:page}.verdict{background:white!important;color:black;border:2px solid black;padding:16px}details{border:0;padding:0}details::details-content{display:block!important;content-visibility:visible!important}details>*{display:block!important}summary{list-style:none}table{font-size:8pt}thead{display:table-header-group}tr,figure,h2,h3{break-inside:avoid}pre{font-size:8pt!important}.table-scroll{overflow:visible}a::after{content:" (" attr(href) ")";font-size:8pt}img{max-height:90mm;object-fit:contain}footer{break-inside:avoid}}
"""


def render_report(comparison: dict, screenshots: dict[str, str] | None = None) -> str:
    """Render a standalone report; PNG arguments must be verified against report hashes.

    No file reads, environment inspection, network calls, candidate execution or dependencies.
    The JSON export is allowlisted and redacted, and is not a substitute for raw sealed records.
    """
    assessment = assess_comparison(comparison)
    comparison = _mapping(comparison)
    selected = {key: comparison[key] for key in _COMPARISON_FIELDS if key in comparison}
    selected["results"] = [
        {key: run[key] for key in _RUN_FIELDS if key in run}
        for run in _list(comparison.get("results"))
        if isinstance(run, dict)
    ]
    evidence = _sanitize(selected)
    evidence["assessment"] = assessment
    evidence["export"] = {
        "format": "keyproof-portable-evidence-v1",
        "redacted": True,
        "note": "Artifact paths, unsupported links and credential-like text omitted. Hashes describe unredacted source. No signature or independent attestation is claimed.",
    }
    for original_run, clean_run in zip(selected["results"], evidence["results"], strict=True):
        clean_run["source_hashes"] = {
            key: hashlib.sha256(original_run[key].encode()).hexdigest()
            for key in ("original_source", "final_source")
            if isinstance(original_run.get(key), str)
        }
    verdict = assessment["verdict"]
    labels = {
        "tie": "Tie · both full contracts pass",
        "team": "Team · only full-contract pass",
        "single": "Single · only full-contract pass",
        "neither": "Neither · no full-contract repair",
        "unavailable": "Unavailable · no verified paired outcome",
    }
    identifier = _text(evidence.get("comparison_id"))
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; img-src data:; style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">'
        f"<title>Keyproof · evidence {identifier}</title><style>{_STYLE}</style></head><body><main>"
        '<header class="hero"><p class="eyebrow">Keyproof / portable evidence</p><h1>Claims end here.<br>Evidence starts here.</h1>'
        '<p class="lede">A recorded team-versus-single repair experiment on an owned keyboard contract. Independent development and sealed holdout gates—not a model’s self-assessment—decide the outcome.</p>'
        f'<p class="mono">Comparison {identifier} · {_text(evidence.get("status"))}</p>'
        "<p>Downloaded evidence snapshot · not a live execution. Fully readable offline; external trace links require a connection.</p></header>"
        f'<section class="verdict {verdict}" aria-label="Authoritative assessment"><h2>{labels[verdict]}</h2><p>{_text(assessment["reason"])}</p><p class="caveat">{_text(assessment["caveat"])}</p></section>'
    ]
    parts.append(
        '<section class="section"><h2>Experiment receipt</h2><dl class="provenance">'
        + "".join(
            f"<div><dt>{label}</dt><dd>{_text(evidence.get(key))}</dd></div>"
            for key, label in (
                ("started_at", "Pair started"),
                ("frozen_at", "Both candidates frozen"),
                ("finished_at", "Pair finished"),
            )
        )
        + "</dl>"
        f"<p>Recorded protocol: {_text(evidence.get('protocol'))}</p><p>Comparison errors: {_text(evidence.get('errors'))}</p>"
    )
    modes = {
        _mapping(run.get("config")).get("mode"): run
        for run in evidence["results"]
        if _mapping(run.get("config")).get("mode") in ("team", "single")
    }
    parts.append(
        '<div class="table-scroll"><table><caption>Configured ceilings and actual consumption · equal limits do not imply equal spend</caption><thead><tr><th>Setting / metric</th><th>Team</th><th>Single</th></tr></thead><tbody>'
    )
    for section, key, label in (
        ("config", "provider", "Provider"),
        ("config", "model", "Explicit model"),
        ("config", "require_weave", "Weave required"),
        ("config", "max_iterations", "Iteration ceiling"),
        ("config", "max_model_calls", "Model-call ceiling"),
        ("config", "max_input_tokens", "Aggregate input-token ceiling"),
        ("config", "max_output_tokens", "Aggregate output-token ceiling"),
        ("usage", "calls", "Actual model calls"),
        ("usage", "input_tokens", "Actual input tokens"),
        ("usage", "output_tokens", "Actual output tokens"),
        ("usage", "cached_input_tokens", "Cached input tokens (optional)"),
        ("usage", "elapsed_ms", "Actual model-request time (ms)"),
        ("usage", "complete", "Metering complete"),
    ):
        cells = "".join(
            f"<td>{_text(_mapping(modes.get(mode, {}).get(section)).get(key))}</td>"
            for mode in ("team", "single")
        )
        parts.append(f'<tr><th scope="row">{label}</th>{cells}</tr>')
    parts.append(
        "</tbody></table></div><p>Unknown usage is never zero. Budget exhaustion can be a measured outcome, but model errors, interruptions and incomplete metering invalidate the pair. Resource usage does not break a correctness tie.</p></section>"
    )
    for run in evidence["results"]:
        parts.append(_run(run, screenshots or {}, comparable=assessment["comparable"]))
    if not evidence["results"]:
        parts.append(
            '<section class="section"><h2>No run evidence available</h2><p>No completed outcome, source, usage or screenshot is invented.</p></section>'
        )
    parts.append(
        '<section class="section limits"><h2>What this evidence does not claim</h2><ul>'
        "<li>This is one pair on a curated, owned synthetic fixture—not a statistical benchmark, production-site audit, WCAG certification or validation by disabled users.</li>"
        "<li>The repair agents can propose source patches, not evaluator verdicts. Holdout follows both freezes; candidates cannot be revised using holdout feedback.</li>"
        "<li>The evaluator executes in the existing OS network-isolated browser boundary with independent contract checks. Finite scripted actions and bounded quiet windows cannot prove the absence of every delayed or untested behavior.</li>"
        "<li>All gates must pass in both final development and holdout for a full repair. A small gate-count lead is not a quality win; lower spend is reported separately.</li>"
        "<li>Only supplied, source-hash-matched evaluator PNGs appear here. No candidate preview, candidate script, iframe, remote asset or executable fallback is included.</li>"
        "<li>Local-only runs are explicitly not sponsor-integrated. A recorded project URL is not a verified trace or evaluation URL. Nothing is published by opening this report.</li>"
        "<li>Hashes bind source snapshots to reports; they are not signatures proving the origin of an arbitrary evidence file. Exported text may be redacted to omit local paths and credential-like strings.</li>"
        "</ul></section>"
    )
    encoded = (
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    parts.append(
        '<section class="section"><h2>Machine-readable receipt</h2><p>The inert JSON block below contains the sanitized recorded evidence and authoritative assessment. Screenshots are embedded above, not repeated in JSON. Expand to inspect or extract the element with id <code>keyproof-evidence</code>.</p>'
        f'<details><summary>Inspect exported JSON evidence</summary><pre class="source">{html.escape(encoded)}</pre></details></section>'
        f'<script id="keyproof-evidence" type="application/json">{encoded}</script>'
        "<footer>Keyproof · evidence-first repair arena. This standalone document has no executable scripts or external dependencies. Print to PDF using your browser; expand details first if your browser does not print closed disclosure content.</footer>"
        "</main></body></html>"
    )
    return "".join(parts)
