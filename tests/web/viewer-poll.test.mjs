import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const app=readFileSync(new URL('../../yapnr/viewer/static/app.js',import.meta.url),'utf8');
const pollSource=app.slice(app.indexOf('async function poll(){'),app.indexOf('poll();',app.indexOf('async function poll(){')));
test('changing lanes during a pending poll cannot strand the new lane without geometry',async()=>{
 let resolve;const timers=[];
 const context=vm.createContext({laneId:'old',revision:12,state:{run:'run',revision:12},
  api:()=>new Promise(r=>{resolve=r}),setTimeout:(fn,ms)=>timers.push(ms)});
 vm.runInContext(pollSource,context);
 const pending=vm.runInContext('poll()',context);
 context.laneId='new';context.revision=-1;
 resolve({revision:12,lanes:{old:{geometry:{parts:[]}},new:{}}});
 await pending;
 assert.equal(context.state.run,'run');
 assert.equal(context.revision,-1);
 assert.deepEqual(timers,[0]);
});
