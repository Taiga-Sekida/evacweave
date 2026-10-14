import test from 'node:test';
import assert from 'node:assert/strict';
import {DEFAULTS} from '../apps/web/static/evac-lab-model.mjs';

class NoopContext {
  beginPath(){} moveTo(){} lineTo(){} arc(){} roundRect(){} closePath(){}
  stroke(){} fill(){} fillRect(){} clearRect(){} fillText(){} save(){} restore(){}
  setLineDash(){} ellipse(){}
  createLinearGradient(){return {addColorStop(){}};}
  createRadialGradient(){return {addColorStop(){}};}
}
class ElementStub {
  constructor(id=''){
    this.id=id;this.style={};this.children=[];this.listeners={};this.textContent='';
    // Each control starts at the model's default, as the page's own inputs do.
    this.value=String(DEFAULTS[id]??'');
    this.width=id==='fitnessCanvas'?520:1000;
    this.height=id==='fitnessCanvas'?105:600;
  }
  addEventListener(name,handler){this.listeners[name]=handler;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(name,value){this[name]=value;}
  getContext(){return new NoopContext();}
  getBoundingClientRect(){return {left:0,top:0,width:1000,height:600};}
}
const dom=new Map();
const get=id=>{if(!dom.has(id))dom.set(id,new ElementStub(id));return dom.get(id);};
globalThis.document={getElementById:get,createElement:()=>new ElementStub()};
// UI only: run planner yields on RAF, but do not create a never-ending animation loop.
globalThis.requestAnimationFrame=fn=>{
  if(fn.name==='animate')return 1;
  setImmediate(()=>fn(0));return 1;
};

await import('../apps/web/static/evac-lab-app.mjs');
const ready=async()=>{
  for(let i=0;i<200;i++){
    if(get('runBtn').disabled===false&&['PASS','FAIL'].includes(get('verdictValue').textContent))return;
    await new Promise(resolve=>setTimeout(resolve,5));
  }
  throw new Error('UI planner did not finish');
};

test('UI boots, computes and shows decisions without browser network calls',async()=>{
  await ready();
  assert.ok(['PASS','FAIL'].includes(get('verdictValue').textContent));
  assert.match(get('evacCount').textContent,/\/ 20$/);
  assert.ok(get('jsonPreview').textContent.includes('evacuation-plan/v1'));
});

test('UI switches strategy and recomputes with changed residents',async()=>{
  await ready();
  get('people').value='13';get('strategy').value='greedy';
  await get('runBtn').listeners.click();
  await ready();
  assert.match(get('evacCount').textContent,/\/ 13$/);
  assert.ok(get('jsonPreview').textContent.includes('baseline_plan'));
});

test('user can run in-browser acceptance tests',async()=>{
  await ready();
  await get('testBtn').listeners.click();
  assert.equal(get('testSummary').textContent,'9/9 PASS');
  assert.equal(get('testList').children.length,9);
});
