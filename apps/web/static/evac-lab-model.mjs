/**
 * EvacWeave 2D laboratory / self-contained deterministic reference engine.
 * No real resident data, external network, model weights, or paid API calls.
 * The JavaScript validator mirrors a subset of the Python static-network-v1
 * event rules, but is NOT the authoritative Python Simulation Core.
 */
export const DEFAULTS = Object.freeze({
  seed: 2026,
  people: 20,
  closurePercent: 17,
  shelterCapacity: 12,
  deadline: 140,
  reactionMax: 22,
  population: 44,
  generations: 48,
  mutation: 0.13,
  riskWeight: 1.4,
  speed: 1.6,
  strategy: 'ga',
  disaster: 'flood',
});

const MIN_MAX = {
  seed: [1, 2147483647], people: [2, 48], closurePercent: [0, 55],
  shelterCapacity: [1, 48], deadline: [35, 300], reactionMax: [0, 80],
  population: [8, 100], generations: [1, 140], mutation: [0, 0.8],
  riskWeight: [0, 5], speed: [0.25, 5],
};

export function settingsFrom(raw = {}) {
  const next = { ...DEFAULTS };
  for (const [name, [lo, hi]] of Object.entries(MIN_MAX)) {
    const n = Number(raw[name] ?? next[name]);
    next[name] = Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : DEFAULTS[name];
  }
  for (const key of ['seed','people','closurePercent','shelterCapacity','deadline','reactionMax','population','generations']) {
    next[key] = Math.floor(next[key]);
  }
  next.strategy = raw.strategy === 'greedy' ? 'greedy' : 'ga';
  next.disaster = raw.disaster === 'earthquake' ? 'earthquake' : 'flood';
  return next;
}

export function makeRandom(initial) {
  let state = (Math.floor(Number(initial)) >>> 0) || 1;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return (state >>> 0) / 4294967296;
  };
}

const columns = 7;
const rows = 5;
const nodeId = (x, y) => `n_${x}_${y}`;
/** The id of the directed road from node a to node b. */
export const roadId = (a, b) => `road_${a}_${b}`;

/** Generate a graph contract accepted structurally by the existing Python Core. */
export function makeScenario(config = {}) {
  const c = settingsFrom(config);
  const rand = makeRandom(c.seed);
  const nodes = [];
  const geometry = {};
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < columns; x++) {
      const id = nodeId(x,y);
      nodes.push(id);
      geometry[id] = { x: 0.09 + (x / (columns-1)) * 0.82, y: 0.12 + (y / (rows-1)) * 0.76 };
    }
  }
  const hazard = c.disaster === 'flood'
    ? { x: 0.43, y: 0.55, r: 0.27, title: '浸水域' }
    : { x: 0.51, y: 0.36, r: 0.24, title: '建物被害域' };
  const undirected = [];
  for (let y = 0; y < rows; y++) for (let x = 0; x < columns; x++) {
    if (x < columns-1) undirected.push([nodeId(x,y), nodeId(x+1,y)]);
    if (y < rows-1) undirected.push([nodeId(x,y), nodeId(x,y+1)]);
  }
  // Deterministic weighted sampling biases closures toward the incident area.
  const closuresToAdd = Math.floor(undirected.length * c.closurePercent / 100);
  const ranked = undirected.map(([a,b],i) => {
    const p=geometry[a], q=geometry[b];
    const dist=Math.hypot((p.x+q.x)/2-hazard.x,(p.y+q.y)/2-hazard.y);
    const localBias = Math.max(0, 1 - dist / (hazard.r*1.6));
    return {i, priority: rand() + localBias*0.6};
  }).sort((a,b)=>b.priority-a.priority || a.i-b.i);
  const closed = new Set(ranked.slice(0, closuresToAdd).map(x=>x.i));
  const roads = [];
  const riskByRoad = {};
  for (let i=0; i<undirected.length; i++) {
    const [a,b] = undirected[i];
    const p=geometry[a],q=geometry[b];
    const dist=Math.hypot((p.x+q.x)/2-hazard.x,(p.y+q.y)/2-hazard.y);
    const risk=Math.max(0, 1-dist/(hazard.r*2));
    const travel = 8 + Math.floor(rand()*5) + (p.y!==q.y?2:0);
    for (const [source,target] of [[a,b],[b,a]]) {
      const id=roadId(source,target);
      roads.push({ id, source, target, travel_time_s:travel,
        closures:closed.has(i) ? [{start_s:0,end_s:c.deadline}] : [] });
      riskByRoad[id] = risk;
    }
  }
  const shelters = [
    {id:'shelter_a',node:nodeId(6,0),capacity:c.shelterCapacity,opens_at_s:0},
    {id:'shelter_b',node:nodeId(6,4),capacity:c.shelterCapacity,opens_at_s:0},
  ];
  const personas=[];
  const profiles={};
  for(let i=0;i<c.people;i++){
    const id=`person_${String(i+1).padStart(2,'0')}`;
    const x=Math.floor(rand()*3),y=Math.floor(rand()*rows);
    personas.push({id,start_node:nodeId(x,y)});
    const behaviorRoll=rand();
    const behavior=behaviorRoll<0.24?'慎重':behaviorRoll<0.5?'遅延':'通常';
    const delay=Math.floor(rand()*(c.reactionMax+1)) + (behavior==='遅延'?Math.floor(c.reactionMax*0.35):0);
    profiles[id]={behavior,reaction_s:Math.min(delay,c.deadline+1),riskTolerance:0.55+rand()*1.1};
  }
  const scenario={
    schema_version:'evacuation-scenario/v1',id:'lab_scenario',dataset_version:'lab_seed_'+c.seed,
    synthetic:true,deadline_s:c.deadline,nodes,roads,shelters,personas,
  };
  return {scenario,geometry,hazard,riskByRoad,profiles,settings:c};
}

// Queue priority of events at the same time, as in packages/simulation/engine.py:
// arrivals are handled before departures.
const ARRIVAL = 0;
const DEPARTURE = 1;

/** A deterministic, non-LLM Python-Core-like event simulator. */
export function simulate(scenario, plan) {
  const roads = new Map(scenario.roads.map(r=>[r.id,r]));
  const shelters = new Map(scenario.shelters.map(s=>[s.id,s]));
  const positions = new Map(scenario.personas.map(p=>[p.id,p.start_node]));
  const occupied=new Map(scenario.shelters.map(s=>[s.id,0]));
  const moving=new Set();
  const evacuated=new Set();
  const events=[];
  const arrivals=[];
  const queue=[];
  plan.intents.forEach((intent,index)=>queue.push({time:Math.min(intent.departure_s,scenario.deadline_s),priority:DEPARTURE,actor:intent.actor_id,index,intent}));
  const reject=(time,intent,reason)=>events.push({time_s:time,actor_id:intent.actor_id,kind:'rejected',reason,node:positions.get(intent.actor_id)??null});
  while(queue.length){
    queue.sort((a,b)=>a.time-b.time || a.priority-b.priority || a.actor.localeCompare(b.actor) || a.index-b.index);
    const task=queue.shift();
    const {time,priority,actor,intent,index}=task;
    if(priority===ARRIVAL){
      const shelter=shelters.get(intent.shelter_id);
      positions.set(actor,shelter.node);
      moving.delete(actor);
      if(time<shelter.opens_at_s) reject(time,intent,'shelter_not_open');
      else if(occupied.get(shelter.id)>=shelter.capacity) reject(time,intent,'shelter_full');
      else {
        occupied.set(shelter.id,occupied.get(shelter.id)+1);
        evacuated.add(actor);
        events.push({time_s:time,actor_id:actor,kind:'arrived',reason:'admitted',node:shelter.node});
        arrivals.push(time);
      }
      continue;
    }
    if(!positions.has(actor)) { reject(time,intent,'unknown_actor');continue; }
    if(intent.departure_s>scenario.deadline_s){ reject(time,intent,'departure_after_deadline');continue; }
    if(evacuated.has(actor)){ reject(time,intent,'already_evacuated');continue; }
    if(moving.has(actor)){ reject(time,intent,'actor_busy');continue; }
    if(!shelters.has(intent.shelter_id)){ reject(time,intent,'unknown_shelter');continue; }
    let node=positions.get(actor),clock=time,error=null;
    for(const id of intent.route){
      const road=roads.get(id);
      if(!road){error='unknown_road';break;}
      if(road.source!==node){error='disconnected_route';break;}
      const end=clock+road.travel_time_s;
      if((road.closures??[]).some(z=>clock<z.end_s && z.start_s<end)){
        error='route_blocked';break;
      }
      node=road.target;clock=end;
    }
    if(!error && node!==shelters.get(intent.shelter_id).node) error='wrong_destination';
    if(!error && clock>scenario.deadline_s) error='deadline_exceeded';
    if(error){reject(time,intent,error);continue;}
    moving.add(actor);
    events.push({time_s:time,actor_id:actor,kind:'departed',reason:'route_validated',node:positions.get(actor)});
    queue.push({time:clock,priority:ARRIVAL,actor,index,intent});
  }
  const notEvacuated=[...positions.keys()].filter(id=>!evacuated.has(id)).sort();
  const reasons={};
  for(const e of events) if(e.kind==='rejected') reasons[e.reason]=(reasons[e.reason]??0)+1;
  return {
    schema_version:'evacuation-result/v1',engine_version:'static-network-v1-js-reference',
    simulation_only:true,scenario_id:scenario.id,plan_id:plan.id,
    status:notEvacuated.length>0||Object.values(reasons).some(Boolean)?'FAIL':'PASS',
    evacuated:[...evacuated].sort(),not_evacuated:notEvacuated,
    final_locations:[...positions.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([actor_id,node])=>({actor_id,node})),
    events,stats:{admitted:evacuated.size,mean_arrival_s:arrivals.length?arrivals.reduce((a,b)=>a+b,0)/arrivals.length:null,
      last_arrival_s:arrivals.length?Math.max(...arrivals):null,rejections:reasons,
      occupancy:Object.fromEntries(occupied)},
  };
}

function edgeNoise(id,variant){
  let hash=2166136261;
  for(let i=0;i<id.length;i++){hash^=id.charCodeAt(i);hash=Math.imul(hash,16777619);}
  hash^=Math.imul(variant+31,2654435761);
  return (hash>>>0)/4294967295;
}

/** Small-graph Dijkstra with deterministic variant-specific edge weights. */
export function shortestPath(scenario,start,target,{allowClosed=false,riskByRoad={},riskWeight=0,riskTolerance=1,variant=0}={}){
  const dist=new Map([[start,0]]),prev=new Map(),seen=new Set(),outgoing=new Map();
  for(const road of scenario.roads){
    if(!allowClosed && (road.closures??[]).some(c=>c.start_s===0&&c.end_s>=scenario.deadline_s))continue;
    if(!outgoing.has(road.source))outgoing.set(road.source,[]);
    outgoing.get(road.source).push(road);
  }
  while(true){
    let at=null,best=Infinity;
    for(const [n,d] of dist){if(!seen.has(n)&&d<best){at=n;best=d;}}
    if(at===null)break;
    if(at===target)break;
    seen.add(at);
    for(const road of outgoing.get(at)??[]){
      const fuzz=variant===0?0:edgeNoise(road.id,variant)*variant*2.7;
      const cost=road.travel_time_s*(1+Math.max(0,riskWeight)*Math.max(0,riskByRoad[road.id]??0)*riskTolerance*0.55)+fuzz;
      if(best+cost<(dist.get(road.target)??Infinity)){
        dist.set(road.target,best+cost);prev.set(road.target,{node:at,road:road.id});
      }
    }
  }
  if(!dist.has(target))return null;
  const route=[];
  let at=target;
  while(at!==start){const edge=prev.get(at);if(!edge)return null;route.unshift(edge.road);at=edge.node;}
  return route;
}

function travelTime(route,roadMap){return route.reduce((sum,id)=>sum+roadMap.get(id).travel_time_s,0);}

export function getCandidateRoutes(lab){
  const {scenario,profiles,riskByRoad,settings}=lab;
  const roadMap=new Map(scenario.roads.map(r=>[r.id,r]));
  return scenario.personas.map(persona=>{
    const options=[],uniq=new Set();
    for(const shelter of scenario.shelters){
      for(let v=0;v<5;v++){
        const route=shortestPath(scenario,persona.start_node,shelter.node,{riskByRoad,riskWeight:settings.riskWeight,
          riskTolerance:profiles[persona.id].riskTolerance,variant:v});
        if(!route?.length)continue;
        const hash=shelter.id+':'+route.join('/');
        if(uniq.has(hash))continue;
        uniq.add(hash);
        const risk=route.reduce((a,id)=>a+(riskByRoad[id]??0),0);
        options.push({shelter_id:shelter.id,route,travel_s:travelTime(route,roadMap),risk});
      }
    }
    return options;
  });
}

/** Contrasting naive shortest-path assignment: ignores closures and capacity. */
export function makeGreedyPlan(lab){
  const {scenario,profiles}=lab;
  const roadMap=new Map(scenario.roads.map(r=>[r.id,r]));
  const intents=[];
  for(const persona of scenario.personas){
    let best=null;
    for(const shelter of scenario.shelters){
      const route=shortestPath(scenario,persona.start_node,shelter.node,{allowClosed:true});
      if(!route?.length)continue;
      const time=travelTime(route,roadMap);
      if(!best || time<best.time){best={shelter_id:shelter.id,route,time};}
    }
    if(best)intents.push({action:'move',actor_id:persona.id,shelter_id:best.shelter_id,
      departure_s:profiles[persona.id].reaction_s,route:best.route});
  }
  return {schema_version:'evacuation-plan/v1',id:'baseline_plan',intents};
}

export function planFromGenes(lab,candidates,genes){
  const intents=[];
  for(let i=0;i<lab.scenario.personas.length;i++){
    const p=lab.scenario.personas[i], option=candidates[i]?.[genes[i]];
    if(!option)continue;
    intents.push({action:'move',actor_id:p.id,shelter_id:option.shelter_id,
      departure_s:lab.profiles[p.id].reaction_s,route:[...option.route]});
  }
  return {schema_version:'evacuation-plan/v1',id:'ga_plan',intents};
}

function fitness(lab, candidates, genes){
  const plan=planFromGenes(lab,candidates,genes);
  const result=simulate(lab.scenario,plan);
  const routedRisk=genes.reduce((a,g,i)=>a+(candidates[i]?.[g]?.risk??0),0);
  const admitted=result.stats.admitted;
  // Primary: admitted people. Secondary: arrivals, uncertainty exposure,
  // and unnecessary nonarrival, without overriding hard simulation results.
  const score=admitted*10000 - (lab.scenario.personas.length-admitted)*1900 -
    (result.stats.mean_arrival_s??lab.scenario.deadline_s)*3 - routedRisk*lab.settings.riskWeight*8 -
    Object.values(result.stats.rejections).reduce((a,b)=>a+b,0)*90;
  return {genes,plan,result,score};
}

/** Genetic optimisation of the vector of (route,shelter) choices for all people. */
export async function makeGeneticPlan(lab,{onProgress,signal,yieldToUi}={}){
  const {settings}=lab;
  const candidates=getCandidateRoutes(lab);
  const rand=makeRandom(settings.seed ^ 0x6d2b79f5);
  const size=settings.population;
  const sample=()=>candidates.map(options=>options.length?Math.floor(rand()*options.length):-1);
  let generation=Array.from({length:size},(_,i)=>{
    if(i===0)return candidates.map(()=>0); // deterministic good initial baseline
    if(i===1)return candidates.map(options=>options.length?options.length-1:-1);
    return sample();
  });
  const history=[];
  let best=null;
  for(let gen=0;gen<settings.generations;gen++){
    if(signal?.aborted)throw new DOMException('Aborted','AbortError');
    const ranked=generation.map(genes=>fitness(lab,candidates,genes)).sort((a,b)=>b.score-a.score);
    if(!best || ranked[0].score>best.score)best=ranked[0];
    history.push(best.score);
    onProgress?.({generation:gen+1,total:settings.generations,bestCount:best.result.stats.admitted,score:best.score,history});
    if(gen===settings.generations-1)break;
    const next=[...ranked.slice(0,Math.min(3,ranked.length)).map(item=>[...item.genes])];
    const parent=()=>{
      let chosen=null;
      for(let j=0;j<3;j++){const candidate=ranked[Math.floor(rand()*Math.min(size,ranked.length))];
        if(!chosen||candidate.score>chosen.score)chosen=candidate;}
      return chosen.genes;
    };
    while(next.length<size){
      const a=parent(),b=parent();
      const child=candidates.map((options,i)=>{
        if(!options.length)return -1;
        if(rand()<settings.mutation)return Math.floor(rand()*options.length);
        return rand()<0.5?a[i]:b[i];
      });
      next.push(child);
    }
    generation=next;
    if(gen%5===4 && yieldToUi)await yieldToUi();
  }
  return {plan:best.plan,result:best.result,history,generations:settings.generations,candidates};
}

/** Pure checks for the UI's local test panel; not a substitute for Python pytest. */
export async function runSelfTests(){
  const cases=[];
  const test=(name,passed)=>cases.push({name,passed:!!passed});
  const road={id:'r_ab',source:'A',target:'B',travel_time_s:10,closures:[]};
  const makeSimple=(n=1,capacity=2,deadline=20,closures=[])=>({
    schema_version:'evacuation-scenario/v1',id:'check',dataset_version:'fixtures',synthetic:true,
    deadline_s:deadline,nodes:['A','B'],roads:[{...road,closures}],
    shelters:[{id:'s1',node:'B',capacity,opens_at_s:0}],
    personas:Array.from({length:n},(_,i)=>({id:`p${i}`,start_node:'A'})),
  });
  const planFor=(n,departure=0)=>({schema_version:'evacuation-plan/v1',id:'checkplan',
    intents:Array.from({length:n},(_,i)=>({action:'move',actor_id:`p${i}`,shelter_id:'s1',departure_s:departure,route:['r_ab']}))});
  test('通行可能な経路はPASS',simulate(makeSimple(),planFor(1)).status==='PASS');
  const blocked=simulate(makeSimple(1,2,20,[{start_s:0,end_s:20}]),planFor(1));
  test('閉鎖道路は移動開始前に拒否',blocked.events.some(e=>e.reason==='route_blocked'));
  const capacity=simulate(makeSimple(2,1),planFor(2));
  test('避難所の定員超過を検知',capacity.evacuated.length===1 && capacity.stats.rejections.shelter_full===1);
  test('期限と同時刻の到着は許可',simulate(makeSimple(1,2,10),planFor(1)).status==='PASS');
  test('閉鎖終了時刻から通行可',simulate(makeSimple(1,2,20,[{start_s:0,end_s:5}]),planFor(1,5)).status==='PASS');
  const conf=settingsFrom({seed:11,people:8,closurePercent:12,population:12,generations:8});
  const a=makeScenario(conf),b=makeScenario(conf);
  test('同じシードの合成マップを再現',JSON.stringify(a)===JSON.stringify(b));
  const evolved=await makeGeneticPlan(a);
  test('GAの結果を独立に再検証',JSON.stringify(simulate(a.scenario,evolved.plan))===JSON.stringify(evolved.result));
  const evolvedAgain=await makeGeneticPlan(a);
  test('GAの乱数シードで結果を再現',JSON.stringify(evolved.plan)===JSON.stringify(evolvedAgain.plan));
  test('GAの出力がMoveIntent契約に沿う',evolved.plan.intents.every(i=>i.action==='move'&&Array.isArray(i.route)&&i.route.length>0));
  return cases;
}
