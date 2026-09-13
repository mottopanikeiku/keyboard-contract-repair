"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const active = (run) => run && (run.status === "queued" || run.status === "running");
  const terminal = (run) => run && !active(run);
  const knownUsage = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const sourceHashes = new Map();
  const previews = new Map();
  const state = {
    status: null,
    statusFresh: false,
    runs: new Map(),
    pendingIds: new Set(),
    selectedId: null,
    comparisonId: null,
    comparison: null,
    starting: false,
    refreshing: false,
    errors: new Map(),
    signatures: new Map(),
    disclosures: new Map(),
    announced: "",
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function human(value) {
    return String(value ?? "Unknown").replaceAll("_", " ");
  }

  function exact(value) {
    if (value === null || value === undefined) return "Unknown / not reported";
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  function tag(text, kind = "") {
    return element("span", `tag ${kind}`, text);
  }

  function notice(text, kind = "") {
    return element("p", `notice ${kind}`, text);
  }

  function dateText(value) {
    if (!value) return "Not reported";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
  }

  function disclosure(label, key, open = false) {
    const node = element("details");
    node.open = state.disclosures.has(key) ? state.disclosures.get(key) : open;
    node.append(element("summary", "", label));
    node.addEventListener("toggle", () => state.disclosures.set(key, node.open));
    return node;
  }

  function dataDisclosure(label, value, key) {
    const node = disclosure(label, key);
    node.className = "event-data";
    node.append(element("pre", "", exact(value)));
    return node;
  }

  function mount(id, signature, build) {
    const serialized = JSON.stringify(signature);
    if (state.signatures.get(id) === serialized) return;
    const container = $(id);
    const scrollPositions = Array.from(container.querySelectorAll(".timeline, .source, .diff, .table-scroll"), (node) => [node.scrollTop, node.scrollLeft]);
    container.replaceChildren(build());
    Array.from(container.querySelectorAll(".timeline, .source, .diff, .table-scroll")).forEach((node, index) => {
      if (scrollPositions[index]) [node.scrollTop, node.scrollLeft] = scrollPositions[index];
    });
    state.signatures.set(id, serialized);
  }

  function weaveLink(value, label = "Open Weave trace") {
    if (typeof value !== "string") return null;
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || url.username || url.password) return null;
      if (url.hostname !== "wandb.ai" && !url.hostname.endsWith(".wandb.ai")) return null;
      const link = element("a", "", label);
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    } catch {
      return null;
    }
  }

  function renderErrors() {
    $("request-errors").hidden = state.errors.size === 0;
    $("request-error-messages").replaceChildren(...Array.from(state.errors, ([area, message]) => element("p", "", `${area}: ${message}`)));
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
      signal: AbortSignal.timeout(20000),
      cache: "no-store",
    });
    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error(`Server returned a non-JSON response (${response.status}).`);
    }
    if (!response.ok) {
      throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status}): ${exact(body.detail ?? body)}`);
    }
    return body;
  }

  function failure(area, error) {
    const message = error.name === "TimeoutError" ? "The server did not respond within 20 seconds. Evidence is retained; retry the connection." : error.message || String(error);
    state.errors.set(area, message);
  }

  function isBusy() {
    return state.starting || state.pendingIds.size > 0 || Boolean(state.status?.busy) || Array.from(state.runs.values()).some(active);
  }

  function renderReadiness() {
    const status = state.status;
    const provider = $("provider").value;
    const available = status && (provider === "codex" ? status.provider.codex_available : status.provider.openai_configured);
    const providerName = provider === "codex" ? "Codex CLI" : "OpenAI API";
    $("provider-status").textContent = !status ? "Model provider: not connected" : `${providerName}: ${available ? "available" : "unavailable"}${state.statusFresh ? "" : " (last known)"}`;
    $("provider-dot").className = `status-dot ${available && state.statusFresh ? "ready" : "blocked"}`;
    const weave = status?.weave;
    $("weave-status").textContent = !status ? "Weave: not connected" : weave.enabled ? `Weave: enabled${weave.project ? ` · ${weave.project}` : ""}${state.statusFresh ? "" : " (last known)"}` : "Weave: blocked / not configured";
    $("weave-dot").className = `status-dot ${weave?.enabled && state.statusFresh ? "ready" : "blocked"}`;
    const link = weaveLink(weave?.url, "Open project");
    $("status-weave-link").replaceChildren(...(link ? [link] : []));
    $("local-choice").hidden = !status || Boolean(weave.enabled);
    $("weave-error").textContent = weave?.error || "";
    if (status?.fixture) {
      $("fixture-title").textContent = status.fixture.title;
      $("fixture-task").textContent = status.fixture.task;
    }
    let blocker = "Ready. Independent evaluator gates decide which changes are kept.";
    let blocked = false;
    if (!state.statusFresh) {
      blocker = "A current server readiness check is required before starting.";
      blocked = true;
    } else if (isBusy()) {
      blocker = "A run or comparison is in progress. New runs are paused to avoid overlap.";
      blocked = true;
    } else if (!available) {
      blocker = `${providerName} is unavailable. Select a configured provider or configure it on the server.`;
      blocked = true;
    } else if (!weave.enabled && !$("local-only").checked) {
      blocker = "Weave is unavailable. Explicitly allow local-only execution to continue without sponsor integration.";
      blocked = true;
    } else if (!weave.enabled) {
      blocker = "Ready for local-only execution. No sponsor-integrated Weave trace will be created.";
    }
    $("run-blocker").textContent = blocker;
    $("run-repair").disabled = blocked;
    $("run-comparison").disabled = blocked;
    $("busy-status").textContent = !state.statusFresh ? "Connection unavailable" : isBusy() ? "Run in progress" : "Ready for a run";
  }

  function selectRun(id) {
    state.selectedId = id;
    renderHistory();
    renderRun();
    refresh();
  }

  function renderHistory() {
    const runs = Array.from(state.runs.values()).sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)));
    $("history-count").textContent = String(runs.length);
    mount("history", [state.selectedId, runs.map((run) => [run.run_id, run.status, run.config.mode, run.started_at])], () => {
      if (!runs.length) return element("p", "muted", "No runs yet. Your retained runs will appear here.");
      const list = element("div", "history-list");
      for (const run of runs) {
        const button = element("button", "history-item");
        button.type = "button";
        button.setAttribute("aria-pressed", String(run.run_id === state.selectedId));
        const top = element("span", "history-item-top");
        top.append(element("span", "", `${human(run.config.mode)} repair`), tag(human(run.status), run.status));
        button.append(top, element("small", "", dateText(run.started_at)), element("small", "mono", run.run_id));
        button.addEventListener("click", () => selectRun(run.run_id));
        list.append(button);
      }
      return list;
    });
  }

  function renderReport(report, label, key, pendingText) {
    const block = element("section", "report-block");
    const heading = element("div", "report-heading");
    heading.append(element("h3", "", label), report ? tag(report.passed ? "Pass" : "Fail", report.passed ? "pass" : "fail") : tag("Not evaluated", "pending"));
    block.append(heading);
    if (!report) {
      block.append(element("p", "muted", pendingText));
      return block;
    }
    if (report.gates.length) {
      const scroll = element("div", "table-scroll");
      const table = element("table", "gate-table");
      table.setAttribute("aria-label", `${label} gate results`);
      const thead = element("thead");
      const header = element("tr");
      for (const title of ["Gate", "Verdict", "Observed evidence"]) {
        const cell = element("th", "", title);
        cell.scope = "col";
        header.append(cell);
      }
      thead.append(header);
      const tbody = element("tbody");
      for (const gate of report.gates) {
        const row = element("tr");
        const name = element("th", "", human(gate.name));
        name.scope = "row";
        const result = element("td");
        result.append(tag(gate.passed ? "Pass" : "Fail", gate.passed ? "pass" : "fail"));
        const evidence = element("td");
        if (gate.detail) evidence.append(element("p", "gate-detail", gate.detail));
        evidence.append(element("p", "gate-value", `Expected: ${exact(gate.expected)}`), element("p", "gate-value", `Actual: ${exact(gate.actual)}`));
        row.append(name, result, evidence);
        tbody.append(row);
      }
      table.append(thead, tbody);
      scroll.append(table);
      block.append(scroll);
    } else {
      block.append(element("p", "muted", "No individual gates were reported. Read evaluator errors before interpreting this result."));
    }
    block.append(element("div", "report-meta mono", `Phase: ${report.phase} · Elapsed: ${exact(report.elapsed_ms)} ms · Source hash: ${report.source_hash}`));
    const extra = element("div", "report-extra");
    if (report.errors.length) extra.append(errorList(report.errors));
    if (report.axe_violations.length) extra.append(dataDisclosure("Reported axe violations", report.axe_violations, `${key}:axe`));
    if (report.actions.length) extra.append(dataDisclosure("Evaluator action evidence", report.actions, `${key}:actions`));
    if (report.artifacts.length) extra.append(dataDisclosure("Retained artifact paths", report.artifacts, `${key}:artifacts`));
    if (extra.childNodes.length) block.append(extra);
    return block;
  }

  function errorList(errors) {
    const wrapper = element("div", "notice error");
    wrapper.append(element("strong", "", "Reported errors"));
    const list = element("ul", "error-list");
    errors.forEach((error) => list.append(element("li", "", error)));
    wrapper.append(list);
    return wrapper;
  }

  function renderOverview(run) {
    const fragment = document.createDocumentFragment();
    fragment.append(element("h2", "", `${human(run.config.mode)} repair / ${human(run.status)}`));
    const meta = element("div", "run-meta");
    meta.append(element("span", "run-id mono", run.run_id), element("span", "", `Started ${dateText(run.started_at)}`));
    if (run.finished_at) meta.append(element("span", "", `Finished ${dateText(run.finished_at)}`));
    fragment.append(meta);
    if (active(run)) {
      const last = run.events.at(-1);
      fragment.append(notice(last ? `In progress · ${last.role}: ${last.summary}` : "Queued. Waiting for the first recorded event; no result is available yet."));
    } else if (run.status === "failed") {
      fragment.append(notice("The run failed. Retained observations and proposals remain below; failure is not a successful repair.", "error"));
    } else if (run.status === "budget_exhausted") {
      fragment.append(notice("The run reached its budget. Inspect the frozen candidate and independent gates; exhaustion is not a success verdict.", "warning"));
    } else {
      fragment.append(notice("Execution completed. Read development and heldout gate results separately; completion alone does not mean the keyboard contract passed."));
    }
    const weave = element("p", "muted");
    if (run.weave.enabled) {
      weave.append(document.createTextNode(`Weave enabled${run.weave.project ? ` · ${run.weave.project}` : ""}. `));
      const link = weaveLink(run.weave.url);
      weave.append(link || document.createTextNode("Trace URL not available."));
    } else {
      weave.textContent = run.config.require_weave ? "Weave required, but not enabled for this record. No sponsor-integration claim." : "Explicit local-only execution · not sponsor-integrated.";
    }
    fragment.append(weave);
    if (run.weave.error) fragment.append(notice(`Weave: ${run.weave.error}`, "warning"));
    if (run.errors.length) fragment.append(errorList(run.errors));
    fragment.append(dataDisclosure("Exact run configuration", run.config, `${run.run_id}:config`));
    return fragment;
  }

  function renderTimeline(run) {
    if (!run.events.length) return element("p", "muted", active(run) ? "Waiting for recorded activity…" : "No events were recorded.");
    const list = element("ol", "timeline");
    for (const event of run.events) {
      const item = element("li");
      const meta = element("div", "event-meta");
      meta.append(element("span", "mono", `#${event.sequence}`), tag(event.role), element("span", "", human(event.kind)), element("time", "", dateText(event.timestamp)));
      item.append(meta, element("p", "event-summary", event.summary));
      if (Object.keys(event.data).length) item.append(dataDisclosure("Event data", event.data, `${run.run_id}:event:${event.sequence}`));
      list.append(item);
    }
    return list;
  }

  function renderUsage(run) {
    const fragment = document.createDocumentFragment();
    const list = element("dl", "usage-list");
    for (const [field, label, suffix] of [
      ["calls", "Model calls", ""],
      ["input_tokens", "Input tokens", ""],
      ["output_tokens", "Output tokens", ""],
      ["cached_input_tokens", "Cached input tokens", ""],
      ["elapsed_ms", "Reported elapsed time", " ms"],
    ]) {
      const value = run.usage[field];
      const unknown = !knownUsage(value);
      const row = element("div");
      row.append(element("dt", "", label), element("dd", unknown ? "unknown" : "", unknown ? "Unknown / not reported" : `${value}${suffix}`));
      list.append(row);
    }
    fragment.append(list, element("p", "field-note", "Unknown values stay unknown, never zero-filled. Tokens are not converted to cost, energy, or carbon estimates."));
    return fragment;
  }

  function renderDiff(diff) {
    const pre = element("pre", "diff");
    pre.setAttribute("aria-label", "Proposed source diff");
    for (const line of diff.split("\n")) {
      let kind = "";
      if (line.startsWith("+") && !line.startsWith("+++")) kind = "diff-add";
      else if (line.startsWith("-") && !line.startsWith("---")) kind = "diff-remove";
      else if (line.startsWith("@@")) kind = "diff-hunk";
      pre.append(element("span", `diff-line ${kind}`, line));
    }
    return pre;
  }

  function renderIterations(run) {
    const fragment = document.createDocumentFragment();
    if (!run.iterations.length) {
      fragment.append(element("p", "muted", active(run) ? "No candidate has been recorded yet." : "No repair iterations were recorded."));
      return fragment;
    }
    for (const iteration of run.iterations) {
      const key = `${run.run_id}:iteration:${iteration.number}`;
      const decided = Boolean(iteration.decision) || terminal(run);
      const verdict = iteration.accepted ? "Accepted" : decided ? "Not accepted / reverted" : "Pending decision";
      const details = disclosure(`Iteration ${iteration.number}`, key, run.iterations.length === 1);
      details.className = "iteration";
      details.firstChild.append(tag(verdict, iteration.accepted ? "accepted" : decided ? "reverted" : "pending"));
      if (iteration.decision) details.append(element("p", "iteration-summary", iteration.decision));
      if (iteration.audit) {
        details.append(element("h3", "", "Audit"), element("p", "iteration-summary", iteration.audit.summary));
        if (iteration.audit.failures.length) details.append(dataDisclosure("Audit-reported failures", iteration.audit.failures, `${key}:failures`));
        if (iteration.audit.observations.length) details.append(dataDisclosure("Audit observations", iteration.audit.observations, `${key}:observations`));
      }
      if (iteration.patch) {
        details.append(element("h3", "", "Patch proposal"), element("p", "iteration-summary", iteration.patch.summary));
        details.append(dataDisclosure("Exact bounded text edits", iteration.patch.edits, `${key}:edits`));
      }
      details.append(iteration.diff ? renderDiff(iteration.diff) : element("p", "muted", "No source diff was recorded for this iteration."));
      details.append(renderReport(iteration.report, `Candidate ${iteration.number} / development`, `${key}:report`, "This candidate has no evaluator report. No passing result is implied."));
      if (iteration.candidate_source !== null && iteration.candidate_source !== undefined) {
        const source = disclosure("Candidate source snapshot", `${key}:source`);
        source.append(element("pre", "source", iteration.candidate_source));
        details.append(source);
      }
      fragment.append(details);
    }
    return fragment;
  }

  function hashForSource(source) {
    if (typeof source !== "string" || !source) return { status: "ready", hash: null };
    if (sourceHashes.has(source)) return sourceHashes.get(source);
    const evidence = { status: "pending", hash: null };
    sourceHashes.set(source, evidence);
    Promise.resolve().then(() => crypto.subtle.digest("SHA-256", new TextEncoder().encode(source))).then((bytes) => {
      evidence.hash = Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
      evidence.status = "ready";
    }).catch(() => {
      evidence.status = "unavailable";
    }).finally(() => {
      renderRun();
      renderComparison();
    });
    return evidence;
  }

  function hashesFor(run) {
    const original = hashForSource(run.original_source);
    const final = hashForSource(run.final_source);
    return {
      originalHash: original.hash,
      finalHash: final.hash,
      status: original.status === "ready" && final.status === "ready" ? "ready" :
        original.status === "unavailable" || final.status === "unavailable" ? "unavailable" : "pending",
    };
  }

  function frozen(run, hashes) {
    return Boolean(terminal(run) && run.final_report && hashes?.finalHash &&
      run.final_report.source_hash === hashes.finalHash &&
      run.events.some((event) => event.role === "controller" && event.kind === "frozen"));
  }

  function clearPreview(id, message) {
    const prior = previews.get(id);
    prior?.controller.abort();
    if (prior?.url) URL.revokeObjectURL(prior.url);
    previews.delete(id);
    const image = $(id);
    image.onload = null;
    image.onerror = null;
    image.hidden = true;
    image.removeAttribute("src");
    $(`${id}-link`).hidden = true;
    $(`${id}-link`).removeAttribute("href");
    $(`${id}-empty`).hidden = false;
    $(`${id}-empty`).textContent = message;
  }

  async function setPreview(id, run, phase, report, eligible, missing) {
    if (!eligible) {
      clearPreview(id, missing);
      return;
    }
    const key = `${run.run_id}:${report.source_hash}`;
    const prior = previews.get(id);
    if (prior?.key === key && (prior.loading || prior.url)) return;
    clearPreview(id, prior?.key === key ? prior.message : "Loading the evaluator PNG. No capture is displayed yet.");
    const entry = { key, controller: new AbortController(), loading: true, url: null };
    previews.set(id, entry);
    const current = () => previews.get(id) === entry;
    const unavailable = (message) => {
      if (!current()) return;
      clearPreview(id, message);
      previews.set(id, { ...entry, loading: false, url: null, message });
    };
    try {
      const response = await fetch(`/api/preview/${encodeURIComponent(run.run_id)}/${phase}`, {
        headers: { Accept: "image/png" },
        signal: AbortSignal.any([entry.controller.signal, AbortSignal.timeout(20000)]),
        cache: "no-store",
        redirect: "error",
      });
      if (!current()) return;
      if (response.status === 409) {
        unavailable("No evaluator capture is available for this source yet. No substitute preview is shown.");
        return;
      }
      if (!response.ok) throw new Error(`Capture request failed (${response.status}).`);
      if (response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "image/png") {
        throw new Error("The server did not return a PNG capture.");
      }
      const blob = await response.blob();
      if (!current()) return;
      entry.url = URL.createObjectURL(blob);
      const image = $(id);
      image.alt = `${phase === "original" ? "Original fixture" : "Frozen final candidate"} in run ${run.run_id}, captured by the isolated evaluator. Keyboard outcomes are reported in the gates, not this image.`;
      image.onload = () => {
        if (!current()) return;
        entry.loading = false;
        image.hidden = false;
        $(`${id}-empty`).hidden = true;
        const link = $(`${id}-link`);
        link.href = entry.url;
        link.hidden = false;
      };
      image.onerror = () => unavailable("The evaluator PNG could not be decoded. No capture is displayed.");
      image.src = entry.url;
    } catch (error) {
      if (current()) unavailable(`No capture is displayed. ${error.name === "TimeoutError" ? "The capture request timed out." : error.message}`);
    }
  }

  function renderRun() {
    const run = state.runs.get(state.selectedId);
    if (!run) {
      clearPreview("original-preview", "No original capture is available for the selected run yet.");
      clearPreview("final-preview", "No verified frozen final capture is available for the selected run yet.");
      $("run-evidence").hidden = true;
      if (state.selectedId) {
        $("selection-status").textContent = "Loading selected run";
        mount("run-overview", [state.selectedId, "pending"], () => element("p", "muted", "Waiting for the selected run record. No prior run evidence is shown as this run."));
      }
      return;
    }
    const hashes = hashesFor(run);
    const hasFrozen = frozen(run, hashes);
    $("run-evidence").hidden = false;
    $("selection-status").textContent = human(run.status);
    $("selection-status").className = `tag ${run.status}`;
    mount("run-overview", [run.run_id, run.status, run.config, run.started_at, run.finished_at, run.events.at(-1), run.weave, run.errors], () => renderOverview(run));
    mount("reports", [run.run_id, run.status, run.initial_report, run.final_report, run.holdout_report, hasFrozen], () => {
      const fragment = document.createDocumentFragment();
      fragment.append(
        renderReport(run.initial_report, "Original / development", `${run.run_id}:initial`, active(run) ? "Waiting for the original fixture evaluation." : "No initial evaluation is available."),
        renderReport(run.final_report, hasFrozen ? "Frozen final / development" : "Final / development (freeze not verified)", `${run.run_id}:final`, active(run) ? "The candidate is not yet frozen. Per-iteration evaluations appear with their decisions below." : "No frozen final development evaluation is available."),
        renderReport(run.holdout_report, hasFrozen ? "Frozen final / heldout" : "Final / heldout (freeze not verified)", `${run.run_id}:holdout`, active(run) ? "Not evaluated. Heldout checks wait until the candidate is frozen." : "Not evaluated or unavailable. A comparison waits for both candidates to freeze. No heldout claim can be made without this report."),
      );
      return fragment;
    });
    $("event-count").textContent = `${run.events.length} recorded`;
    mount("timeline", [run.run_id, run.status, run.events], () => renderTimeline(run));
    mount("usage", [run.run_id, run.usage], () => renderUsage(run));
    mount("iterations", [run.run_id, run.status, run.iterations], () => renderIterations(run));
    if ($("original-source").textContent !== run.original_source) $("original-source").textContent = run.original_source || "No original source was recorded.";
    $("final-source-title").textContent = hasFrozen ? "Frozen final behavior.js" : "Recorded behavior.js / freeze not verified";
    if ($("final-source").textContent !== run.final_source) $("final-source").textContent = run.final_source || "No final source was recorded.";
    setPreview("original-preview", run, "original", run.initial_report,
      Boolean(run.initial_report && hashes.originalHash && run.initial_report.source_hash === hashes.originalHash),
      "No source-matched original evaluator capture is available yet.");
    setPreview("final-preview", run, "final", run.final_report, hasFrozen,
      active(run) ? "Waiting for a verified frozen final evaluation. Live proposals are not shown." : "No verified frozen final evaluator capture is available for this run.");
    const announcement = `${run.run_id}:${run.status}`;
    if (announcement !== state.announced) {
      $("announcer").textContent = `${human(run.config.mode)} repair ${human(run.status)}. ${run.events.length} events recorded.`;
      state.announced = announcement;
    }
  }

  function renderComparison() {
    $("comparison-panel").hidden = !state.comparisonId;
    if (!state.comparisonId) return;
    const comparison = state.comparison;
    if (!comparison) {
      mount("comparison-content", [state.comparisonId, "pending"], () => element("p", "muted", "Comparison accepted. Waiting for the two actual run records…"));
      return;
    }
    const hashes = comparison.results.map(hashesFor);
    mount("comparison-content", [comparison, hashes], () => {
      const fragment = document.createDocumentFragment();
      fragment.append(element("p", "comparison-status", `Comparison ${comparison.comparison_id} · ${human(comparison.status)}`));
      const team = comparison.results.find((run) => run.config.mode === "team");
      const single = comparison.results.find((run) => run.config.mode === "single");
      const configFields = ["provider", "model", "max_iterations", "max_model_calls", "max_output_tokens", "max_input_tokens", "require_weave"];
      const limits = ["max_iterations", "max_model_calls", "max_output_tokens", "max_input_tokens"];
      const configKnown = (run) => run && typeof run.config.provider === "string" && Boolean(run.config.provider.trim()) &&
        typeof run.config.model === "string" && Boolean(run.config.model.trim()) &&
        typeof run.config.require_weave === "boolean" &&
        limits.every((key) => knownUsage(run.config[key]) && run.config[key] > 0);
      const knownConfig = Boolean(configKnown(team) && configKnown(single));
      const matched = Boolean(knownConfig && configFields.every((key) => team.config[key] === single.config[key]));
      const reports = (run) => [run.initial_report, run.final_report, run.holdout_report].filter(Boolean);
      const sourceMismatch = (run) => {
        const evidence = hashesFor(run);
        return evidence.status === "ready" && [
          [run.initial_report, evidence.originalHash],
          [run.final_report, evidence.finalHash],
          [run.holdout_report, evidence.finalHash],
        ].some(([report, hash]) => report && (!hash || report.source_hash !== hash));
      };
      let result = "No data / awaiting independent heldout reports";
      let description = "No win, tie, or loss can be assigned before both frozen candidates have independent heldout evidence.";
      if (active(comparison) || comparison.results.some(active)) {
        result = "Unavailable / comparison in progress";
        description = "Queued or running records are not final evidence. Wait for both frozen candidates and completed independent evaluations.";
      } else if (comparison.status === "failed" || comparison.errors?.length ||
        comparison.results.some((run) => run.status === "failed" || run.errors?.length)) {
        result = "Unavailable / comparison or run errors";
        description = "Execution errors cannot establish a repair-mode win, tie, or loss. Inspect the failed records and their retained evidence.";
      } else if (comparison.results.some((run) => reports(run).some((report) => report.errors?.length))) {
        result = "Unavailable / evaluator errors";
        description = "An evaluator report contains errors. An evaluator failure is not evidence of a repair-mode win or loss.";
      } else if (team && single && !knownConfig) {
        result = "Unknown / configuration not fully reported";
        description = "An explicit shared model, provider, integration setting, and finite positive resource limits are required for a resource-matched verdict.";
      } else if (team && single && !matched) {
        result = "No fair verdict / configuration mismatch";
        description = "The recorded configurations are not matched. These results cannot be presented as a fair paired comparison.";
      } else if (team && single && (!team.original_source || team.original_source !== single.original_source)) {
        result = "No fair verdict / original source mismatch";
        description = "The runs must start from the same known recorded source. Matched resource limits alone are insufficient.";
      } else if (comparison.results.some(sourceMismatch)) {
        result = "No fair verdict / evaluator source mismatch";
        description = "An evaluator source hash does not match its recorded source snapshot. These reports cannot establish a frozen-candidate outcome.";
      } else if (comparison.status !== "completed" || !team || !single ||
        comparison.results.length !== 2 || comparison.run_ids.length !== 2 ||
        team.run_id === single.run_id || !comparison.run_ids.includes(team.run_id) || !comparison.run_ids.includes(single.run_id)) {
        result = "Unavailable / paired records incomplete";
        description = "A completed comparison must identify exactly one team record and one single record.";
      } else if (!["completed", "budget_exhausted"].includes(team.status) || !["completed", "budget_exhausted"].includes(single.status) ||
        hashes.some((evidence) => evidence.status !== "ready") ||
        !frozen(team, hashesFor(team)) || !frozen(single, hashesFor(single)) ||
        !team.initial_report || !single.initial_report || !team.holdout_report || !single.holdout_report) {
        result = "Unavailable / frozen evidence not verified";
        description = "Both runs need controller freeze events, source-matched development and heldout reports, and verifiable source hashes. A recorded final_source alone is not proof of freezing.";
      } else if (![team, single].every((run) => ["calls", "input_tokens", "output_tokens"].every((key) => knownUsage(run.usage?.[key])))) {
        result = "Unknown / resource usage not fully reported";
        description = "A resource-matched verdict requires finite known model-call, input-token, and output-token usage for both runs. Missing usage is never inferred as zero.";
      } else if (typeof team.holdout_report.passed === "boolean" && typeof single.holdout_report.passed === "boolean") {
        const teamPass = team.holdout_report.passed;
        const singlePass = single.holdout_report.passed;
        result = teamPass === singlePass ? `Tie / ${teamPass ? "both pass" : "neither passes"} the heldout contract` : teamPass ? "Team win / heldout contract outcome" : "Team loss / single passes the heldout contract";
        description = "This verdict compares only the reported full-contract pass/fail outcome in this one pair. It is not a statistical result or evidence that one mode is generally superior.";
      }
      fragment.append(element("p", "comparison-result", result), element("p", "muted", description));
      fragment.append(element("p", "field-note", "One pair is not evidence of general superiority. Budget exhaustion alone does not invalidate measured evidence when both frozen holdouts and resource usage are known; the reported heldout contract determines the outcome."));
      if (comparison.errors?.length) fragment.append(errorList(comparison.errors));
      const links = element("div", "comparison-links");
      for (const [index, id] of comparison.run_ids.entries()) {
        const run = comparison.results.find((item) => item.run_id === id);
        const button = element("button", "button secondary small", `Inspect ${run ? run.config.mode : index === 0 ? "team" : "single"}${run ? ` · ${human(run.status)}` : " · pending"}`);
        button.type = "button";
        button.addEventListener("click", () => selectRun(id));
        links.append(button);
      }
      fragment.append(links);
      const table = element("table", "comparison-config");
      table.append(element("caption", "", team && single ? !knownConfig ? "Recorded configuration / required values unknown" : matched ? "Matched configured limits (actual usage may differ)" : "Recorded configuration mismatch" : "Recorded configuration / waiting for both runs"));
      const head = element("thead");
      const header = element("tr");
      for (const title of ["Setting / evidence", "Team", "Single"]) {
        const cell = element("th", "", title);
        cell.scope = "col";
        header.append(cell);
      }
      head.append(header);
      const body = element("tbody");
      function row(label, first, second) {
        const tr = element("tr");
        const th = element("th", "", label);
        th.scope = "row";
        tr.append(th, element("td", "", first), element("td", "", second));
        body.append(tr);
      }
      for (const key of configFields) row(human(key), team ? exact(team.config[key]) : "Pending", single ? exact(single.config[key]) : "Pending");
      for (const [key, label] of [["initial_report", "Original development"], ["final_report", "Final development"], ["holdout_report", "Final heldout"]]) {
        const verdict = (run) => !run?.[key] ? "Not evaluated" : run[key].errors?.length ? "Evaluator errors" : run[key].passed === true ? "Pass" : run[key].passed === false ? "Fail" : "Unknown";
        row(label, verdict(team), verdict(single));
      }
      row("Verified frozen source", team ? frozen(team, hashesFor(team)) ? "Verified" : "Not verified" : "Pending", single ? frozen(single, hashesFor(single)) ? "Verified" : "Not verified" : "Pending");
      for (const [key, label] of [["calls", "Model calls"], ["input_tokens", "Input tokens"], ["output_tokens", "Output tokens"], ["cached_input_tokens", "Cached input tokens"], ["elapsed_ms", "Elapsed milliseconds"]]) {
        const usage = (run) => knownUsage(run?.usage?.[key]) ? exact(run.usage[key]) : "Unknown / not reported";
        row(label, usage(team), usage(single));
      }
      table.append(head, body);
      const scroll = element("div", "table-scroll");
      scroll.append(table);
      fragment.append(scroll, element("p", "field-note", "Equal configured limits do not imply equal actual usage. Unknown token counts stay unknown. Inspect each run for all gates, errors, source hashes, and Weave links."));
      return fragment;
    });
  }

  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    const comparisonId = state.comparisonId;
    const selectedId = state.selectedId;
    const work = [
      (async () => {
        try {
          state.status = await request("/api/status");
          state.statusFresh = true;
          state.errors.delete("Readiness");
        } catch (error) {
          state.statusFresh = false;
          failure("Readiness", error);
        }
      })(),
      (async () => {
        try {
          const result = await request("/api/runs");
          for (const run of result.runs) state.runs.set(run.run_id, run);
          if (!state.selectedId && result.runs.length) {
            state.selectedId = [...result.runs].sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0].run_id;
          }
          state.errors.delete("Run history");
        } catch (error) {
          failure("Run history", error);
        }
      })(),
    ];
    await Promise.all(work);
    if (selectedId) await (async () => {
      try {
        const run = await request(`/api/runs/${encodeURIComponent(selectedId)}`);
        state.runs.set(run.run_id, run);
        state.errors.delete("Selected run");
      } catch (error) {
        failure("Selected run", error);
      }
    })();
    if (comparisonId) await (async () => {
      try {
        const comparison = await request(`/api/comparisons/${encodeURIComponent(comparisonId)}`);
        if (state.comparisonId === comparisonId) {
          state.comparison = comparison;
          for (const run of comparison.results) state.runs.set(run.run_id, run);
          state.errors.delete("Comparison");
        }
      } catch (error) {
        if (state.comparisonId === comparisonId) failure("Comparison", error);
      }
    })();
    try {
      for (const id of state.pendingIds) {
        if (state.runs.has(id)) state.pendingIds.delete(id);
      }
      renderErrors();
      renderReadiness();
      renderHistory();
      renderRun();
      renderComparison();
    } finally {
      state.refreshing = false;
    }
  }

  function configFromForm() {
    const fields = new FormData($("run-form"));
    return {
      mode: fields.get("mode"),
      provider: fields.get("provider"),
      model: fields.get("model").trim() || null,
      max_iterations: Number(fields.get("max_iterations")),
      max_model_calls: Number(fields.get("max_model_calls")),
      max_output_tokens: Number(fields.get("max_output_tokens")),
      max_input_tokens: Number(fields.get("max_input_tokens")),
      require_weave: Boolean(state.status?.weave.enabled) || !$("local-only").checked,
    };
  }

  async function start(comparison) {
    if (!$("run-form").reportValidity()) return;
    renderReadiness();
    if ($(comparison ? "run-comparison" : "run-repair").disabled) return;
    const config = configFromForm();
    state.starting = true;
    state.errors.delete("Start request");
    renderReadiness();
    renderErrors();
    try {
      const result = await request(comparison ? "/api/comparisons" : "/api/runs", { method: "POST", body: JSON.stringify(config) });
      if (comparison) {
        state.comparisonId = result.comparison_id;
        state.comparison = null;
        state.selectedId = result.run_ids[0];
        result.run_ids.forEach((id) => state.pendingIds.add(id));
      } else {
        state.selectedId = result.run_id;
        state.pendingIds.add(result.run_id);
      }
      // The server accepted work. Keep start controls blocked until readiness is refreshed.
      state.statusFresh = false;
      $("announcer").textContent = comparison ? "Comparison accepted. Waiting for team and single run evidence." : "Repair accepted. Waiting for recorded evidence.";
      renderRun();
      renderComparison();
    } catch (error) {
      failure("Start request", error);
      state.statusFresh = false;
    } finally {
      state.starting = false;
      renderErrors();
      renderReadiness();
      await refresh();
    }
  }

  $("run-form").addEventListener("submit", (event) => {
    event.preventDefault();
    start(false);
  });
  $("run-comparison").addEventListener("click", () => start(true));
  $("provider").addEventListener("change", renderReadiness);
  $("local-only").addEventListener("change", renderReadiness);
  $("retry").addEventListener("click", () => refresh());
  $("close-comparison").addEventListener("click", () => {
    state.comparisonId = null;
    state.comparison = null;
    state.errors.delete("Comparison");
    renderErrors();
    renderComparison();
  });
  refresh();
  window.setInterval(() => refresh(), 1100);
})();
