import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';
const app=readFileSync(new URL('../../yapnr/viewer/static/app.js',import.meta.url),'utf8');
const start=app.indexOf(" $('air').disabled=");
const end=app.indexOf(' let search=',start);
function draw(geometry,checked=true){
 const control={checked,parentElement:{}},lines=[],circles=[];
 vm.runInNewContext(app.slice(start,end),{g:geometry,$:()=>control,Math,
  line:(...args)=>lines.push(args),circle:(...args)=>circles.push(args)});
 return {control,lines,circles};
}
test('connected net pads never create airwires',()=>{
 const result=draw({parts:[{pads:[{net:'GND',xy:[0,0]},{net:'GND',xy:[10,0]}]}],airwires:[]});
 assert.equal(result.lines.length,0);assert.equal(result.control.disabled,false);
});
test('draw only supplied missing links and mark coincident disconnected anchors',()=>{
 const result=draw({airwires:[{a:[1,2],b:[3,4]},{a:[5,5],b:[5,5]}]});
 assert.equal(result.lines.length,2);assert.equal(result.circles.length,1);
 assert.equal(draw({airwires:[{a:[0,0],b:[1,1]}]},false).lines.length,0);
});
test('legacy or graph-only geometry reports unavailable instead of fabricating wires',()=>{
 const result=draw({parts:[{pads:[{net:'GND',xy:[0,0]},{net:'GND',xy:[10,0]}]}]});
 assert.equal(result.lines.length,0);assert.equal(result.control.disabled,true);
 assert.match(result.control.parentElement.title,/unavailable/);
});
