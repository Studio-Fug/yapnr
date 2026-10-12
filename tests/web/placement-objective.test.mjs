import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const source=readFileSync(new URL('../../yapnr/viewer/static/placement-lab.js',import.meta.url),'utf8');
const controls=source.slice(source.indexOf('function objectiveDirty()'),source.indexOf("$('optimizer-step').onclick"));
const initial='wirelength + source_constraints',edited='8*wirelength + source_constraints';
function harness({draft=edited,conflict=false,invalid=false,lag=0}={}){
 const elements=new Map(),calls=[];
 const $=id=>{if(!elements.has(id))elements.set(id,{value:'',disabled:false});return elements.get(id);};
 $('expression').value=draft;
 const run={id:'trial',control:{revision:4,expression:initial},status:{state:'paused',step:3,iterations:20,control_revision:4,expression:initial},request:{expression:initial},frame:{step:3}};
 let acknowledged=0;
 const context=vm.createContext({$,current:'trial',run,objectiveBase:initial,stepping:false,catalog:null,surface:null,Date,Promise,encodeURIComponent,
  setTimeout:fn=>{fn();},render:()=>{},load:async()=>{},selectRun:async()=>{},error:e=>{$('command').textContent=e.message;},
  api:async(path,body)=>{
   if(body){calls.push(body);if(body.action==='apply'){
    if(invalid)throw Error('Invalid expression');
    return {control:{revision:5}};
   }return {};}
   acknowledged++;
   return {...run,control:{revision:conflict?6:5,expression:edited},status:{...run.status,control_revision:acknowledged>lag?5:4,expression:acknowledged>lag?edited:initial}};
  }});
 vm.runInContext(controls,context);
 return {context,$,calls,run,execute:()=>vm.runInContext("command('step')",context)};
}
test('edited Step waits for acknowledgment then steps with its revision guard',async()=>{
 const h=harness({lag:2});await h.execute();
 assert.deepEqual(h.calls.map(c=>c.action),['apply','step']);
 assert.equal(h.calls[0].expression,edited);
 assert.equal(h.calls[0].expect_revision,4);assert.equal(h.calls[1].expect_revision,5);
 assert.equal(h.context.run.status.step,3); // Apply did not move/reset the placement.
});
test('unchanged Step sends only one step',async()=>{
 const h=harness({draft:initial});await h.execute();
 assert.deepEqual(h.calls.map(c=>c.action),['step']);assert.equal(h.calls[0].expect_revision,4);
});
test('invalid edit or concurrent control change never advances the optimizer',async()=>{
 for(const options of [{invalid:true},{conflict:true}]){
  const h=harness(options);await h.execute();
  assert.deepEqual(h.calls.map(c=>c.action),['apply']);assert.equal(h.context.stepping,false);
  assert.match(h.$('command').textContent,/Invalid|changed elsewhere/);
 }
});
test('Apply alone acknowledges without stepping or resuming',async()=>{
 const h=harness();await vm.runInContext("command('apply')",h.context);
 assert.deepEqual(h.calls.map(c=>c.action),['apply']);
 assert.match(h.$('command').textContent,/applied.*revision 5.*iteration 3/);
});
test('polling preserves a draft and labels Apply & step; clean editors follow remote changes',()=>{
 const h=harness();vm.runInContext('syncObjective()',h.context);
 assert.equal(h.$('expression').value,edited);assert.equal(h.$('optimizer-step').textContent,'Apply & step');
 h.$('expression').value=initial;h.run.control.expression=edited;
 vm.runInContext('syncObjective()',h.context);
 assert.equal(h.$('expression').value,edited);assert.equal(h.$('optimizer-step').textContent,'Step');
});
test('objective changes invalidate the heatmap without changing the placement step',()=>{
 const h=harness();h.context.surface={frame_step:3,objective_expression:initial};
 assert.equal(vm.runInContext('surfaceIsCurrent()',h.context),true);
 h.run.status.expression=edited;
 assert.equal(vm.runInContext('surfaceIsCurrent()',h.context),false);
});
