import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';
const app=readFileSync(new URL('../../yapnr/viewer/static/app.js',import.meta.url),'utf8');
const source=app.slice(app.indexOf('const colors='),app.indexOf('const display='));
const six=['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu'];
function setup(){
 const node=tag=>({tag,children:[],style:{},append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items}});
 const panel=node('div'),c=vm.createContext({document:{createElement:node},$:()=>panel,render:()=>{}});
 vm.runInContext(source,c);return {c,panel,run:code=>vm.runInContext(code,c)};
}
test('empty native layers appear in physical order, name then checkbox; toggles survive lane changes',()=>{
 const {c,panel,run}=setup();c.g={copper_layers:six,tracks:[['net','In4.Cu']]};run('syncLayers(g)');
 assert.deepEqual(panel.children.map(r=>r.children[0].textContent),six);
 assert.ok(panel.children.every(r=>r.children[1].checked));
 const checkbox=panel.children[4].children[1];checkbox.checked=false;checkbox.onchange();
 assert.equal(run("layers.has('In4.Cu')"),false);
 c.g={copper_layers:['F.Cu','B.Cu']};run('syncLayers(g)');assert.equal(panel.children.length,2);
 c.g={copper_layers:six};run('syncLayers(g)');assert.equal(panel.children[4].children[1].checked,false);
 run("syncLayers(g,['In3.Cu'])");assert.deepEqual([...run('[...layers]')],['In3.Cu']);
 assert.equal(run("viaVisible({layers:['F.Cu','In1.Cu']})"),false);
 assert.equal(run("viaVisible({layers:['In3.Cu','In4.Cu']})"),true);
 run('syncLayers(null)');assert.equal(panel.children.length,0);
});
test('3D uses the declared stack even when inner layers carry no tracks or zones',()=>{
 const {c,run}=setup();const three=readFileSync(new URL('../../yapnr/viewer/static/viewer3d.js',import.meta.url),'utf8');
 vm.runInContext(three.slice(three.indexOf('function stackOf('),three.indexOf('function z0Of(')),c);
 c.g={copper_layers:six,tracks:[],zones:[]};assert.deepEqual([...run('stackOf(g)')],six);
 c.g={parts:[{pads:[{layers:['In30.Cu']}]}],tracks:[['n','In4.Cu']],zones:[{layer:'In2.Cu'}]};
 assert.deepEqual([...run('copperStack(g)')],['F.Cu','In2.Cu','In4.Cu','B.Cu']);
});
