/** Produce a fixed browser-export-shaped scenario for the Python Core parity test. */
import {writeFileSync} from 'node:fs';
import {makeScenario,settingsFrom,makeGeneticPlan} from '../apps/web/static/evac-lab-model.mjs';
const file=process.argv[2];
if(!file)throw new Error('Usage: node tests_js/export-lab-fixture.mjs <output.json>');
const settings=settingsFrom({seed:2026,people:18,closurePercent:18,shelterCapacity:11,
  deadline:130,population:32,generations:30,mutation:0.14});
const lab=makeScenario(settings);
const {plan,result}=await makeGeneticPlan(lab);
writeFileSync(file,JSON.stringify({settings,scenario:lab.scenario,plan,result},null,2));
console.log(`Exported ${lab.scenario.personas.length} simulated people; JS: ${result.status}, ${result.evacuated.length} accepted.`);
