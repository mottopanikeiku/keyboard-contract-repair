"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const active = (run) => run && (run.status === "queued" || run.status === "running");
  const terminal = (run) => run && !active(run);
  const knownUsage = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const sourceHashes = new Map();
  const previews = new Map();
  const requestedComparison = new URL(location.href).searchParams.get("comparison");
  const requestedChallenge = new URL(location.href).searchParams.get("challenge");
  const state = {
    status: null,
    statusFresh: false,
    runs: new Map(),
    pendingIds: new Set(),
    selectedId: null,
    comparisonId: /^[a-f0-9]{32}$/.test(requestedComparison || "") ? requestedComparison : null,
    comparison: null,
    comparisons: [],
    presets: [],
    challenges: [],
    challengeId: /^[a-f0-9]{32}$/.test(requestedChallenge || "") ? requestedChallenge : null,
    challenge: null,
    challengePaused: false,
    challengeReconcile: false,
    challengeLoading: false,
    libraryLoaded: false,
    libraryRefreshing: false,
    challengeAnnounced: "",
    starting: false,
    connecting: false,
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
    const focused = container.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = focused?.dataset.focusKey;
    const focusText = focused?.tagName === "SUMMARY" ? focused.textContent : null;
    container.replaceChildren(build());
    Array.from(container.querySelectorAll(".timeline, .source, .diff, .table-scroll")).forEach((node, index) => {
      if (scrollPositions[index]) [node.scrollTop, node.scrollLeft] = scrollPositions[index];
    });
    if (focused) {
      const replacement = Array.from(container.querySelectorAll("button, a, summary")).find((node) =>
        focusKey ? node.dataset.focusKey === focusKey : focusText && node.tagName === "SUMMARY" && node.textContent === focusText);
      replacement?.focus({ preventScroll: true });
    }
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
    return state.starting || state.connecting || state.pendingIds.size > 0 || Boolean(state.status?.busy) || active(state.challenge) || state.challenges.some(active) || Array.from(state.runs.values()).some(active);
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
    $("connect-weave").hidden = Boolean(weave?.enabled);
    $("connect-weave").disabled = isBusy() || !state.statusFresh;
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
      blocker = "The controller is busy. New operations are paused to avoid overlap.";
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
    $("busy-status").textContent = !state.statusFresh ? "Connection unavailable" : isBusy() ? "Controller busy" : "Ready for a run";
    renderChallengeReadiness();
  }

  function selectRun(id) {
    state.selectedId = id;
    renderHistory();
    renderRun();
    $("workspace").focus({ preventScroll: true });
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
        button.dataset.focusKey = `run:${run.run_id}`;
        const top = element("span", "history-item-top");
        top.append(element("span", "", `${human(run.config.mode)} repair`), tag(`${active(run) ? "Live" : "Recorded"} · ${human(run.status)}`, run.status));
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
    heading.append(element("h3", "", label), report ? tag(report.errors?.length ? "Evaluator error" : report.passed === true ? "Pass" : report.passed === false ? "Fail" : "Unknown", report.errors?.length || report.passed === false ? "fail" : report.passed === true ? "pass" : "pending") : tag("Not evaluated", "pending"));
    block.append(heading);
    if (!report) {
      block.append(element("p", "muted", pendingText));
      return block;
    }
    if (report.gates.length) {
      const failures = report.gates.filter((gate) => !gate.passed);
      block.append(element("p", "field-note", `${report.gates.length - failures.length} / ${report.gates.length} gates passed.`));
      if (failures.length) {
        const failed = element("ul", "error-list");
        failures.slice(0, 3).forEach((gate) => failed.append(element("li", "", `${human(gate.name)}${gate.detail ? ` — ${gate.detail}` : ""}`)));
        block.append(failed);
        if (failures.length > 3) {
          const remaining = disclosure(`${failures.length - 3} more failed gates`, `${key}:failures`);
          const list = element("ul", "error-list");
          failures.slice(3).forEach((gate) => list.append(element("li", "", `${human(gate.name)}${gate.detail ? ` — ${gate.detail}` : ""}`)));
          remaining.append(list);
          block.append(remaining);
        }
      }
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
      const gates = disclosure(`Inspect all ${report.gates.length} gates and exact evidence`, `${key}:gates`);
      gates.append(scroll);
      block.append(gates);
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
    const evaluationLink = weaveLink(run.weave.evaluation_url, "Open published Weave evaluation");
    if (run.weave.enabled && evaluationLink) weave.append(document.createTextNode(" · "), evaluationLink);
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
      if (Object.keys(event.data || {}).length) item.append(dataDisclosure("Event data", event.data, `${run.run_id || run.challenge_id}:event:${event.sequence}`));
      list.append(item);
    }
    return list;
  }

  function renderUsage(run) {
    const fragment = document.createDocumentFragment();
    if (run.usage.complete === false) {
      fragment.append(element("p", "notice", "Incomplete totals: only observed usage is shown below. An in-flight request may not be included."));
    }
    const list = element("dl", "usage-list");
    for (const [field, label, suffix] of [
      ["calls", "Model calls", ""],
      ["input_tokens", "Input tokens", ""],
      ["output_tokens", "Output tokens", ""],
      ["cached_input_tokens", "Cached input tokens", ""],
      ["elapsed_ms", "Model request time", " ms"],
    ]) {
      const value = run.usage[field];
      const unknown = !knownUsage(value);
      const row = element("div");
      const displayed = unknown ? "Unknown / not reported" : `${run.usage.complete === false ? "At least " : ""}${value}${suffix}`;
      row.append(element("dt", "", label), element("dd", unknown ? "unknown" : "", displayed));
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
      renderChallenge();
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

  async function setPreview(id, run, phase, report, eligible, missing, endpoint = null) {
    if (!eligible) {
      clearPreview(id, missing);
      return;
    }
    const key = `${endpoint || run.run_id}:${phase}:${report.source_hash}:${JSON.stringify(report.artifacts || [])}`;
    const prior = previews.get(id);
    if (prior?.key === key) return;
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
      const response = await fetch(endpoint || `/api/preview/${encodeURIComponent(run.run_id)}/${phase}`, {
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
      image.alt = `${endpoint ? `Challenge ${phase} candidate` : phase === "original" ? "Original fixture" : "Frozen final candidate"} in record ${run.run_id || run.challenge_id}, captured by the isolated evaluator. Keyboard outcomes are reported in the gates, not this image.`;
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
    $("selection-status").textContent = `${active(run) ? "Live execution" : "Recorded evidence"} · ${human(run.status)}`;
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
    mount("agent-flow", [run.run_id, run.iterations, run.events, hasFrozen], () => renderAgentFlow(run, hasFrozen));
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
      mount("comparison-content", [state.comparisonId, "pending"], () => element("p", "muted", "Loading this comparison's actual run records. No prior comparison is shown."));
      return;
    }
    const hashes = comparison.results.map(hashesFor);
    mount("comparison-content", [comparison, hashes], () => {
      const fragment = document.createDocumentFragment();
      fragment.append(tag(active(comparison) ? "LIVE EXECUTION / actual controller state" : "RECORDED EVIDENCE / not a live rerun", active(comparison) ? "running" : ""), element("p", "comparison-status", `Comparison ${comparison.comparison_id} · ${human(comparison.status)}`));
      const team = comparison.results.find((run) => run.config.mode === "team");
      const single = comparison.results.find((run) => run.config.mode === "single");
      const configFields = ["provider", "model", "max_iterations", "max_model_calls", "max_output_tokens", "max_input_tokens", "require_weave"];
      const assessment = comparison.assessment;
      const labels = { team: "Team passes; single does not", single: "Single passes; team does not", tie: "Tie / both candidates pass", neither: "Neither candidate passes", unavailable: "No comparable verdict" };
      fragment.append(element("p", "comparison-result", labels[assessment?.verdict] || "Assessment unavailable"),
        element("p", "muted", assessment?.reason || "The server has not supplied an authoritative assessment. No client-side winner is inferred."),
        element("p", "field-note", assessment?.caveat || "One pair is not evidence of general superiority."));
      if (assessment) fragment.append(tag(assessment.comparable ? "Comparable recorded pair" : "Not comparable", assessment.comparable ? "" : "pending"));
      if (comparison.errors?.length) fragment.append(errorList(comparison.errors));
      const scores = element("div", "pair-scores");
      for (const [mode, run] of [["Team", team], ["Single", single]]) {
        const card = element("section", "pair-score");
        card.append(element("h3", "", mode));
        for (const [field, label] of [["final_report", "Development"], ["holdout_report", "Sealed holdout"]]) {
          const report = run?.[field];
          const line = element("p", "", `${label}: `);
          line.append(tag(!report ? "Not evaluated" : report.errors?.length ? "Evaluator error" : report.passed === true ? "Pass" : report.passed === false ? "Fail" : "Unknown",
            !report ? "pending" : report.errors?.length || report.passed === false ? "fail" : report.passed === true ? "pass" : "pending"));
          card.append(line);
        }
        const usage = element("dl");
        for (const [key, label] of [["calls", "Actual model calls"], ["input_tokens", "Input tokens"], ["output_tokens", "Output tokens"]]) {
          const row = element("div");
          row.append(element("dt", "", label), element("dd", "", knownUsage(run?.usage?.[key]) ? `${run.usage.complete === false ? "At least " : ""}${exact(run.usage[key])}` : "Unknown"));
          usage.append(row);
        }
        card.append(usage);
        if (run?.usage?.complete === false) card.append(element("p", "field-note", "Incomplete totals / observed usage only."));
        scores.append(card);
      }
      fragment.append(scores);
      const links = element("div", "comparison-links");
      for (const [index, id] of comparison.run_ids.entries()) {
        const run = comparison.results.find((item) => item.run_id === id);
        const button = element("button", "button secondary small", `Inspect ${run ? run.config.mode : index === 0 ? "team" : "single"}${run ? ` · ${human(run.status)}` : " · pending"}`);
        button.type = "button";
        button.dataset.focusKey = `inspect:${id}`;
        button.addEventListener("click", () => selectRun(id));
        links.append(button);
      }
      const reportLink = element("a", "button secondary small", "Download evidence report");
      reportLink.href = `/api/comparisons/${encodeURIComponent(comparison.comparison_id)}/report`;
      reportLink.download = `keyproof-${comparison.comparison_id}.html`;
      reportLink.dataset.focusKey = "comparison-report";
      links.append(reportLink);
      fragment.append(links);
      const table = element("table", "comparison-config");
      table.append(element("caption", "", "Recorded configuration and measured outcomes / verdict supplied by the server"));
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
      for (const [key, label] of [["calls", "Model calls"], ["input_tokens", "Input tokens"], ["output_tokens", "Output tokens"], ["cached_input_tokens", "Cached input tokens"], ["elapsed_ms", "Model request milliseconds"]]) {
        const usage = (run) => knownUsage(run?.usage?.[key]) ? `${run.usage.complete === false ? "At least " : ""}${exact(run.usage[key])}` : "Unknown / not reported";
        row(label, usage(team), usage(single));
      }
      table.append(head, body);
      const scroll = element("div", "table-scroll");
      scroll.append(table);
      const details = disclosure("Inspect configured limits, phase results & actual usage", `${comparison.comparison_id}:comparison-config`);
      details.append(scroll, element("p", "field-note", "Equal configured limits do not imply equal actual usage. Unknown token counts stay unknown. Inspect each run for all gates, errors, source hashes, and Weave links."));
      fragment.append(details);
      return fragment;
    });
  }

  function renderAgentFlow(run, hasFrozen) {
    const list = element("ol", "stage-list");
    const audits = run.iterations.filter((item) => item.audit).length;
    const patches = run.iterations.filter((item) => item.patch).length;
    const accepted = run.iterations.filter((item) => item.accepted).length;
    const rejected = run.iterations.filter((item) => item.decision && !item.accepted).length;
    for (const [label, evidence] of [
      ["Audit", `${audits} recorded audits`],
      ["Patch", `${patches} recorded proposals`],
      ["Accept / reject", `${accepted} accepted · ${rejected} rejected`],
      ["Freeze + holdout", hasFrozen ? run.holdout_report ? "Frozen source verified · holdout reported" : "Frozen source verified · holdout not reported" : "Freeze not verified"],
    ]) {
      const item = element("li");
      item.append(element("strong", "", label), element("span", "", evidence));
      list.append(item);
    }
    return list;
  }

  function renderChallengeReadiness() {
    const consentNeeded = Boolean(state.status && !state.status.weave.enabled && !$("local-only").checked);
    let message = "Ready: freeze this candidate, then run both independent evaluations. No model calls.";
    if (!state.statusFresh) message = "A current server readiness check is required.";
    else if (isBusy()) message = "Controller busy. A new challenge waits until the current operation finishes.";
    else if (!state.presets.length) message = "The server has not supplied a challenge catalog.";
    else if (consentNeeded) message = "Weave is unavailable. Explicit local-only consent is required below.";
    else if (!state.status.weave.enabled) message = "Ready for explicit local-only evaluation / not sponsor-integrated. No model calls.";
    $("challenge-blocker").textContent = message;
    $("challenge-consent").hidden = !consentNeeded;
    $("run-challenge").disabled = !state.statusFresh || isBusy() || !state.presets.length || consentNeeded;
    $("challenge-preset").disabled = !state.presets.length || state.starting;
    const preset = state.presets.find((item) => item.preset_id === $("challenge-preset").value);
    $("challenge-description").textContent = preset?.description || "No candidate description is available.";
  }

  function renderLibrary() {
    $("refresh-evidence").disabled = state.libraryRefreshing;
    mount("challenge-preset", state.presets, () => {
      const fragment = document.createDocumentFragment();
      const selected = $("challenge-preset").value;
      if (!state.presets.length) fragment.append(element("option", "", "No catalog available"));
      for (const preset of state.presets) {
        const option = element("option", "", preset.title);
        option.value = preset.preset_id;
        option.selected = preset.preset_id === selected;
        fragment.append(option);
      }
      return fragment;
    });
    mount("comparison-history", [state.comparisons, state.comparisonId], () => {
      if (!state.comparisons.length) return element("p", "muted", state.errors.has("Comparison library") ? "The comparison library could not be loaded. Retry the connection; no replacement evidence is invented." : "No retained pairs yet. Run a matched comparison below to create real evidence.");
      const list = element("div", "recording-grid");
      for (const comparison of [...state.comparisons].sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))) {
        const button = element("button", "history-item recording-card");
        button.type = "button";
        button.dataset.focusKey = `comparison:${comparison.comparison_id}`;
        button.setAttribute("aria-pressed", String(comparison.comparison_id === state.comparisonId));
        button.append(tag(active(comparison) ? "Live execution" : "Recorded evidence", active(comparison) ? "running" : ""),
          element("strong", "", `Team vs. single · ${human(comparison.status)}`),
          element("small", "", dateText(comparison.started_at)),
          element("small", "mono", comparison.comparison_id),
          element("span", "recording-action", "Open the actual pair →"));
        button.addEventListener("click", () => selectComparison(comparison.comparison_id));
        list.append(button);
      }
      return list;
    });
    mount("challenge-history", [state.challenges, state.challengeId], () => {
      if (!state.challenges.length) return element("p", "muted", state.errors.has("Challenge library") ? "Challenge history unavailable. Retry the connection." : "No attempts recorded yet.");
      const list = element("div", "history-list");
      for (const run of [...state.challenges].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))) {
        const button = element("button", "history-item");
        button.type = "button";
        button.dataset.focusKey = `challenge:${run.challenge_id}`;
        button.setAttribute("aria-pressed", String(run.challenge_id === state.challengeId));
        button.append(element("strong", "", state.presets.find((item) => item.preset_id === run.preset_id)?.title || human(run.preset_id)),
          element("small", "", `${active(run) ? "Live" : "Recorded"} · ${human(run.status)} · ${human(run.verdict)}`),
          element("small", "", dateText(run.created_at)));
        button.addEventListener("click", () => selectChallenge(run.challenge_id));
        list.append(button);
      }
      return list;
    });
    renderChallengeReadiness();
  }

  async function loadLibrary() {
    if (state.libraryRefreshing) return;
    state.libraryRefreshing = true;
    $("refresh-evidence").disabled = true;
    await Promise.all([
      (async () => {
        try {
          const result = await request("/api/comparisons");
          state.comparisons = result.comparisons;
          state.errors.delete("Comparison library");
        } catch (error) { failure("Comparison library", error); }
      })(),
      (async () => {
        try {
          const result = await request("/api/challenges");
          state.presets = result.presets;
          state.challenges = result.runs;
          state.errors.delete("Challenge library");
        } catch (error) { failure("Challenge library", error); }
      })(),
    ]);
    state.libraryLoaded = true;
    state.libraryRefreshing = false;
    renderLibrary();
    renderErrors();
  }

  async function selectComparison(id) {
    state.comparisonId = id;
    state.comparison = null;
    state.errors.delete("Comparison");
    const url = new URL(location.href);
    url.searchParams.set("comparison", id);
    history.replaceState(null, "", url);
    renderComparison();
    renderLibrary();
    $("comparison-panel").focus();
    await refresh();
  }

  async function selectChallenge(id) {
    state.challengeId = id;
    state.challenge = null;
    state.challengePaused = false;
    state.challengeReconcile = false;
    state.errors.delete("Challenge");
    const url = new URL(location.href);
    url.searchParams.set("challenge", id);
    history.replaceState(null, "", url);
    renderChallenge();
    renderLibrary();
    $("challenge-evidence").focus();
    await refreshChallenge();
  }

  function renderChallenge() {
    const run = state.challenge;
    $("challenge-details").hidden = !run;
    if (!run) {
      for (const phase of ["development", "holdout"]) clearPreview(`challenge-${phase}-preview`, "No source-matched challenge capture is available.");
      if (state.challengeId) mount("challenge-overview", [state.challengeId, state.challengePaused], () => notice(state.challengePaused ? "Challenge loading paused after a request error. Use Retry connection to reconcile the record." : "Loading the selected challenge. No previous attempt is shown."));
      return;
    }
    const hash = hashForSource(run.candidate_source);
    const verified = Boolean(run.frozen_at && hash.hash && hash.hash === run.source_hash);
    mount("challenge-overview", [run, hash, state.challengePaused], () => {
      const fragment = document.createDocumentFragment();
      const preset = state.presets.find((item) => item.preset_id === run.preset_id);
      fragment.append(tag(active(run) ? state.challengePaused ? "LIVE STATE UNKNOWN / polling paused" : "LIVE EVALUATION" : "RECORDED EVIDENCE / not a live rerun", active(run) ? "running" : ""),
        element("h3", "challenge-candidate-title", preset?.title || human(run.preset_id)));
      const verdicts = { not_evaluated: "No verdict yet", accepted: "Accepted by the contract", rejected: "Rejected by the contract", error: "Evaluator / execution error" };
      fragment.append(element("p", "challenge-verdict", verdicts[run.verdict] || "Unknown verdict"), element("p", "field-note", `Controller: ${human(run.status)} · Curated adversarial sample · ${run.challenge_id}`));
      const stages = element("ol", "challenge-stages");
      for (const [label, value] of [
        ["Frozen candidate", run.frozen_at ? verified ? "Source verified" : hash.status === "pending" ? "Checking source hash" : "Source not verified" : "Not frozen"],
        ["Development", run.development_report ? "Report recorded" : "Not evaluated"],
        ["Sealed holdout", run.holdout_report ? "Report revealed" : "Not evaluated"],
      ]) {
        const item = element("li");
        item.append(element("strong", "", label), element("span", "", value));
        stages.append(item);
      }
      fragment.append(stages);
      if (state.challengePaused) fragment.append(notice("Polling paused after a request error. The last known evidence remains visible, not live. Use Retry connection to reconcile.", "warning"));
      else if (active(run)) fragment.append(notice(run.events.at(-1)?.summary || "Queued. Waiting for the first recorded evaluator event."));
      if (run.frozen_at && hash.status !== "pending" && !verified) fragment.append(notice("Candidate source verification failed. No capture is displayed; do not treat this record as verified frozen evidence.", "error"));
      fragment.append(element("p", "field-note", `Created ${dateText(run.created_at)} · Frozen ${dateText(run.frozen_at)}${run.finished_at ? ` · Finished ${dateText(run.finished_at)}` : ""}`));
      const telemetry = element("p", "field-note", run.weave?.enabled ? "Weave enabled. " : "Local-only record / not sponsor-integrated. ");
      const trace = weaveLink(run.weave?.url);
      if (trace) telemetry.append(trace);
      const evaluationLink = weaveLink(run.weave?.evaluation_url, "Open published Weave evaluation");
      if (run.weave?.enabled && evaluationLink) telemetry.append(document.createTextNode(" · "), evaluationLink);
      if (run.weave?.error) telemetry.append(document.createTextNode(` ${run.weave.error}`));
      fragment.append(telemetry);
      if (run.errors?.length) fragment.append(errorList(run.errors));
      return fragment;
    });
    mount("challenge-reports", [run.challenge_id, run.development_report, run.holdout_report, verified], () => {
      const fragment = document.createDocumentFragment();
      for (const [phase, title] of [["development", "Development / fixed candidate"], ["holdout", "Sealed holdout / never repair feedback"]]) {
        const report = run[`${phase}_report`];
        if (report && (!verified || report.source_hash !== hash.hash)) {
          fragment.append(notice(`${title}: report source is not verified against the frozen candidate. This report cannot prove this candidate's outcome.`, "warning"));
        }
        fragment.append(renderReport(report, title, `${run.challenge_id}:${phase}`, phase === "holdout" ? "Sealed checks have not reported. The candidate must freeze before evaluation; no passing outcome is implied." : "Waiting for the frozen candidate's development report."));
      }
      return fragment;
    });
    mount("challenge-diff", [run.challenge_id, run.source_diff], () => run.source_diff ? renderDiff(run.source_diff) : element("p", "muted", "No change from the recorded original source."));
    $("challenge-source").textContent = run.candidate_source;
    $("challenge-original").textContent = run.original_source;
    $("challenge-hash").textContent = `Frozen SHA-256: ${run.source_hash || "Not reported"} · ${verified ? "Matches candidate source" : "Not verified"}`;
    $("challenge-events-label").textContent = `Recorded event ledger / ${run.events.length} events`;
    mount("challenge-events", [run.challenge_id, run.events], () => renderTimeline(run));
    for (const phase of ["development", "holdout"]) {
      const report = run[`${phase}_report`];
      setPreview(`challenge-${phase}-preview`, run, phase, report, Boolean(verified && report && report.source_hash === hash.hash),
        "No verified source-matched evaluator capture for this phase. No substitute image is shown.",
        `/api/challenges/${encodeURIComponent(run.challenge_id)}/preview/${phase}`);
    }
    const announcement = `${run.challenge_id}:${run.status}:${run.verdict}:${run.events.length}:${state.challengePaused}`;
    if (announcement !== state.challengeAnnounced) {
      $("announcer").textContent = `Challenge ${human(run.preset_id)}: ${state.challengePaused ? "polling paused, last known state" : human(run.status)}. ${human(run.verdict)}. ${run.events.at(-1)?.summary || ""}`;
      state.challengeAnnounced = announcement;
    }
  }

  async function refreshChallenge() {
    const id = state.challengeId;
    if (!id || state.challengeLoading || state.challengePaused || (state.challenge && !active(state.challenge) && !state.challengeReconcile)) return;
    state.challengeLoading = true;
    try {
      const run = await request(`/api/challenges/${encodeURIComponent(id)}`);
      if (state.challengeId !== id) return;
      state.challengeReconcile = !active(run) && (!state.challenge || active(state.challenge));
      state.challenge = run;
      state.challenges = [run, ...state.challenges.filter((item) => item.challenge_id !== id)];
      state.errors.delete("Challenge");
      if (!active(run)) state.libraryLoaded = false;
    } catch (error) {
      if (state.challengeId === id) {
        state.challengePaused = true;
        failure("Challenge", error);
      }
    } finally {
      state.challengeLoading = false;
      renderChallenge();
      renderLibrary();
      renderReadiness();
      renderErrors();
    }
  }

  async function startChallenge(event) {
    event.preventDefault();
    renderChallengeReadiness();
    if ($("run-challenge").disabled) return;
    state.starting = true;
    renderReadiness();
    state.errors.delete("Challenge start");
    try {
      const result = await request("/api/challenges", { method: "POST", body: JSON.stringify({
        preset_id: $("challenge-preset").value,
        require_weave: Boolean(state.status?.weave.enabled) || !$("local-only").checked,
      }) });
      state.challengeId = result.challenge_id;
      state.challenge = result;
      state.challengePaused = false;
      state.challengeReconcile = !active(result);
      state.challenges = [result, ...state.challenges.filter((item) => item.challenge_id !== result.challenge_id)];
      const url = new URL(location.href);
      url.searchParams.set("challenge", result.challenge_id);
      history.replaceState(null, "", url);
      state.libraryLoaded = false;
      renderChallenge();
      $("challenge-evidence").focus();
    } catch (error) {
      failure("Challenge start", error);
    } finally {
      state.starting = false;
      state.statusFresh = false;
      renderErrors();
      renderReadiness();
      await refresh();
    }
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
    if (!state.libraryLoaded || (!state.challengePaused && state.challenges.some(active)) || state.comparisons.some(active)) work.push(loadLibrary());
    work.push(refreshChallenge());
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
          state.comparisons = [comparison, ...state.comparisons.filter((item) => item.comparison_id !== comparisonId)];
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
      renderChallenge();
      renderLibrary();
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
        const url = new URL(location.href);
        url.searchParams.set("comparison", result.comparison_id);
        history.replaceState(null, "", url);
        state.comparison = null;
        state.selectedId = result.run_ids[0];
        result.run_ids.forEach((id) => state.pendingIds.add(id));
      } else {
        state.selectedId = result.run_id;
        state.pendingIds.add(result.run_id);
      }
      state.libraryLoaded = false;
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
  $("retry").addEventListener("click", () => {
    state.challengePaused = false;
    state.challengeReconcile = true;
    state.libraryLoaded = false;
    for (const [id, preview] of previews) if (!preview.loading && !preview.url) clearPreview(id, "Retrying verified capture.");
    refresh();
  });
  $("challenge-form").addEventListener("submit", startChallenge);
  $("challenge-preset").addEventListener("change", renderChallengeReadiness);
  $("refresh-evidence").addEventListener("click", () => loadLibrary());
  for (const [id, host, imageIds] of [
    ["refresh-challenge-captures", $("challenge-captures"), ["challenge-development-preview", "challenge-holdout-preview"]],
    ["refresh-run-captures", $("previews-title").parentElement, ["original-preview", "final-preview"]],
  ]) {
    const button = element("button", "text-button", "Refresh verified captures");
    button.type = "button";
    button.id = id;
    button.addEventListener("click", () => {
      imageIds.forEach((imageId) => clearPreview(imageId, "Refreshing source-matched evaluator capture."));
      renderRun();
      renderChallenge();
    });
    if (host.tagName === "DETAILS") host.firstElementChild.after(button);
    else host.append(button);
  }
  window.addEventListener("pagehide", () => {
    for (const id of previews.keys()) clearPreview(id, "Capture released.");
  });
  $("connect-weave").addEventListener("click", async () => {
    if (isBusy()) return;
    state.connecting = true;
    renderReadiness();
    try {
      const result = await request("/api/telemetry/connect", { method: "POST", body: "{}" });
      state.errors.delete("Telemetry");
      $("announcer").textContent = result.enabled ? "Weave connected." : result.error || "Weave is not connected.";
    } catch (error) {
      failure("Telemetry", error);
    } finally {
      state.connecting = false;
      await refresh();
      renderReadiness();
    }
  });
  $("close-comparison").addEventListener("click", () => {
    state.comparisonId = null;
    const url = new URL(location.href);
    url.searchParams.delete("comparison");
    history.replaceState(null, "", url);
    state.comparison = null;
    state.errors.delete("Comparison");
    renderErrors();
    renderComparison();
    renderLibrary();
    $("recorded-title").tabIndex = -1;
    $("recorded-title").focus();
  });
  refresh();
  window.setInterval(() => refresh(), 1100);
})();
