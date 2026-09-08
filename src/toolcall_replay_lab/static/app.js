"use strict";

const state = {
  scenarios: [],
  limits: null,
  selectedId: null,
  importedTrace: null,
  importedName: null,
  result: null,
  filter: "all",
  busy: true,
  inspectorTrigger: null,
  replayEpoch: 0,
  importEpoch: 0,
  catalogEpoch: 0,
  catalogController: null,
  replayController: null
};

const elements = {
  shell: document.querySelector(".app-shell"),
  labRail: document.querySelector(".lab-rail"),
  workspace: document.querySelector("#workspace"),
  scenarioList: document.querySelector("#scenario-list"),
  scenarioContext: document.querySelector("#scenario-context"),
  traceFile: document.querySelector("#trace-file"),
  importStatus: document.querySelector("[data-testid='import-status']"),
  replayButton: document.querySelector("#replay-button"),
  resetButton: document.querySelector("#reset-button"),
  runHelper: document.querySelector("#run-helper"),
  runState: document.querySelector("#run-state-label"),
  labStatus: document.querySelector("#lab-status"),
  errorRegion: document.querySelector("#error-region"),
  runControls: document.querySelector("#run-controls"),
  resultsRoot: document.querySelector("#results-root"),
  inspector: document.querySelector("#inspector"),
  inspectorTitle: document.querySelector("#inspector-title"),
  inspectorContent: document.querySelector("#inspector-content"),
  closeInspector: document.querySelector("#close-inspector")
};

function node(tag, options = {}) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.testId) element.dataset.testid = options.testId;
  for (const [name, value] of Object.entries(options.attributes ?? {})) {
    element.setAttribute(name, String(value));
  }
  return element;
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function selectedScenario() {
  return state.scenarios.find((scenario) => scenario.id === state.selectedId) ?? null;
}

function setBusy(busy, message) {
  state.busy = busy;
  elements.workspace.setAttribute("aria-busy", String(busy));
  elements.replayButton.disabled = busy || !state.selectedId;
  elements.labStatus.textContent = message;
  elements.runState.textContent = busy ? "Replay in progress" : "Ready offline";
}

function clearError() {
  elements.errorRegion.replaceChildren();
}

function invalidateReplay(message) {
  state.replayEpoch += 1;
  if (!state.replayController) return;
  state.replayController.abort();
  state.replayController = null;
  setBusy(false, message);
}

function invalidateImport() {
  state.importEpoch += 1;
}

function showError(message, retry) {
  elements.errorRegion.replaceChildren();
  const panel = node("section", {
    className: "error-panel",
    attributes: { role: "alert", "aria-labelledby": "application-error-title" }
  });
  panel.append(
    node("p", { className: "error-kind", text: "Application or input error" }),
    node("h2", {
      className: "error-title",
      text: "Evaluation could not be completed",
      testId: "application-error-title",
      attributes: { id: "application-error-title" }
    }),
    node("p", { text: message })
  );
  if (retry) {
    const button = node("button", { className: "secondary-action", text: "Try again" });
    button.type = "button";
    button.addEventListener("click", retry, { once: true });
    panel.append(button);
  }
  elements.errorRegion.append(panel);
  elements.labStatus.textContent = message;
}

function rejectImport(message) {
  showError(message);
  elements.importStatus.textContent = "Import rejected";
  elements.traceFile.value = "";
}

async function readJsonResponse(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("The local service returned an unreadable response.");
  }
  if (!response.ok) {
    const message = isRecord(payload?.error) && typeof payload.error.message === "string"
      ? payload.error.message
      : "The local service could not complete this request.";
    throw new Error(message);
  }
  return payload;
}

function validateScenarioPayload(payload) {
  if (
    !isRecord(payload) ||
    payload.schema_version !== "1.0" ||
    !Array.isArray(payload.scenarios) ||
    !isRecord(payload.limits) ||
    !Number.isSafeInteger(payload.limits.max_upload_bytes) ||
    payload.limits.max_upload_bytes < 1 ||
    !Number.isSafeInteger(payload.limits.max_events) ||
    payload.limits.max_events < 0 ||
    !Number.isSafeInteger(payload.limits.max_json_nodes) ||
    payload.limits.max_json_nodes < 1 ||
    !Number.isSafeInteger(payload.limits.max_json_depth) ||
    payload.limits.max_json_depth < 1
  ) {
    throw new Error("The local scenario catalog is invalid.");
  }
  for (const scenario of payload.scenarios) {
    if (
      !isRecord(scenario) ||
      typeof scenario.id !== "string" ||
      !scenario.id ||
      typeof scenario.name !== "string" ||
      !scenario.name ||
      typeof scenario.description !== "string" ||
      typeof scenario.tone !== "string" ||
      !["PASS", "FAIL"].includes(scenario.expected_verdict)
    ) {
      throw new Error("The local scenario catalog is invalid.");
    }
  }
  return payload;
}

function renderScenarios() {
  const focusedScenario = elements.scenarioList.contains(document.activeElement)
    ? document.activeElement.dataset.scenarioId
    : null;
  elements.scenarioList.replaceChildren();
  if (state.scenarios.length === 0) {
    elements.scenarioList.append(node("p", { className: "quiet", text: "No built-in scenarios available." }));
    return;
  }
  for (const scenario of state.scenarios) {
    const button = node("button", {
      className: "scenario-button",
      attributes: {
        type: "button",
        "aria-pressed": scenario.id === state.selectedId,
        "data-tone": scenario.tone,
        "data-scenario-id": scenario.id
      }
    });
    const copy = node("span");
    copy.append(
      node("span", { className: "scenario-name", text: scenario.name }),
      node("span", { className: "scenario-description", text: scenario.description }),
      node("span", {
        className: "scenario-expected",
        text: `Expected trace verdict: ${scenario.expected_verdict}`
      })
    );
    button.append(copy);
    button.addEventListener("click", () => chooseScenario(scenario.id));
    elements.scenarioList.append(button);
    if (scenario.id === focusedScenario) button.focus();
  }
}

function updateContext() {
  const scenario = selectedScenario();
  elements.scenarioContext.replaceChildren();
  if (!scenario) {
    elements.scenarioContext.textContent = "No scenario selected.";
    elements.runHelper.textContent = "Select a scenario to begin.";
    return;
  }
  elements.scenarioContext.append("You are reviewing: ", node("strong", { text: scenario.name }));
  elements.runHelper.textContent = state.importedTrace
    ? "The imported trace has no preset verdict. Its result comes from the evaluator."
    : scenario.expected_verdict === "FAIL"
      ? "This built-in trace is expected to receive FAIL. That means the evaluator should reject it."
      : "This built-in trace is expected to receive PASS. Every evaluated rule should match.";
  elements.replayButton.textContent = state.importedTrace ? "Replay imported trace" : "Replay trace";
  elements.replayButton.disabled = state.busy;
}

function chooseScenario(id) {
  if (id === state.selectedId && !state.result && !state.importedTrace) return;
  invalidateReplay("Replay cancelled after scenario change.");
  invalidateImport();
  state.selectedId = id;
  state.result = null;
  state.importedTrace = null;
  state.importedName = null;
  state.filter = "all";
  elements.traceFile.value = "";
  elements.importStatus.textContent = "No trace imported";
  clearError();
  closeInspector(false);
  renderScenarios();
  updateContext();
  renderEmpty();
  elements.labStatus.textContent = `${selectedScenario()?.name ?? "Scenario"} selected.`;
}

async function loadScenarios() {
  state.catalogController?.abort();
  const catalogEpoch = state.catalogEpoch + 1;
  const controller = new AbortController();
  state.catalogEpoch = catalogEpoch;
  state.catalogController = controller;
  clearError();
  setBusy(true, "Loading built-in scenarios.");
  elements.scenarioList.replaceChildren(node("p", { className: "loading-marker", text: "Loading built-in scenarios" }));
  try {
    const response = await fetch("/api/scenarios", {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      signal: controller.signal
    });
    const payload = validateScenarioPayload(await readJsonResponse(response));
    if (catalogEpoch !== state.catalogEpoch) return;
    state.scenarios = payload.scenarios;
    state.limits = payload.limits;
    state.selectedId = payload.scenarios[0]?.id ?? null;
    renderScenarios();
    updateContext();
    setBusy(false, state.selectedId ? "Built-in scenarios ready." : "No built-in scenarios available.");
  } catch (error) {
    if (catalogEpoch !== state.catalogEpoch) return;
    state.scenarios = [];
    state.selectedId = null;
    renderScenarios();
    updateContext();
    setBusy(false, "Built-in scenarios could not be loaded.");
    showError(error instanceof Error ? error.message : "Built-in scenarios could not be loaded.", loadScenarios);
  } finally {
    if (catalogEpoch === state.catalogEpoch) state.catalogController = null;
  }
}

function renderEmpty() {
  elements.resultsRoot.before(elements.runControls);
  const section = node("section", {
    className: "empty-state",
    testId: "results-empty",
    attributes: { "aria-labelledby": "empty-title" }
  });
  section.append(
    node("p", { className: "empty-kicker", text: "No replay yet" }),
    node("h2", { text: "Replay a trace to inspect its evidence", attributes: { id: "empty-title" } }),
    node("p", {
      text: "The approved trace is selected by default for a one-click start. You can also choose the expected failing example or import a bounded JSON trace. Results come from the local evaluator."
    })
  );
  elements.resultsRoot.replaceChildren(section);
}

const commonEventKeys = new Set(["schema_version", "case_id", "step", "kind"]);
const eventKeys = {
  tool_call: new Set([...commonEventKeys, "tool", "arguments", "approval_id"]),
  approval: new Set([...commonEventKeys, "approval_id", "status", "tool", "arguments"]),
  result: new Set([...commonEventKeys, "status", "output"])
};

function validateEvent(event, index) {
  if (!isRecord(event) || !eventKeys[event.kind]) {
    throw new Error(`Event ${index + 1} must be a tool_call, approval, or result object.`);
  }
  if (Object.keys(event).some((key) => !eventKeys[event.kind].has(key))) {
    throw new Error(`Event ${index + 1} contains an unknown field.`);
  }
  if (
    event.schema_version !== "1.0" ||
    typeof event.case_id !== "string" ||
    !/^[a-z][a-z0-9-]*$/.test(event.case_id) ||
    !Number.isSafeInteger(event.step) ||
    event.step !== index + 1
  ) {
    throw new Error(`Event ${index + 1} has an invalid schema version, case ID, or step.`);
  }
  if (event.kind === "tool_call") {
    if (
      typeof event.tool !== "string" ||
      !/^[a-z][a-z0-9_.-]*$/.test(event.tool) ||
      !isRecord(event.arguments) ||
      (Object.hasOwn(event, "approval_id") &&
        (typeof event.approval_id !== "string" || !event.approval_id))
    ) {
      throw new Error(`Tool call ${index + 1} requires a tool and arguments object.`);
    }
  } else if (event.kind === "approval") {
    if (
      typeof event.approval_id !== "string" ||
      !event.approval_id ||
      !["granted", "denied"].includes(event.status) ||
      typeof event.tool !== "string" ||
      !/^[a-z][a-z0-9_.-]*$/.test(event.tool) ||
      !isRecord(event.arguments)
    ) {
      throw new Error(`Approval ${index + 1} is incomplete.`);
    }
  } else if (!["completed", "failed"].includes(event.status) || !Object.hasOwn(event, "output")) {
    throw new Error(`Result ${index + 1} requires a status and output value.`);
  }
}

function validateImportedTrace(value) {
  if (!isRecord(value)) throw new Error("The imported trace must be a JSON object.");
  const keys = Object.keys(value);
  if (keys.some((key) => key !== "schema_version" && key !== "events") || keys.length !== 2) {
    throw new Error("The trace root may contain only schema_version and events.");
  }
  if (value.schema_version !== "1.0") throw new Error("The trace must use schema version 1.0.");
  if (!Array.isArray(value.events)) throw new Error("The trace events field must be an array.");
  if (value.events.length === 0) throw new Error("The trace must contain at least one event.");
  if (value.events.length > state.limits.max_events) {
    throw new Error(`The trace exceeds the ${state.limits.max_events} event limit.`);
  }
  value.events.forEach(validateEvent);
  let nodes = 0;
  for (const event of value.events) {
    nodes += boundedJsonNodeCount(event, state.limits.max_json_nodes - nodes);
    if (nodes > state.limits.max_json_nodes) {
      const limit = state.limits.max_json_nodes.toLocaleString("en-US");
      throw new Error(`The trace exceeds the ${limit}-node limit.`);
    }
  }
  if (value.events.at(-1).kind !== "result" || value.events.filter((event) => event.kind === "result").length !== 1) {
    throw new Error("The trace must end with exactly one result event.");
  }
  const caseId = value.events[0].case_id;
  if (value.events.some((event) => event.case_id !== caseId)) {
    throw new Error("Every trace event must use the same case ID.");
  }
  return value;
}

function assertRoundTripSafeJson(value, maxDepth) {
  const pending = [[value, 0]];
  while (pending.length > 0) {
    const [current, depth] = pending.pop();
    if (depth > maxDepth) {
      throw new Error(`The trace exceeds the ${maxDepth}-level nesting limit.`);
    }
    if (typeof current === "number") {
      if (!Number.isFinite(current) || (Number.isInteger(current) && !Number.isSafeInteger(current))) {
        throw new Error("JSON numbers must be finite numbers and safe integers.");
      }
    } else if (Array.isArray(current)) {
      for (const item of current) pending.push([item, depth + 1]);
    } else if (isRecord(current)) {
      for (const item of Object.values(current)) pending.push([item, depth + 1]);
    }
  }
}

function boundedJsonNodeCount(value, limit) {
  let nodes = 0;
  const pending = [value];
  while (pending.length > 0) {
    const current = pending.pop();
    nodes += 1;
    if (Array.isArray(current)) {
      for (const item of current) pending.push(item);
    } else if (isRecord(current)) {
      const values = Object.values(current);
      nodes += values.length;
      for (const item of values) pending.push(item);
    }
    if (nodes > limit) return nodes;
  }
  return nodes;
}

class DuplicateJsonKeyError extends Error {}

function assertNoDuplicateJsonKeys(text) {
  const stack = [];
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (character === "{") {
      stack.push({ kind: "object", keys: new Set(), expectingKey: true });
      continue;
    }
    if (character === "[") {
      stack.push({ kind: "array" });
      continue;
    }
    if (character === "}" || character === "]") {
      stack.pop();
      continue;
    }
    if (character === ",") {
      const frame = stack.at(-1);
      if (frame?.kind === "object") frame.expectingKey = true;
      continue;
    }
    if (character !== '"') continue;

    const start = index;
    let escaped = false;
    for (index += 1; index < text.length; index += 1) {
      const stringCharacter = text[index];
      if (escaped) {
        escaped = false;
      } else if (stringCharacter === "\\") {
        escaped = true;
      } else if (stringCharacter === '"') {
        break;
      }
    }
    const frame = stack.at(-1);
    if (frame?.kind !== "object" || !frame.expectingKey) continue;
    let after = index + 1;
    while (/\s/.test(text[after] ?? "")) after += 1;
    if (text[after] !== ":") continue;
    const key = JSON.parse(text.slice(start, index + 1));
    if (frame.keys.has(key)) {
      const preview = key.length > 64 ? `${key.slice(0, 61)}...` : key;
      throw new DuplicateJsonKeyError(`Duplicate JSON key ${JSON.stringify(preview)} is not allowed.`);
    }
    frame.keys.add(key);
    frame.expectingKey = false;
  }
}

async function importTrace(file) {
  invalidateReplay("Replay cancelled before trace import.");
  invalidateImport();
  const importEpoch = state.importEpoch;
  clearError();
  state.importedTrace = null;
  state.importedName = null;
  state.result = null;
  renderEmpty();
  updateContext();
  if (!file) {
    elements.importStatus.textContent = "No trace imported";
    return;
  }
  if (!state.limits) {
    rejectImport("Wait for the local scenario catalog before importing a trace.");
    return;
  }
  if (!file.name.toLowerCase().endsWith(".json") || !["", "application/json"].includes(file.type)) {
    rejectImport("Choose a JSON file with the application/json type.");
    return;
  }
  if (file.size > state.limits.max_upload_bytes) {
    const kib = Math.floor(state.limits.max_upload_bytes / 1024);
    rejectImport(`The file exceeds the ${kib} KB limit.`);
    return;
  }
  try {
    let text;
    try {
      const bytes = await file.arrayBuffer();
      if (importEpoch !== state.importEpoch) return;
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      throw new Error("The imported trace must contain valid UTF-8.");
    }
    let parsed;
    try {
      assertNoDuplicateJsonKeys(text);
      parsed = JSON.parse(text);
    } catch (error) {
      if (error instanceof DuplicateJsonKeyError) throw error;
      throw new Error("The imported trace must contain valid JSON.");
    }
    assertRoundTripSafeJson(parsed, state.limits.max_json_depth);
    state.importedTrace = validateImportedTrace(parsed);
    state.importedName = file.name;
    elements.importStatus.textContent = `${file.name} ready`;
    updateContext();
    elements.labStatus.textContent = `${file.name} validated locally.`;
  } catch (error) {
    if (importEpoch !== state.importEpoch) return;
    rejectImport(error instanceof Error ? error.message : "The imported trace is invalid.");
    updateContext();
  }
}

function validateReplayPayload(payload) {
  if (
    !isRecord(payload) ||
    payload.schema_version !== "1.0" ||
    !isRecord(payload.scenario) ||
    !["PASS", "FAIL"].includes(payload.scenario.expected_verdict) ||
    !isRecord(payload.report) ||
    !["PASS", "FAIL"].includes(payload.report.verdict) ||
    !Array.isArray(payload.report.cases) ||
    payload.report.cases.length === 0 ||
    !isRecord(payload.timeline) ||
    !Array.isArray(payload.timeline.baseline) ||
    !Array.isArray(payload.timeline.candidate) ||
    !isRecord(payload.determinism) ||
    typeof payload.determinism.verified !== "boolean" ||
    typeof payload.determinism.digest !== "string" ||
    !Number.isSafeInteger(payload.determinism.runs_compared)
  ) {
    throw new Error("The local evaluator returned an invalid replay result.");
  }
  return payload;
}

async function replay() {
  const scenario = selectedScenario();
  if (!scenario || state.busy) return;
  invalidateImport();
  const replayEpoch = state.replayEpoch + 1;
  const controller = new AbortController();
  state.replayEpoch = replayEpoch;
  state.replayController = controller;
  clearError();
  closeInspector(false);
  setBusy(true, `Replaying ${scenario.name}.`);
  elements.replayButton.textContent = "Replaying";
  try {
    const body = state.importedTrace
      ? { scenario_id: scenario.id, source: "upload", trace: state.importedTrace }
      : { scenario_id: scenario.id, source: "builtin" };
    const response = await fetch("/api/replay", {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body),
      signal: controller.signal
    });
    const payload = validateReplayPayload(await readJsonResponse(response));
    if (replayEpoch !== state.replayEpoch) return;
    state.result = payload;
    state.filter = "all";
    renderResults();
    setBusy(false, `Replay complete. Overall verdict ${state.result.report.verdict}.`);
    elements.runState.textContent = "Replay complete";
  } catch (error) {
    if (replayEpoch !== state.replayEpoch) return;
    state.result = null;
    renderEmpty();
    setBusy(false, "Replay failed without publishing a result.");
    showError(error instanceof Error ? error.message : "Replay failed without publishing a result.");
  } finally {
    if (replayEpoch === state.replayEpoch) state.replayController = null;
  }
  if (replayEpoch === state.replayEpoch) updateContext();
}

function ruleStepSet(rule) {
  const steps = new Set();
  const evidence = isRecord(rule.evidence) ? rule.evidence : {};
  if (Array.isArray(evidence.violating_steps)) {
    for (const step of evidence.violating_steps) if (Number.isSafeInteger(step)) steps.add(step);
  }
  if (Array.isArray(evidence.violations)) {
    for (const item of evidence.violations) {
      if (isRecord(item) && Number.isSafeInteger(item.step)) steps.add(item.step);
    }
  }
  if (rule.verdict === "FAIL" && Array.isArray(evidence.steps)) {
    for (const step of evidence.steps) if (Number.isSafeInteger(step)) steps.add(step);
  }
  if (rule.verdict === "FAIL" && rule.type === "call_order") {
    for (const key of ["first_tool_step", "then_tool_step"]) {
      if (Number.isSafeInteger(evidence[key])) steps.add(evidence[key]);
    }
  }
  if (
    rule.verdict === "FAIL" &&
    rule.type === "step_budget" &&
    Number.isSafeInteger(evidence.max_steps)
  ) {
    for (const event of state.result?.timeline?.candidate ?? []) {
      if (isRecord(event) && Number.isSafeInteger(event.step) && event.step > evidence.max_steps) {
        steps.add(event.step);
      }
    }
  }
  if (rule.verdict === "FAIL" && rule.type === "expected_result") {
    const events = state.result?.timeline?.candidate ?? [];
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if (isRecord(event) && event.kind === "result" && Number.isSafeInteger(event.step)) {
        steps.add(event.step);
        break;
      }
    }
  }
  return steps;
}

function caseResult() {
  return state.result.report.cases[0];
}

function failuresForEvent(event) {
  return caseResult().candidate.rules.filter(
    (rule) => rule.verdict === "FAIL" && ruleStepSet(rule).has(event.step)
  );
}

function eventName(event) {
  if (event.kind === "result") return "Result";
  if (typeof event.tool === "string") return event.tool;
  return event.kind;
}

function eventMeta(event) {
  if (event.kind === "approval") return `Step ${event.step} · approval · ${event.status}`;
  if (event.kind === "result") return `Step ${event.step} · ${event.status}`;
  return `Step ${event.step} · tool call`;
}

function badge(verdict) {
  return node("span", { className: `badge ${verdict === "PASS" ? "pass" : ""}`, text: verdict });
}

function filteredEvents() {
  const events = state.result.timeline.candidate;
  if (state.filter === "divergences") return events.filter((event) => failuresForEvent(event).length > 0);
  if (state.filter === "tool_call") return events.filter((event) => event.kind === "tool_call");
  if (state.filter === "approval") return events.filter((event) => event.kind === "approval");
  if (state.filter === "result") return events.filter((event) => event.kind === "result");
  return events;
}

function emptyTimelineMessage() {
  if (state.result.timeline.candidate.length === 0) return "No candidate events were observed.";
  if (state.filter === "approval") return "No approval events match this trace.";
  if (state.filter === "divergences") return "No divergent events match this trace.";
  if (state.filter === "tool_call") return "No tool call events match this trace.";
  if (state.filter === "result") return "No result events match this trace.";
  return "No candidate events were observed.";
}

function renderTimeline(container) {
  const list = node("ol", { className: "timeline-list" });
  const events = filteredEvents();
  if (events.length === 0) {
    container.append(node("p", { className: "timeline-empty", testId: "timeline-empty", text: emptyTimelineMessage() }));
    return;
  }
  for (const event of events) {
    const failures = failuresForEvent(event);
    const item = node("li");
    const button = node("button", {
      className: `timeline-button ${failures.length ? "fail" : ""}`,
      testId: "timeline-event",
      attributes: {
        type: "button",
        "aria-label": `Step ${event.step}, ${eventName(event)}, ${failures.length ? "divergence" : "passes"}`
      }
    });
    button.append(node("span", { className: "step-number", text: event.step }));
    const copy = node("span");
    copy.append(
      node("span", { className: "event-name", text: eventName(event) }),
      node("span", { className: "timeline-meta", text: eventMeta(event) })
    );
    button.append(copy, badge(failures.length ? "FAIL" : "PASS"));
    button.addEventListener("click", () => {
      if (failures[0]) openInspector(failures[0], button);
      else openEventInspector(event, button);
    });
    item.append(button);
    list.append(item);
  }
  container.append(list);
}

function setFilter(filter) {
  const restoreFocus = document.activeElement?.classList.contains("filter-button");
  state.filter = filter;
  renderResults();
  if (restoreFocus) {
    elements.resultsRoot.querySelector("[data-filter][aria-pressed='true']")?.focus();
  }
  elements.labStatus.textContent = `${filter === "all" ? "All" : filter} timeline filter active.`;
}

function filterButton(label, value, count) {
  const button = node("button", {
    className: "filter-button",
    text: label,
    attributes: { type: "button", "aria-pressed": state.filter === value, "data-filter": value }
  });
  if (count !== undefined) button.append(node("span", { className: "filter-count", text: count }));
  button.addEventListener("click", () => setFilter(value));
  return button;
}

function passingCount(evaluation) {
  return evaluation.rules.filter((rule) => rule.verdict === "PASS").length;
}

function renderRules(container, caseData) {
  const list = node("ul", { className: "rule-list" });
  caseData.candidate.rules.forEach((candidateRule, index) => {
    const baselineRule = caseData.baseline.rules[index];
    const item = node("li");
    const button = node("button", {
      className: `rule-button ${candidateRule.verdict === "FAIL" ? "fail" : ""}`,
      attributes: { type: "button" }
    });
    const copy = node("span");
    copy.append(
      node("span", { className: "rule-name", text: candidateRule.rule_id }),
      node("span", { className: "rule-type", text: candidateRule.type }),
      node("span", { className: "rule-message", text: candidateRule.message })
    );
    button.append(copy, badge(candidateRule.verdict));
    button.addEventListener("click", () => openInspector(candidateRule, button, baselineRule));
    item.append(button);
    list.append(item);
  });
  container.append(list);
}

function appendDiffGroup(container, title, values, variant) {
  container.append(node("h3", { className: "subheading", text: title }));
  if (values.length === 0) {
    container.append(node("p", { className: "diff-empty", text: `No ${title.toLowerCase()}.` }));
    return;
  }
  const list = node("ul", { className: `diff-list ${variant}` });
  values.forEach((id) => list.append(node("li", { text: id })));
  container.append(list);
}

function evidenceValue(value) {
  const rendered = typeof value === "string" ? value : JSON.stringify(value);
  return rendered.length > 96 ? `${rendered.slice(0, 93)}...` : rendered;
}

function naturalList(values) {
  if (values.length < 2) return values.join("");
  return `${values.slice(0, -1).join(", ")} and ${values.at(-1)}`;
}

function ruleComparisonSummary(candidateRule, baselineRule) {
  const evidence = isRecord(candidateRule.evidence) ? candidateRule.evidence : {};
  if (
    candidateRule.type === "call_presence" &&
    typeof evidence.tool === "string" &&
    Number.isSafeInteger(evidence.min_calls) &&
    Number.isSafeInteger(evidence.max_calls) &&
    Number.isSafeInteger(evidence.observed_count)
  ) {
    const steps = Array.isArray(evidence.steps)
      ? evidence.steps.filter((step) => Number.isSafeInteger(step))
      : [];
    const location = steps.length > 0
      ? ` at ${steps.length === 1 ? "step" : "steps"} ${naturalList(steps)}`
      : "";
    return `Expected ${evidence.tool} between ${evidence.min_calls} and ${evidence.max_calls} calls. Observed ${evidence.observed_count} calls${location}.`;
  }
  if (
    candidateRule.type === "call_order" &&
    typeof evidence.first_tool === "string" &&
    typeof evidence.then_tool === "string"
  ) {
    const firstStep = Number.isSafeInteger(evidence.first_tool_step)
      ? `step ${evidence.first_tool_step}`
      : "no observed step";
    const thenStep = Number.isSafeInteger(evidence.then_tool_step)
      ? `step ${evidence.then_tool_step}`
      : "no observed step";
    return `Expected ${evidence.first_tool} before ${evidence.then_tool}. Observed ${evidence.first_tool} at ${firstStep} and ${evidence.then_tool} at ${thenStep}.`;
  }
  if (candidateRule.type === "argument_constraint") {
    const observations = Array.isArray(evidence.observations) ? evidence.observations : [];
    const violatingSteps = Array.isArray(evidence.violating_steps) ? evidence.violating_steps : [];
    const observation = observations.find((item) =>
      isRecord(item) && violatingSteps.includes(item.step)
    ) ?? observations[0];
    const allowed = Array.isArray(evidence.allowed_values) ? evidence.allowed_values : [];
    if (typeof evidence.argument === "string" && isRecord(observation) && allowed.length > 0) {
      const expected = allowed.slice(0, 3).map(evidenceValue).join(" or ");
      const observed = observation.present ? evidenceValue(observation.value) : "no value";
      return `Expected ${evidence.argument} to match ${expected}. Observed ${observed} at step ${observation.step}.`;
    }
  }
  if (candidateRule.type === "forbidden_tool" && typeof evidence.tool === "string") {
    const step = Array.isArray(evidence.violating_steps) ? evidence.violating_steps[0] : undefined;
    return `Expected ${evidence.tool} never to be called. Observed a call${Number.isSafeInteger(step) ? ` at step ${step}` : ""}.`;
  }
  if (candidateRule.type === "approval_required" && typeof evidence.tool === "string") {
    const violation = Array.isArray(evidence.violations) ? evidence.violations[0] : undefined;
    const step = isRecord(violation) && Number.isSafeInteger(violation.step)
      ? ` at step ${violation.step}`
      : "";
    const reason = isRecord(violation) && typeof violation.reason === "string"
      ? violation.reason.replaceAll("_", " ")
      : "an invalid approval";
    return `Expected a valid approval before ${evidence.tool}. Observed ${reason}${step}.`;
  }
  if (
    candidateRule.type === "expected_result" &&
    typeof evidence.expected_status === "string" &&
    typeof evidence.observed_status === "string" &&
    typeof evidence.output_matches === "boolean"
  ) {
    const output = evidence.output_matches ? "matching output" : "different output";
    return `Expected result status ${evidence.expected_status} with matching output. Observed status ${evidence.observed_status} with ${output}.`;
  }
  if (
    candidateRule.type === "step_budget" &&
    Number.isSafeInteger(evidence.max_steps) &&
    Number.isSafeInteger(evidence.observed_steps)
  ) {
    return `Expected no more than ${evidence.max_steps} steps. Observed ${evidence.observed_steps} steps.`;
  }
  return `Baseline ${baselineRule?.verdict ?? "not available"}; candidate ${candidateRule.verdict}.`;
}

async function copyDigest(digest, status) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(digest);
    status.textContent = "Digest copied.";
  } catch {
    status.textContent = "Copy unavailable. Select the digest manually.";
  }
}

function renderResults() {
  const payload = state.result;
  const data = caseResult();
  const root = node("div", { className: "result-view" });
  const verdictPanel = node("section", {
    className: `verdict-panel ${payload.report.verdict === "PASS" ? "pass" : ""}`,
    attributes: { "aria-labelledby": "verdict-title" }
  });
  const primary = node("div", { className: "verdict-primary" });
  const expectedVerdict = payload.scenario.expected_verdict;
  let verdictMeaning;
  if (payload.imported) {
    verdictMeaning = payload.report.verdict === "PASS"
      ? "The evaluator completed successfully. This imported trace satisfies every evaluated rule."
      : "The evaluator completed successfully. This imported trace violates at least one evaluated rule.";
  } else if (payload.report.verdict === expectedVerdict) {
    verdictMeaning = payload.report.verdict === "PASS"
      ? "The evaluator completed successfully and confirmed this built-in trace."
      : "The evaluator completed successfully and correctly rejected this built-in trace.";
  } else {
    verdictMeaning = `The evaluator completed successfully. Expected ${expectedVerdict}, observed ${payload.report.verdict}.`;
  }
  primary.append(
    node("p", {
      className: "metric-label",
      text: "Evaluation complete",
      testId: "evaluation-status"
    }),
    node("p", { className: "verdict-label", text: "Trace verdict" }),
    node("h2", {
      className: "verdict-word",
      text: payload.report.verdict,
      testId: "overall-verdict",
      attributes: { id: "verdict-title" }
    }),
    node("p", {
      className: "verdict-copy",
      text: verdictMeaning,
      testId: "verdict-meaning"
    })
  );
  const baseline = node("div", { className: "verdict-metric", testId: "baseline-summary" });
  baseline.append(
    node("p", { className: "metric-label", text: "Baseline" }),
    badge(data.baseline.verdict),
    node("p", {
      className: "metric-value",
      text: `${passingCount(data.baseline)} / ${data.baseline.rules.length} rules passed`
    })
  );
  const candidate = node("div", { className: "verdict-metric", testId: "candidate-summary" });
  candidate.append(
    node("p", { className: "metric-label", text: "Candidate" }),
    badge(data.candidate.verdict),
    node("p", {
      className: "metric-value",
      text: `${passingCount(data.candidate)} / ${data.candidate.rules.length} rules passed`
    })
  );
  verdictPanel.append(primary, baseline, candidate);

  const metadata = node("div", { className: "metadata-strip" });
  const provenance = node("span", { testId: "provenance" });
  provenance.append(node("strong", { text: "Source: " }), payload.imported ? "Imported JSON" : "Built-in fixture");
  const suite = node("span");
  suite.append(node("strong", { text: "Suite: " }), String(payload.report.suite_id));
  const caseLabel = node("span");
  caseLabel.append(node("strong", { text: "Case: " }), String(data.case_id));
  metadata.append(provenance, suite, caseLabel);

  const filters = node("nav", { className: "filter-bar", attributes: { "aria-label": "Timeline filters" } });
  const divergenceCount = payload.timeline.candidate.filter((event) => failuresForEvent(event).length > 0).length;
  filters.append(
    filterButton("All", "all"),
    filterButton("Divergences", "divergences", divergenceCount),
    filterButton("Tool calls", "tool_call"),
    filterButton("Approvals", "approval"),
    filterButton("Result", "result")
  );

  const grid = node("div", { className: "results-grid" });
  const timelinePanel = node("section", { className: "section-panel", attributes: { "aria-labelledby": "timeline-title" } });
  timelinePanel.append(node("h2", { className: "section-heading", text: "Candidate call timeline", attributes: { id: "timeline-title" } }));
  renderTimeline(timelinePanel);

  const stack = node("div", { className: "details-stack" });
  const rulesPanel = node("section", { className: "section-panel", attributes: { "aria-labelledby": "rules-title" } });
  rulesPanel.append(node("h2", { className: "section-heading", text: "Results by rule", attributes: { id: "rules-title" } }));
  renderRules(rulesPanel, data);

  const evidencePanel = node("section", { className: "section-panel", attributes: { "aria-labelledby": "evidence-title" } });
  evidencePanel.append(node("h2", { className: "section-heading", text: "Diff and determinism", attributes: { id: "evidence-title" } }));
  const diffBlock = node("div", { className: "diff-block" });
  appendDiffGroup(diffBlock, "New candidate failures", data.diff.new_failures, "new");
  appendDiffGroup(diffBlock, "Resolved failures", data.diff.resolved_failures, "resolved");
  appendDiffGroup(diffBlock, "Unchanged failures", data.diff.unchanged_failures, "unchanged");
  const determinism = node("div", { className: "determinism-block" });
  const digestStatus = node("p", {
    className: "copy-status",
    text: "",
    testId: "digest-copy-status",
    attributes: { "aria-live": "polite" }
  });
  const digestRow = node("div", { className: "digest-row" });
  const digestValue = node("code", {
    className: "digest-value",
    text: payload.determinism.digest
  });
  const copyButton = node("button", {
    className: "digest-copy-button",
    text: "Copy digest",
    attributes: { type: "button" }
  });
  copyButton.addEventListener("click", () => copyDigest(payload.determinism.digest, digestStatus));
  digestRow.append(digestValue, copyButton);
  determinism.append(
    node("h3", { className: "subheading", text: "Determinism summary" }),
    node("p", {
      text: payload.determinism.verified
        ? `Verified across ${payload.determinism.runs_compared} local runs.`
        : `Not verified across ${payload.determinism.runs_compared} local runs.`
    }),
    digestRow,
    digestStatus
  );
  evidencePanel.append(diffBlock, determinism);
  stack.append(rulesPanel, evidencePanel);
  grid.append(timelinePanel, stack);
  root.append(verdictPanel, elements.runControls, metadata, filters, grid);
  elements.resultsRoot.replaceChildren(root);
}

function evidenceBlock(title, evidence, observed = false) {
  const section = node("section", { className: `comparison-block ${observed ? "observed" : ""}` });
  section.append(
    node("h3", { text: title }),
    node("pre", { className: "evidence-code", text: JSON.stringify(evidence ?? {}, null, 2) })
  );
  return section;
}

function correspondingBaselineRule(candidateRule) {
  return caseResult().baseline.rules.find((rule) => rule.rule_id === candidateRule.rule_id) ?? null;
}

function setInspectorTitle(title) {
  elements.inspectorTitle.textContent = title;
  elements.closeInspector.setAttribute("aria-label", `Close ${title.toLowerCase()}`);
}

function openInspector(candidateRule, trigger, baselineRule = correspondingBaselineRule(candidateRule)) {
  setInspectorTitle(candidateRule.verdict === "FAIL" ? "Divergence details" : "Rule details");
  state.inspectorTrigger = trigger;
  elements.inspectorContent.replaceChildren();
  elements.inspectorContent.append(
    node("p", { className: "inspector-status", text: candidateRule.verdict === "FAIL" ? "Rule violation" : "Rule result" }),
    node("h2", { className: "inspector-rule", text: candidateRule.rule_id }),
    node("p", { className: "inspector-copy", text: candidateRule.message }),
    node("p", {
      className: "comparison-summary",
      text: ruleComparisonSummary(candidateRule, baselineRule),
      testId: "comparison-summary"
    }),
    evidenceBlock("Expected", baselineRule?.evidence ?? {}),
    evidenceBlock("Observed", candidateRule.evidence ?? {}, true)
  );
  elements.inspector.hidden = false;
  elements.labRail.inert = true;
  elements.workspace.inert = true;
  elements.shell.classList.add("inspector-open");
  elements.closeInspector.focus();
}

function openEventInspector(event, trigger) {
  setInspectorTitle("Event details");
  state.inspectorTrigger = trigger;
  elements.inspectorContent.replaceChildren(
    node("p", { className: "inspector-status", text: "Observed event" }),
    node("h2", { className: "inspector-rule", text: eventName(event) }),
    node("p", { className: "inspector-copy", text: eventMeta(event) }),
    evidenceBlock("Observed", event, true)
  );
  elements.inspector.hidden = false;
  elements.labRail.inert = true;
  elements.workspace.inert = true;
  elements.shell.classList.add("inspector-open");
  elements.closeInspector.focus();
}

function closeInspector(restoreFocus = true) {
  elements.inspector.hidden = true;
  elements.labRail.inert = false;
  elements.workspace.inert = false;
  elements.shell.classList.remove("inspector-open");
  if (restoreFocus && state.inspectorTrigger?.isConnected) state.inspectorTrigger.focus();
  state.inspectorTrigger = null;
}

function resetLab() {
  invalidateReplay("Replay cancelled by reset.");
  invalidateImport();
  state.selectedId = state.scenarios[0]?.id ?? null;
  state.importedTrace = null;
  state.importedName = null;
  state.result = null;
  state.filter = "all";
  elements.traceFile.value = "";
  elements.importStatus.textContent = "No trace imported";
  clearError();
  closeInspector(false);
  renderScenarios();
  updateContext();
  renderEmpty();
  if (state.scenarios.length === 0) {
    void loadScenarios();
    return;
  }
  elements.labStatus.textContent = "Lab reset to its first built-in scenario.";
}

elements.replayButton.addEventListener("click", replay);
elements.resetButton.addEventListener("click", resetLab);
elements.traceFile.addEventListener("change", () => importTrace(elements.traceFile.files?.[0] ?? null));
elements.closeInspector.addEventListener("click", () => closeInspector(true));
document.addEventListener("keydown", (event) => {
  if (elements.inspector.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeInspector(true);
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...elements.inspector.querySelectorAll(
    "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
  )];
  if (focusable.length === 0) {
    event.preventDefault();
    elements.closeInspector.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable.at(-1);
  if (focusable.length === 1 || (event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  }
});

renderEmpty();
loadScenarios();
