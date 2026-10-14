/* The /lab page: DOM wiring for the 2D evacuation laboratory. It draws the synthetic map
 * and its residents on a canvas, shows the genetic algorithm's progress and the verdict on
 * its plan, and runs the self-test panel. Everything is computed in the browser by
 * evac-lab-model.mjs; this page makes no network request.
 * The animation loop must stay a function named `animate`: tests_js/evac-lab-ui.test.mjs
 * recognises it by that name so that the test does not loop forever. */
import {
  settingsFrom, makeScenario, simulate, makeGreedyPlan,
  makeGeneticPlan, runSelfTests, roadId,
} from './evac-lab-model.mjs';

const $ = id => document.getElementById(id);
const canvas = $('map');
const ctx = canvas.getContext('2d');
const fitnessCanvas = $('fitnessCanvas');
const graph = fitnessCanvas.getContext('2d');
const paramIds = ['people', 'closurePercent', 'shelterCapacity', 'deadline', 'reactionMax', 'population',
  'generations', 'mutation', 'riskWeight'];
const suffixes = {people: ' 人', closurePercent: ' %', shelterCapacity: ' 人', deadline: ' 秒', reactionMax: ' 秒',
  population: '', generations: '', mutation: ' %', riskWeight: ''};

const VIRTUAL_SECONDS_PER_SECOND = 18;  // playback at 1x speed
const MAX_FRAME_S = 0.1;  // a long gap between frames (a hidden tab) does not jump the clock
const EVENT_LOG_ROWS = 9;  // the most recent events listed
const PREVIEW_INTENTS = 3;  // MoveIntents shown in the JSON excerpt
const PICK_PERSON_RADIUS = 0.022;  // how near a click must be to select a resident (share of the map)
const PICK_ROAD_RADIUS = 0.016;  // and to close or reopen a road
const PROGRESS_EVERY = 2;  // generations between progress redraws
// Event kinds and rejection reasons, as the event log names them.
const EVENT_MEANINGS = {
  departed: '避難を開始', arrived: '避難所に到着', rejected: '移動が拒否', route_blocked: '道路閉鎖',
  shelter_full: '定員超過', deadline_exceeded: '期限超過', wrong_destination: '目的地違い',
  departure_after_deadline: '開始が遅すぎる', disconnected_route: '経路不整合', already_evacuated: '避難済み',
  actor_busy: '移動中', shelter_not_open: '避難所未開設', unknown_road: '道路なし',
};
// Green patches behind the map, [x, y, radius] as shares of the canvas.
const LANDSCAPE_PATCHES = [[.14, .18, .1], [.62, .16, .09], [.31, .82, .1], [.86, .64, .07]];

const state = {
  lab: null, plan: null, result: null, ga: null, greedy: null, playing: false, t: 0, lastFrame: 0,
  request: 0, pending: null, selected: null, busy: false,
};

// --- settings and the side panels ------------------------------------------------------

function readSettings() {
  const raw = Object.fromEntries(paramIds.map(id => [id, Number($(id).value)]));
  return settingsFrom({...raw, seed: Number($('seed').value), strategy: $('strategy').value,
    disaster: $('disaster').value, speed: Number($('speed').value)});
}
function syncControls() {
  for (const id of paramIds) {
    const val = Number($(id).value);
    const shown = id === 'mutation' ? Math.round(val * 100) : val;
    $(id + '-val').textContent = shown + suffixes[id];
  }
  $('gaControls').style.opacity = $('strategy').value === 'greedy' ? '0.43' : '1';
}
/* mm:ss for a time in seconds. */
function formatClock(sec) {
  const s = Math.max(0, Math.floor(sec));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}
function setStatus(label, mode) {
  $('stageBadge').textContent = label;
  $('stageBadge').className = mode ?? 'stage-ready';
}
function setMessage(message) {
  $('simMessage').textContent = message;
}
function drawJsonPreview(plan) {
  const sample = {schema_version: plan.schema_version, id: plan.id, intents: plan.intents.slice(0, PREVIEW_INTENTS)};
  const rest = plan.intents.length > PREVIEW_INTENTS
    ? `\n\n… 残り ${plan.intents.length - PREVIEW_INTENTS} 件はJSON保存で確認できます。` : '';
  $('jsonPreview').textContent = JSON.stringify(sample, null, 2) + rest;
}
function drawVerdict(result) {
  const fail = result.status === 'FAIL';
  $('verdict').className = 'verdict ' + (fail ? 'fail' : 'pass');
  $('verdictValue').textContent = result.status;
  $('verdictDesc').textContent = fail ? '未避難の住民または拒否された移動があります' : '全住民が期限内に受け入れられました';
  $('evacCount').textContent = `${result.evacuated.length} / ${state.lab.scenario.personas.length}`;
  $('failedCount').textContent = result.not_evacuated.length + ' 人';
  $('averageArrival').textContent =
    result.stats.mean_arrival_s === null ? '—' : `${result.stats.mean_arrival_s.toFixed(1)} s`;
  $('shelterUsage').replaceChildren();
  for (const shelter of state.lab.scenario.shelters) {
    const used = result.stats.occupancy[shelter.id] || 0;
    const row = document.createElement('div');
    row.className = 'shelter-bar';
    const title = document.createElement('span');
    title.textContent = shelter.id === 'shelter_a' ? '避難所 A' : '避難所 B';
    const rail = document.createElement('div');
    rail.className = 'rail';
    const fill = document.createElement('i');
    fill.className = 'fill';
    fill.style.width = (used / Math.max(1, shelter.capacity) * 100) + '%';
    rail.append(fill);
    const count = document.createElement('b');
    count.textContent = `${used} / ${shelter.capacity}`;
    row.append(title, rail, count);
    $('shelterUsage').append(row);
  }
}
/* The GA's plan against the shortest-route baseline on the same map: residents evacuated. */
function drawComparison() {
  if (!state.ga || !state.greedy) return;
  const total = state.lab.scenario.personas.length;
  const ga = state.ga.result.evacuated.length;
  const greedy = state.greedy.result.evacuated.length;
  $('gaBar').style.width = `${ga / total * 100}%`;
  $('greedyBar').style.width = `${greedy / total * 100}%`;
  $('gaCount').textContent = `${ga} 人`;
  $('greedyCount').textContent = `${greedy} 人`;
}
/* The best score so far, generation by generation. */
function drawFitness(history, completed) {
  const w = fitnessCanvas.width;
  const h = fitnessCanvas.height;
  graph.clearRect(0, 0, w, h);
  graph.strokeStyle = '#e7edf5';
  graph.lineWidth = 1;
  for (let i = 0; i < 4; i++) {
    const y = 11 + (h - 25) * i / 3;
    graph.beginPath();
    graph.moveTo(0, y);
    graph.lineTo(w, y);
    graph.stroke();
  }
  if (history?.length) {
    const low = Math.min(...history);
    const high = Math.max(...history);
    const span = high - low || 1;
    graph.beginPath();
    history.forEach((score, i) => {
      const x = 12 + i / Math.max(1, history.length - 1) * (w - 24);
      const y = h - 14 - (score - low) / span * (h - 30);
      if (!i) graph.moveTo(x, y);
      else graph.lineTo(x, y);
    });
    graph.strokeStyle = '#1985d8';
    graph.lineWidth = 3;
    graph.lineJoin = 'round';
    graph.lineCap = 'round';
    graph.stroke();
    const endX = w - 12;
    const endY = h - 14 - (history[history.length - 1] - low) / span * (h - 30);
    graph.beginPath();
    graph.arc(endX, endY, 4.2, 0, Math.PI * 2);
    graph.fillStyle = '#27b7ad';
    graph.fill();
  } else {
    graph.fillStyle = '#8fa2b8';
    graph.font = '13px sans-serif';
    graph.fillText('経路探索待ち', 18, h / 2);
  }
  $('generationLabel').textContent = completed === 0 ? '探索未実施' : `${completed} 世代`;
}
function renderEventLog() {
  const events = state.result
    ? state.result.events.filter(e => e.time_s <= state.t).slice(-EVENT_LOG_ROWS).reverse() : [];
  $('eventCount').textContent = `${state.result?.events.length ?? 0} EVENTS`;
  $('eventLog').replaceChildren();
  if (!events.length) {
    const p = document.createElement('p');
    p.className = 'placeholder';
    p.textContent = '避難開始後、時刻に応じてイベントが表示されます。';
    $('eventLog').append(p);
    return;
  }
  for (const e of events) {
    const row = document.createElement('div');
    row.className = 'event';
    const time = document.createElement('time');
    time.textContent = formatClock(e.time_s);
    const description = document.createElement('span');
    description.textContent = `${e.actor_id.replace('person_', 'P')}：${EVENT_MEANINGS[e.kind] || e.kind}`;
    const reason = document.createElement('b');
    reason.textContent = e.kind === 'rejected' ? (EVENT_MEANINGS[e.reason] || e.reason)
      : e.kind === 'arrived' ? '収容' : '出発';
    if (e.kind === 'rejected') reason.className = 'bad';
    row.append(time, description, reason);
    $('eventLog').append(row);
  }
}

// --- playback --------------------------------------------------------------------------

function updatePlayback(t) {
  if (!state.lab) return;
  state.t = Math.max(0, Math.min(state.lab.scenario.deadline_s, t));
  $('timeSlider').value = String(state.t);
  $('timeLabel').textContent = `${formatClock(state.t)} / ${formatClock(state.lab.scenario.deadline_s)}`;
  renderScene();
  renderEventLog();
  if (state.result) {
    const departed = state.result.events.filter(e => e.kind === 'departed' && e.time_s <= state.t).length;
    const arrived = state.result.events.filter(e => e.kind === 'arrived' && e.time_s <= state.t).length;
    const rejected = state.result.events.filter(e => e.kind === 'rejected' && e.time_s <= state.t).length;
    const selected = state.selected ? ' · ' + state.selected + 'を選択中' : '';
    setMessage(`${Math.floor(state.t)}秒経過 · 避難開始 ${departed} 人 / 収容 ${arrived} 人 / 移動拒否 ${rejected} 件${selected}`);
  }
}
/* Runs on every animation frame for the life of the page; moves the clock while playing. */
function animate(now) {
  if (state.playing && state.lab) {
    if (state.lastFrame) {
      const dt = Math.min(MAX_FRAME_S, (now - state.lastFrame) / 1000);
      updatePlayback(state.t + dt * VIRTUAL_SECONDS_PER_SECOND * Number($('speed').value));
      if (state.t >= state.lab.scenario.deadline_s) {
        state.playing = false;
        syncPlayButton();
      }
    }
    state.lastFrame = now;
  } else state.lastFrame = 0;
  requestAnimationFrame(animate);
}
function syncPlayButton() {
  $('playBtn').textContent = state.playing ? 'Ⅱ' : '▶';
  $('playBtn').setAttribute('aria-label', state.playing ? '一時停止' : '再生');
}

// --- the canvas ------------------------------------------------------------------------

/* Where a resident is at the playback time, and in what state, from the plan and events. */
function getPersonState(person) {
  const {scenario, geometry} = state.lab;
  const at = geometry[person.start_node];
  if (!state.plan || !state.result) return {x: at.x, y: at.y, status: 'waiting'};
  const intent = state.plan.intents.find(i => i.actor_id === person.id);
  if (!intent) return {x: at.x, y: at.y, status: 'waiting'};
  const eventList = state.result.events.filter(e => e.actor_id === person.id);
  const start = eventList.find(e => e.kind === 'departed');
  const terminal = eventList.find(e => e.kind === 'arrived' || e.kind === 'rejected');
  if (terminal && state.t >= terminal.time_s) {
    const pos = geometry[terminal.node] ?? at;
    return {...pos, status: terminal.kind === 'arrived' ? 'safe' : 'rejected'};
  }
  if (!start || state.t < start.time_s) return {...at, status: state.t < intent.departure_s ? 'waiting' : 'rejected'};
  const roads = new Map(scenario.roads.map(r => [r.id, r]));
  let clock = start.time_s;
  for (const id of intent.route) {
    const road = roads.get(id);
    const end = clock + road.travel_time_s;
    if (state.t <= end) {
      const p = geometry[road.source];
      const q = geometry[road.target];
      const ratio = Math.max(0, Math.min(1, (state.t - clock) / road.travel_time_s));
      return {x: p.x + (q.x - p.x) * ratio, y: p.y + (q.y - p.y) * ratio, status: 'moving'};
    }
    clock = end;
  }
  return {...(geometry[scenario.shelters.find(s => s.id === intent.shelter_id).node] || at), status: 'moving'};
}
function paintBg(w, h) {
  const background = ctx.createLinearGradient(0, 0, w, h);
  background.addColorStop(0, '#e8f1f3');
  background.addColorStop(1, '#f2f7f9');
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = '#dce8ed';
  ctx.lineWidth = 1;
  for (let x = 0; x < w; x += 45) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }
  for (let y = 0; y < h; y += 45) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  // Landscape strips are illustrative only, not imported geographic data.
  ctx.save();
  ctx.globalAlpha = .5;
  ctx.fillStyle = '#cce6d7';
  for (const [x, y, r] of LANDSCAPE_PATCHES) {
    ctx.beginPath();
    ctx.ellipse(w * x, h * y, r * w * .6, r * h * .7, -.3, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}
function renderScene() {
  const w = canvas.width;
  const h = canvas.height;
  paintBg(w, h);
  if (!state.lab) return;
  const {scenario, geometry, hazard, settings} = state.lab;
  const hx = w * hazard.x;
  const hy = h * hazard.y;
  const r = Math.min(w, h) * hazard.r * 1.5;
  const grad = ctx.createRadialGradient(hx, hy, r * .1, hx, hy, r);
  if (settings.disaster === 'earthquake') {
    grad.addColorStop(0, '#ff996743');
    grad.addColorStop(.8, '#ffc59330');
    grad.addColorStop(1, '#ff996700');
  } else {
    grad.addColorStop(0, '#51a5f34e');
    grad.addColorStop(.7, '#7ac6e135');
    grad.addColorStop(1, '#4fb5e200');
  }
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.arc(hx, hy, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = settings.disaster === 'earthquake' ? '#b96f37' : '#448eb2';
  ctx.font = 'bold 12px system-ui';
  ctx.fillText(settings.disaster === 'earthquake' ? '震源付近' : '浸水想定域', hx - r * .45, hy - r * .54);
  ctx.lineCap = 'round';
  const used = new Map();
  for (const intent of state.plan?.intents ?? []) {
    for (const road of intent.route) used.set(road, (used.get(road) ?? 0) + 1);
  }
  for (const road of scenario.roads) {
    if (road.source > road.target) continue;  // render each physical street once
    const p = geometry[road.source];
    const q = geometry[road.target];
    const a = {x: p.x * w, y: p.y * h};
    const b = {x: q.x * w, y: q.y * h};
    const blocked = road.closures.length > 0;
    ctx.setLineDash([]);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 11;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.strokeStyle = blocked ? '#ec6c6b' : '#9bb7c6';
    ctx.lineWidth = blocked ? 5 : 4;
    ctx.setLineDash(blocked ? [7, 5] : []);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.setLineDash([]);
    // The plan may use the street in either direction.
    if (!blocked && (used.get(road.id) || used.get(roadId(road.target, road.source)))) {
      ctx.strokeStyle = '#5ac9bd88';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
    if (blocked) {
      const mid = {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
      ctx.strokeStyle = '#c74755';
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(mid.x - 5, mid.y - 5);
      ctx.lineTo(mid.x + 5, mid.y + 5);
      ctx.moveTo(mid.x - 5, mid.y + 5);
      ctx.lineTo(mid.x + 5, mid.y - 5);
      ctx.stroke();
    }
  }
  const usedOccupancy = {};
  for (const e of (state.result?.events ?? [])) {
    if (e.kind === 'arrived' && e.time_s <= state.t) {
      const intent = state.plan.intents.find(i => i.actor_id === e.actor_id);
      if (intent) usedOccupancy[intent.shelter_id] = (usedOccupancy[intent.shelter_id] ?? 0) + 1;
    }
  }
  for (const shelter of scenario.shelters) {
    const p = geometry[shelter.node];
    const x = p.x * w;
    const y = p.y * h;
    ctx.fillStyle = '#fff';
    ctx.strokeStyle = '#16a68d';
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(x, y, 24, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    // A small house: roof, wall, door.
    ctx.fillStyle = '#21a88c';
    ctx.beginPath();
    ctx.moveTo(x - 12, y - 2);
    ctx.lineTo(x, y - 13);
    ctx.lineTo(x + 12, y - 2);
    ctx.closePath();
    ctx.fill();
    ctx.fillRect(x - 9, y - 2, 18, 13);
    ctx.fillStyle = '#fff';
    ctx.fillRect(x - 2, y + 3, 5, 8);
    ctx.font = 'bold 12px system-ui';
    ctx.fillStyle = '#176b67';
    ctx.textAlign = 'center';
    ctx.fillText(`${usedOccupancy[shelter.id] ?? 0}/${shelter.capacity}`, x, y + 39);
    ctx.textAlign = 'start';
  }
  const points = [];
  for (let i = 0; i < scenario.personas.length; i++) {
    const p = scenario.personas[i];
    const point = getPersonState(p);
    points.push({id: p.id, ...point});
  }
  // Stable fractional offsets distinguish agents with the same location.
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const x = p.x * w;
    const y = p.y * h;
    const overlapIndex = points.slice(0, i)
      .filter(q => Math.abs(q.x - p.x) < .0001 && Math.abs(q.y - p.y) < .0001).length;
    const dx = (overlapIndex % 4 - 1.5) * 8;
    const dy = Math.floor(overlapIndex / 4) * -11;
    const selected = state.selected === p.id;
    if (selected) {
      ctx.beginPath();
      ctx.arc(x + dx, y + dy, 16, 0, Math.PI * 2);
      ctx.fillStyle = '#fbc34988';
      ctx.fill();
    }
    const color = p.status === 'safe' ? '#0fa486' : p.status === 'rejected' ? '#df716a'
      : p.status === 'moving' ? '#237bd5' : '#6b92b2';
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x + dx, y + dy - 3, 5.6, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.beginPath();
    ctx.roundRect(x + dx - 5.6, y + dy + 3, 11.2, 10, 3);
    ctx.fill();
    ctx.stroke();
  }
  ctx.fillStyle = '#51768d';
  ctx.font = 'bold 11px system-ui';
  ctx.fillText(`SEED ${settings.seed}  •  ${scenario.personas.length} PEOPLE  •  t = ${Math.floor(state.t)} s`, 22, 25);
}
function distanceToSegment(p, a, b) {
  const len = (b.x - a.x) ** 2 + (b.y - a.y) ** 2;
  if (len === 0) return Math.hypot(p.x - a.x, p.y - a.y);
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * (b.x - a.x) + (p.y - a.y) * (b.y - a.y)) / len));
  return Math.hypot(p.x - a.x - t * (b.x - a.x), p.y - a.y - t * (b.y - a.y));
}
/* A click as a share of the canvas, the units of the scenario's geometry. */
function scenarioPoint(event) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left) / rect.width,
    y: (event.clientY - rect.top) / rect.height,
  };
}
/* A click selects the nearest resident, or else closes (or reopens) the nearest street. */
async function clickedCanvas(event) {
  if (!state.lab || state.busy) return;
  const p = scenarioPoint(event);
  let bestAgent = null;
  let bestAgentDistance = PICK_PERSON_RADIUS;
  for (const person of state.lab.scenario.personas) {
    const pt = getPersonState(person);
    const distance = Math.hypot(p.x - pt.x, p.y - pt.y);
    if (distance < bestAgentDistance) {
      bestAgentDistance = distance;
      bestAgent = person.id;
    }
  }
  if (bestAgent) {
    state.selected = bestAgent;
    renderScene();
    setMessage(`${bestAgent} を選択しました。色は待機・移動・収容・拒否の状態を表します。`);
    return;
  }
  const {scenario, geometry} = state.lab;
  let closest = null;
  let closestDistance = PICK_ROAD_RADIUS;
  for (const road of scenario.roads) {
    if (road.source > road.target) continue;
    const d = distanceToSegment(p, geometry[road.source], geometry[road.target]);
    if (d < closestDistance) {
      closestDistance = d;
      closest = road;
    }
  }
  if (!closest) return;
  const closed = closest.closures.length > 0;
  for (const road of scenario.roads) {
    if ((road.source === closest.source && road.target === closest.target) ||
        (road.source === closest.target && road.target === closest.source)) {
      road.closures = closed ? [] : [{start_s: 0, end_s: scenario.deadline_s}];
    }
  }
  await calculate({regenerate: false, fromManual: true});
}

// --- planning --------------------------------------------------------------------------

/* Builds the scenario (or keeps it after a road click), plans it with the shortest-route
 * baseline and the GA, shows the chosen strategy's plan and starts playback. A newer call
 * makes this one stop. */
async function calculate({regenerate = true, fromManual = false} = {}) {
  state.request++;
  const mine = state.request;
  state.pending?.abort();
  state.pending = new AbortController();
  state.playing = false;
  syncPlayButton();
  state.busy = true;
  $('runBtn').disabled = true;
  const settings = readSettings();
  if (regenerate || !state.lab) {
    state.lab = makeScenario(settings);
    state.selected = null;
  } else {
    state.lab.settings = {...state.lab.settings, ...settings,
      deadline: state.lab.scenario.deadline_s, people: state.lab.scenario.personas.length,
      disaster: state.lab.settings.disaster, seed: state.lab.settings.seed};
  }
  $('incidentBadge').textContent = state.lab.settings.disaster === 'earthquake' ? '地震シナリオ' : '洪水シナリオ';
  $('timeSlider').max = String(state.lab.scenario.deadline_s);
  state.t = 0;
  state.result = null;
  state.plan = null;
  setStatus('OPTIMIZING', 'pending');
  setMessage(fromManual ? '道路条件を反映して再探索中…' : '道路網を生成し、住民ごとの行動候補を探索しています…');
  const greedyPlan = makeGreedyPlan(state.lab);
  const greedyResult = simulate(state.lab.scenario, greedyPlan);
  state.greedy = {plan: greedyPlan, result: greedyResult};
  try {
    const ga = await makeGeneticPlan(state.lab, {
      signal: state.pending.signal,
      onProgress: ({generation, total, bestCount, history}) => {
        if (mine !== state.request) return;
        if (generation % PROGRESS_EVERY === 0 || generation === total) {
          drawFitness(history, generation);
          setMessage(`GA 最適化 ${generation}/${total} 世代 · 暫定的に ${bestCount} 人が収容可能`);
        }
      },
      yieldToUi: () => new Promise(resolve => requestAnimationFrame(resolve)),
    });
    if (mine !== state.request) return;
    state.ga = ga;
    if (settings.strategy === 'greedy') {
      state.plan = greedyPlan;
      state.result = greedyResult;
    } else {
      state.plan = ga.plan;
      state.result = ga.result;
    }
    drawVerdict(state.result);
    drawJsonPreview(state.plan);
    drawComparison();
    updatePlayback(0);
    setStatus(state.result.status === 'PASS' ? 'PASS' : 'FAIL', state.result.status === 'PASS' ? 'stage-ready' : 'failed');
    state.playing = true;
    syncPlayButton();
    state.lastFrame = 0;
  } catch (err) {
    if (err.name !== 'AbortError') {
      setStatus('ERROR', 'failed');
      setMessage(`探索を中止しました：${err.message}`);
      console.error(err);
    }
  } finally {
    if (mine === state.request) {
      state.busy = false;
      $('runBtn').disabled = false;
    }
  }
}

// --- wiring ----------------------------------------------------------------------------

for (const id of paramIds) $(id).addEventListener('input', syncControls);
$('strategy').addEventListener('change', syncControls);
$('runBtn').addEventListener('click', () => calculate());
$('playBtn').addEventListener('click', () => {
  if (!state.result) return;
  if (state.t >= state.lab.scenario.deadline_s) updatePlayback(0);
  state.playing = !state.playing;
  state.lastFrame = 0;
  syncPlayButton();
});
$('restartBtn').addEventListener('click', () => {
  state.playing = false;
  syncPlayButton();
  updatePlayback(0);
});
$('timeSlider').addEventListener('input', e => {
  state.playing = false;
  syncPlayButton();
  updatePlayback(Number(e.target.value));
});
$('map').addEventListener('click', clickedCanvas);
$('testBtn').addEventListener('click', async () => {
  $('testBtn').disabled = true;
  $('testSummary').textContent = 'テスト実行中…';
  try {
    const results = await runSelfTests();
    $('testList').replaceChildren();
    for (const result of results) {
      const row = document.createElement('div');
      row.className = 'test-item';
      const badge = document.createElement('b');
      badge.className = result.passed ? '' : 'bad';
      badge.textContent = result.passed ? '✓' : '×';
      const label = document.createElement('span');
      label.textContent = result.name;
      row.append(badge, label);
      $('testList').append(row);
    }
    const count = results.filter(x => x.passed).length;
    $('testSummary').textContent = `${count}/${results.length} PASS`;
    setMessage(`${count} / ${results.length} のブラウザ内テストに成功しました。Python本体のテストとは別です。`);
  } catch (err) {
    $('testSummary').textContent = 'テストエラー';
    setMessage(`テスト失敗：${err.message}`);
  } finally {
    $('testBtn').disabled = false;
  }
});
$('copyBtn').addEventListener('click', async () => {
  if (!state.plan) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(state.plan, null, 2));
    $('copyBtn').textContent = 'コピーしました';
  } catch {
    setMessage('ブラウザの権限制限によりコピーできませんでした。JSON保存をご利用ください。');
  }
});
$('exportBtn').addEventListener('click', () => {
  if (!state.plan) return;
  const json = {
    notice: 'Synthetic research simulation; not for real-life evacuation guidance. JS reference validator only.',
    settings: state.lab.settings, scenario: state.lab.scenario,
    visual_metadata: {geometry: state.lab.geometry, profiles: state.lab.profiles, hazard: state.lab.hazard},
    algorithm: state.lab.settings.strategy, plan: state.plan, result: state.result,
    comparison: {ga: state.ga?.result, shortest: state.greedy?.result},
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(json, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = `evacweave-lab-seed-${state.lab.settings.seed}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
syncControls();
drawFitness([], 0);
requestAnimationFrame(animate);
calculate();
