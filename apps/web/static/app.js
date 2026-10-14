"use strict";

/* Every diagram value and figure for slides 1-6 and 9 comes from /v1/showcase, which
 * is computed using the repository's deterministic Simulation Core. index.html holds
 * no scenario numbers: they are filled in by bindFacts() and drawAgent(). Slide 9
 * renders the agent trace; a Gemini trace appears only when the server replay-verified
 * it. Slides 7, 8 and 10 are explicitly marked as architecture/design/roadmap.
 *
 * Slide N is section#shot-N (and /slides?shot=N). The numbering printed on the slides
 * themselves ("01 / ...") starts at 01 on shot-2, after the cover.
 * Uses from map-view.js: the name tables, the DOM helpers (div, span, bind, setText),
 * drawNetwork, drawTimeChart, drawChecks, captionAt, challengeSummary, describePlan,
 * postJson, isVerifiedGeminiRun, liveErrorText, the shared wait and failure messages,
 * and smaller helpers (lastArrival, roadName, roadLabel, tally, traceEffect, ...). */

const SLIDE_COUNT = 10;
const DEFAULT_SHOT = 2;
const PLAYBACK_SHOT = 2;  // the slide with the map, the virtual clock and the challenge
const REPAIRED_PLAN_SHOTS = [5, 6];  // slides that open on plan B (repaired), not plan A
let evidence = null;
let liveStatus = null;  // GET /v1/agent/live; null while unknown or unreachable
const liveRuns = {};  // scenario key -> verified live Gemini run from this visit
let liveBusy = false;
let holdLiveStatus = false;  // keep the last live result message until the next redraw
const LIVE_MAX_CALLS = 6;  // per run, enforced by the private service
let activeTrace = "rule";
let activeScenario = "base";
let activeKind = "blocked";
let activeShot = DEFAULT_SHOT;
const playback = {time: 0, frame: null, last: null};
/* Challenge mode on slide 2: the visitor closes roads, the agent answers in that world.
 * ruleRun is the ChallengeRun for those closures; geminiRun a verified LiveRun for them. */
const challenge = {key: "base", closed: [], ruleRun: null, geminiRun: null, busy: false};
const score = {tries: 0, rebuilt: 0, handed: 0, cheated: 0};
const PLAYBACK_MS = 6000;  // one run of the virtual clock, start to deadline

function blockedLeg(ev) {
  return blockedLegOf(ev.scenario, ev.baseline_plan, ev.baseline_result);
}
function deriveFacts(ev) {
  const repaired = ev.repaired_result;
  const arrival = lastArrival(repaired);
  const intent = ev.repaired_plan.intents[0];
  const shelter = intent && ev.scenario.shelters.find(s => s.id === intent.shelter_id);
  const leg = blockedLeg(ev);
  const dash = value => (value === null || value === undefined ? "—" : value);
  return {
    people: ev.scenario.personas.length,
    deadline: ev.scenario.deadline_s,
    "baseline-evacuated": ev.baseline_result.evacuated.length,
    "repaired-evacuated": repaired.evacuated.length,
    "timeline-pill": "合成" + ev.scenario.personas.length + "名・到着" + dash(arrival) + "秒",
    "repaired-arrival-s": dash(arrival),
    "repaired-departure-s": dash(firstDeparture(repaired)),
    "margin-s": arrival === null ? "—" : ev.scenario.deadline_s - arrival,
    "repaired-route": intent ? intent.route.map(roadName).join(" → ") : "—",
    "repaired-shelter": intent ? shelterPlaceName(ev.scenario, intent.shelter_id) : "—",
    "repaired-capacity": dash(shelter && shelter.capacity),
    "blocked-road": leg ? roadLabel(leg.road) : "—",
    "closure-start": leg ? leg.closure.start_s : "—",
    "closure-end": leg ? leg.closure.end_s : "—",
    "move-start": leg ? leg.from : "—",
    "move-end": leg ? leg.to : "—",
  };
}
function bindFacts(ev) {
  for (const [name, value] of Object.entries(deriveFacts(ev))) bind(name, value);
}

function challengeInputs(key) {
  const extra = evidence.more_runs.find(run => run.key === key);
  return extra ? {scenario: extra.scenario, plan: extra.baseline_plan, title: extra.title}
    : {scenario: evidence.scenario, plan: evidence.baseline_plan, title: "基本シナリオ"};
}
/* What slide 2 shows: plan A or B of the base scenario, or the visitor's challenge. */
function stageView() {
  if (activeKind !== "challenge") {
    const blocked = activeKind === "blocked";
    return {
      scenario: evidence.scenario,
      plan: blocked ? evidence.baseline_plan : evidence.repaired_plan,
      result: blocked ? evidence.baseline_result : evidence.repaired_result,
      kind: activeKind,
    };
  }
  const inputs = challengeInputs(challenge.key);
  const closedByUser = new Set(challenge.closed);
  const trace = challenge.geminiRun ? challenge.geminiRun.trace
    : challenge.ruleRun ? challenge.ruleRun.trace : null;
  if (!trace) return {scenario: inputs.scenario, plan: inputs.plan, result: null, kind: "blocked", closedByUser, editable: true};
  const result = trace.final_result || trace.initial_result;
  return {
    scenario: challenge.ruleRun.world,
    plan: trace.final_plan || inputs.plan,
    result,
    kind: result.status === "PASS" ? "repaired" : "blocked",
    closedByUser,
    editable: true,
    trace,
  };
}
function drawMoment(time) {
  const view = stageView();
  const {scenario, plan, result} = view;
  playback.time = time;
  const second = Math.round(time);
  document.querySelectorAll('[data-network="active"]').forEach(target =>
    drawNetwork(target, scenario, plan, view.kind, {time, result}, view));
  drawTimeChart(document.getElementById("time-chart"), scenario, plan, result, time);
  const clock = document.getElementById("clock");
  clock.max = String(scenario.deadline_s);
  clock.value = String(second);
  setText(document.getElementById("clock-label"), "t = " + second + " 秒");
  setText(document.getElementById("live-caption"), captionAt(scenario, plan, result, second));
}
/* The same playback loop as play.js startPlayback(); each page draws its own views. */
function stopPlayback() {
  if (playback.frame !== null) cancelAnimationFrame(playback.frame);
  playback.frame = null;
  playback.last = null;
  const button = document.getElementById("play");
  button.textContent = "▶";
  button.setAttribute("aria-label", "再生");
}
function startPlayback(fromStart) {
  stopPlayback();
  const deadline = stageView().scenario.deadline_s;
  if (reducedMotion()) {
    drawMoment(deadline);
    return;
  }
  let time = fromStart || playback.time >= deadline ? 0 : playback.time;
  const button = document.getElementById("play");
  button.textContent = "❚❚";
  button.setAttribute("aria-label", "一時停止");
  const tick = now => {
    if (playback.last !== null) time = Math.min(deadline, time + (now - playback.last) / PLAYBACK_MS * deadline);
    playback.last = now;
    drawMoment(time);
    playback.frame = time >= deadline ? null : requestAnimationFrame(tick);
    if (playback.frame === null) stopPlayback();
  };
  playback.frame = requestAnimationFrame(tick);
}
function drawPlanChoices(ev) {
  for (const [kind, plan, result] of [
    ["blocked", ev.baseline_plan, ev.baseline_result],
    ["repaired", ev.repaired_plan, ev.repaired_result],
  ]) {
    bind("plan-" + kind, describePlan(ev.scenario, plan));
    bind("plan-" + kind + "-status", result.status);
    document.querySelectorAll('[data-bind="plan-' + kind + '-status"]').forEach(badge => {
      badge.className = "verdict-badge " + result.status.toLowerCase();
    });
  }
}
function drawActive(animate = false) {
  if (!evidence) return;
  const view = stageView();
  const {scenario, plan, result} = view;
  document.getElementById("challenge").hidden = activeKind !== "challenge";
  if (activeKind === "challenge") drawChallenge(view);
  document.querySelectorAll("[data-choose]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.choose === activeKind));
  });
  stopPlayback();
  if (!result) {
    drawWaiting(scenario, plan);
    drawMoment(0);
    return;
  }
  const last = lastArrival(result);
  const deadline = scenario.deadline_s;
  const pass = result.status === "PASS";

  bind("status", result.status);
  bind("engine", "Simulation Core（" + result.engine_version + "）");
  bind("evacuated", result.evacuated.length + " / " + scenario.personas.length + "名");
  bind("evacuated-note", result.not_evacuated.length
    ? "未避難 " + result.not_evacuated.length + "名" : "全員が避難所へ到着");
  bind("arrival", last !== null ? last + " 秒" : "到着なし");
  bind("arrival-note", "期限 " + deadline + "秒" + (pass && last !== null ? "・余裕 " + (deadline - last) + "秒" : ""));
  bind("digest", result.input_digest.slice(0, 16) + "…");
  bind("result-title", activeKind === "challenge"
    ? (pass ? "立て直し成功：全員が期限内に避難" : "立て直せず：人へ引き継ぎ")
    : pass ? "全員が期限内に避難：PASS" : "この計画は成立しない：FAIL");
  document.querySelectorAll('[data-bind="digest"]').forEach(node => { node.title = result.input_digest; });
  document.querySelectorAll('[data-bind="status"], [data-bind="result-title"]').forEach(node => {
    node.classList.toggle("fail-text", !pass);
    node.classList.toggle("pass-text", pass);
  });
  drawChecks(document.getElementById("check-list"), scenario, plan, result);
  if (animate && !reducedMotion()) startPlayback(true);
  else drawMoment(finalMoment(result));
}
function drawWaiting(scenario, plan) {
  bind("status", "—");
  bind("engine", "道路を選ぶと、Simulation Coreが計算します");
  bind("evacuated", "— / " + scenario.personas.length + "名");
  bind("evacuated-note", "まだ計算していません");
  bind("arrival", "—");
  bind("arrival-note", "期限 " + scenario.deadline_s + "秒");
  bind("digest", "—");
  bind("result-title", "エージェントに挑戦");
  document.querySelectorAll('[data-bind="status"], [data-bind="result-title"]').forEach(node => {
    node.classList.remove("fail-text", "pass-text");
  });
  drawChecks(document.getElementById("check-list"), scenario, plan, null);
}
function drawChallenge(view) {
  const tabs = document.getElementById("challenge-scenarios");
  if (!tabs.childElementCount) {
    const keys = ["base"].concat(evidence.more_runs.map(run => run.key));
    tabs.replaceChildren(...keys.map(key => {
      const {scenario, title} = challengeInputs(key);
      const tab = document.createElement("button");
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.dataset.challengeScenario = key;
      tab.textContent = title + "（" + scenario.personas.length + "名・道路" + scenario.roads.length + "本）";
      tab.addEventListener("click", () => {
        if (challenge.busy || challenge.key === key) return;
        Object.assign(challenge, {key, closed: [], ruleRun: null, geminiRun: null});
        drawActive();
      });
      return tab;
    }));
  }
  tabs.querySelectorAll("[data-challenge-scenario]").forEach(tab => {
    tab.setAttribute("aria-selected", String(tab.dataset.challengeScenario === challenge.key));
  });
  const {scenario} = challengeInputs(challenge.key);
  document.getElementById("challenge-roads").replaceChildren(...scenario.roads.map(road => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "road-chip";
    chip.textContent = roadName(road.id);
    chip.setAttribute("aria-pressed", String(challenge.closed.includes(road.id)));
    chip.disabled = challenge.busy;
    chip.addEventListener("click", () => toggleRoad(road.id));
    return chip;
  }));
  const ruleButton = document.getElementById("challenge-run");
  ruleButton.disabled = challenge.busy || !challenge.closed.length || challenge.ruleRun !== null;
  ruleButton.textContent = challenge.ruleRun ? "立て直し済み（道路を変えて再挑戦）" : "エージェントで立て直す";
  const geminiButton = document.getElementById("challenge-gemini");
  geminiButton.hidden = !(liveStatus && liveStatus.enabled);
  geminiButton.disabled = challenge.busy || !challenge.closed.length || challenge.geminiRun !== null ||
    !liveStatus || liveStatus.runs_left_today === 0;
  const trace = view.trace;
  const steps = document.getElementById("challenge-steps");
  steps.replaceChildren(...(trace ? trace.steps : []).map(step => {
    const item = document.createElement("li");
    const action = step.decision ? actionNames[step.decision.action] : "判断なし";
    item.append(span("challenge-step-action", action));
    item.append(span("challenge-step-effect",
      step.policy_error ? policyErrorNames[step.policy_error] : stepEffect(step.effect, step.decision.action)));
    return item;
  }));
  if (!challenge.busy) {
    setText(document.getElementById("challenge-status"), trace ? challengeSummary(trace, view.scenario, view.plan, challenge.geminiRun ? "Gemini" : "エージェント（ルール方策）")
      : challenge.closed.length ? "選んだ道路：" + challenge.closed.map(roadName).join("・")
        : "まだ道路を選んでいません");
  }
  setText(document.getElementById("score-tries"), String(score.tries));
  setText(document.getElementById("score-rebuilt"), String(score.rebuilt));
  setText(document.getElementById("score-handed"), String(score.handed));
  setText(document.getElementById("score-cheated"), String(score.cheated));
}
function toggleRoad(id) {
  if (challenge.busy) return;
  const at = challenge.closed.indexOf(id);
  if (at >= 0) challenge.closed.splice(at, 1);
  else if (challenge.closed.length < MAX_CLOSED) challenge.closed.push(id);
  else {
    setText(document.getElementById("challenge-status"), "通行止めにできるのは" + MAX_CLOSED + "本までです");
    return;
  }
  challenge.ruleRun = null;
  challenge.geminiRun = null;
  drawActive();
}
async function runChallenge(withGemini) {
  if (challenge.busy || !challenge.closed.length || !evidence) return;
  challenge.busy = true;
  const status = document.getElementById("challenge-status");
  setText(status, withGemini ? GEMINI_WAIT_MESSAGE : "エージェントが立て直しています…");
  drawChallenge(stageView());
  const body = {scenario: challenge.key, closed_roads: challenge.closed.slice()};
  let message = null;
  let fresh = false;  // a rule answer fetched now has not been counted yet
  try {
    if (!challenge.ruleRun) {
      const {response, payload} = await postJson("/v1/agent/challenge", body);
      if (!response.ok) throw new Error("challenge " + response.status);
      challenge.ruleRun = payload;
      fresh = true;
      if (!withGemini) tally(score, payload.trace);
    }
    if (withGemini) {
      const {response, payload} = await postJson("/v1/agent/live", body);
      if (!response.ok) {
        message = liveErrorText(payload, "Geminiの実行に失敗しました（" + response.status + "）");
      } else if (!isVerifiedGeminiRun(payload)) {
        message = "検証済みのGeminiの結果ではないため表示しません";
      } else {
        challenge.geminiRun = payload;
        tally(score, payload.trace);
      }
      if (message && fresh) tally(score, challenge.ruleRun.trace);
      await loadLiveStatus();
    }
  } catch (err) {
    message = NETWORK_FAILED_MESSAGE;
  } finally {
    challenge.busy = false;
    drawActive(true);
    if (message) setText(status, message + "（ルール方策の結果を表示しています）");
  }
}
function drawFailureTrace() {
  const parent = document.getElementById("failure-trace");
  parent.replaceChildren();
  evidence.baseline_result.events.filter(e => e.kind === "rejected").forEach(e => {
    const row = div("audit-row");
    row.append(span("audit-time", e.time_s + " 秒"));
    row.append(span("audit-label", personLabel(evidence.scenario, e.actor_id) + "（" + e.actor_id + "）"));
    row.append(span("audit-code", e.reason));
    row.append(span("audit-status", reasonNames[e.reason] || "移動不可"));
    parent.append(row);
  });
}
function drawTimeline() {
  const parent = document.getElementById("success-timeline");
  parent.replaceChildren();
  evidence.scenario.personas.forEach(persona => {
    const events = evidence.repaired_result.events.filter(e => e.actor_id === persona.id);
    const end = events.find(e => e.kind === "arrived");
    const start = events.find(e => e.kind === "departed");
    const row = div("timeline-person");
    row.append(span("timeline-person-name", personLabel(evidence.scenario, persona.id)));
    const line = div("timeline-line");
    const progress = div("progress");
    progress.style.width = ((end?.time_s || 0) / evidence.scenario.deadline_s * 100) + "%";
    line.append(progress);
    line.append(div("start-marker"));
    const marker = div("end-marker");
    marker.style.left = ((end?.time_s || 0) / evidence.scenario.deadline_s * 100 - 1) + "%";
    line.append(marker);
    row.append(line);
    row.append(span("timeline-person-time",
      start && end ? start.time_s + "秒 出発 → " + end.time_s + "秒 到着" : "未到着"
    ));
    parent.append(row);
  });
  const key = div("timeline-key");
  const facts = deriveFacts(evidence);
  key.append(span("", "出発 " + facts["repaired-departure-s"] + "秒"));
  key.append(span("", "到着 " + facts["repaired-arrival-s"] + "秒"));
  key.append(span("", "期限 " + facts.deadline + "秒"));
  parent.append(key);
}
function labelled(label, content) {
  const box = div("");
  box.append(span("step-label", label));
  box.append(typeof content === "string" ? div("step-text", content) : content);
  return box;
}
function drawStep(step) {
  const item = document.createElement("li");
  item.className = "agent-step" + (step.policy_error ? " halted" : "");
  const obs = step.observation;
  const head = div("step-head");
  head.append(span("step-number", "STEP " + (step.index + 1)));
  head.append(span("step-verdict " + (obs.plan_status === "PASS" ? "pass" : "fail"), obs.plan_status));
  item.append(head);
  const reasons = obs.rejection_reasons.map(r => reasonNames[r] || r).join("、");
  item.append(labelled("観測", obs.evacuated + "／" + obs.residents + "名が避難" + (reasons ? "・" + reasons : "")));

  const decision = div("");
  decision.append(span("step-label", "判断"));
  decision.append(div("step-action", step.decision ? actionNames[step.decision.action] : "判断なし"));
  if (step.decision) decision.append(div("step-reason", decisionReasonNames[step.decision.reason_code]));
  item.append(decision);

  const chips = div("allowed-chips");
  obs.allowed_actions.forEach(action => {
    chips.append(span(step.decision && step.decision.action === action ? "chosen" : "", actionNames[action]));
  });
  item.append(labelled("許可された行動", chips));
  item.append(div("step-effect",
    step.policy_error ? policyErrorNames[step.policy_error] : stepEffect(step.effect, step.decision.action)));
  return item;
}
function currentRun() {
  const extra = evidence.more_runs.find(run => run.key === activeScenario);
  return extra
    ? {scenario: extra.scenario, trace: extra.agent_trace, extra: true}
    : {scenario: evidence.scenario, trace: evidence.agent_trace, extra: false};
}
function drawScenarioTabs() {
  const tabs = document.getElementById("scenario-tabs");
  const entries = [{key: "base", title: "基本シナリオ", scenario: evidence.scenario}]
    .concat(evidence.more_runs.map(run => ({key: run.key, title: run.title, scenario: run.scenario})));
  tabs.replaceChildren(...entries.map(entry => {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.dataset.scenario = entry.key;
    tab.textContent = entry.title + "（" + entry.scenario.personas.length + "名）";
    tab.addEventListener("click", () => {
      activeScenario = entry.key;
      drawAgent();
    });
    return tab;
  }));
}
function drawHandover(trace) {
  const box = document.getElementById("handover");
  const needsHuman = trace.outcome === "escalated" || trace.outcome === "verified_with_residual_risk" ||
    trace.outcome === "verified_untested" || trace.outcome.startsWith("halted");
  box.hidden = !needsHuman;
  if (!needsHuman) return;
  let first = "検証を通る計画はありません。計画の作成から人が判断します。";
  if (trace.outcome.startsWith("halted")) {
    first = (trace.outcome === "halted_policy_violation" ? "許可外または不正な判断" : "モデル障害") +
      "のため途中で停止しました。途中の計画は渡さず、人が判断します。";
  } else if (trace.final_result) {
    first = "検証済みの計画（PASS）を担当者へ渡します。";
  }
  const items = [first];
  const stress = traceEffect(trace, "run_redteam");
  if (!stress && trace.final_result) items.push("弱点探索は実行していません。");
  if (stress && stress.roads_not_probed) items.push("未検査の道路が " + stress.roads_not_probed + "本あります。");
  const plans = (traceEffect(trace, "plan_contingency") || {contingencies: []}).contingencies;
  (stress ? stress.probes : []).filter(probe => probe.status === "FAIL").forEach(probe => {
    const fallback = plans.find(c => c.road_id === probe.road_id);
    if (fallback && fallback.reason === "verified_alternative") return;
    const missing = !fallback ? "代替計画は未作成です。"
      : fallback.reason === "search_budget_exhausted" ? "探索の上限に達し、代替計画を確認できませんでした。"
        : "検証を通る代替計画がありません。";
    items.push(roadLabel(probe.road_id) + " が使えなくなると " + probe.not_evacuated.length +
      "名が避難できず、" + missing);
  });
  const title = document.createElement("h4");
  title.textContent = "人への引き継ぎ事項";
  const list = document.createElement("ul");
  list.replaceChildren(...items.map(text => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
  box.replaceChildren(title, list);
}
function drawContingencies(trace, scenario) {
  const parent = document.getElementById("contingency-list");
  parent.replaceChildren();
  const stress = traceEffect(trace, "run_redteam");
  if (!stress) {
    parent.append(div("contingency-item", "この実行では弱点探索を行っていません。"));
    return;
  }
  const plans = (traceEffect(trace, "plan_contingency") || {contingencies: []}).contingencies;
  stress.probes.forEach(probe => {
    const card = div("contingency-item");
    const title = document.createElement("strong");
    title.textContent = roadLabel(probe.road_id) + " が全時間閉鎖";
    card.append(title);
    if (probe.status === "PASS") {
      card.append(div("", "元の計画のまま成立します。"));
      parent.append(card);
      return;
    }
    const before = div("");
    before.append(document.createTextNode("元の計画："));
    before.append(span("fail-text", "FAIL"));
    before.append(document.createTextNode("（避難できない住民 " + probe.not_evacuated.length + "名）"));
    card.append(before);
    const fallback = plans.find(c => c.road_id === probe.road_id);
    const after = div("");
    if (fallback && fallback.reason === "verified_alternative") {
      after.append(document.createTextNode("代替計画：" + describePlan(scenario, fallback.plan) +
        "。最終到着 " + fallback.last_arrival_s + "秒 "));
      after.append(span("pass-text", "PASS"));
    } else {
      const text = !fallback ? "代替計画は未作成"
        : fallback.reason === "search_budget_exhausted" ? "探索上限のため未確認（人の判断へ）" : "代替計画なし（人の判断へ）";
      after.append(document.createTextNode(text));
    }
    card.append(after);
    parent.append(card);
  });
}
/* The Gemini trace for the shown scenario: a live run from this visit first, else the
 * recorded run (base scenario only). Both were replayed through the Core server-side. */
function geminiTraceFor(run) {
  const live = liveRuns[activeScenario];
  if (live) return {trace: live.trace, live};
  const recorded = run.extra ? null : evidence.recorded_ai_run;
  return recorded ? {trace: recorded.trace, recorded} : null;
}
function drawAgent() {
  const run = currentRun();
  const gemini = geminiTraceFor(run);
  const showGemini = activeTrace === "gemini" && gemini !== null;
  const trace = showGemini ? gemini.trace : run.trace;
  const list = document.getElementById("agent-steps");
  list.replaceChildren(...trace.steps.map(drawStep));
  drawContingencies(trace, run.scenario);
  drawHandover(trace);
  document.querySelectorAll("[data-scenario]").forEach(tab => {
    tab.setAttribute("aria-selected", String(tab.dataset.scenario === activeScenario));
  });

  bind("agent-outcome", outcomeNames[trace.outcome] || trace.outcome);
  bind("agent-meta", (showGemini ? "判断：" + trace.model : "判断：ルール方策") +
    "・" + trace.steps.length + "ステップ・モデル呼出し " + trace.model_calls + "回");
  const banner = document.getElementById("agent-outcome");
  banner.classList.toggle("halted", trace.outcome.startsWith("halted"));
  banner.classList.toggle("escalated", trace.outcome === "escalated");

  const pill = document.getElementById("agent-source-pill");
  pill.textContent = !showGemini ? "ルール方策で実行・AIではありません"
    : gemini.live ? "Vertex AIのGeminiがライブで判断・Coreで再検証済み"
      : "Geminiの実行記録（署名なし）を再生検証して表示";
  pill.className = "pill " + (showGemini ? "pill-live" : "pill-design");
  document.getElementById("agent-note").textContent = agentNote(run, gemini, showGemini);
  document.querySelectorAll("[data-trace]").forEach(tab => {
    const isGemini = tab.dataset.trace === "gemini";
    tab.hidden = isGemini && gemini === null;
    if (isGemini && gemini) tab.textContent = gemini.live ? "Gemini（ライブ・Coreで再検証）" : "Gemini（記録・再生検証済み）";
    tab.setAttribute("aria-selected", String(tab.dataset.trace === (showGemini ? "gemini" : "rule")));
  });
  drawLiveBar();
}
function agentNote(run, gemini, showGemini) {
  if (gemini && gemini.live && showGemini) {
    const live = gemini.live;
    return "ライブ実行：" + live.trace.model + "・" +
      (live.duration_ms / 1000).toFixed(1) + "秒・全ステップを非公開サービスのCloud Loggingに記録";
  }
  if (gemini && gemini.recorded && showGemini) {
    return "Geminiの記録：" + gemini.recorded.recorded_at +
      "・再生で結果が一致（判断の作成者は署名で保証していません）";
  }
  if (gemini) return "Geminiの判断は「Gemini」タブで表示できます";
  if (liveStatus && liveStatus.enabled) return "このシナリオは「Geminiでライブ実行」でGeminiに判断させられます";
  if (run.extra) return "このシナリオはルール方策で実行しています（Geminiの記録は基本シナリオのみ）";
  return {
    absent: "Geminiの実行記録：未記録",
    rejected: "Geminiの記録は再生検証に一致しないため表示していません",
    replay_verified: "",
  }[evidence.recorded_ai_run_status];
}
function drawLiveBar() {
  const bar = document.getElementById("live-bar");
  bar.hidden = !(liveStatus && liveStatus.enabled);
  if (bar.hidden) return;
  const button = document.getElementById("live-run");
  const title = activeScenario === "base" ? "基本シナリオ"
    : (evidence.more_runs.find(r => r.key === activeScenario) || {title: activeScenario}).title;
  button.disabled = liveBusy || liveStatus.runs_left_today === 0;
  button.textContent = liveBusy ? "Geminiが判断中…" : "▶ Geminiでライブ実行（" + title + "）";
  if (!liveBusy && !holdLiveStatus) {
    setText(document.getElementById("live-status"), liveStatus.runs_left_today === 0
      ? "本日のライブ実行枠を使い切りました"
      : "Vertex AIのGeminiが各ステップを判断し、結果はCoreで再検証します");
    setText(document.getElementById("live-detail"), "本日あと " + liveStatus.runs_left_today + " 回／" +
      liveStatus.daily_limit + " 回・1回につきGemini呼出し最大" + LIVE_MAX_CALLS + "回");
  }
}
async function loadLiveStatus() {
  try {
    const response = await fetch("/v1/agent/live", {cache: "no-store"});
    if (response.ok) liveStatus = await response.json();
  } catch (err) {
    liveStatus = null;  // live AI is optional; the recorded and rule runs still show
  }
}
async function runLive() {
  if (liveBusy || !evidence) return;
  liveBusy = true;
  holdLiveStatus = true;
  const scenario = activeScenario;
  const started = Date.now();
  setText(document.getElementById("live-status"), GEMINI_WAIT_MESSAGE);
  const tick = () => setText(document.getElementById("live-detail"),
    "経過 " + Math.round((Date.now() - started) / 1000) + " 秒");
  tick();
  const timer = setInterval(tick, 1000);
  drawLiveBar();
  try {
    const {response, payload} = await postJson("/v1/agent/live", {scenario});
    if (!response.ok) {
      setText(document.getElementById("live-status"), liveErrorText(payload,
        "Geminiの実行に失敗しました（" + response.status + "）。ルール方策と記録はそのまま表示しています"));
    } else if (!isVerifiedGeminiRun(payload)) {
      setText(document.getElementById("live-status"), "検証済みのGeminiの結果ではないため表示しません");
    } else {
      liveRuns[scenario] = payload;
      setText(document.getElementById("live-status"), "完了：Geminiの判断をCoreが再検証しました");
      if (scenario === activeScenario) activeTrace = "gemini";
    }
  } catch (err) {
    setText(document.getElementById("live-status"), NETWORK_FAILED_MESSAGE);
  } finally {
    clearInterval(timer);
    liveBusy = false;
    await loadLiveStatus();
    setText(document.getElementById("live-detail"), liveStatus
      ? "本日あと " + liveStatus.runs_left_today + " 回／" + liveStatus.daily_limit + " 回" : "—");
    drawAgent();
    holdLiveStatus = false;
  }
}
function navigateTo(shot, replaceUrl = false) {
  activeShot = Math.max(1, Math.min(SLIDE_COUNT, Number(shot) || DEFAULT_SHOT));
  if (activeShot !== PLAYBACK_SHOT && evidence) stopPlayback();
  document.querySelectorAll(".slide").forEach(panel => {
    panel.classList.toggle("active", panel.id === "shot-" + activeShot);
  });
  document.querySelectorAll("[data-shot]").forEach(button => {
    button.classList.toggle("active", Number(button.dataset.shot) === activeShot);
    button.setAttribute("aria-pressed", String(Number(button.dataset.shot) === activeShot));
  });
  document.getElementById("slide-index").textContent =
    String(activeShot).padStart(2, "0") + " / " + SLIDE_COUNT;
  // A chart drawn while its slide was hidden had no width to fit; redraw it now.
  if (activeShot === PLAYBACK_SHOT && evidence && playback.frame === null) drawMoment(playback.time);
  const url = new URL(window.location.href);
  url.searchParams.set("shot", String(activeShot));
  if (replaceUrl) history.replaceState({shot: activeShot}, "", url);
  else history.pushState({shot: activeShot}, "", url);
}
function checkEvidence(source) {
  if (source.schema_version !== "evacweave-showcase/v2" ||
      source.evidence_kind !== "synthetic-deterministic" ||
      source.live_ai_used !== false ||
      source.baseline_result?.status !== "FAIL" ||
      source.repaired_result?.status !== "PASS" ||
      source.agent_trace?.policy !== "rule-policy" ||
      !Array.isArray(source.probes) ||
      !Array.isArray(source.contingencies) ||
      !Array.isArray(source.more_runs)) {
    throw new Error("検証結果の整合性を確認できません");
  }
}
async function bootstrap() {
  const shot = new URL(window.location.href).searchParams.get("shot") || String(DEFAULT_SHOT);
  navigateTo(shot, true);

  document.querySelectorAll("[data-shot]").forEach(button => {
    button.addEventListener("click", () => navigateTo(button.dataset.shot));
  });
  document.querySelectorAll("[data-goto]").forEach(button => {
    button.addEventListener("click", () => navigateTo(button.dataset.goto));
  });
  document.querySelectorAll("[data-choose]").forEach(button => {
    button.addEventListener("click", () => {
      activeKind = button.dataset.choose;
      drawActive(true);
    });
  });
  document.getElementById("challenge-run").addEventListener("click", () => runChallenge(false));
  document.getElementById("challenge-gemini").addEventListener("click", () => runChallenge(true));
  document.querySelectorAll('[data-network="active"]').forEach(map => map.addEventListener("click", event => {
    const road = event.target.closest("[data-road]");
    if (road && activeKind === "challenge") toggleRoad(road.dataset.road);
  }));
  document.getElementById("play").addEventListener("click", () => {
    if (!evidence) return;
    if (playback.frame !== null) stopPlayback();
    else startPlayback(false);
  });
  document.getElementById("clock").addEventListener("input", event => {
    if (!evidence) return;
    stopPlayback();
    drawMoment(Number(event.target.value));
  });
  document.querySelectorAll("[data-trace]").forEach(tab => {
    tab.addEventListener("click", () => {
      activeTrace = tab.dataset.trace;
      if (evidence) drawAgent();
    });
  });
  document.getElementById("live-run").addEventListener("click", runLive);
  window.addEventListener("resize", () => {
    if (evidence && playback.frame === null) drawMoment(playback.time);
  });
  window.addEventListener("popstate", () => {
    const shot = new URL(window.location.href).searchParams.get("shot") || String(DEFAULT_SHOT);
    navigateTo(shot, true);
  });

  try {
    const response = await fetch("/v1/showcase", {
      headers: {Accept: "application/json"},
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw new Error("検証用データの取得に失敗しました");
    const payload = await response.json();
    checkEvidence(payload);
    evidence = payload;

    const choice = REPAIRED_PLAN_SHOTS.includes(activeShot) ? "repaired" : "blocked";
    activeKind = choice;
    bindFacts(evidence);
    drawPlanChoices(evidence);
    drawActive(activeShot === PLAYBACK_SHOT);
    drawFailureTrace();
    drawTimeline();
    drawScenarioTabs();
    await loadLiveStatus();
    drawAgent();
    document.querySelectorAll('[data-network="blocked"]').forEach(el =>
      drawNetwork(el, evidence.scenario, evidence.baseline_plan, "blocked")
    );
    document.querySelectorAll('[data-network="repaired"]').forEach(el =>
      drawNetwork(el, evidence.scenario, evidence.repaired_plan, "repaired")
    );
    drawNetwork(
      document.getElementById("hero-network"),
      evidence.scenario, evidence.repaired_plan, "repaired"
    );

    const health = await fetch("/health", {cache: "no-store"});
    if (health.ok) {
      const info = await health.json();
      const cloud = info.revision && info.revision !== "dev";
      const status = document.getElementById("runtime-status");
      status.replaceChildren(span("pulse", ""), document.createTextNode(cloud ? " Google Cloudで稼働中" : " 開発環境"));
    }
    document.body.dataset.ready = "true";
  } catch (err) {
    document.body.dataset.ready = "error";
    const status = document.getElementById("revision");
    status.textContent = "表示エラー: " + String(err.message || err);
  }
}
window.addEventListener("DOMContentLoaded", bootstrap);
