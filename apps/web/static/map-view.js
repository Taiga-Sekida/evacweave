"use strict";

/* Shared view code for the slides (app.js) and the play page (play.js): names, the
 * map with residents, the roads x time chart, the Core's verdict basis and captions.
 * Everything here only draws what the Simulation Core computed; no verdict is made
 * in the browser.
 *
 * A classic script, loaded before app.js or play.js: its top-level names are globals
 * that those files use directly, so none of them may be declared again there. The main
 * ones: the name tables (roadNames, nodeNames, reasonNames, actionNames, outcomeNames,
 * policyErrorNames, ...), the DOM helpers (svg, div, span, bind, setText),
 * drawNetwork, drawTimeChart, drawChecks, captionAt, challengeSummary, postJson and the
 * checks on a live Gemini answer (isVerifiedGeminiRun, liveErrorText).
 *
 * Sections, in order: names and map layout; DOM helpers; residents' journeys from the
 * Core's events; the map; the roads x time chart; the verdict basis; captions and
 * summaries; server calls and live Gemini answers. */

// --- names and map layout --------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";
const NODE_POSITIONS = {
  home: [110, 210],
  junction: [345, 290],
  shelter_a_node: [560, 75],
  shelter_b_node: [600, 290],
  // River district (challenge mode): homes on the left, shelters on the right.
  north_homes: [85, 60],
  river_homes: [70, 255],
  east_homes: [330, 350],
  bridge: [195, 300],
  center: [340, 165],
  school: [565, 60],
  hall: [650, 175],
  hill_foot: [505, 290],
  hill_park: [660, 345],
};
// Where along a road its label sits (0 = source, 1 = target), where the midpoint is crowded.
const ROAD_LABEL_SHARE = {flooded_lane: 0.35, river_bridge: 0.6, river_path: 0.62};
const nodeNames = {
  home: "住宅地",
  junction: "分岐点",
  shelter_a_node: "避難所A",
  shelter_b_node: "避難所B",
  school: "小学校",
  hall: "公民館",
  hill_park: "高台公園",
  north_homes: "北地区",
  river_homes: "川沿い",
  east_homes: "東地区",
  bridge: "橋のたもと",
  center: "中央",
  hill_foot: "坂下",
};
// Short names drawn on the map; full names stay in roadNames for lists and hand-overs.
const mapNames = {
  north_exit: "北出口",
  flooded_lane: "浸水路",
  river_path: "橋への道",
  river_bridge: "橋",
  east_direct: "東の直通",
  east_lane: "東の小道",
  school_access: "学校へ",
  hall_access: "公民館へ",
  center_to_hill: "坂下へ",
  hill_road: "高台への坂",
};
const roadNames = {
  road_a: "道路A",
  road_b1: "道路B1",
  road_b2: "道路B2",
  north_exit: "北地区の出口",
  flooded_lane: "川沿いの道（浸水）",
  river_path: "川沿いから橋への道",
  river_bridge: "橋",
  east_direct: "東地区から中央への道",
  east_lane: "東地区から坂下への道",
  school_access: "小学校への道",
  hall_access: "公民館への道",
  center_to_hill: "中央から坂下への道",
  hill_road: "高台への坂道",
};
const reasonNames = {
  route_blocked: "通行止めにより移動拒否",
  disconnected_route: "接続していない経路",
  deadline_exceeded: "避難期限を超過",
  shelter_full: "避難所の収容人数を超過",
  unknown_road: "存在しない道路",
  wrong_destination: "指定避難所に未到達",
  unknown_actor: "存在しない住民",
  departure_after_deadline: "出発が避難期限より後",
  already_evacuated: "すでに避難済み",
  actor_busy: "移動中のため重複指示",
  unknown_shelter: "存在しない避難所",
  shelter_not_open: "避難所が開設前",
  unsuitable_shelter: "本人の事情に合わない避難所",
};
const actionNames = {
  run_repair: "計画を修復",
  run_redteam: "弱点を探索",
  plan_contingency: "代替計画を作成",
  finish: "完了",
  escalate: "人へ引き継ぎ",
};
const decisionReasonNames = {
  plan_failed_verification: "検証で不成立だったため",
  check_robustness: "完了前に頑健性を確かめるため",
  cover_single_points_of_failure: "単一障害点に備えるため",
  verified_plan_ready: "検証済みで弱点にも備えたため",
  accept_residual_risk: "残るリスクを許容したため",
  no_verified_plan: "検証を通る計画がないため",
  needs_human_judgement: "人の判断が必要なため",
};
const outcomeNames = {
  verified_robust: "検証済み・単一障害点なし",
  verified_with_contingencies: "検証済み・すべての単一障害点に代替計画",
  verified_with_residual_risk: "検証済み・一部の弱点は未対策",
  verified_untested: "検証済み・弱点は未確認",
  escalated: "人へ引き継ぎ",
  halted_policy_violation: "許可外の判断のため停止",
  halted_model_error: "モデル障害のため停止",
};
const policyErrorNames = {
  action_not_allowed: "許可外の行動のため実行せず停止",
  invalid_output: "不正な出力のため停止",
  model_error: "モデル障害のため停止",
  model_budget_exhausted: "呼出し上限のため停止",
};
const MAX_CLOSED = 2;  // the API accepts at most this many roads

// --- DOM helpers (text only: nothing here parses markup) -------------------------------

function svg(name, attrs = {}, label = null) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
  if (label !== null) element.textContent = String(label);
  return element;
}
function div(className, text = null) {
  const element = document.createElement("div");
  element.className = className;
  if (text !== null) element.textContent = String(text);
  return element;
}
function span(className, text) {
  const element = document.createElement("span");
  element.className = className;
  element.textContent = String(text);
  return element;
}
function bind(name, value) {
  document.querySelectorAll('[data-bind="' + name + '"]').forEach(
    node => { node.textContent = String(value); }
  );
}
function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

// --- residents' journeys from the Core's events, and their names -----------------------

function lastArrival(result) {
  const times = result.events.filter(e => e.kind === "arrived").map(e => e.time_s);
  return times.length ? Math.max(...times) : null;
}
function firstDeparture(result) {
  const times = result.events.filter(e => e.kind === "departed").map(e => e.time_s);
  return times.length ? Math.min(...times) : null;
}
/* Display only: finds the leg the Core rejected as route_blocked, using the same
 * half-open [start, end) overlap rule as packages/simulation/engine.py. The verdict
 * itself always comes from the Core result, never from this function. */
function blockedLegOf(scenario, plan, result) {
  const rejected = result.events.find(e => e.kind === "rejected" && e.reason === "route_blocked");
  const journey = rejected && journeys(scenario, plan, result).find(j => j.intent.actor_id === rejected.actor_id);
  const leg = journey && journey.legs.find(item => item.closure);
  return leg ? {road: leg.road.id, closure: leg.closure, from: leg.from, to: leg.to} : null;
}
/* Display only: each resident's fixed route, timed the way the Core times it, joined
 * with the Core's own events (departed / arrived / rejected). Bars and tokens are
 * placed from this; PASS/FAIL is never derived here. */
function journeys(scenario, plan, result) {
  const roads = Object.fromEntries(scenario.roads.map(r => [r.id, r]));
  return plan.intents.map(intent => {
    const events = result ? result.events.filter(e => e.actor_id === intent.actor_id) : [];
    const departed = events.find(e => e.kind === "departed") || null;
    const rejected = events.find(e => e.kind === "rejected") || null;
    const arrived = events.find(e => e.kind === "arrived") || null;
    const persona = scenario.personas.find(p => p.id === intent.actor_id);
    let clock = departed ? departed.time_s
      : rejected ? rejected.time_s : Math.min(intent.departure_s, scenario.deadline_s);
    const legs = [];
    for (const id of intent.route) {
      const road = roads[id];
      if (!road) break;
      const end = clock + road.travel_time_s;
      const closure = road.closures.find(c => clock < c.end_s && c.start_s < end) || null;
      legs.push({road, from: clock, to: end, closure});
      clock = end;
    }
    return {intent, legs, departed, rejected, arrived, start: persona ? persona.start_node : null};
  });
}
function personNumber(scenario, id) {
  const index = scenario.personas.findIndex(p => p.id === id);
  return index < 0 ? null : index + 1;
}
function personLabel(scenario, id) {
  const number = personNumber(scenario, id);
  return number === null ? id : "住民" + number;
}
function roadName(id) {
  return roadNames[id] || id;
}
/* A shelter, named after the place it stands in. */
function shelterPlaceName(scenario, shelterId) {
  const shelter = scenario.shelters.find(s => s.id === shelterId);
  return shelter ? (nodeNames[shelter.node] || shelter.id) : "—";
}

// --- the map ---------------------------------------------------------------------------

/* The base scenario's map: few enough roads to label each one beside the road, and to
 * list every road in the time chart. */
function isSmallMap(scenario) {
  return scenario.roads.length <= 4;
}
function nodePosition(id, index) {
  if (NODE_POSITIONS[id]) return NODE_POSITIONS[id];
  const columns = 3;
  return [110 + (index % columns) * 230, 105 + Math.floor(index / columns) * 135];
}
function textWidth(text) {
  let width = 0;
  for (const ch of text) width += ch.charCodeAt(0) < 128 ? 8 : 14;
  return width;
}
function drawTag(target, x, y, text, className = "") {
  const width = Math.max(64, textWidth(text) + 24);
  const g = svg("g", className ? {class: className} : {});
  g.append(svg("rect", {x: x - width / 2, y: y - 17, width, height: 29, rx: 8, class: "tag"}));
  g.append(svg("text", {x, y: y + 2, "text-anchor": "middle", class: "road-label"}, text));
  target.append(g);
}
function closureAt(road, time) {
  return road.closures.find(c => c.start_s <= time && time < c.end_s) || null;
}
function closureNote(road, time) {
  const now = closureAt(road, time);
  if (now) return "通行止め中（" + now.end_s + "秒まで）";
  const next = road.closures.filter(c => c.start_s > time).sort((a, b) => a.start_s - b.start_s)[0];
  if (next) return next.start_s + "秒から通行止め";
  const ended = road.closures.filter(c => c.end_s <= time).sort((a, b) => b.end_s - a.end_s)[0];
  return ended ? ended.end_s + "秒に通行止め解除" : null;
}
/* Draws the road network into an <svg>. kind "blocked" draws the plan's roads as
 * planned only (the plan failed, or has no verdict yet); any other kind ("repaired")
 * draws them as verified routes.
 * moment = {time, result} draws one moment of the Core's run: closures open and close
 * with the virtual clock and residents are placed along their routes; without it the
 * map is a still diagram. opts, all optional: closedByUser (a Set of road ids) marks the
 * visitor's closures, editable makes roads clickable (data-road), probing names the road
 * that flashes during the weakness search. */
function drawNetwork(target, scenario, plan, kind, moment = null, opts = {}) {
  const closedByUser = opts.closedByUser || new Set();
  const small = isSmallMap(scenario);
  // Larger maps get a taller canvas, and labels below their low-lying nodes.
  target.setAttribute("viewBox", small ? "0 0 720 380" : "0 0 720 410");
  const labelBelow = y => !small && y >= 290;
  const used = new Set(plan.intents.flatMap(intent => intent.route));
  const byNode = Object.fromEntries(scenario.nodes.map(
    (node, i) => [node, nodePosition(node, i)]
  ));
  target.replaceChildren();

  for (const road of scenario.roads) {
    const [x1, y1] = byNode[road.source];
    const [x2, y2] = byNode[road.target];
    const closed = moment
      ? closureAt(road, moment.time) !== null
      : road.closures.some(c => c.start_s < road.travel_time_s && c.end_s > 0);
    const selected = used.has(road.id);
    const byUser = closedByUser.has(road.id);
    const classes = ["road"];
    if (opts.probing === road.id) classes.push("probing");
    if (byUser) classes.push("closed", "user-closed");
    else if (selected && kind !== "blocked") classes.push("highlight");
    else if (closed) classes.push("closed");
    else if (selected) classes.push("planned");
    const group = svg("g", opts.editable ? {class: "road-group editable", "data-road": road.id} : {});
    group.append(svg("line", {
      x1, y1, x2, y2, class: classes.join(" "),
      "stroke-linecap": "round",
    }));
    if (opts.editable) {
      const hit = svg("line", {x1, y1, x2, y2, class: "road-hit"});
      hit.append(svg("title", {}, roadName(road.id) + "：クリックで通行止め"));
      group.append(hit);
    }
    target.append(group);
    const share = small ? 0.5 : ROAD_LABEL_SHARE[road.id] ?? 0.5;
    const midX = x1 + (x2 - x1) * share;
    const midY = y1 + (y2 - y1) * share;
    // Small maps label beside the road; on larger ones the label sits on it like a sign.
    // Road A, the upper route, is labelled above and the B roads below, so no label sits
    // between the two routes.
    const lift = !small ? 0 : road.id === "road_a" ? -25 : 23;
    const name = mapNames[road.id] || roadName(road.id);
    drawTag(target, midX, midY + lift, byUser ? "✕ " + name : name + " " + road.travel_time_s + "秒",
      byUser ? "closure-tag" : "");
    const note = moment && small && road.closures.length && !byUser ? closureNote(road, moment.time) : null;
    if (note) {
      drawTag(target, midX, midY + lift + (lift < 0 ? -36 : 36), note,
        "closure-tag" + (closed ? "" : " lifted"));
    }
  }
  scenario.nodes.forEach((id, i) => {
    const [x, y] = byNode[id];
    const shelter = scenario.shelters.some(item => item.node === id);
    const cls = "node " + (shelter ? "shelter" : id === "home" ? "home" : "");
    target.append(svg("circle", {cx: x, cy: y, r: 18, class: cls}));
    target.append(svg("text", {
      x, y: labelBelow(y) ? y + 42 : y - 35, class: "node-label", "text-anchor": "middle",
    }, nodeNames[id] || id));
  });
  if (moment) drawTokens(target, scenario, plan, moment, byNode, labelBelow);
}
function tokenAt(journey, byNode, time) {
  const refused = journey.rejected !== null && time >= journey.rejected.time_s;
  if (!journey.departed || time < journey.departed.time_s || !journey.legs.length) {
    return {point: byNode[journey.start], state: refused ? "rejected" : "waiting"};
  }
  for (const leg of journey.legs) {
    if (time < leg.to) {
      const [x1, y1] = byNode[leg.road.source];
      const [x2, y2] = byNode[leg.road.target];
      const share = Math.max(0, time - leg.from) / Math.max(1, leg.to - leg.from);
      return {point: [x1 + (x2 - x1) * share, y1 + (y2 - y1) * share], state: "moving"};
    }
  }
  const end = byNode[journey.legs[journey.legs.length - 1].road.target];
  return {point: end, state: journey.arrived ? "arrived" : refused ? "rejected" : "moving"};
}
/* The residents at one moment ({time, result}) of the Core's run, as numbered tokens. */
function drawTokens(target, scenario, plan, moment, byNode, labelBelow = () => false) {
  const list = journeys(scenario, plan, moment.result).filter(j => j.start && byNode[j.start]);
  const refusedAt = new Set();
  // Residents at the same spot are laid out in a small grid around it, four per row.
  const spots = new Map();
  const placed = list.map(journey => {
    const at = tokenAt(journey, byNode, moment.time);
    const key = Math.round(at.point[0]) + "," + Math.round(at.point[1]);
    spots.set(key, (spots.get(key) || 0) + 1);
    return {journey, ...at, key};
  });
  const seen = new Map();
  placed.forEach(({journey, point: [x, y], state, key}) => {
    // Refused before leaving home, or turned away at a shelter (full or not yet open).
    if (state === "rejected") refusedAt.add(key + "," + (journey.departed ? "受け入れ拒否" : "出発拒否"));
    const index = seen.get(key) || 0;
    seen.set(key, index + 1);
    const perRow = Math.min(4, spots.get(key));
    const rows = Math.ceil(spots.get(key) / perRow);
    const dx = ((index % perRow) - (perRow - 1) / 2) * 22;
    const dy = (Math.floor(index / perRow) - (rows - 1) / 2) * 22;
    const g = svg("g", {class: "token " + state});
    g.append(svg("circle", {cx: x + dx, cy: y + dy, r: 10}));
    g.append(svg("text", {x: x + dx, y: y + dy + 4, "text-anchor": "middle"},
      personNumber(scenario, journey.intent.actor_id) ?? "?"));
    target.append(g);
  });
  refusedAt.forEach(key => {
    const [x, y, text] = key.split(",");
    // Opposite side from the node's own label.
    drawTag(target, Number(x), Number(y) + (labelBelow(Number(y)) ? -44 : 44), text, "closure-tag");
  });
}

// --- the roads x time chart ------------------------------------------------------------

function niceStep(span, maxTicks) {
  for (const step of [5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600]) {
    if (span / step <= maxTicks) return step;
  }
  return Math.ceil(span / maxTicks);
}
/* Roads x virtual time: closure windows, every move the plan makes (or the Core
 * refused), the deadline and the playback cursor. Display only. */
function drawTimeChart(target, scenario, plan, result, time) {
  const deadline = scenario.deadline_s;
  // Drawn in CSS pixels at the panel's own width, so labels stay legible on a phone.
  const width = Math.round(Math.min(640, Math.max(300, target.clientWidth || 640)));
  // Rows: roads the plan uses or that are closed at some time; all of them on small maps.
  const used = new Set(plan.intents.flatMap(intent => intent.route));
  const rows = isSmallMap(scenario) ? scenario.roads
    : scenario.roads.filter(road => used.has(road.id) || road.closures.length);
  const labelWidth = Math.max(...rows.map(road => textWidth(roadName(road.id))), 40);
  const left = Math.min(width < 480 ? 110 : 170, Math.max(width < 480 ? 64 : 92, labelWidth + 22));
  const right = width - 24;
  const top = 34;
  const row = rows.length > 4 ? 32 : 46;
  const bar = rows.length > 4 ? 18 : 24;
  const bottom = top + rows.length * row;
  const x = s => left + (right - left) * Math.min(Math.max(s, 0), deadline) / Math.max(1, deadline);
  target.setAttribute("viewBox", "0 0 " + width + " " + (bottom + 30));
  target.replaceChildren();

  const pattern = svg("pattern", {
    id: "closure-hatch", width: 8, height: 8,
    patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)",
  });
  pattern.append(svg("rect", {width: 8, height: 8, class: "hatch-back"}));
  pattern.append(svg("line", {x1: 0, y1: 0, x2: 0, y2: 8, class: "hatch-line"}));
  const defs = svg("defs");
  defs.append(pattern);
  target.append(defs);

  const step = niceStep(deadline, Math.max(2, Math.floor((right - left) / 70)));
  // The deadline carries its own label, so the last regular tick stays a step and a half clear.
  for (let s = 0; s <= deadline - step * 1.5; s += step) {
    target.append(svg("line", {x1: x(s), y1: top - 6, x2: x(s), y2: bottom, class: "grid"}));
    target.append(svg("text", {x: x(s), y: bottom + 20, "text-anchor": "middle", class: "tick"}, s + "秒"));
  }

  const all = journeys(scenario, plan, result);
  rows.forEach((road, r) => {
    const y = top + r * row + (row - bar) / 2;
    target.append(svg("text", {
      x: left - 12, y: y + bar / 2 + 5, "text-anchor": "end", class: "row-label",
    }, roadName(road.id)));
    target.append(svg("rect", {x: left, y, width: right - left, height: bar, rx: 5, class: "track"}));
    road.closures.forEach(c => {
      // fill as an attribute: url(#id) in an external stylesheet is not resolved by every engine.
      target.append(svg("rect", {
        x: x(c.start_s), y, width: Math.max(2, x(c.end_s) - x(c.start_s)), height: bar,
        class: "closure", fill: "url(#closure-hatch)",
      }));
    });
    const groups = new Map();
    let labelEnd = -Infinity;  // right edge of the last label drawn on this row
    all.forEach(journey => journey.legs.filter(leg => leg.road.id === road.id).forEach(leg => {
      const state = journey.departed ? "move" : leg.closure ? "blocked" : "refused";
      const key = [leg.from, leg.to, state].join("|");
      const group = groups.get(key) || {leg, state, count: 0};
      group.count += 1;
      groups.set(key, group);
    }));
    for (const {leg, state, count} of groups.values()) {
      const x1 = x(leg.from);
      const span = Math.max(3, x(leg.to) - x1);
      const rect = svg("rect", {x: x1, y: y + 3, width: span, height: bar - 6, rx: 4, class: "leg " + state});
      rect.append(svg("title", {}, count + "名・" + leg.from + "–" + leg.to + "秒"));
      target.append(rect);
      let label = count + "名";
      if (state === "blocked") {
        const o1 = x(Math.max(leg.from, leg.closure.start_s));
        const o2 = x(Math.min(leg.to, leg.closure.end_s));
        target.append(svg("rect", {x: o1, y: y - 4, width: Math.max(2, o2 - o1), height: bar + 8, rx: 4, class: "overlap"}));
        label += "・重なり→拒否";
      }
      const inside = state === "move" && span > textWidth(label) + 12;
      // Outside labels start past the bar and any closure it overlaps, so hatching stays clear.
      const after = x(Math.max(leg.to, leg.closure && state === "blocked" ? leg.closure.end_s : 0)) + 8;
      if (!inside && after + textWidth(label) > width) label = count + "名" + (state === "blocked" ? "・拒否" : "");
      const fits = after + textWidth(label) <= width;
      const start = inside ? x1 + span / 2 - textWidth(label) / 2 : fits ? after : width - 4 - textWidth(label);
      if (start < labelEnd + 4) continue;  // the bar stays; its count is in the tooltip
      labelEnd = start + textWidth(label);
      target.append(svg("text", {
        x: inside ? x1 + span / 2 : fits ? after : width - 4, y: y + bar / 2 + 4,
        "text-anchor": inside ? "middle" : fits ? "start" : "end",
        class: "leg-label" + (inside ? "" : state === "blocked" ? " alert" : " outside"),
      }, label));
    }
  });

  target.append(svg("line", {x1: x(deadline), y1: top - 10, x2: x(deadline), y2: bottom + 4, class: "deadline"}));
  target.append(svg("text", {
    x: x(deadline), y: bottom + 20, "text-anchor": "end", class: "tick deadline-tick",
  }, "期限 " + deadline + "秒"));
  const cx = x(time);
  target.append(svg("line", {x1: cx, y1: top - 10, x2: cx, y2: bottom, class: "cursor"}));
  target.append(svg("text", {
    x: Math.min(Math.max(cx, left + 20), right - 20), y: top - 16, "text-anchor": "middle", class: "cursor-label",
  }, "t=" + Math.round(time)));
  target.setAttribute("aria-label", "道路ごとの通行止めと移動の時間。期限" + deadline + "秒" +
    (result ? "、判定" + result.status : ""));
}

// --- the verdict basis -----------------------------------------------------------------

const checkNames = {
  unknown_road: "経路",
  disconnected_route: "経路",
  wrong_destination: "経路",
  route_blocked: "通行止め",
  deadline_exceeded: "期限",
  departure_after_deadline: "期限",
  shelter_full: "定員",
  shelter_not_open: "開設時刻",
  unsuitable_shelter: "本人の事情",
};
/* What the Core's verdict rests on. A PASS means every intent passed every check, so
 * each check is listed with its figures; a FAIL lists the Core's own rejections. */
function drawChecks(target, scenario, plan, result) {
  const items = [];
  const deadline = scenario.deadline_s;
  if (!result) {
    items.push(["info", "1", "地図の道路か上のボタンで、通行止めにする道路を選ぶ（最大" + MAX_CLOSED + "本）"]);
    items.push(["info", "2", "「エージェントで立て直す」を押す（Geminiのライブ実行も選べます）"]);
    items.push(["info", "3", "エージェントが修復・弱点探索・代替計画を自分で進め、Coreが毎回検証します"]);
  } else if (result.status === "PASS") {
    const last = lastArrival(result);
    items.push(["ok", "経路", "全員の経路が道路でつながり、指定の避難所に着く"]);
    items.push(["ok", "通行止め", "どの移動も、通行止めの時間と重ならない"]);
    items.push(["ok", "期限", "最終到着 " + last + "秒 ≤ 期限 " + deadline + "秒（余裕 " + (deadline - last) + "秒）"]);
    const load = new Map();
    plan.intents.filter(i => result.evacuated.includes(i.actor_id))
      .forEach(i => load.set(i.shelter_id, (load.get(i.shelter_id) || 0) + 1));
    items.push(["ok", "定員", [...load].map(([id, count]) => {
      const shelter = scenario.shelters.find(s => s.id === id);
      return shelterPlaceName(scenario, id) + " " + count + "名 ≤ 定員 " + shelter.capacity + "名";
    }).join("・")]);
    if (scenario.shelters.some(s => s.opens_at_s > 0)) items.push(["ok", "開設時刻", "どの避難所にも開設後に到着"]);
  } else {
    const byReason = new Map();
    result.events.filter(e => e.kind === "rejected").forEach(e => {
      byReason.set(e.reason, (byReason.get(e.reason) || []).concat(personLabel(scenario, e.actor_id)));
    });
    for (const [reason, names] of byReason) {
      let text = names.join("・") + "：" + (reasonNames[reason] || reason);
      const leg = reason === "route_blocked" ? blockedLegOf(scenario, plan, result) : null;
      if (leg) {
        text += "（" + roadName(leg.road) + "の通行止め " + leg.closure.start_s + "–" + leg.closure.end_s +
          "秒 ∩ 移動 " + leg.from + "–" + leg.to + "秒）";
      }
      items.push(["ng", checkNames[reason] || "計画", text]);
    }
    const refused = journeys(scenario, plan, result).filter(j => j.rejected);
    const early = refused.length > 0 && refused.every(j => !j.departed);
    items.push(["ng", "結果", "未避難 " + result.not_evacuated.length + "名で計画全体がFAIL" +
      (early ? "。出発前に拒否されたため、期限・定員の検査には進んでいません" : "")]);
  }
  target.replaceChildren(...items.map(([state, name, text]) => {
    const item = document.createElement("li");
    item.className = state;
    item.append(span("check-mark", state === "ok" ? "✓" : state === "ng" ? "✗" : "→"),
      span("check-name", name), span("check-text", text));
    return item;
  }));
}

// --- captions and summaries ------------------------------------------------------------

function captionAt(scenario, plan, result, time) {
  const deadline = scenario.deadline_s;
  if (!result) return "道路を選んで、エージェントに立て直させてください";
  if (time >= deadline) {
    return "期限 " + deadline + "秒：" + result.evacuated.length + "名が避難・" +
      result.not_evacuated.length + "名が未避難。Coreの判定は " + result.status;
  }
  const moving = journeys(scenario, plan, result).filter(j =>
    j.departed && time >= j.departed.time_s &&
    !(j.arrived && time >= j.arrived.time_s) && !(j.rejected && time >= j.rejected.time_s));
  if (moving.length) {
    const roads = new Set(moving.map(j => (j.legs.find(leg => time < leg.to) || j.legs[j.legs.length - 1]).road.id));
    return time + "秒：" + moving.map(j => personLabel(scenario, j.intent.actor_id)).join("・") +
      "が移動中（" + [...roads].map(roadName).join("・") + "）";
  }
  const past = result.events.filter(e => e.time_s <= time);
  if (!past.length) {
    // A plan may wait for a closure to end: say who is waiting and until when.
    const waiting = plan.intents.filter(intent => intent.departure_s > time);
    if (!waiting.length) return time + "秒：まだ誰も出発していません";
    const first = Math.min(...waiting.map(intent => intent.departure_s));
    return time + "秒：" + waiting.map(intent => personLabel(scenario, intent.actor_id)).join("・") +
      "が自宅で待機中（" + first + "秒に出発）";
  }
  const at = past[past.length - 1].time_s;
  const parts = new Map();
  past.filter(e => e.time_s === at).forEach(e => {
    const label = e.kind === "arrived" ? (nodeNames[e.node] || e.node) + "に到着"
      : e.kind === "departed" ? "出発" : reasonNames[e.reason] || e.reason;
    parts.set(label, (parts.get(label) || []).concat(personLabel(scenario, e.actor_id)));
  });
  return at + "秒：" + [...parts].map(([label, names]) => names.join("・") + "が" + label).join("、");
}
/* The still frame shown when the clock does not play (reduced motion, or no animation
 * asked for): the last arrival, else the last event. */
function finalMoment(result) {
  if (!result) return 0;
  const arrival = lastArrival(result);
  if (arrival !== null) return arrival;
  return result.events.length ? result.events[result.events.length - 1].time_s : 0;
}
function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
function challengeSummary(trace, scenario, plan, who) {
  const result = trace.final_result;
  if (result && result.status === "PASS") {
    const risk = trace.outcome === "escalated" ? "。代わりのない弱点は人へ引き継ぎ"
      : trace.outcome === "verified_with_residual_risk" ? "。一部の弱点は未対策のまま" : "";
    return who + "が立て直しました ─ " + describePlan(scenario, plan) + risk;
  }
  if (trace.outcome.startsWith("halted")) return who + "は" + outcomeNames[trace.outcome] + "。途中の計画は渡しません";
  return who + "は全員を避難させる計画を見つけられず、人へ引き継ぎました。一部だけの計画を「完了」にはしません" +
    "（元の計画では避難できない住民 " + trace.initial_result.not_evacuated.length + "名）";
}
/* Counts what the agent did in each challenge. "cheated" counts a finish on a plan that
 * failed verification; the loop's guard makes it impossible, and the board shows that. */
function tally(board, trace) {
  board.tries += 1;
  if (trace.final_result && trace.final_result.status === "PASS") board.rebuilt += 1;
  if (trace.outcome === "escalated") board.handed += 1;
  const cheated = trace.steps.some(s => s.decision && s.decision.action === "finish" && !s.policy_error &&
      s.observation.plan_status !== "PASS") ||
    (trace.outcome.startsWith("verified") && !(trace.final_result && trace.final_result.status === "PASS"));
  if (cheated) board.cheated += 1;
}
function stepEffect(effect, action) {
  if (effect.status === "no_candidate") return "検証を通る修復案なし";
  if (effect.status === "search_budget_exhausted") return "探索上限に達して中止";
  if (action === "run_repair") return "修復案 " + effect.plan_id + " → " + effect.plan_status;
  if (action === "run_redteam") {
    const failing = effect.probes.filter(p => p.status === "FAIL").length;
    const skipped = effect.roads_not_probed ? "（未検査 " + effect.roads_not_probed + "本）" : "";
    return "単一障害点 " + failing + "件／検査 " + effect.probes.length + "本" + skipped;
  }
  if (action === "plan_contingency") {
    const covered = effect.contingencies.filter(c => c.reason === "verified_alternative").length;
    return "代替計画 " + covered + "／" + effect.contingencies.length + "件が検証済み";
  }
  return effect.plan_status + "の計画で" + actionNames[action];
}
function traceEffect(trace, action) {
  const step = trace.steps.find(s => s.decision && s.decision.action === action && s.effect);
  return step ? step.effect : null;
}
function roadLabel(id) {
  return roadNames[id] ? roadNames[id] + "（" + id + "）" : id;
}
function describePlan(scenario, plan) {
  const groups = new Map();
  plan.intents.forEach(intent => {
    const key = [intent.departure_s, intent.route.join(" → "), intent.shelter_id].join("|");
    groups.set(key, (groups.get(key) || 0) + 1);
  });
  return [...groups].map(([key, count]) => {
    const [departure, route, shelter] = key.split("|");
    const start = Number(departure) > 0 ? departure + "秒まで待機して " : "すぐに ";
    const roads = route.split(" → ").map(roadName).join(" → ");
    return count + "名：" + start + roads + " → " + shelterPlaceName(scenario, shelter);
  }).join("／");
}

// --- server calls and live Gemini answers ----------------------------------------------

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json", Accept: "application/json"},
    credentials: "same-origin",
    cache: "no-store",
    body: JSON.stringify(body),
  });
  return {response, payload: await response.json().catch(() => ({}))};
}
/* The server's refusals of a live Gemini request (its "detail"), in words for the visitor. */
const liveErrors = {
  "another live run is in progress": "ほかの方が実行中です。少し待ってからお試しください",
  "daily live run limit reached": "本日のライブ実行枠を使い切りました",
  "live AI is disabled": "ライブ実行は現在停止中です",
  "agent service timeout": "Geminiの実行が時間内に終わりませんでした",
  "agent answer failed Core re-verification": "Geminiの結果がCoreの再検証と一致しないため表示しません",
  "reader named a road it was not offered": "Geminiが街にない道路を答えたため、読み取りを使いません",
  "reader answer is not a valid reading": "Geminiの読み取り結果が正しい形ではないため使いません",
  "live run limit unavailable": "本日の実行回数を確認できないため、Geminiを呼びませんでした",
  "agent service failed": "Geminiを呼ぶサービスがエラーを返しました",
  "agent service unavailable": "Geminiを呼ぶサービスに接続できませんでした",
  "agent answer is not a valid trace": "Geminiの判断の記録が正しい形ではありませんでした",
  "agent answer is not a Gemini run": "Geminiの判断として確かめられませんでした",
  "reader answer is not from Gemini": "Geminiの読み取りとして確かめられませんでした",
  "reader named a resident or shelter it was not offered": "Geminiがこの街にない住民か避難所を答えたため、読み取りを使いません",
  "reader timeout": "Geminiの読み取りが時間内に終わりませんでした",
  "too many requests": "短い時間に操作が続いたため、受け付けを一時止めています。少し待ってからお試しください",
};
/* What the visitor reads when a live Gemini request failed: the server's reason when it
 * gave a known one, else the caller's fallback. payload is null when nothing came back. */
function liveErrorText(payload, fallback) {
  return (payload && liveErrors[payload.detail]) || fallback;
}
/* The rule that stops an unverified answer from being shown as Gemini's: the server
 * replayed it through the Core, and the trace is Gemini's, not the rule policy's. */
function isVerifiedGeminiRun(payload) {
  return payload.verification === "replay_verified" && payload.trace?.policy === "vertex-gemini";
}
const GEMINI_WAIT_MESSAGE = "非公開サービスでGeminiが判断中です（長いときは1分あまり）";
const NETWORK_FAILED_MESSAGE = "通信に失敗しました。時間をおいてお試しください";
