"use strict";

/* The play page. Gemini reads the residents' circumstances (notes a visitor may rewrite)
 * against the shelters' facilities and says who must avoid which shelter; a visitor may
 * also report a road situation in words or close roads on the map. The agent rebuilds
 * the evacuation plan within those limits, and the map, the residents, the KPI strip,
 * the agent timeline and the backend strip follow it step by step. Every plan and
 * verdict comes from the server (Gemini through the private service, or the rule
 * policy) and is checked by the Simulation Core; this file only replays it.
 *
 * Layout: constants and game state → selectors (read the state, change nothing) → the
 * map and the playback clock → the drawX() panels, all called by render() → state
 * changes the visitor makes → server calls (dispatch() runs one try) → bootstrap.
 * A try moves game.phase through idle → care? → read? → run → done: "care" while Gemini
 * reads the residents' notes, "read" while it reads a report, "run" while the agent
 * decides. It may stop early in "unread" (Gemini read nothing to act on) or "failed".
 * Uses from map-view.js: the name tables, the DOM helpers (div, span, setText),
 * drawNetwork, captionAt, challengeSummary, stepEffect, postJson, isVerifiedGeminiRun,
 * liveErrorText, the shared wait and failure messages, and smaller helpers (roadName,
 * shelterPlaceName, personLabel, finalMoment, ...). */

// --- constants and game state ----------------------------------------------------------

const STEP_MS = 700;  // pause between revealed agent steps
const PROBE_MS = 260;  // how long each probed road flashes
const PLAYBACK_MS = 7000;  // one run of the virtual clock, start to deadline
const NOTE_MAX_CHARS = 100;  // mirrors NoteText in packages/domain/care.py
const SWEEP_SCENARIO = "river_district";  // the town with a pool of residents to sweep
// Example reports per town; a visitor can also type their own.
const SAMPLE_REPORTS = {
  base: ["道路B1が冠水して通れない", "分岐点の先の道路B2で事故、通行止め", "道路B1も道路B2も倒木でふさがっている"],
  // First the reports the agent can rebuild a plan for, then one where nobody can.
  river_district: ["小学校の前の道で土砂崩れ", "公民館へ向かう道に倒木", "橋が冠水して渡れないと消防から連絡"],
};

const towns = [];  // {key, title, scenario, plan, care}: care is the town's CareSet, or null
const RESTRICTION_REASON_NAMES = {
  step_free_access: "段差・坂",
  location_privacy: "所在を守る",
  night_safety: "夜間の安全",
  infant_care: "授乳",
  medical_power: "電源",
  pet_companion: "ペット",
  language_support: "言語",
  dietary_needs: "食事",
  quiet_room: "静かな部屋",
};
/* How the sweep's meet_need change is worded after the shelter name. location_privacy
 * has no entry because the sweep never proposes it. */
const NEED_CHANGE_PHRASES = {
  step_free_access: "を段差なしにする",
  night_safety: "に夜間の照明と巡回",
  infant_care: "に授乳・おむつ替えの場所",
  medical_power: "に非常用発電機",
  pet_companion: "でペットを受け入れる",
  language_support: "に多言語の案内",
  dietary_needs: "にアレルギー・宗教に配慮した非常食",
  quiet_room: "に静かな小部屋",
};

const game = {
  townIndex: 0,
  closed: [],
  ruleRun: null,  // ChallengeRun for the current closures (the world the map draws)
  geminiRun: null,  // verified LiveRun for the same closures
  reportReading: null,  // Gemini's reading of a report, while the closures still come from it
  // Restrictions in use: {restrictions, source: "gemini"|"reviewed", fallback, ms, model, seq}
  careReading: null,
  notes: {},  // notes the visitor rewrote, by resident id
  editing: null,  // resident whose note is being rewritten
  mode: null,  // "gemini" | "rule": who decides the run in view
  phase: "idle",  // idle | care | read | run | done | unread | failed (see the top of the file)
  liveFailed: false,  // Gemini was asked but the rule policy's answer is shown
  refused: false,  // the public service turned the reading down before calling anything
  busy: false,
  revealed: 0,  // agent steps shown so far
  probing: null,  // road flashing during the weakness search
  runSeq: 0,  // bumped on every change; each async step compares its copy and stops when stale
  fallbacks: [],  // what this run did instead of a failed Gemini reading, in words
  message: null,  // shown in place of the usual hint
  live: null,  // GET /v1/agent/live
  sweep: null,  // GET /v1/agent/care/sweep: every group of residents, checked without a model
  sweepFailed: false,
};
const playback = {time: 0, frame: null, last: null};

const $ = id => document.getElementById(id);
const sleep = ms => new Promise(done => setTimeout(done, ms));
const seconds = ms => (ms / 1000).toFixed(1) + " 秒";

// --- selectors: read the state, change nothing -----------------------------------------

function town() {
  return towns[game.townIndex];
}
function shownTrace() {
  return game.geminiRun ? game.geminiRun.trace : game.ruleRun ? game.ruleRun.trace : null;
}
function liveOn() {
  return Boolean(game.live && game.live.enabled);
}
/* The town's reviewed CareSet (static), not the restrictions in use (game.careReading). */
function careSet() {
  return town().care;
}
/* Live requests a Gemini run costs: a care reading, a report reading, then the run. */
function geminiNeeds() {
  return (careSet() ? 1 : 0) + (reportText() ? 1 : 0) + 1;
}
function restrictions() {
  return game.careReading ? game.careReading.restrictions : [];
}
function noteOf(resident) {
  return game.notes[resident.actor_id] ?? resident.note;
}
function shelterName(id) {
  const care = careSet();
  const facility = care && care.shelters.find(shelter => shelter.id === id);
  return facility ? facility.name : shelterPlaceName(town().scenario, id);
}
function geminiReady() {
  return liveOn() && game.live.runs_left_today >= geminiNeeds();
}
/* One line for the server's contract: pasted tabs or newlines become spaces. */
function oneLine(text) {
  return text.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
}
function reportText() {
  return oneLine($("report").value);
}
/* The readings made by the run in view (not ones left over from an earlier run). */
function freshReportReading() {
  const reading = game.reportReading;
  return reading && reading.seq === game.runSeq && game.mode === "gemini" ? reading : null;
}
function freshCareReading() {
  const reading = game.careReading;
  return reading && reading.source === "gemini" && reading.seq === game.runSeq ? reading : null;
}
function residentLabel(id) {
  const care = careSet();
  const resident = care && care.residents.find(r => r.actor_id === id);
  return resident ? resident.label : personLabel(town().scenario, id);
}
function repairStepIndex(trace) {
  return trace.steps.findIndex(step => step.decision && step.decision.action === "run_repair");
}
/* What the map shows now: the untouched world while choosing roads; during the agent's
 * steps, the failing plan until its repair is revealed; then the verified plan. */
function mapView() {
  const current = town();
  const closedByUser = new Set(game.closed);
  const trace = shownTrace();
  const editable = !game.busy;
  if (!trace) {
    return {scenario: current.scenario, plan: current.plan, result: null, kind: "blocked", closedByUser, editable,
      probing: game.probing};
  }
  const repaired = game.revealed > repairStepIndex(trace) && trace.final_plan !== null;
  const plan = repaired ? trace.final_plan : current.plan;
  const result = repaired ? trace.final_result : trace.initial_result;
  return {
    scenario: game.ruleRun.world, plan, result, kind: result.status === "PASS" ? "repaired" : "blocked",
    closedByUser, editable, probing: game.probing,
  };
}

// --- the map and the playback clock ----------------------------------------------------

function drawMap(time) {
  const view = mapView();
  playback.time = time;
  const second = Math.round(time);
  drawNetwork($("play-map"), view.scenario, view.plan, view.kind, {time, result: view.result}, view);
  const range = $("clock");
  range.max = String(view.scenario.deadline_s);
  range.value = String(second);
  setText($("hud-clock"), "t = " + second + " 秒");
  setText($("hud-deadline"), "期限 " + view.scenario.deadline_s + " 秒");
  setText($("caption"), view.result ? captionAt(view.scenario, view.plan, view.result, second) : idleCaption());
}
/* The caption before any run: what to do next. */
function idleCaption() {
  if (careSet()) return "「" + primaryLabel() + "」を押すと、住民ごとの避難先が決まります";
  if (game.closed.length) return "通行止めの道路が決まりました。「" + primaryLabel() + "」で始めます";
  if (liveOn()) return "状況を報告するか、地図の道路をクリックして通行止めにしてください";
  return "地図の道路をクリックして通行止めにしてください";
}
/* The same playback loop as app.js startPlayback(); each page draws its own views. */
function stopPlayback() {
  if (playback.frame !== null) cancelAnimationFrame(playback.frame);
  playback.frame = null;
  playback.last = null;
  $("play").textContent = "▶";
  $("play").setAttribute("aria-label", "再生");
}
function startPlayback(fromStart) {
  stopPlayback();
  const deadline = mapView().scenario.deadline_s;
  if (reducedMotion()) {
    drawMap(finalMoment(mapView().result));
    return;
  }
  let time = fromStart || playback.time >= deadline ? 0 : playback.time;
  $("play").textContent = "❚❚";
  $("play").setAttribute("aria-label", "一時停止");
  const tick = now => {
    if (playback.last !== null) time = Math.min(deadline, time + (now - playback.last) / PLAYBACK_MS * deadline);
    playback.last = now;
    drawMap(time);
    playback.frame = time >= deadline ? null : requestAnimationFrame(tick);
    if (playback.frame === null) stopPlayback();
  };
  playback.frame = requestAnimationFrame(tick);
}

// --- panels: each drawX() fills one part of the page; render() calls them all ----------

function drawTownTabs() {
  const tabs = $("stage-tabs");
  if (!tabs.childElementCount) {
    tabs.replaceChildren(...towns.map((entry, index) => {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.dataset.stage = String(index);  // the town's index
      tab.textContent = entry.title + "・" + entry.scenario.personas.length + "名";
      tab.addEventListener("click", () => {
        if (game.busy || game.townIndex === index) return;
        game.townIndex = index;
        $("report").value = "";
        forgetNotes();
        resetRun(true);
      });
      return tab;
    }));
  }
  tabs.querySelectorAll("[data-stage]").forEach(tab => {
    tab.setAttribute("aria-selected", String(Number(tab.dataset.stage) === game.townIndex));
    tab.disabled = game.busy;
  });
}
function drawChips() {
  $("road-chips").replaceChildren(...town().scenario.roads.map(road => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "road-chip";
    chip.textContent = roadName(road.id);
    chip.setAttribute("aria-pressed", String(game.closed.includes(road.id)));
    chip.disabled = game.busy;
    chip.addEventListener("click", () => toggleRoad(road.id));
    return chip;
  }));
  $("slots").replaceChildren(...game.closed.map(id => {
    const read = game.reportReading && game.reportReading.roads.includes(id);
    return span("slot filled" + (read ? " read" : ""), "✕ " + roadName(id) + (read ? "（Gemini）" : ""));
  }));
  $("report-samples").replaceChildren(...(SAMPLE_REPORTS[town().key] || []).map(text => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "sample-chip";
    chip.textContent = text;
    chip.disabled = game.busy || !liveOn();
    chip.addEventListener("click", () => {
      $("report").value = text;
      drawControls();
      $("dispatch-gemini").focus();
    });
    return chip;
  }));
}
function primaryLabel() {
  if (!liveOn()) return "Gemini（停止中）";
  if (game.live.runs_left_today === 0) return "本日のGemini実行枠は終了";
  return careSet() ? "Geminiで事情を読み、計画を組み直す" : "Geminiで計画を立て直す";
}
function drawControls() {
  const text = reportText();
  const geminiButton = $("dispatch-gemini");
  geminiButton.disabled = game.busy || !geminiReady() || (!careSet() && !text && !game.closed.length);
  setText(geminiButton, game.busy && game.mode === "gemini" ? "Geminiが判断しています…" : primaryLabel());
  const tooFewRunsLeft = liveOn() && game.live.runs_left_today > 0 && game.live.runs_left_today < geminiNeeds();
  $("dispatch").disabled = game.busy || (!careSet() && !game.closed.length);
  $("dispatch").title = careSet() ? "職員が同じメモから設定した条件で、ルール方策（AIなし）が計画します" : "";
  const care = careSet();
  $("care-lead").hidden = !care;
  if (care) {
    setText($("care-lead"), "住民" + care.residents.length + "人の事情メモと、避難所" + care.shelters.length +
      "か所の設備をGeminiが読み、誰がどこを避けるべきかを決めます。");
  }
  const box = $("road-box");
  if (box.dataset.stage !== String(game.townIndex)) {  // on entering a town (data-stage: its index)
    box.dataset.stage = String(game.townIndex);
    box.open = !care;  // the roads are the whole story only where there are no notes
  }
  setText($("road-box-title"), care ? "道路の状況も伝える（任意）" : "道路の状況");
  $("report").disabled = game.busy || !liveOn();
  $("report").placeholder = liveOn() ? "例：小学校の前の道で土砂崩れ" : "Geminiの停止中は使えません";
  const quota = $("quota");
  quota.className = "pl-quota" + (liveOn() ? "" : " off");
  setText(quota, liveOn() ? "Gemini 本日あと " + game.live.runs_left_today + " 回" : "Gemini 停止中");
  $("reset").disabled = game.busy || (!game.closed.length && !shownTrace() && !text && !Object.keys(game.notes).length);
  setText($("hint"), game.message || hintText(text, tooFewRunsLeft));
}
/* The hint under the buttons when there is no message: what the visitor can do now. */
function hintText(text, tooFewRunsLeft) {
  if (game.busy) {
    const who = game.mode === "gemini" && !game.liveFailed ? "Gemini" : "ルール方策";
    return shownTrace() ? who + "の判断を1ステップずつ再生しています" : "計算しています…";
  }
  if (!liveOn()) return "Geminiは停止中です。地図の道路を選び、「AIなしで比較」で試せます";
  if (tooFewRunsLeft) return "本日の残り回数では足りません。「AIなしで比較」で試せます";
  if (text) return "Geminiが報告から通れない道路を読み取り、そのまま計画を立て直します";
  if (shownTrace()) return careSet() ? "メモや道路を変えて、もう一度試せます" : "報告や道路を変えて、もう一度試せます";
  if (careSet()) return "住民のメモは書き換えられます。道路の状況も足せます";
  return game.closed.length ? "準備ができました" : "報告の例を選ぶか、自分で書いてください";
}

/* One row of the agent timeline. pill and actor are {cls, text} badges. */
function stepRow({className, dot, title, pill = null, actor = null, meta = "", effect = null, choices = null}) {
  const item = document.createElement("li");
  item.className = "st " + className;
  item.append(span("st-dot", dot));
  const body = div("st-body");
  const top = div("st-top");
  top.append(span("st-title", title));
  if (pill) top.append(span("st-pill " + pill.cls, pill.text));
  body.append(top);
  const line = div("st-meta");
  if (actor) line.append(span("st-actor " + actor.cls, actor.text));
  line.append(document.createTextNode(meta));
  body.append(line);
  if (effect) body.append(div("st-effect", effect));
  if (choices) body.append(choices);
  item.append(body);
  return item;
}
/* A step's effect in words, without internal ids (the slides show those). */
function effectText(effect, action) {
  if (action === "run_repair" && effect.plan_status) return "計画を作り直し → Coreの判定 " + effect.plan_status;
  return stepEffect(effect, action);
}
/* What the Core said about a step's effect, for its dot and pill. */
function stepVerdict(step) {
  if (step.policy_error || !step.decision) return {cls: "fail", pill: {cls: "fail", text: "停止"}};
  const effect = step.effect;
  switch (step.decision.action) {
    case "run_repair": {
      const ok = effect && effect.plan_status === "PASS";
      return {cls: ok ? "pass" : "fail", pill: {cls: ok ? "pass" : "fail", text: ok ? "PASS" : "FAIL"}};
    }
    case "run_redteam": {
      const weak = effect.probes.filter(probe => probe.status === "FAIL").length;
      return {cls: weak ? "hand" : "pass", pill: {cls: weak ? "warn" : "pass", text: weak ? "弱点 " + weak : "弱点なし"}};
    }
    case "plan_contingency": {
      const covered = effect.contingencies.filter(c => c.reason === "verified_alternative").length;
      const all = covered === effect.contingencies.length;
      return {cls: all ? "pass" : "hand", pill: {cls: all ? "pass" : "warn", text: "代替 " + covered + "/" + effect.contingencies.length}};
    }
    case "finish":
      return {cls: "pass", pill: {cls: "pass", text: "PASS"}};
    default:
      return {cls: "hand", pill: null};
  }
}
function choicesLine(step) {
  const line = div("st-choices");
  line.append(document.createTextNode("選べた手："));
  step.observation.allowed_actions.forEach((action, index) => {
    if (index) line.append(document.createTextNode("・"));
    const name = document.createElement(step.decision && step.decision.action === action ? "b" : "span");
    name.textContent = actionNames[action];
    line.append(name);
  });
  return line;
}
/* The care reading's row: whose notes were read, by whom, and one restriction as an example. */
function careRow() {
  const reading = game.careReading;
  const people = new Set(reading.restrictions.map(r => r.actor_id)).size;
  const byGemini = reading.source === "gemini";
  const first = reading.restrictions[0];
  const example = first ? "（例：" + residentLabel(first.actor_id) + " → " + shelterName(first.shelter_id) +
    " ✕ " + RESTRICTION_REASON_NAMES[first.reason] + "）" : "";
  const agreement = byGemini ? agreementText() : "";
  return stepRow({
    className: "read",
    dot: "事",
    title: byGemini ? "事情を読み取り" : "職員の設定を使用",
    pill: byGemini ? {cls: "ai", text: seconds(reading.ms)} : null,
    actor: byGemini ? {cls: "ai", text: "Gemini"} : {cls: "rule", text: "職員"},
    meta: reading.fallback ? "Geminiが読み取れなかったため、職員が同じメモから設定した条件で続行"
      : careSet().residents.length + "人のメモと" + careSet().shelters.length + "か所の設備から" +
        (agreement ? "。" + agreement : ""),
    effect: people + "人に" + reading.restrictions.length + "件の条件" + example,
  });
}
/* The report reading's row: the report as written and the roads Gemini closed from it. */
function reportRow() {
  const reading = game.reportReading;
  const roads = reading.roads.map(roadName);
  return stepRow({
    className: "read",
    dot: "読",
    title: "報告を読み取り",
    pill: {cls: "ai", text: seconds(reading.ms)},
    actor: {cls: "ai", text: "Gemini"},
    meta: "「" + reading.report + "」",
    effect: roads.length ? roads.join("・") + " を通行止めと判断" : "通れない道路は読み取れませんでした",
  });
}
/* One revealed agent step: the move and why, what the agent saw, and what the move did. */
function agentStepRow(step, index, actor) {
  const obs = step.observation;
  const verdict = stepVerdict(step);
  const title = step.decision ? actionNames[step.decision.action] : "判断なし";
  const why = step.decision ? decisionReasonNames[step.decision.reason_code] : "";
  const seen = "観測 " + obs.evacuated + "／" + obs.residents + "名・" + obs.plan_status;
  return stepRow({
    className: verdict.cls + (index === game.revealed - 1 && game.busy ? " fresh" : ""),
    dot: String(index + 1),
    title,
    pill: verdict.pill,
    actor,
    meta: why + "（" + seen + "）",
    effect: step.policy_error ? policyErrorNames[step.policy_error] : effectText(step.effect, step.decision.action),
    choices: choicesLine(step),
  });
}
/* The row for what the run is waiting for. */
function pendingRow(trace) {
  return stepRow({
    className: "pending",
    dot: "…",
    title: waitingText(trace),
    meta: game.mode === "gemini" && !trace ? "非公開のCloud Run経由でVertex AIを呼んでいます" : "",
  });
}
function waitingText(trace) {
  if (game.phase === "care") return "Geminiが住民の事情を読み取り中";
  if (game.phase === "read") return "Geminiが報告を読み取り中";
  if (game.phase === "run" && game.mode === "gemini") return "Geminiが次の一手を判断中";
  return !trace ? "計算中" : "次のステップ";
}
const EMPTY_STEPS = [
  "Geminiが報告から、通れない道路を読み取ります",
  "エージェントが「修復」「弱点探し」「代替計画」から次の一手を選びます",
  "一手ごとにSimulation Coreが計算し、成り立つかを判定します",
  "全員を救えないときは「完了」とは言わず、人に引き継ぎます",
];
function drawLog() {
  const trace = shownTrace();
  const items = [];
  if (game.careReading) items.push(careRow());
  if (game.reportReading) items.push(reportRow());
  if (trace) {
    const actor = game.geminiRun ? {cls: "ai", text: "Gemini"} : {cls: "rule", text: "ルール"};
    trace.steps.slice(0, game.revealed).forEach((step, index) => items.push(agentStepRow(step, index, actor)));
  }
  if (game.busy && (!trace || game.revealed < trace.steps.length)) items.push(pendingRow(trace));
  const log = $("agent-log");
  log.classList.toggle("empty", !items.length);
  if (!items.length) {
    log.replaceChildren(...EMPTY_STEPS.map((text, index) => {
      const item = document.createElement("li");
      item.append(span("", String(index + 1)), span("", text));
      return item;
    }));
  } else {
    log.replaceChildren(...items);
  }
  const who = $("agent-who");
  const labelAsGemini = game.mode === "gemini" ? !game.liveFailed : liveOn() && game.mode !== "rule";
  who.className = "pl-who" + (labelAsGemini ? " ai" : "");
  setText(who, labelAsGemini ? "Vertex AI Gemini" + (game.geminiRun ? "（" + game.geminiRun.trace.model + "）" : "")
    : game.mode ? "ルール方策（AIなし）" : "ルール方策");
}
function drawKpis() {
  const view = mapView();
  const people = view.scenario.personas.length;
  const verdict = $("kpi-verdict");
  if (view.result) {
    setText($("kpi-evac"), view.result.evacuated.length + "／" + people + "名");
    setText(verdict, view.result.status);
    verdict.className = view.result.status === "PASS" ? "pass" : "fail";
  } else {
    setText($("kpi-evac"), "—／" + people + "名");
    setText(verdict, "未判定");
    verdict.className = "";
  }
  const third = $("kpi-closed");
  if (careSet()) {
    setText($("kpi-third-label"), "事情に合わない割り当て");
    const unsuitable = view.result ? view.result.events.filter(e => e.reason === "unsuitable_shelter").length : null;
    setText(third, unsuitable === null ? "—" : unsuitable + "件");
    third.className = unsuitable === null ? "" : unsuitable ? "fail" : "pass";
  } else {
    setText($("kpi-third-label"), "通行止め");
    setText(third, game.closed.length ? game.closed.length + "本" : "なし");
    third.className = "";
  }
  const ai = $("kpi-ai");
  const trace = shownTrace();
  const [text, cls] = game.geminiRun ? ["Gemini " + game.geminiRun.trace.model_calls + "回", "ai"]
    : game.busy && game.mode === "gemini" && !game.liveFailed ? ["Gemini 判断中", "ai"]
      : trace ? ["ルール（AIなし）", ""] : ["—", ""];
  setText(ai, text);
  ai.className = cls;
}
function drawResult() {
  const trace = shownTrace();
  const box = $("result");
  const done = trace && !game.busy && game.revealed >= trace.steps.length;
  box.hidden = !done;
  if (!done) return;
  const view = mapView();
  const summary = careSet() && !trace.outcome.startsWith("halted")
    ? careResultSummary(trace, view) : roadResultSummary(trace, view);
  box.className = "pl-result " + summary.tone;
  setText($("result-tag"), summary.tag);
  setText($("result-title"), summary.title);
  setText($("result-text"), summary.text);
}
/* Where residents have needs, everyone placed within their limits is the result; road
 * weaknesses are a footnote. */
function careResultSummary(trace, view) {
  const rebuilt = trace.final_result && trace.final_result.status === "PASS";
  if (!rebuilt) {
    return {
      tone: "handover",
      tag: "HAND OVER",
      title: "事情を守ると全員を避難させる計画がないため、人へ引き継ぎ",
      text: "条件を守れる避難所が足りません。一部の人だけの計画を「完了」にはせず、人に判断を引き継ぎます。",
    };
  }
  const escalated = trace.outcome === "escalated";
  const who = game.geminiRun ? "Gemini" : "エージェント（ルール方策）";
  return {
    tone: "clear",
    tag: "REBUILT",
    title: "全員を、事情に合う避難所へ" + (escalated ? "（代わりのない弱点は人へ）" : ""),
    text: who + "が組み直した計画を、Simulation Coreが確認しました：事情に合わない割り当て 0件、定員と期限と通行止めを守って " +
      view.scenario.personas.length + "名全員が避難。" +
      (escalated ? "道路1本の通行止めで崩れる弱点のうち、代わりの計画がないものは人に引き継ぎます。" : ""),
  };
}
/* Elsewhere the roads are the story: rebuilt, handed over to people, or halted. */
function roadResultSummary(trace, view) {
  const rebuilt = trace.final_result && trace.final_result.status === "PASS";
  const who = game.geminiRun ? "Gemini" : "エージェント（ルール方策）";
  const text = challengeSummary(trace, view.scenario, view.plan, who);
  if (trace.outcome.startsWith("halted")) {
    return {tone: "halted", tag: "HALTED", title: outcomeNames[trace.outcome], text};
  }
  if (!rebuilt) return {tone: "handover", tag: "HAND OVER", title: "全員を救う計画がないため、人へ引き継ぎ", text};
  if (trace.outcome === "escalated") {
    return {tone: "handover", tag: "REBUILT + HAND OVER", title: "全員の計画を立て直し、代わりのない弱点は人へ", text};
  }
  return {tone: "clear", tag: "REBUILT", title: outcomeNames[trace.outcome] || trace.outcome, text};
}

/* Gemini's reading set against the reviewer's for the same notes, resident and shelter
 * by shelter. Only when no note was rewritten: the reviewer never saw a rewrite. */
function agreementText() {
  const reading = game.careReading;
  if (!reading || reading.source !== "gemini" || Object.keys(game.notes).length) return "";
  const pair = r => r.actor_id + "/" + r.shelter_id;
  const read = new Set(reading.restrictions.map(pair));
  const reviewed = new Set(careSet().reviewed_restrictions.map(pair));
  const same = [...read].filter(key => reviewed.has(key)).length;
  return "職員の設定と比べて、一致 " + same + "件・Geminiだけ " + (read.size - same) + "件・見落とし " +
    (reviewed.size - same) + "件";
}

/* The residents: each one's note, what was read from it, and where the plan sends them. */
function drawResidents() {
  const care = careSet();
  $("people-card").hidden = !care;
  if (!care) return;
  const pooled = care.patterns > 1;
  $("swap").hidden = !pooled;
  $("swap").disabled = game.busy;
  setText($("people-lead"), (pooled ? "全員架空の住民です（100人のうち、組み合わせ " + (care.pattern + 1) + " の12人）。"
    : "全員架空の住民です。") + "番号は地図の住民と同じです。メモを書き換えると、Geminiが読み直して避難先が変わります。");
  const view = mapView();
  const limits = new Map();
  restrictions().forEach(r => limits.set(r.actor_id, (limits.get(r.actor_id) || []).concat(r)));
  const outcome = {
    intents: new Map(view.plan.intents.map(intent => [intent.actor_id, intent])),
    arrived: new Set(view.result ? view.result.evacuated : []),
    rejected: new Map((view.result ? view.result.events : [])
      .filter(event => event.kind === "rejected").map(event => [event.actor_id, event.reason])),
    limits,
  };
  $("people").replaceChildren(...care.residents.map(resident => residentItem(resident, view, outcome)));
  const reading = game.careReading;
  setText($("people-status"), !reading ? "まだ読み取っていません"
    : (reading.source === "gemini" ? "Geminiの読み取り" : "職員の設定（AIなし）") + "・条件 " +
      reading.restrictions.length + " 件");
}
/* One resident's card: number, name and home; the note (or its editor); the shelters the
 * reading rules out; and, once the Core has run, where they went. */
function residentItem(resident, view, outcome) {
  const id = resident.actor_id;
  const mine = outcome.limits.get(id) || [];
  const item = document.createElement("li");
  item.className = "person" + (mine.length ? " restricted" : "") +
    (view.result ? (outcome.arrived.has(id) ? " arrived" : " rejected") : "");
  const persona = view.scenario.personas.find(person => person.id === id);
  const top = div("person-top");
  top.append(span("person-no", String(personNumber(view.scenario, id))), span("person-name", resident.label),
    span("person-home", persona ? nodeNames[persona.start_node] || "" : ""));
  item.append(top);
  if (game.editing === id) {
    item.append(noteEditor(resident));
  } else {
    const note = div("person-note");
    note.append(document.createTextNode(noteOf(resident)));
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "書き換え";
    edit.disabled = game.busy || !liveOn();
    edit.setAttribute("aria-label", resident.label + "のメモを書き換える");
    edit.addEventListener("click", () => {
      game.editing = id;
      render();
    });
    note.append(edit);
    item.append(note);
    if (id in game.notes) item.append(span("person-edited", "書き換え済み：Geminiが読み直します"));
  }
  if (mine.length) {
    const tags = div("person-tags");
    mine.forEach(r => tags.append(span("person-tag",
      "✕ " + shelterName(r.shelter_id) + "（" + RESTRICTION_REASON_NAMES[r.reason] + "）")));
    item.append(tags);
  }
  if (view.result) {
    const intent = outcome.intents.get(id);
    const why = outcome.rejected.get(id);
    const ok = outcome.arrived.has(id);
    item.append(span("person-go " + (ok ? "ok" : "ng"), ok ? "→ " + shelterName(intent.shelter_id) + "へ避難"
      : "✕ " + (why ? reasonNames[why] || why : "未避難")));
  }
  return item;
}

/* The sweep: thirty groups of residents, which ones a plan can serve, and the changes to
 * a shelter that would serve more. All of it computed by the server without a model. */
function changeText(change) {
  const shelter = shelterName(change.shelter_id);
  return change.change === "add_capacity" ? shelter + "の定員を" + game.sweep.capacity_step + "人増やす"
    : shelter + NEED_CHANGE_PHRASES[change.reason];
}
function causeText(check) {
  const care = careSet();
  const label = id => {
    const resident = care.residents.find(r => r.actor_id === id);
    return resident ? resident.label : "住民";
  };
  if (check.cause === "no_shelter") {
    return check.residents.map(label).join("・") + "の事情に合う避難所がありません";
  }
  if (check.cause === "capacity") {
    const capacity = town().scenario.shelters.filter(s => check.shelters.includes(s.id))
      .reduce((sum, s) => sum + s.capacity, 0);
    return check.shelters.map(shelterName).join("・") + "にしか行けない人が" + check.residents.length +
      "人いて、定員の" + capacity + "人を超えます";
  }
  return "期限内に全員が着ける経路と避難所の組み合わせがありません";
}
function drawSweep() {
  const care = careSet();
  const card = $("sweep-card");
  card.hidden = !care || care.patterns <= 1;
  if (card.hidden) return;
  const sweep = game.sweep;
  if (!sweep) {
    setText($("sweep-total"), game.sweepFailed ? "検算できませんでした" : "検算中…");
    setText($("sweep-sub"), "");
    $("sweep-grid").replaceChildren();
    setText($("sweep-current"), game.sweepFailed ? "時間をおいてページを読み込み直してください" : "—");
    $("sweep-steps").replaceChildren();
    return;
  }
  const total = sweep.patterns.length;
  setText($("sweep-total"), total + "通りのうち、全員の避難先が決まるのは " + sweep.verified + "通り");
  const causes = {no_shelter: 0, capacity: 0, route: 0};
  sweep.patterns.forEach(check => { if (check.cause) causes[check.cause] += 1; });
  const parts = [["事情に合う避難所がない人がいる", causes.no_shelter], ["定員が足りない", causes.capacity],
    ["経路が足りない", causes.route]].filter(([, count]) => count).map(([name, count]) => name + " " + count + "通り");
  setText($("sweep-sub"), parts.length ? "決まらない理由：" + parts.join("・") : "");
  $("sweep-grid").replaceChildren(...sweep.patterns.map(check => {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "sw-cell " + (check.verified ? "pass" : "fail");
    cell.textContent = String(check.pattern + 1);
    const state = check.verified ? "全員の避難先が決まる" : "全員の避難先は決まらない";
    cell.setAttribute("aria-label", "組み合わせ " + (check.pattern + 1) + "：" + state);
    cell.title = "組み合わせ " + (check.pattern + 1) + "：" + state + "（押すとこの12人に入れ替え）";
    cell.setAttribute("aria-current", String(check.pattern === care.pattern));
    cell.disabled = game.busy;
    cell.addEventListener("click", () => loadPattern(check.pattern));
    return cell;
  }));
  const current = sweep.patterns.find(check => check.pattern === care.pattern);
  setText($("sweep-current"), !current ? "—"
    : "表示中の組み合わせ " + (care.pattern + 1) + "：" + (current.verified
      ? "職員の設定なら、全員の避難先が決まります"
      : causeText(current) + "。エージェントは一部だけの計画を完了とせず、人に引き継ぎます"));
  const row = (no, text, count, className = "") => {
    const item = document.createElement("li");
    item.className = className;
    const number = document.createElement("b");
    number.textContent = no;
    const result = document.createElement("em");
    result.textContent = count;
    item.append(number, span("", text), result);
    return item;
  };
  let before = sweep.verified;
  const rows = sweep.steps.map((step, index) => {
    const gain = step.verified - before;
    before = step.verified;
    return row(String(index + 1), changeText(step), step.verified + "通り（+" + gain + "）");
  });
  $("sweep-steps").replaceChildren(row("0", "いまの設備のまま", sweep.verified + "通り", "base"), ...rows);
}
/* The Enter that confirms an IME conversion is not a submit. Safari ends the composition
 * before that Enter's keydown, so only its keyCode (229) gives it away. */
function isImeConfirm(event) {
  return event.isComposing || event.keyCode === 229;
}
function noteEditor(resident) {
  const form = div("person-edit");
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = NOTE_MAX_CHARS;
  input.value = noteOf(resident);
  input.setAttribute("aria-label", resident.label + "の事情メモ");
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "決定";
  const commit = () => {
    const note = oneLine(input.value).slice(0, NOTE_MAX_CHARS);
    if (note && note !== resident.note) game.notes[resident.actor_id] = note;
    else delete game.notes[resident.actor_id];
    game.editing = null;
    resetRun(false);  // a rewritten note needs a new reading
  };
  save.addEventListener("click", commit);
  input.addEventListener("keydown", event => {
    if (isImeConfirm(event)) return;
    if (event.key === "Enter") commit();
    if (event.key === "Escape") {
      game.editing = null;
      render();
    }
  });
  form.append(input, save);
  requestAnimationFrame(() => input.focus());
  return form;
}

/* The backend strip: each hop a request went through, filled from the answers the
 * server returned (model, call count, time), never from guesses. Internal ids and
 * versions stay out of the page: they are in Cloud Logging for whoever operates it. */
function hopStates() {
  const geminiRun = game.geminiRun;
  const viaAi = game.mode === "gemini";
  const off = (detail) => ({state: "off", detail});
  const hops = {
    web: {state: "idle", detail: "この画面"},
    public: {state: "idle", detail: "画面と計算の窓口"},
    private: liveOn() ? {state: "idle", detail: "認証された依頼だけを受け付け"} : off("停止中"),
    gemini: liveOn() ? {state: "idle", detail: "事情の読み取りと次の一手"} : off("停止中"),
    core: {state: "idle", detail: "成り立つかを計算で判定"},
    log: {state: "idle", detail: "実行ごとに記録"},
  };
  if (game.phase === "idle") return hops;
  hops.web.state = "done";
  hops.public.state = "done";
  if (game.phase === "care" || game.phase === "read" || (game.phase === "run" && viaAi)) {
    hops.private = {state: "active", detail: "認証して呼び出し中"};
    hops.gemini = {state: "active", detail: game.phase === "care" ? "住民の事情を読み取り中"
      : game.phase === "read" ? "報告を読み取り中" : "各ステップの判断中"};
    return hops;
  }
  if (game.phase === "run") {
    hops.public.state = "active";
    hops.private = off("使いません");
    hops.gemini = off("AIなし");
    hops.core.state = "active";
    hops.log = off("—");
    return hops;
  }
  const failed = game.phase === "failed";
  const reportReading = freshReportReading();
  const careReading = freshCareReading();
  if (geminiRun || reportReading || careReading) {
    const model = (geminiRun && geminiRun.trace.model) || (reportReading && reportReading.model) ||
      (careReading && careReading.model);
    const parts = [];
    if (careReading) parts.push("事情1回");
    if (reportReading) parts.push("報告1回");
    if (geminiRun) parts.push("判断" + geminiRun.trace.model_calls + "回");
    const ms = geminiMs(geminiRun, reportReading, careReading);
    hops.private = {state: "done", detail: "認証済み・" + seconds(ms)};
    hops.gemini = {state: "done", detail: model + "・" + parts.join("・")};
    const calls = [careReading, reportReading, geminiRun].filter(Boolean).length;
    hops.log = {state: "done", detail: "この実行を記録（" + calls + "件）"};
  } else if (viaAi && game.refused) {
    hops.public = {state: "failed", detail: "この依頼は受け付けませんでした"};
    hops.private = off("呼び出していません");
    hops.gemini = off("呼び出していません");
    hops.log = off("—");
  } else {
    hops.private = viaAi ? {state: "failed", detail: "応答なし"} : off("使いません");
    hops.gemini = viaAi ? {state: "failed", detail: game.liveFailed ? "失敗：ルール方策で表示" : "応答なし"} : off("AIなし");
    hops.log = off("—");
  }
  if (game.liveFailed) hops.gemini = {state: "failed", detail: "判断は失敗：ルール方策で表示"};
  const trace = shownTrace();
  if (trace) {
    hops.core = {state: "done", detail: geminiRun
      ? "Geminiの判断を計算し直して一致（" + trace.steps.length + "ステップ）"
      : "ルール方策の判断を検算（" + trace.steps.length + "ステップ）"};
  } else if (game.phase === "unread") {
    hops.core = {state: "idle", detail: "道路が決まらず未実行"};
  } else if (failed) {
    hops.core = {state: "failed", detail: "未実行"};
  }
  return hops;
}
/* Time spent in Gemini calls during the run in view, as the server measured it. */
function geminiMs(geminiRun, reportReading, careReading) {
  return (careReading ? careReading.ms : 0) + (reportReading ? reportReading.ms : 0) +
    (geminiRun ? geminiRun.duration_ms : 0);
}
const HOPS = [
  {key: "web", tag: "WEB", name: "ブラウザ"},
  {key: "public", tag: "RUN", name: "Cloud Run 公開"},
  {key: "private", tag: "IAM", name: "Cloud Run 非公開"},
  {key: "gemini", tag: "AI", name: "Vertex AI Gemini"},
  {key: "core", tag: "CORE", name: "Simulation Core"},
  {key: "log", tag: "LOG", name: "Cloud Logging"},
];
function drawBackend() {
  const states = hopStates();
  $("hops").replaceChildren(...HOPS.map(hop => {
    const item = document.createElement("li");
    const state = states[hop.key];
    item.className = "hop " + state.state;
    item.append(span("hop-tag", hop.tag), span("hop-name", hop.name), span("hop-detail", state.detail));
    return item;
  }));
  setText($("backend-status"), backendStatusText(game.geminiRun, freshReportReading(), freshCareReading()));
  setText($("backend-note"), !liveOn()
    ? "ライブAIは停止中です。有効にすると、公開サービスがIAMで認証して非公開サービスを呼び、そこでだけVertex AIのGeminiが動きます。"
    : game.mode === "rule" && !game.liveFailed
      ? "「AIなしで比較」は公開サービスの中のルール方策が判断します。AIは使わず、結果はSimulation Coreが計算します。"
      : "公開サービスはVertex AIの権限を持たず、IAMで認証した非公開サービスだけがGeminiを呼べます。Geminiの判断は公開サービスがSimulation Coreで再生し、結果が一致したものだけを表示します。各実行はCloud Loggingに記録されます。");
}
function backendStatusText(geminiRun, reportReading, careReading) {
  if (game.phase === "idle") return "待機中";
  if (game.phase === "care" || game.phase === "read" || game.phase === "run") return "実行中…";
  if (game.phase === "failed") return "失敗";
  if (game.liveFailed) return "完了（Geminiの実行は失敗・ルール方策で表示）";
  if (geminiRun || reportReading || careReading) {
    return "完了・Gemini " + seconds(geminiMs(geminiRun, reportReading, careReading));
  }
  return "完了";
}
function render() {
  drawTownTabs();
  drawChips();
  drawControls();
  drawResidents();
  drawSweep();
  drawLog();
  drawKpis();
  drawResult();
  drawBackend();
  drawMap(playback.time);
}

// --- state changes the visitor makes ---------------------------------------------------

function resetRun(clearRoads) {
  game.runSeq += 1;
  if (clearRoads) {
    game.closed = [];
    game.reportReading = null;
  }
  game.ruleRun = null;
  game.geminiRun = null;
  game.careReading = null;
  game.mode = null;
  game.phase = "idle";
  game.liveFailed = false;
  game.refused = false;
  game.revealed = 0;
  game.probing = null;
  game.message = null;
  stopPlayback();
  playback.time = 0;
  render();
}
/* Drops the visitor's rewritten notes and closes any open note editor. */
function forgetNotes() {
  game.notes = {};
  game.editing = null;
}
/* Shows text in place of the hint for one redraw; the redraw after it is back to normal. */
function flashHint(text, redraw = drawControls) {
  game.message = text;
  redraw();
  game.message = null;
}
function toggleRoad(id) {
  if (game.busy) return;
  const at = game.closed.indexOf(id);
  if (at >= 0) game.closed.splice(at, 1);
  else if (game.closed.length < MAX_CLOSED) game.closed.push(id);
  else {
    flashHint("通行止めにできるのは" + MAX_CLOSED + "本までです。外す道路をクリックしてください");
    return;
  }
  game.reportReading = null;  // the closures no longer come from a report
  resetRun(false);
}

// --- server calls: one try of the agent, and what the page loads -----------------------

/* Shows the agent's recorded steps one at a time, in sync with the map. */
async function reveal(runSeq, trace) {
  const pause = reducedMotion() ? 0 : STEP_MS;
  for (let index = 0; index < trace.steps.length; index += 1) {
    if (runSeq !== game.runSeq) return false;
    const step = trace.steps[index];
    if (pause && step.decision && step.decision.action === "run_redteam" && step.effect && step.effect.probes) {
      for (const probe of step.effect.probes) {
        game.probing = probe.road_id;
        drawMap(0);
        await sleep(PROBE_MS);
        if (runSeq !== game.runSeq) return false;
      }
      game.probing = null;
    }
    game.revealed = index + 1;
    render();
    if (pause) await sleep(pause);
  }
  return runSeq === game.runSeq;
}
/* A failure whose message is shown to the visitor as is. */
class VisitorNotice extends Error {}
/* postJson that never throws: a network failure comes back as no response. */
async function safePost(url, body) {
  try {
    return await postJson(url, body);
  } catch (err) {
    return {response: null, payload: null};
  }
}
/* Gemini reads the report; its roads become the visitor's closures. False if none. */
async function readReport(runSeq, text) {
  const {response, payload} = await safePost("/v1/agent/read", {scenario: town().key, report: text});
  await loadLive();
  if (runSeq !== game.runSeq) return false;
  if (!response || !response.ok) {
    const reason = liveErrorText(payload, "Geminiが報告文を読み取れませんでした");
    if (careSet() || game.closed.length) {
      // The residents (or the clicked roads) are still worth planning for: go on without the report.
      game.fallbacks.push(reason + "。道路の状況は反映していません");
      return true;
    }
    throw new VisitorNotice(reason + "。地図の道路をクリックすれば、AIなしでも試せます");
  }
  const reading = payload.reading;
  game.reportReading = {report: text, roads: reading.closed_roads, ms: payload.duration_ms, model: reading.model,
    seq: runSeq};
  if (!reading.closed_roads.length) {
    game.phase = "unread";
    game.message = "Geminiは報告から、通れない道路を読み取れませんでした。道路や場所の名前を入れてみてください";
    return false;
  }
  game.closed = reading.closed_roads.slice();
  return true;
}
/* Gemini reads every resident's note (the visitor's rewrites included) against the
 * shelters; its restrictions become limits of the plan. False if it names none. */
async function readCare(runSeq) {
  const notes = Object.entries(game.notes).map(([actor_id, note]) => ({actor_id, note}));
  const {response, payload} = await safePost("/v1/agent/care",
    {scenario: town().key, pattern: careSet().pattern || 0, notes});
  await loadLive();
  if (runSeq !== game.runSeq) return false;
  if (!response || !response.ok) {
    // The plan still has to respect everyone's needs: use what a reviewer set from the same notes.
    game.careReading = {restrictions: careSet().reviewed_restrictions, source: "reviewed", fallback: true, seq: runSeq};
    const reason = liveErrorText(payload, "Geminiが住民の事情を読み取れませんでした");
    game.fallbacks.push(reason + "。職員が同じメモから設定した条件で続けます" +
      (Object.keys(game.notes).length ? "（書き換えたメモは反映していません）" : ""));
    return true;
  }
  const reading = payload.reading;
  game.careReading = {restrictions: reading.restrictions, source: "gemini", ms: payload.duration_ms,
    model: reading.model, seq: runSeq};
  if (!reading.restrictions.length && !game.closed.length && !reportText()) {
    game.phase = "unread";
    game.message = "Geminiはメモから、避けるべき避難所を読み取りませんでした。メモを書き足してみてください";
    return false;
  }
  return true;
}
/* Starts a try: clears what the last one showed and returns its runSeq. Gemini reads the
 * residents' notes first (where the town has them), then a report, then the agent runs. */
function beginRun(mode, text) {
  const runSeq = ++game.runSeq;
  game.busy = true;
  game.mode = mode;
  game.phase = careSet() && mode === "gemini" ? "care" : text ? "read" : "run";
  game.ruleRun = null;
  game.geminiRun = null;
  game.liveFailed = false;
  game.refused = false;
  game.revealed = 0;
  if (text) game.reportReading = null;
  game.careReading = null;
  game.message = null;
  game.fallbacks = [];
  game.editing = null;
  if (careSet() && mode === "rule") {
    // Without a model, nobody reads the notes: the reviewer's restrictions stand in.
    game.careReading = {restrictions: careSet().reviewed_restrictions, source: "reviewed", seq: runSeq};
  }
  stopPlayback();
  playback.time = 0;
  render();
  return runSeq;
}
/* The rule policy's run for these closures and restrictions; the map draws its world. */
async function fetchRuleRun(body) {
  const {response, payload} = await postJson("/v1/agent/challenge", body);
  if (!response.ok) throw new VisitorNotice("計画を計算できませんでした。条件を変えてもう一度お試しください");
  return payload;
}
/* Gemini's run for the same body, through the private service. A run that did not come
 * back, is not verified or was halted is not shown: fallbackNote says why instead. */
async function askGemini(body) {
  const {response, payload} = await safePost("/v1/agent/live", body);
  const shownInstead = "。同じ条件でのルール方策（AIなし）の結果を表示します";
  if (!response || !response.ok) {
    return {run: null, fallbackNote: liveErrorText(payload, "Geminiの判断が返ってきませんでした") + shownInstead};
  }
  if (!isVerifiedGeminiRun(payload)) {
    return {run: null, fallbackNote: "検証済みのGeminiの結果ではないため、ルール方策の結果を表示します"};
  }
  if (payload.trace.outcome.startsWith("halted")) {
    // The guard stopped Gemini midway (a late answer, or a move outside the menu): the
    // visitor still gets a complete, verified plan, and is told whose it is.
    const why = payload.trace.outcome === "halted_model_error"
      ? "Geminiの応答が時間内に返らず、判断が途中で止まりました"
      : "Geminiが許可されていない手を選んだため、ガードが判断を止めました";
    return {run: null, fallbackNote: why + shownInstead};
  }
  return {run: payload, fallbackNote: null};
}
async function dispatch(mode) {
  const text = mode === "gemini" ? reportText() : "";
  if (game.busy || (!careSet() && !text && !game.closed.length)) return;
  const runSeq = beginRun(mode, text);
  try {
    if (careSet() && mode === "gemini") {
      if (!(await readCare(runSeq))) return;
      game.phase = text ? "read" : "run";
      render();
    }
    if (text) {
      if (!(await readReport(runSeq, text))) return;
      $("report").value = "";
      game.phase = "run";
      render();
    }
    const body = {scenario: town().key, closed_roads: game.closed.slice(), restrictions: restrictions()};
    const ruleRun = await fetchRuleRun(body);
    if (runSeq !== game.runSeq) return;
    game.ruleRun = ruleRun;
    let fallbackNote = null;
    if (mode === "gemini") {
      game.message = GEMINI_WAIT_MESSAGE;
      render();
      const geminiAnswer = await askGemini(body);
      if (geminiAnswer.run) game.geminiRun = geminiAnswer.run;
      fallbackNote = geminiAnswer.fallbackNote;
      game.liveFailed = fallbackNote !== null;
      await loadLive();
    }
    if (runSeq !== game.runSeq) return;
    game.phase = "done";
    game.message = [...game.fallbacks, fallbackNote].filter(Boolean).join("。") || null;
    if (!(await reveal(runSeq, shownTrace()))) return;
    game.busy = false;
    render();
    startPlayback(true);
  } catch (err) {
    if (runSeq !== game.runSeq) return;
    game.phase = "failed";
    game.message = err instanceof VisitorNotice ? err.message : NETWORK_FAILED_MESSAGE;
  } finally {
    if (runSeq === game.runSeq && game.busy) {
      game.busy = false;
      render();
    }
  }
}

/* Another group of residents in the same homes. The roads stay as the visitor set them. */
async function loadPattern(pattern) {
  const current = town();
  if (game.busy || !current.care || pattern === current.care.pattern) return;
  game.busy = true;
  render();
  let loaded = null;
  try {
    const response = await fetch("/v1/agent/care?scenario=" + encodeURIComponent(current.key) +
      "&pattern=" + pattern, {cache: "no-store"});
    if (response.ok) loaded = await response.json();
  } catch (err) {
    loaded = null;
  }
  game.busy = false;
  if (!loaded) {
    flashHint("住民を入れ替えられませんでした。もう一度お試しください", render);
    return;
  }
  current.care = loaded;
  forgetNotes();
  resetRun(false);
}
async function loadSweep() {
  try {
    const response = await fetch("/v1/agent/care/sweep?scenario=" + SWEEP_SCENARIO, {cache: "no-store"});
    if (!response.ok) throw new Error("sweep");
    game.sweep = await response.json();
  } catch (err) {
    game.sweepFailed = true;
  }
  drawSweep();
}
async function loadLive() {
  try {
    const response = await fetch("/v1/agent/live", {cache: "no-store"});
    if (response.ok) game.live = await response.json();
  } catch (err) {
    game.live = null;  // live Gemini is optional
  }
}

// --- bootstrap -------------------------------------------------------------------------

async function bootstrap() {
  $("dispatch-gemini").addEventListener("click", () => dispatch("gemini"));
  $("dispatch").addEventListener("click", () => dispatch("rule"));
  $("reset").addEventListener("click", () => {
    $("report").value = "";
    forgetNotes();
    resetRun(true);
  });
  $("report").addEventListener("input", drawControls);
  $("swap").addEventListener("click", () => {
    const care = careSet();
    if (care) loadPattern((care.pattern + 1) % care.patterns);
  });
  $("report").addEventListener("keydown", event => {
    if (isImeConfirm(event)) return;
    if (event.key === "Enter" && !$("dispatch-gemini").disabled) dispatch("gemini");
  });
  $("play-map").addEventListener("click", event => {
    const road = event.target.closest("[data-road]");
    if (road) toggleRoad(road.dataset.road);
  });
  $("play").addEventListener("click", () => {
    if (playback.frame !== null) stopPlayback();
    else startPlayback(false);
  });
  $("clock").addEventListener("input", event => {
    stopPlayback();
    drawMap(Number(event.target.value));
  });
  window.addEventListener("resize", () => {
    if (towns.length && playback.frame === null) drawMap(playback.time);
  });
  try {
    const response = await fetch("/v1/showcase", {headers: {Accept: "application/json"}, cache: "no-store"});
    if (!response.ok) throw new Error("検証用データの取得に失敗しました");
    const evidence = await response.json();
    towns.push({key: "base", title: "基本シナリオ", scenario: evidence.scenario, plan: evidence.baseline_plan,
      care: null});
    for (const run of evidence.more_runs) {
      const care = await fetch("/v1/agent/care?scenario=" + encodeURIComponent(run.key), {cache: "no-store"});
      towns.push({key: run.key, title: run.title, scenario: run.scenario, plan: run.baseline_plan,
        care: care.ok ? await care.json() : null});
    }
    await loadLive();
    // The district has residents with circumstances to read: start there.
    if (towns.length > 1) game.townIndex = 1;
    render();
    loadSweep();  // no model, computed once on the server: shown when it arrives
    const health = await fetch("/health", {cache: "no-store"});
    if (health.ok) {
      const info = await health.json();
      const cloud = info.revision && info.revision !== "dev";
      $("runtime-status").replaceChildren(span("pulse", ""),
        document.createTextNode(cloud ? " Google Cloudで稼働中" : " 開発環境"));
      drawBackend();
    }
    document.body.dataset.ready = "true";
  } catch (err) {
    document.body.dataset.ready = "error";
    setText($("hint"), "表示エラー: " + String(err.message || err));
  }
}
window.addEventListener("DOMContentLoaded", bootstrap);
