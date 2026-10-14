import test from 'node:test';
import assert from 'node:assert/strict';
import {
  settingsFrom, makeScenario, makeGreedyPlan, makeGeneticPlan,
  getCandidateRoutes, shortestPath, simulate, runSelfTests,
} from '../apps/web/static/evac-lab-model.mjs';

const base = settingsFrom({seed:2026,people:20,closurePercent:17,shelterCapacity:12,deadline:140,
  population:24,generations:20,mutation:.15});

// These tests run entirely offline and incur no Google Cloud or model charges.
test('local validation cases all pass',async()=>{
  const results=await runSelfTests();
  assert.ok(results.length>=9);
  assert.deepEqual(results.filter(x=>!x.passed),[]);
});

test('settings are bounded and reject non-finite tunables',()=>{
  const a=settingsFrom({people:200,closurePercent:-20,mutation:Infinity,seed:NaN,population:1,
    strategy:'magic',disaster:'magic'});
  assert.equal(a.people,48); assert.equal(a.closurePercent,0);
  assert.equal(a.mutation,.13); assert.equal(a.seed,2026);
  assert.equal(a.population,8); assert.equal(a.strategy,'ga');
  assert.equal(a.disaster,'flood');
});

test('random scenario is reproducible and uses valid Core contract fields',()=>{
  const a=makeScenario(base),b=makeScenario(base);
  assert.deepEqual(a,b);
  assert.equal(a.scenario.personas.length,base.people);
  assert.equal(a.scenario.shelters.length,2);
  assert.ok(a.scenario.roads.length>0);
  assert.deepEqual(Object.keys(a.scenario).sort(),[
    'dataset_version','deadline_s','id','nodes','personas','roads',
    'schema_version','shelters','synthetic',
  ].sort());
  assert.deepEqual(Object.keys(a.scenario.personas[0]).sort(),['id','start_node']);
  assert.deepEqual(Object.keys(a.scenario.roads[0]).sort(),[
    'closures','id','source','target','travel_time_s',
  ]);
});

test('baseline has valid action intents but may be blocked',()=>{
  const lab=makeScenario(base);
  const plan=makeGreedyPlan(lab),result=simulate(lab.scenario,plan);
  assert.equal(plan.intents.length,lab.scenario.personas.length);
  assert.equal(result.evacuated.length+result.not_evacuated.length,lab.scenario.personas.length);
  assert.equal(result.simulation_only,true);
});

test('Dijkstra route consists of contiguous directed roads',()=>{
  const lab=makeScenario({...base,closurePercent:20});
  const roadMap=new Map(lab.scenario.roads.map(r=>[r.id,r]));
  const p=lab.scenario.personas[0],s=lab.scenario.shelters[0];
  const path=shortestPath(lab.scenario,p.start_node,s.node,{allowClosed:true});
  assert.ok(path.length>0);
  let node=p.start_node;
  for(const id of path){const road=roadMap.get(id);assert.equal(road.source,node);node=road.target;}
  assert.equal(node,s.node);
});

test('candidate routes avoid incident-time closed segments',()=>{
  const lab=makeScenario(base);
  const roads=new Map(lab.scenario.roads.map(r=>[r.id,r]));
  const candidates=getCandidateRoutes(lab);
  assert.equal(candidates.length,base.people);
  for(const group of candidates)for(const option of group){
    for(const id of option.route)assert.equal(roads.get(id).closures.length,0);
  }
});

test('GA result is independent of its own claimed outcome',async()=>{
  const lab=makeScenario(base);
  const ga=await makeGeneticPlan(lab);
  assert.deepEqual(ga.result,simulate(lab.scenario,ga.plan));
  assert.equal(ga.history.length,base.generations);
});

test('GA is reproducible and does not change the scenario',async()=>{
  const lab=makeScenario(base),snapshot=JSON.stringify(lab.scenario);
  const a=await makeGeneticPlan(lab),b=await makeGeneticPlan(lab);
  assert.deepEqual(a.plan,b.plan);
  assert.deepEqual(a.history,b.history);
  assert.equal(JSON.stringify(lab.scenario),snapshot);
});

test('GA handles an impassable map without false PASS',async()=>{
  const lab=makeScenario({...base,people:6,shelterCapacity:6,generations:8,population:10});
  lab.scenario.roads.forEach(road=>{road.closures=[{start_s:0,end_s:lab.scenario.deadline_s}];});
  const ga=await makeGeneticPlan(lab);
  assert.equal(ga.result.status,'FAIL');
  assert.equal(ga.result.evacuated.length,0);
  assert.equal(ga.plan.intents.length,0);
});

test('GA never creates capacity in excess of scenario constraints',async()=>{
  const lab=makeScenario({...base,people:25,shelterCapacity:2,closurePercent:0,generations:12,population:18});
  const ga=await makeGeneticPlan(lab);
  assert.ok(ga.result.evacuated.length<=4);
  assert.equal(ga.result.status,'FAIL');
  for(const shelter of lab.scenario.shelters){
    assert.ok(ga.result.stats.occupancy[shelter.id]<=shelter.capacity);
  }
});

test('GA does not regress against naive shortest route on example seed',async()=>{
  const lab=makeScenario(base);
  const ga=await makeGeneticPlan(lab);
  const greedy=simulate(lab.scenario,makeGreedyPlan(lab));
  assert.ok(ga.result.evacuated.length>=greedy.evacuated.length);
});

test('abort prevents further GA computation',async()=>{
  const lab=makeScenario({...base,generations:30});
  const controller=new AbortController();
  controller.abort();
  await assert.rejects(makeGeneticPlan(lab,{signal:controller.signal}),{name:'AbortError'});
});
