import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
const text=await readFile(new URL('../../yapnr/agent/web/dock-model.js',import.meta.url),'utf8');
const {initialLayout,transact,leaves,location,validate}=await import('data:text/javascript;base64,'+Buffer.from(text).toString('base64'));
test('transactions preserve inputs and ordered group membership',()=>{
 const old=initialLayout(),snapshot=structuredClone(old);
 const moved=transact(old,{type:'move',pane:'board',target:'source'});
 assert.deepEqual(old,snapshot);assert.deepEqual(moved.panes.source.tabs,['source','board','three']);assert.equal(moved.panes.source.active,'board');assert.equal(leaves(moved.tree).length,2);
});
for(const edge of ['left','right','top','bottom'])test('split '+edge+' is one atomic view move',()=>{
 const l=transact(initialLayout(),{type:'move',tab:'three',target:'schematic',edge});
 assert.equal(leaves(l.tree).length,4);assert.equal(l.panes[location(l,'three')].active,'three');assert.equal(l.panes.board.active,'board');
});
test('reorder and cross-drawer move retain tabs and active view',()=>{
 let l=transact(initialLayout(),{type:'move',tab:'controls',target:'left',index:0});
 assert.deepEqual(l.panes.left.tabs,['controls','source-browser','experiments','performance','exploration']);l=transact(l,{type:'move',tab:'controls',target:'right',index:1});
 assert.equal(l.panes.right.tabs[1],'controls');assert.equal(l.panes.right.active,'controls');assert.equal(l.drawers.right.open,true);
 assert.deepEqual(validate(JSON.parse(JSON.stringify(l))),l);
});
test('minimize and restore recover original slot and active view',()=>{
 const old=initialLayout(),min=transact(old,{type:'minimize',pane:'schematic'}),restored=transact(min,{type:'restore',pane:'schematic'});
 assert.equal(leaves(min.tree).length,2);assert.deepEqual(restored.tree,old.tree);assert.deepEqual(restored.panes,old.panes);
});
test('close last central tab retains an empty drop destination',()=>{
 let l=initialLayout();for(const t of ['board','three','schematic','source'])l=transact(l,{type:'close',tab:t});
 assert.equal(leaves(l.tree).length,1);assert.deepEqual(l.panes[leaves(l.tree)[0]].tabs,[]);
 assert.equal(transact(l,{type:'open',tab:'board'}).panes[leaves(l.tree)[0]].active,'board');
});
test('repeated presets do not lose inspect state or extra tabs',()=>{
 const old=initialLayout();let l=transact(old,{type:'preset',name:'conversation'});l=transact(l,{type:'preset',name:'conversation'});
 l=transact(l,{type:'open',tab:'scratchpad'});l=transact(l,{type:'preset',name:'inspect'});
 assert.deepEqual(l.tree,old.tree);assert.equal(location(l,'ask'),'right');assert.ok(location(l,'scratchpad'));assert.equal(l.panes.right.active,'ask');
});
test('mobile projection never mutates desktop splits',()=>{
 const old=initialLayout(),next=transact(old,{type:'mobile',pane:'source'});assert.deepEqual(next.tree,old.tree);assert.equal(next.mobilePane,'source');
});
test('invalid targets and duplicate panes reject without mutation',()=>{
 const old=initialLayout();assert.throws(()=>transact(old,{type:'move',tab:'board',target:'unknown'}));assert.deepEqual(old,initialLayout());
 const bad=initialLayout();bad.tree.second={pane:'board'};assert.throws(()=>validate(bad));
});

test('opening an existing document updates the narrow-screen projection',()=>{
 const next=transact(initialLayout(),{type:'open',tab:'source'});assert.equal(next.mobilePane,'source');assert.equal(next.panes.source.active,'source');
});
