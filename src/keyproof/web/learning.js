"use strict";

(() => {
  const ui = window.KeyproofUI;
  const { element: el, human, exact, tag, notice, dateText, disclosure, dataDisclosure, mount, request, active } = ui;
  const $ = (id) => document.getElementById(id);
  const requested = new URL(location.href).searchParams.get("learning");
  const state = {
    runs: new Map(), summaries: new Map(), revisions: new Map(),
    selected: /^[a-f0-9]{32}$/.test(requested || "") ? requested : null,
    catalog: [], loaded: false, refreshing: false, starting: false, uncertain: false,
    error: "", announced: "", selectedError: "",
  };
  const endpoint = (id) => `/api/learning/${encodeURIComponent(id)}`;
  const counterexample = (report) => report?.passed === false && report.errors?.length === 0 && report.gates?.some((gate) => gate.passed === false);
  const eligible = (run) => run.status === "completed" && run.memory_frozen_at && run.memory_hash && run.memory_count > 0;
  const orderedFreeze = (run) => {
    const frozen = Date.parse(run.memory_frozen_at);
    const revealed = Date.parse(run.transfer_source_revealed_at);
    return Boolean(run.memory_hash) && Number.isFinite(frozen) && Number.isFinite(revealed) && frozen <= revealed;
  };
  const transferHits = (run) => orderedFreeze(run) ? run.cases.filter((item) => item.partition === "transfer").flatMap((item) =>
    item.initial_memory_reports.filter((report) => report.source_hash === item.original_source_hash && counterexample(report) && run.memory.some((entry) => entry.probe_hash === report.probe_hash))) : [];

  function readiness() {
    const { status, fresh, busy } = ui.readiness();
    const provider = $("learning-provider").value;
    const available = provider === "codex" ? status?.provider?.codex_available : status?.provider?.openai_configured;
    const traced = Boolean(status?.weave?.enabled);
    $("learning-local-choice").hidden = !status || traced;
    let message = "Ready for a real model run. All configured budgets are submitted to the controller.";
    let blocked = false;
    if (!fresh) { message = "A current shared readiness check is required."; blocked = true; }
    else if (state.uncertain) { message = "The start response was not confirmed. Refresh retained history before trying again; never automatically resubmit a paid job."; blocked = true; }
    else if (!state.loaded || state.error) { message = "Refresh learning history successfully before starting another execution."; blocked = true; }
    else if (busy) { message = "Shared controller busy. New learning, challenge and repair operations are paused."; blocked = true; }
    else if (!available) { message = `${human(provider)} is unavailable. Choose a configured provider; no model fallback is used.`; blocked = true; }
    else if (!traced && !$("learning-local").checked) { message = "Weave is unavailable. Explicit local-only consent is required for this learning run."; blocked = true; }
    else if (!traced) message = "Ready for explicit local-only execution / not sponsor-integrated.";
    $("learning-blocker").textContent = message;
    $("learning-start").disabled = blocked || state.starting;
  }

  function syncBusy() {
    ui.setLearningBusy(state.starting || state.uncertain || [...state.summaries.values()].some(active));
  }

  function connection() {
    const message = [state.error, state.selectedError].filter(Boolean).join(" ");
    $("learning-connection").hidden = !message;
    $("learning-connection").textContent = message ? `${message} Last received evidence remains visible; it is not current execution confirmation.` : "";
  }

  function select(id, focus = true) {
    state.selected = id;
    state.selectedError = "";
    const url = new URL(location.href);
    url.searchParams.set("learning", id);
    history.replaceState(null, "", url);
    render();
    if (focus) $("learning-evidence").focus();
    refresh();
  }

  function renderHistory() {
    const runs = [...state.summaries.values()].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    mount("learning-history", [state.selected, state.loaded, runs.map((run) => [run.learning_id, run.status, run.verdict, run.memory_count, run.created_at])], () => {
      const list = el("div", "learning-history-list");
      if (!runs.length) list.append(el("p", "muted", state.loaded ? "No retained learning runs. No discovered failure or transfer result is claimed." : "Loading retained learning evidence…"));
      for (const run of runs) {
        const button = el("button", `recording-card learning-history-card${run.learning_id === state.selected ? " selected" : ""}`);
        button.type = "button";
        button.dataset.focusKey = `learning:${run.learning_id}`;
        button.setAttribute("aria-pressed", String(run.learning_id === state.selected));
        button.append(el("strong", "", `${active(run) ? "Current execution" : "Recorded"} / ${human(run.status)}`),
          el("span", "", human(run.verdict)), el("small", "", `${dateText(run.created_at)} · ${run.memory_count} memory entries`),
          el("span", "mono field-note", run.learning_id), el("span", "recording-action", "Open evidence / no model calls"));
        button.addEventListener("click", () => select(run.learning_id));
        list.append(button);
      }
      return list;
    });
    const seeds = runs.filter(eligible);
    const selectedSeed = $("learning-seed").value;
    mount("learning-seed", seeds.map((run) => [run.learning_id, run.memory_hash]), () => {
      const fragment = document.createDocumentFragment();
      const empty = el("option", "", "Start without prior memory");
      empty.value = "";
      fragment.append(empty);
      seeds.forEach((run) => {
        const option = el("option", "", `${dateText(run.created_at)} · ${run.memory_count} entries · ${run.learning_id.slice(0, 8)}`);
        option.value = run.learning_id;
        fragment.append(option);
      });
      return fragment;
    });
    $("learning-seed").value = seeds.some((run) => run.learning_id === selectedSeed) ? selectedSeed : "";
    mount("learning-catalog", state.catalog, () => dataDisclosure("Server curriculum / exact metadata", state.catalog, "learning:catalog"));
  }

  function renderRail(run) {
    mount("learning-rail", run ? [run.learning_id, run.cases.map((item) => [item.initial_development_report, item.initial_holdout_report, item.discoveries, item.initial_memory_reports]), run.memory.length, run.memory_hash, run.memory_frozen_at, run.transfer_source_revealed_at, run.verdict] : null, () => {
      const fragment = document.createDocumentFragment();
      const baselines = run?.cases.flatMap((item) => [item.initial_development_report, item.initial_holdout_report]).filter(Boolean) || [];
      const green = baselines.filter((report) => report.passed && !report.errors.length).length;
      const proposals = run?.cases.reduce((sum, item) => sum + item.discoveries.length, 0) || 0;
      const hits = run ? transferHits(run).length : 0;
      const stages = [
        ["01 / Fixed suite green?", baselines.length ? `${green} / ${baselines.length} recorded suites pass` : "Not evaluated", baselines.length && green === baselines.length],
        ["02 / Challenger proposal", proposals ? `${proposals} evaluated model proposal${proposals === 1 ? "" : "s"}` : "No evaluated proposal", proposals > 0],
        ["03 / Independent replays", run?.memory.length ? `${run.memory.length} server-admitted ${run.memory.length === 1 ? "entry" : "entries"}` : "No admitted failure", Boolean(run?.memory.length)],
        ["04 / Executable memory", run?.memory_frozen_at ? `${run.memory.length} ${run.memory.length === 1 ? "entry" : "entries"} frozen` : "Not frozen", Boolean(run?.memory_frozen_at && run.memory.length)],
        ["05 / Frozen transfer", hits ? `${hits} pre-repair detection${hits === 1 ? "" : "s"}` : run?.verdict === "transfer_missed" ? "Memory missed this defect" : "No verified detection", hits > 0],
      ];
      for (const [label, value, confirmed] of stages) {
        const item = el("li", confirmed ? "confirmed" : "");
        item.append(el("span", "", label), el("strong", "", value));
        fragment.append(item);
      }
      return fragment;
    });
  }

  function planView(plan, label = "Model-proposed executable steps") {
    const wrapper = el("div", "learning-plan");
    wrapper.append(el("h4", "", label), el("strong", "", plan.name), el("p", "", plan.hypothesis));
    const list = el("ol", "learning-steps");
    for (const step of plan.steps) {
      const item = el("li");
      const text = step.kind === "edit" ? `Edit display name to ${JSON.stringify(step.value)}` : step.kind === "save" ? `Activate Save with ${step.value}` : `Wait ${step.value} virtual ms`;
      item.append(el("code", "", text));
      list.append(item);
    }
    wrapper.append(list, el("p", "field-note", `${plan.steps.length} / 10 bounded steps. Real Tab navigation; edit uses Control+A and typing; Save uses Enter or Space. Python, not the model, derives expected writes.`));
    return wrapper;
  }

  function probeView(report, label, key, showPlan = true) {
    const block = el("section", "learning-probe");
    if (showPlan) block.append(planView(report.plan));
    const outcome = report.errors.length ? "Runtime / evaluator error — not a counterexample" : counterexample(report) ? "Contract failure witnessed" : report.passed ? "Probe passed — no counterexample" : "No admissible failure";
    block.append(el("p", `notice ${report.errors.length ? "error" : counterexample(report) ? "warning" : ""}`, `${label}: ${outcome}`));
    const ledger = el("div", "learning-ledger");
    for (const [heading, value] of [["Expected names / Python activation snapshots", report.expected_names], ["Actual POST request ledger / Python persistence", report.observed_requests]]) {
      const side = el("div");
      side.append(el("h4", "", heading), el("pre", "source", exact(value)));
      ledger.append(side);
    }
    block.append(ledger, el("p", "field-note mono", `Probe SHA-256: ${report.probe_hash}`));
    block.append(ui.renderReport({ ...report, phase: "generated probe", axe_violations: [], actions: report.observations }, label, key, "Not replayed."));
    return block;
  }

  function reportPair(item, prefix, initial) {
    const wrapper = el("div", "learning-report-pair");
    wrapper.append(
      ui.renderReport(initial ? item.initial_development_report : item.final_development_report, `${initial ? "Untouched baseline" : "Final"} / development`, `${prefix}:development`, "Not evaluated. No green gate is assumed."),
      ui.renderReport(initial ? item.initial_holdout_report : item.final_holdout_report, `${initial ? "Untouched baseline" : "Frozen final"} / sealed holdout`, `${prefix}:holdout`, "Not evaluated. Holdout results never enter model prompts."),
    );
    return wrapper;
  }

  function previews(run, item, key) {
    const wrapper = disclosure("Source-bound development PNG previews / optional", `${key}:captures`);
    wrapper.append(el("p", "field-note", "Static browser captures only. No candidate code executes here. The API serves a PNG only when its development report matches the recorded source; a screenshot is not keyboard proof."));
    const grid = el("div", "preview-grid");
    for (const [phase, label, report] of [["before", "Untouched source", item.initial_development_report], ["after", "Recorded final source", item.final_development_report]]) {
      const cell = el("div");
      cell.append(el("h4", "", label));
      if (!report) cell.append(el("p", "field-note", "No development report / capture not available."));
      else {
        const button = el("button", "text-button", "Load verified PNG");
        button.type = "button";
        button.dataset.focusKey = `${key}:capture:${phase}`;
        const status = el("p", "field-note", "Not loaded.");
        status.setAttribute("role", "status");
        button.addEventListener("click", () => {
          button.disabled = true;
          status.textContent = "Loading source-bound capture…";
          const image = el("img", "preview-image");
          image.alt = `${item.title} / ${label}, isolated evaluator development capture`;
          image.hidden = true;
          const url = `${endpoint(run.learning_id)}/captures/${encodeURIComponent(item.case_id)}/${phase}`;
          image.addEventListener("load", () => {
            image.hidden = false;
            status.textContent = "Source-bound development capture; appearance is not a keyboard verdict.";
            const link = el("a", "preview-link", "Open full-size PNG (new tab)");
            link.href = url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            cell.append(link);
          }, { once: true });
          image.addEventListener("error", () => {
            image.remove();
            status.textContent = "Capture unavailable, unbound or not yet retained. No visual evidence is claimed.";
            button.disabled = false;
          }, { once: true });
          image.src = url;
          cell.append(image);
        });
        cell.append(button, status);
      }
      grid.append(cell);
    }
    wrapper.append(grid);
    return wrapper;
  }

  function caseView(run, item) {
    const key = `learning:${run.learning_id}:${item.case_id}`;
    const section = el("section", "learning-case");
    const heading = el("div", "section-heading");
    heading.append(el("h3", "", `${human(item.partition)} / ${item.title}`), tag(human(item.status), item.status));
    section.append(heading, el("p", "field-note mono", `Untouched source SHA-256: ${item.original_source_hash}`));
    if (item.errors.length) section.append(ui.errorList(item.errors));
    section.append(reportPair(item, `${key}:baseline`, true));
    const initial = disclosure(item.partition === "transfer" ? "Frozen memory against untouched transfer / before any repair" : "Existing memory against untouched training source", `${key}:initial-memory`, item.partition === "transfer");
    if (!item.initial_memory_reports.length) initial.append(el("p", "muted", "No initial memory replay recorded. No detection is claimed."));
    item.initial_memory_reports.forEach((report, index) => initial.append(probeView(report, `Initial memory replay ${index + 1}`, `${key}:initial:${index}`)));
    section.append(initial);
    if (item.partition === "training") {
      const proposals = disclosure(`Challenger discoveries / ${item.discoveries.length} evaluated proposals`, `${key}:proposals`, true);
      if (!item.discoveries.length) proposals.append(el("p", "muted", "No evaluated model proposal recorded. Invalid proposals and provider errors, if any, appear in the event ledger."));
      item.discoveries.forEach((report, index) => proposals.append(probeView(report, `Challenger witness ${index + 1} / admission requires a second independent replay`, `${key}:discovery:${index}`)));
      section.append(proposals);
    } else section.append(el("p", "notice", "Transfer does not generate new Challenger probes. Only memory frozen before source reveal is eligible for transfer detection."));
    const repairs = disclosure(`Repair decisions & source diffs / ${item.repairs.length} proposals`, `${key}:repairs`);
    if (!item.repairs.length) repairs.append(el("p", "muted", "No repair proposal recorded."));
    for (const repair of item.repairs) {
      const repairKey = `${key}:repair:${repair.number}`;
      const block = el("section", "learning-repair");
      block.append(el("h4", "", `Repair ${repair.number} / ${repair.accepted ? "Accepted" : "Not accepted"}`), el("p", "", repair.decision || "No controller decision recorded yet."));
      if (repair.patch) block.append(el("p", "", repair.patch.summary));
      block.append(repair.source_diff ? ui.renderDiff(repair.source_diff) : el("p", "muted", "No source diff recorded."));
      block.append(ui.renderReport(repair.development_report, "Proposed repair / development", `${repairKey}:development`, "Not evaluated."));
      repair.probe_reports.forEach((report, index) => block.append(probeView(report, `Repair memory replay ${index + 1}`, `${repairKey}:probe:${index}`, false)));
      block.append(dataDisclosure("Exact proposed patch & candidate source", { patch: repair.patch, candidate_source: repair.candidate_source }, `${repairKey}:source`));
      repairs.append(block);
    }
    section.append(repairs);
    const final = disclosure("Final gates & regression replays", `${key}:final`);
    final.append(el("p", "field-note", `Candidate freeze: ${dateText(item.frozen_at)}`), reportPair(item, `${key}:final`, false));
    if (!item.final_probe_reports.length) final.append(el("p", "muted", "No final regression replay recorded."));
    item.final_probe_reports.forEach((report, index) => final.append(probeView(report, `Final memory replay ${index + 1}`, `${key}:final:probe:${index}`, false)));
    section.append(final);
    const source = disclosure("Exact untouched & recorded final source / never executed here", `${key}:sources`);
    source.append(el("h4", "", "Untouched source"), el("pre", "source", item.original_source), el("h4", "", "Recorded final source"), el("pre", "source", item.final_source || "No final source recorded."));
    section.append(source, previews(run, item, key));
    return section;
  }

  function memoryView(run) {
    const fragment = document.createDocumentFragment();
    fragment.append(el("p", "field-note mono", `Content SHA-256: ${run.memory_hash || "Not frozen"}`), el("p", "field-note", `Frozen: ${dateText(run.memory_frozen_at)} · Transfer source revealed: ${dateText(run.transfer_source_revealed_at)}`));
    if (!run.memory.length) fragment.append(el("p", "notice", "No executable regression has been admitted. An empty memory hash is not evidence of learning."));
    for (const entry of run.memory) {
      const key = `learning:${run.learning_id}:memory:${entry.entry_id}`;
      const imported = entry.origin_run_id !== run.learning_id;
      const block = disclosure(`${imported ? "Imported" : "Learned in this run"} / ${entry.plan.name}`, key, true);
      block.append(el("p", "field-note", `Server-admitted ${dateText(entry.verified_at)} · discovered on ${entry.discovered_case_id}`), el("p", "mono field-note", `Entry: ${entry.entry_id} · Origin: ${entry.origin_run_id}`), el("p", "mono field-note", `Probe SHA-256: ${entry.probe_hash}`));
      block.append(planView(entry.plan, "Executable memory / exact retained proposal"));
      const matching = counterexample(entry.witness) && counterexample(entry.confirmation) && entry.witness.source_hash === entry.confirmation.source_hash && entry.probe_hash === entry.witness.probe_hash && entry.probe_hash === entry.confirmation.probe_hash;
      block.append(notice(matching ? "Admission recorded by Python: two error-free failing replays on the same source and executable probe. Open both browser observations for independent replay identifiers; the dashboard does not invent provenance." : "Admission record is inconsistent with matching error-free failures. Do not treat this entry as verified.", matching ? "" : "error"));
      for (const [label, report] of [["First independent browser witness", entry.witness], ["Fresh independent browser confirmation", entry.confirmation]]) {
        const replay = disclosure(label, `${key}:${label}`);
        replay.append(probeView(report, label, `${key}:${label}:report`, false), dataDisclosure("Exact browser observations / replay provenance", report.observations, `${key}:${label}:provenance`));
        block.append(replay);
      }
      const failingSource = disclosure("Original failing source retained with this regression", `${key}:source`);
      failingSource.append(el("pre", "source", entry.failing_source));
      block.append(failingSource);
      fragment.append(block);
    }
    return fragment;
  }

  function render() {
    renderHistory();
    connection();
    const run = state.runs.get(state.selected);
    renderRail(run);
    $("learning-record").hidden = !run;
    if (!run) {
      $("learning-live").textContent = state.selected ? `Requested record ${state.selected} is not loaded. Refresh to retry; no paid job will be started.` : "No learning execution selected.";
      return;
    }
    const status = `${active(run) ? "Current execution" : "Recorded evidence / not a running demo"}: ${human(run.status)} · ${run.learning_id}`;
    $("learning-live").textContent = status;
    if (state.announced !== status) { $("announcer").textContent = status; state.announced = status; }
    mount("learning-overview", [run.learning_id, run.status, run.verdict, run.created_at, run.finished_at, run.weave, run.errors], () => {
      const fragment = document.createDocumentFragment();
      const verdicts = { not_evaluated: "The learning claim is not yet evaluated.", transfer_verified: "Controller verdict: transfer verified.", transfer_missed: "Frozen memory missed the transfer defect.", repair_rejected: "A repair was rejected. Detection is not repair success.", error: "The experiment reported an error. No successful outcome is implied." };
      fragment.append(el("h3", "", verdicts[run.verdict] || human(run.verdict)), el("p", "field-note", `Created ${dateText(run.created_at)} · Finished ${dateText(run.finished_at)}`));
      const telemetry = el("p", "field-note", run.weave.enabled ? "Weave enabled for this record. " : "Untraced record / not sponsor-integrated. ");
      const trace = run.weave.enabled ? ui.weaveLink(run.weave.url) : null;
      const evaluation = run.weave.enabled ? ui.weaveLink(run.weave.evaluation_url, "Open published Weave evaluation") : null;
      telemetry.append(trace || document.createTextNode("No verified trace link available."));
      if (evaluation) telemetry.append(document.createTextNode(" · "), evaluation);
      fragment.append(telemetry);
      if (run.weave.error) fragment.append(notice(`Weave: ${run.weave.error}`, "warning"));
      if (run.errors.length) fragment.append(ui.errorList(run.errors));
      return fragment;
    });
    mount("learning-downloads", [run.learning_id, run.memory_frozen_at], () => {
      const fragment = document.createDocumentFragment();
      for (const [path, label] of [["memory", "Download executable memory JSON"], ["evidence", "Download full evidence JSON"]]) {
        const link = el("a", "button secondary small", label);
        link.href = `${endpoint(run.learning_id)}/${path}`;
        link.download = `keyproof-${run.learning_id}-${path}.json`;
        link.dataset.focusKey = `learning:${run.learning_id}:${path}`;
        fragment.append(link);
      }
      const link = el("a", "field-note", "Permalink to this record / no rerun");
      link.href = `/?learning=${encodeURIComponent(run.learning_id)}#learning-lab`;
      fragment.append(link);
      return fragment;
    });
    mount("learning-transfer", [run.memory_hash, run.memory_frozen_at, run.transfer_source_revealed_at, run.cases.map((item) => [item.partition, item.original_source_hash, item.initial_memory_reports]), run.verdict], () => {
      const hits = transferHits(run);
      const block = el("section", `learning-transfer ${hits.length ? "detected" : ""}`);
      block.append(el("h3", "", hits.length ? "Frozen memory detected a separate defect — before repair." : "Transfer detection must be earned."));
      block.append(el("p", "", hits.length ? `${hits.length} error-free failing initial ${hits.length === 1 ? "replay matches" : "replays match"} the untouched transfer source and frozen memory probe hashes. Detection is separate from whether a later repair passes.` : run.verdict === "transfer_missed" ? "The controller records a miss. Existing probes did not establish the separate defect; no new transfer Challenger probe may fill the gap." : "No qualifying pre-repair detection is recorded. A proposal, runtime error, later repair failure or an empty memory hash is not transfer proof."));
      block.append(el("p", "field-note mono", `Memory frozen ${run.memory_frozen_at || "not recorded"} → transfer source revealed ${run.transfer_source_revealed_at || "not recorded"}.`));
      return block;
    });
    // Appending the transfer case must not discard existing training evidence.
    if ($("learning-cases").dataset.run !== run.learning_id) {
      $("learning-cases").replaceChildren();
      $("learning-cases").dataset.run = run.learning_id;
      state.caseGeneration = (state.caseGeneration || 0) + 1;
    }
    run.cases.forEach((item, index) => {
      const id = `learning-case-${index}`;
      if (!$(id)) {
        const slot = el("div");
        slot.id = id;
        $("learning-cases").append(slot);
      }
      mount(id, [state.caseGeneration, run.learning_id, item], () => caseView(run, item));
    });
    mount("learning-memory", [run.learning_id, run.memory, run.memory_hash, run.memory_frozen_at, run.transfer_source_revealed_at], () => memoryView(run));
    mount("learning-usage", [run.learning_id, run.usage, run.config], () => {
      const fragment = document.createDocumentFragment();
      fragment.append(ui.renderUsage(run), dataDisclosure("Exact submitted configuration / aggregate budgets", run.config, `learning:${run.learning_id}:config`));
      return fragment;
    });
    mount("learning-events", [run.learning_id, run.events], () => ui.renderTimeline(run));
  }

  async function refresh(manual = false) {
    if (state.refreshing) return;
    state.refreshing = true;
    $("learning-refresh").disabled = true;
    let updated = false;
    try {
      const result = await request("/api/learning");
      state.catalog = result.catalog;
      state.summaries = new Map(result.runs.map((run) => [run.learning_id, run]));
      state.loaded = true;
      state.error = "";
      if (manual) state.uncertain = false;
      if (!state.selected && result.runs.length) {
        state.selected = [...result.runs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0].learning_id;
      }
      const selected = state.selected;
      const revision = state.summaries.get(selected)?.revision;
      if (selected && (manual || !state.runs.has(selected) || state.revisions.get(selected) !== revision)) {
        try {
          const run = await request(endpoint(selected));
          state.runs.set(run.learning_id, run);
          updated = true;
          state.revisions.set(selected, revision);
          if (state.selected === selected) state.selectedError = "";
        } catch (error) {
          if (state.selected === selected) state.selectedError = `Selected learning record: ${error.message}`;
        }
      }
    } catch (error) {
      state.error = `Learning history unavailable: ${error.message}`;
    } finally {
      state.refreshing = false;
      $("learning-refresh").disabled = false;
      syncBusy();
      if (updated || !state.runs.has(state.selected)) render();
      else { renderHistory(); connection(); readiness(); }
    }
  }

  $("learning-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!$("learning-form").reportValidity()) return;
    readiness();
    if ($("learning-start").disabled) return;
    const fields = new FormData($("learning-form"));
    const config = {
      mode: "team", provider: fields.get("provider"), model: fields.get("model").trim() || null,
      max_iterations: Number(fields.get("max_iterations")), max_model_calls: Number(fields.get("max_model_calls")),
      max_output_tokens: Number(fields.get("max_output_tokens")), max_input_tokens: Number(fields.get("max_input_tokens")),
      require_weave: Boolean(ui.readiness().status?.weave?.enabled) || !$("learning-local").checked,
      memory_run_id: fields.get("memory_run_id") || null,
    };
    state.starting = true;
    state.error = "";
    syncBusy();
    try {
      const run = await request("/api/learning", { method: "POST", body: JSON.stringify(config) });
      state.runs.set(run.learning_id, run);
      state.summaries.set(run.learning_id, {
        learning_id: run.learning_id, status: run.status, verdict: run.verdict,
        created_at: run.created_at, finished_at: run.finished_at,
        memory_count: run.memory.length, memory_hash: run.memory_hash,
        memory_frozen_at: run.memory_frozen_at,
      });
      select(run.learning_id);
    } catch (error) {
      state.uncertain = true;
      state.error = `Start not confirmed: ${error.message} Refresh retained history before any new launch; this request will not be automatically repeated.`;
      connection();
    } finally {
      state.starting = false;
      syncBusy();
      await ui.refreshReadiness();
    }
  });
  $("learning-provider").addEventListener("change", readiness);
  $("learning-local").addEventListener("change", readiness);
  $("learning-refresh").addEventListener("click", () => refresh(true));
  $("retry").addEventListener("click", () => refresh(true));
  document.addEventListener("keyproof-readiness", readiness);
  readiness();
  refresh();
  window.setInterval(() => refresh(), 2500);
})();
