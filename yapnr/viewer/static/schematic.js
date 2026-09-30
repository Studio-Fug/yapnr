'use strict';
// Schematic view for the live PnR viewer.
// Extends app.js / cost-inspector.js the same way cost-inspector.js extends app.js:
// shared top-level bindings (render, fit, jumpToComponent, selectedPartRef, laneId,
// lane(), geo(), view, ctx, canvas, screen) and one render wrapper. The PCB drawing
// itself is untouched; highlights are drawn on top only when the user asks for one.
//
// Model: GET /api/schematic?lane= -> {status,key,overlay}; the payload at
// /api/schematic/payload/<key> is content addressed, so every trial of one block
// template shares one payload and one layout (cached here by key).
// Layout: ELK layered (elkjs, fetched pinned at build time into the dist, lazy loaded), mechanical from the
// netlist: symbol pins are FIXED_POS ports, wired nets are star edges from the pin of
// the part with most pins, rails / ground / labels are drawn in place at the pin.
// ELK runs in 0.01 mm units and wire ends are snapped onto the pins (see SCH_ELK_SCALE).

const SCH_NS='http://www.w3.org/2000/svg';
const SCH_FS={ref:1.8,val:1.2,inst:1,pin:1.1,num:.8,net:1.25},SCH_CW=.62;
const SCH_TIER={1:'#ff8a4c',2:'#4dabf7',3:'#69db7c'},SCH_HOT='#ff5b3a',SCH_PATH='#ffb347';
const SCH_PALETTE=['#4dabf7','#f783ac','#69db7c','#ffa94d','#b197fc','#3bc9db','#ffd43b','#ff8787','#a9e34b','#e599f7','#63e6be','#ffc078','#74c0fc','#f06595'];
const SCH_LAYERED={'elk.algorithm':'layered','elk.direction':'RIGHT','elk.edgeRouting':'ORTHOGONAL','elk.layered.mergeEdges':'true',
 'elk.spacing.nodeNode':'2.5','elk.layered.spacing.nodeNodeBetweenLayers':'5','elk.spacing.edgeEdge':'1','elk.spacing.edgeNode':'1.2',
 'elk.layered.spacing.edgeNodeBetweenLayers':'1.5','elk.spacing.componentComponent':'5','elk.separateConnectedComponents':'true',
 'elk.aspectRatio':'1.6','elk.layered.considerModelOrder.strategy':'NODES_AND_EDGES','elk.randomSeed':'1',
 'elk.layered.nodePlacement.strategy':'NETWORK_SIMPLEX','elk.layered.compaction.postCompaction.strategy':'EDGE_LENGTH',
 // Bounded layers keep the passives around a big IC from forming one tall column.
 'elk.layered.layering.strategy':'COFFMAN_GRAHAM','elk.layered.layering.coffmanGraham.layerBound':'10',
 // ELK defaults that are absolute lengths; spelled out so they scale with SCH_ELK_SCALE.
 'elk.layered.spacing.edgeEdgeBetweenLayers':'10','elk.spacing.portPort':'10','elk.spacing.nodeSelfLoop':'10','elk.padding':'[top=12,left=12,bottom=12,right=12]'};
// ELK's network-simplex node placement works in whole units and moves every port anchor onto
// an integer, while KiCad pins sit on a 2.54 mm grid: laid out in mm, wires missed their pins by
// up to 0.9 mm. ELK therefore runs in 0.01 mm units, and schSnapWire() puts every wire end
// exactly on its pin afterwards (the residue is <= 0.01 mm, the snap keeps wires orthogonal).
const SCH_ELK_SCALE=100;
const sch={mode:'pcb',lane:null,want:null,req:0,busy:false,errorAt:0,key:null,data:null,overlay:null,layout:null,built:null,
 scopePref:'auto',colorBy:null,colorByUser:false,focus:null,focusKey:null,net:null,hoverNet:null,selRef:undefined,pendingCenter:null,
 v:{x:0,y:0,k:4},fitKey:null,payloads:new Map(),layouts:new Map(),elkP:null,url:new URLSearchParams(location.search),urlLane:null,urlRef:null,urlHl:null};

// ------------------------------------------------------------------ DOM
(function schDom(){
 const boardwrap=$('boardwrap'),views=document.createElement('div');views.id='views';boardwrap.before(views);
 const wrap=document.createElement('div');wrap.id='schwrap';
 wrap.innerHTML=`<div id="sch-main"><svg id="sch-svg"><g id="sch-root"></g></svg>
<div id="sch-bar"><span class="seg" id="sch-scope" title="Scope of the drawing"><button data-s="auto">Block</button><button data-s="board">Board context</button></span><span class="seg" id="sch-color" title="What the colours mean"><button data-c="role">Power roles</button><button data-c="group">Subcircuits</button></span><button id="sch-fit" title="Fit the schematic (or the highlighted subcircuit)">Fit</button><button id="sch-legend-toggle" title="Show or hide the legend">Legend</button></div>
<div id="sch-hover"></div><div id="sch-status" class="muted"></div></div><div id="sch-legend"><div id="sch-legend-body"></div></div>`;
 views.append(boardwrap,wrap);
 const sw=document.createElement('span');sw.className='seg viewswitch';sw.id='viewswitch';
 sw.innerHTML='<button data-v="pcb" title="PCB only">PCB</button><button data-v="split" title="PCB and schematic side by side">Split</button><button data-v="sch" title="Schematic only">Schematic</button>';
 const chip=document.createElement('span');chip.id='sch-chip';chip.hidden=true;$('sch-scope').hidden=true;
 const tools=$('pause').parentElement;tools.prepend(sw,chip);tools.classList.add('tools');
})();
const schSvg=$('sch-svg'),schRoot=$('sch-root');
function schEl(tag,attrs,parent){let e=document.createElementNS(SCH_NS,tag);if(attrs)for(let k in attrs)if(attrs[k]!==undefined&&attrs[k]!==null)e.setAttribute(k,attrs[k]);if(parent)parent.appendChild(e);return e}
function schText(parent,x,y,text,cls,anchor,extra){let t=schEl('text',Object.assign({x:+x.toFixed(3),y:+y.toFixed(3),class:cls,'text-anchor':anchor||'start'},extra||{}),parent);t.textContent=text;return t}
function schStatus(msg){$('sch-status').textContent=msg||'';$('sch-status').style.display=msg?'block':'none'}
const schSleep=ms=>new Promise(r=>setTimeout(r,ms));
const schNatural=(a,b)=>String(a).localeCompare(String(b),undefined,{numeric:true});

// ------------------------------------------------------------------ view mode
function schSetMode(mode,save=true){
 // 3D pane (viewer3d.js): '3d' alone, '3d-pcb' beside the PCB, '3d-sch' beside the schematic. sch.mode keeps meaning "which schematic layout is shown".
 const m3=/^3d(-pcb|-sch)?$/.test(mode);if(!m3&&!['pcb','split','sch'].includes(mode))mode='pcb';sch.mode=m3?(mode==='3d-sch'?'sch':'pcb'):mode;document.body.dataset.view=mode;schLegendPref();sch.fitKey=null;
 for(let b of $('viewswitch').children)b.classList.toggle('active',b.dataset.v===(m3?'3d':mode));
 if(save)try{localStorage.setItem('pnr-view-mode',mode)}catch(e){}
 requestAnimationFrame(()=>{
  if(sch.mode!=='sch'&&mode!=='3d'){if(!fitted&&geo())fit();if(sch.pendingCenter){let r=sch.pendingCenter;sch.pendingCenter=null;jumpToComponent(r)}}
  render();if(sch.mode!=='pcb'){schEnsure();schFitIfNeeded()}
 });
 window.Yapnr3D?.show(m3?mode:null);
}
for(let b of $('viewswitch').children)b.onclick=()=>schSetMode(b.dataset.v);
for(let b of $('sch-scope').children)b.onclick=()=>{sch.scopePref=b.dataset.s;sch.focusKey=null;schEnsure(true)};
for(let b of $('sch-color').children)b.onclick=()=>{sch.colorBy=b.dataset.c;sch.colorByUser=true;schApplyClasses();schLegend()};
$('sch-fit').onclick=()=>schFit(true);
$('sch-legend-toggle').onclick=()=>{let key='pnr-sch-legend-'+(schSplitLike()?'split':'sch'),hide=!document.body.classList.contains('sch-nolegend');document.body.classList.toggle('sch-nolegend',hide);try{localStorage.setItem(key,hide?'0':'1')}catch(e){}requestAnimationFrame(()=>{sch.fitKey=null;schFitIfNeeded()})};
function schLegendPref(){let v=null;try{v=localStorage.getItem('pnr-sch-legend-'+(schSplitLike()?'split':'sch'))}catch(e){}if(sch.url.get('legend'))v=sch.url.get('legend');document.body.classList.toggle('sch-nolegend',v?v==='0':schSplitLike())}
// half-width schematic (Split, or beside the 3D view): the legend is an overlay, hidden by default
function schSplitLike(){return sch.mode==='split'||document.body.dataset.view==='3d-sch'}

// Guards for the hidden canvas (schematic-only mode): never fit or recentre a 0-width canvas.
const schFitBefore=fit;fit=function(){if(canvas.clientWidth<80||canvas.clientHeight<80)return;schFitBefore()};$('fit').onclick=()=>fit();
const schJumpBefore=jumpToComponent;
jumpToComponent=function(query){
 if(canvas.clientWidth>=80&&canvas.clientHeight>=80)return schJumpBefore(query);
 let ref=String(query??'').trim().toUpperCase(),g=geo(),part=g?.parts?.find(p=>p.ref.toUpperCase()===ref);
 if(!g){pendingComponentRef=ref;return false}
 if(!part){$('component-status').textContent=ref?schOutside(ref):'Enter a component reference.';return false}
 selectedPartRef=part.ref;$('component-query').value=part.ref;sch.pendingCenter=part.ref;$('component-status').textContent=part.ref+' selected · PCB view hidden';render();return true;
};
function schOutside(ref){let blk=sch.data?.scope?.kind==='block'||sch.overlay?.lane_refs;return blk&&sch.data?.components?.length?`${ref} is outside this block trial's board.`:`No component ${ref} in this checkpoint.`}

// ------------------------------------------------------------------ loading
function schLoadElk(){
 if(window.ELK)return Promise.resolve();if(sch.elkP)return sch.elkP;
 sch.elkP=new Promise((res,rej)=>{let s=document.createElement('script');s.src='elk.bundled.js';s.onload=()=>res();s.onerror=()=>{sch.elkP=null;rej(Error('layout engine (elk.bundled.js) failed to load: serve the assembled dist (bazel run //:viewer)'))};document.head.append(s)});
 return sch.elkP;
}
async function schEnsure(force){
 if(sch.mode==='pcb'||!laneId||!display()?.lanes?.[laneId])return;
 let l=lane(),want=[laneId,sch.scopePref,!!(l?.geometry||l?.board_sha256||l?.layout_sha256)].join('|');
 if(!force&&sch.want===want&&(sch.built||sch.busy||Date.now()-sch.errorAt<15000))return;
 sch.want=want;let token=++sch.req;sch.busy=true;
 try{
  let r;
  for(let i=0;;i++){
   r=await api('/api/schematic?lane='+encodeURIComponent(laneId)+(sch.scopePref==='board'?'&scope=board':''));if(token!==sch.req)return;
   if(r.status==='ready')break;
   if(r.status==='pending'||r.status==='busy'){schStatus(i?'Building schematic from the netlist…':'Preparing schematic…');await schSleep(600);if(token!==sch.req)return;continue}
   throw Error(r.error||r.reason||r.status);
  }
  let data=sch.payloads.get(r.key);
  if(!data){schStatus('Loading schematic…');data=await api(r.url);if(token!==sch.req)return;sch.payloads.set(r.key,data);if(sch.payloads.size>6)sch.payloads.delete(sch.payloads.keys().next().value)}
  if(!window.ELK){schStatus('Loading layout engine…');await schLoadElk();if(token!==sch.req)return}
  let lay=sch.layouts.get(r.key);
  if(!lay){schStatus('Laying out '+data.components.length+' parts…');let t0=performance.now();lay=await schLayout(data);lay.ms=performance.now()-t0;if(token!==sch.req)return;sch.layouts.set(r.key,lay);if(sch.layouts.size>6)sch.layouts.delete(sch.layouts.keys().next().value)}
  sch.overlay=r.overlay;sch.data=data;sch.layout=lay;sch.scopeKind=r.scope;
  if(sch.built?.key!==r.key)schBuild(r.key,data,lay);
  sch.key=r.key;schApplyOverlay();schStatus('');schFitIfNeeded();
 }catch(e){if(token===sch.req){sch.errorAt=Date.now();schStatus('Schematic unavailable: '+e.message)}}
 finally{if(token===sch.req)sch.busy=false}
}

// ------------------------------------------------------------------ model + layout
const schRot=(p,k)=>k===90?[-p[1],p[0]]:k===-90?[p[1],-p[0]]:k===180?[-p[0],-p[1]]:p;
const schOut={W:[-1,0],E:[1,0],N:[0,-1],S:[0,1]};
function schModel(data){
 const comp=new Map(data.components.map(c=>[c.ref,c])),netOf=new Map();
 for(let n of data.nets)for(let [r,p] of n.pins)netOf.set(r+'/'+p,n);
 const rep=new Map(),members=new Map();
 for(let a of data.arrays||[]){let rs=a.refs.filter(r=>comp.has(r));if(rs.length<2)continue;for(let r of rs)rep.set(r,rs[0]);members.set(rs[0],rs)}
 const R=r=>rep.get(r)||r;
 return {comp,netOf,rep,members,R,drawn:data.components.filter(c=>R(c.ref)===c.ref)};
}
function schTextBox(x,y,text,fs,anchor){ // rough extent of an SVG text (monospace-ish metrics)
 let w=String(text).length*fs*SCH_CW,x0=anchor==='end'?x-w:anchor==='middle'?x-w/2:x;return [x0,y-fs*.8,x0+w,y+fs*.25];
}
function schGeom(data,M,c){
 const sym=data.symbols[c.lib],u=sym.units[0];
 const pins=u.pins.map(p=>({...p,net:M.netOf.get(c.ref+'/'+p.number)||null}));
 let k=0;
 if(sch.url.get('rot')!=='0'&&pins.length===2&&pins.every(p=>p.side==='W'||p.side==='E')){
  let g=pins.find(p=>p.net?.kind==='ground'),r=pins.find(p=>p.net&&p.net.kind!=='ground'&&(p.net.draw==='rail'||p.net.draw==='label'));
  if(g)k=g.side==='W'?-90:90;else if(r)k=r.side==='W'?90:-90;
 }
 const P=pins.map(p=>{let at=schRot(p.at,k),tip=schRot(p.tip,k),d=[tip[0]-at[0],tip[1]-at[1]];
  let side=Math.abs(d[0])>=Math.abs(d[1])?(d[0]>0?'W':'E'):(d[1]>0?'N':'S');return {...p,at,tip,side}});
 let bx=u.bbox,a=schRot([bx[0],bx[1]],k),b=schRot([bx[2],bx[3]],k);
 const body=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])];
 const big=P.length>2,boxes=[body],texts=[],decos=[];
 const add=(bx)=>{boxes.push(bx);return bx};
 // pin names / numbers (IC-like parts only)
 if(big)for(let p of P){
  if(p.hidden)continue;let inw={W:[1,0],E:[-1,0],N:[0,1],S:[0,-1]}[p.side];
  if(!sym.pin_names_hidden&&p.name&&p.name!=='~'){let nm=p.name.replace(/~\{([^}]*)\}/g,'/$1');
   if(p.side==='W'||p.side==='E')texts.push({x:p.tip[0]+inw[0]*.55,y:p.tip[1]+.36,t:nm,cls:'pn',a:p.side==='W'?'start':'end'});
   else texts.push({x:p.tip[0]+.36,y:p.tip[1]+inw[1]*.55,t:nm,cls:'pn',a:p.side==='N'?'end':'start',rot:true});}
  if(!sym.pin_numbers_hidden){
   if(p.side==='W'||p.side==='E')texts.push({x:(p.at[0]+p.tip[0])/2,y:p.at[1]-.28,t:p.number,cls:'pq',a:'middle'});
   else texts.push({x:p.at[0]-.28,y:(p.at[1]+p.tip[1])/2,t:p.number,cls:'pq',a:'middle',rot:true});}
 }
 // in-place decorations for pins whose net is not a wire. Adjacent pins of one side on
 // the same rail / label / ground share one bus stub and one flag (FET D/S pins, GND rows).
 const runs=[],byKey=new Map();
 for(let p of P){let n=p.net;if(!n||n.draw==='wire')continue;if(n.draw==='nc'){runs.push([p]);continue}
  let key=p.side+'|'+n.name+'|'+n.draw;(byKey.get(key)||byKey.set(key,[]).get(key)).push(p)}
 for(let list of byKey.values()){let ax=(list[0].side==='W'||list[0].side==='E')?1:0;list.sort((a,b)=>a.at[ax]-b.at[ax]);
  let cur=[list[0]];for(let p of list.slice(1)){let q=cur[cur.length-1];if(Math.abs(p.at[ax]-q.at[ax])<=2.6&&Math.abs(p.at[1-ax]-q.at[1-ax])<.01)cur.push(p);else{runs.push(cur);cur=[p]}}runs.push(cur)}
 for(let run of runs){
  let p=run[0],n=p.net;
  if(run.length>1){let a=run[0].at,b=run[run.length-1].at;p={...run[0],at:[(a[0]+b[0])/2,(a[1]+b[1])/2],bus:[a,b]}}
  let o=schOut[p.side],[ax,ay]=p.at;
  if(n.draw==='nc'){decos.push({t:'nc',p});add([ax-.7,ay-.7,ax+.7,ay+.7]);continue}
  let e=[ax+o[0]*1.2,ay+o[1]*1.2];
  if(n.kind==='ground'){decos.push({t:'gnd',p,e});let q=[e[0]+o[0]*1.3,e[1]+o[1]*1.3];add([Math.min(ax,q[0])-1.3,Math.min(ay,q[1])-1.3,Math.max(ax,q[0])+1.3,Math.max(ay,q[1])+1.3]);continue}
  let label=n.label,fs=SCH_FS.net,w=label.length*fs*SCH_CW+(n.draw==='rail'?1.6:1.4);
  if(p.side==='W'||p.side==='E'){let x0=p.side==='W'?e[0]-w:e[0],x1=x0+w;decos.push({t:n.draw,p,e,w});add([Math.min(x0,ax),ay-1,Math.max(x1,ax),ay+1])}
  else{let y1=e[1]+o[1]*2.3;decos.push({t:n.draw,p,e,w});add([ax-w/2,Math.min(ay,y1)-.4,ax+w/2,Math.max(ay,y1)+.4])}
 }
 // ref / value / instance text
 let arr=M.members.get(c.ref),ref=c.ref+(arr?' ×'+arr.length:''),val=c.value||'',inst=arr?(c.local||'').replace(/_?\d+$/,'')+'[…]':(c.local||'');
 if(big||k===0){let y=body[1]-.7-(inst?SCH_FS.inst+.25:0);
  texts.push({x:body[0],y:y-SCH_FS.val-.25,t:ref,cls:'rf',a:'start'},{x:body[0],y,t:val,cls:'vl',a:'start'});
  if(inst)texts.push({x:body[0],y:y+SCH_FS.inst+.25,t:inst,cls:'it',a:'start'});}
 else{let x=body[2]+.8,cy=(body[1]+body[3])/2;
  texts.push({x,y:cy-.55,t:ref,cls:'rf',a:'start'},{x,y:cy+.75,t:val,cls:'vl',a:'start'});
  if(inst)texts.push({x,y:cy+1.95,t:inst,cls:'it',a:'start'});}
 for(let t of texts){let fs={rf:SCH_FS.ref,vl:SCH_FS.val,it:SCH_FS.inst,pn:SCH_FS.pin,pq:SCH_FS.num}[t.cls];
  if(t.rot){let w=t.t.length*fs*SCH_CW;add([t.x-fs*.85,t.a==='end'?t.y:t.y-w,t.x+.2,t.a==='end'?t.y+w:t.y])}else add(schTextBox(t.x,t.y,t.t,fs,t.a))}
 const bb=[Math.min(...boxes.map(b=>b[0]))-.6,Math.min(...boxes.map(b=>b[1]))-.6,Math.max(...boxes.map(b=>b[2]))+.6,Math.max(...boxes.map(b=>b[3]))+.6];
 return {ref:c.ref,k,pins:P,graphics:u.graphics,body,bb,texts,decos,big};
}
function schScaleOpts(o,S){ // lengths in ELK options (spacings, padding) -> ELK units
 let out={};for(let k in o){let v=o[k];
  if(k==='elk.padding'&&typeof v==='string')v=v.replace(/=(-?[\d.]+)/g,(m,n)=>'='+(+n*S));
  else if(k.includes('.spacing.')&&isFinite(+v)&&String(v).trim()!=='')v=String(+v*S);
  out[k]=v}
 return out}
function schOpts(){let o={...SCH_LAYERED};try{Object.assign(o,JSON.parse(sch.url.get('elk')||'{}'))}catch(e){}return o}
// Put a routed wire's ends exactly on its pins. pts: ELK section [start,...bends,end];
// s/e: exact pin points (or null to keep). The bend next to a moved end moves with it
// across the segment, so every segment stays horizontal or vertical; a straight wire whose
// ends no longer line up gets a jog in the middle.
function schSnapWire(pts,s,e){
 const eq=(a,b)=>Math.abs(a-b)<1e-6;let P=[];
 for(let p of pts){let q=P[P.length-1];if(q&&eq(q[0],p[0])&&eq(q[1],p[1]))continue;
  let r=P[P.length-2];if(q&&r&&((eq(r[0],q[0])&&eq(q[0],p[0]))||(eq(r[1],q[1])&&eq(q[1],p[1]))))P.pop();P.push([p[0],p[1]])}
 if(P.length<2)P=[pts[0].slice(),pts[pts.length-1].slice()];
 const n=P.length,horiz=i=>eq(P[i][1],P[i+1][1])&&!eq(P[i][0],P[i+1][0]),vert=i=>eq(P[i][0],P[i+1][0])&&!eq(P[i][1],P[i+1][1]);
 const h0=horiz(0),v0=vert(0),hN=horiz(n-2),vN=vert(n-2);s=s||P[0];e=e||P[n-1];
 if(n===2){
  if(h0&&!eq(s[1],e[1])){let m=(s[0]+e[0])/2;return [s,[m,s[1]],[m,e[1]],e]}
  if(v0&&!eq(s[0],e[0])){let m=(s[1]+e[1])/2;return [s,[s[0],m],[e[0],m],e]}
  return [s,e]}
 if(h0)P[1][1]=s[1];else if(v0)P[1][0]=s[0];
 if(hN)P[n-2][1]=e[1];else if(vN)P[n-2][0]=e[0];
 P[0]=[s[0],s[1]];P[n-1]=[e[0],e[1]];return P}
async function schLayout(data){
 const M=schModel(data),G=new Map(M.drawn.map(c=>[c.ref,schGeom(data,M,c)]));
 const S=+sch.url.get('elkscale')||SCH_ELK_SCALE,PW=.01*S; // PW: port size (0.01 mm) in ELK units, centred on the pin
 const nodeOf=new Map(),portOf=new Map();
 function leaf(c){
  let g=G.get(c.ref),[x0,y0,x1,y1]=g.bb,ports=[];
  for(let p of g.pins){if(!p.net||p.net.draw!=='wire')continue;let id=c.ref+'/'+p.number;if(portOf.has(id))continue;
   let off={W:p.at[0]-x0,E:x1-p.at[0],N:p.at[1]-y0,S:y1-p.at[1]}[p.side];portOf.set(id,{ref:c.ref,p});
   ports.push({id,width:PW,height:PW,x:(p.at[0]-x0)*S-PW/2,y:(p.at[1]-y0)*S-PW/2,layoutOptions:{'elk.port.side':{W:'WEST',E:'EAST',N:'NORTH',S:'SOUTH'}[p.side],'elk.port.borderOffset':String(-off*S-PW/2)}})}
  let n={id:c.ref,width:(x1-x0)*S,height:(y1-y0)*S,ports,layoutOptions:{'elk.portConstraints':'FIXED_POS'}};nodeOf.set(c.ref,n);return n;
 }
 const units=(data.units&&data.units.length?data.units:[{id:'unit:all',label:'',refs:data.components.map(c=>c.ref)}]);
 const unitOf=new Map(),containers=[];
 for(let u of units){let kids=[];for(let r of u.refs){let d=M.R(r);if(unitOf.has(d)||!G.has(d))continue;unitOf.set(d,u.id);kids.push(leaf(M.comp.get(d)))}
  if(kids.length)containers.push({u,kids,edges:[]})}
 for(let c of M.drawn)if(!unitOf.has(c.ref)){unitOf.set(c.ref,containers[0].u.id);containers[0].kids.push(leaf(c))}
 const cont=new Map(containers.map(c=>[c.u.id,c])),edgeNet=new Map(),extraLabels=new Set();let eid=0;
 const npins=r=>data.symbols[M.comp.get(r).lib].units[0].pins.length;
 for(let n of data.nets){
  if(n.draw!=='wire')continue;
  let ids=[...new Set(n.pins.map(([r,p])=>M.R(r)+'/'+p))].filter(id=>portOf.has(id));
  if(ids.length<2){for(let id of ids)extraLabels.add(id);continue}
  ids.sort((a,b)=>npins(b.split('/')[0])-npins(a.split('/')[0])||schNatural(a,b));
  let hub=ids[0],hubRef=hub.split('/')[0],back=portOf.get(hub).p.side==='W',u=unitOf.get(hubRef);
  for(let t of ids.slice(1)){let tr=t.split('/')[0];
   if(tr===hubRef||unitOf.get(tr)!==u){extraLabels.add(t);if(unitOf.get(tr)!==u)extraLabels.add(hub);continue}
   let id='e'+(eid++);edgeNet.set(id,n.name);cont.get(u).edges.push({id,sources:[back?t:hub],targets:[back?hub:t]})}
 }
 // Layout-only affinity edges: a part sharing a rail or labelled net with the unit's
 // biggest part on that net is laid out beside it (decoupling caps next to their IC).
 let gid=0;const ghostSeen=new Set();
 for(let n of data.nets){
  if(n.kind==='ground'||!(n.draw==='rail'||n.draw==='label'))continue;
  let byUnit=new Map();for(let [r,p] of n.pins){let d=M.R(r);if(!G.has(d))continue;let u=unitOf.get(d);if(!byUnit.has(u))byUnit.set(u,new Map());byUnit.get(u).set(d,p)}
  for(let [u,refs] of byUnit){if(sch.noGhost||refs.size<2||refs.size>14)continue;
   let list=[...refs.keys()].sort((a,b)=>npins(b)-npins(a)||schNatural(a,b)),hub=list[0];if(npins(hub)<=2)continue;
   let hp=G.get(hub).pins.find(p=>p.number===refs.get(hub)),back=hp?.side==='W';
   for(let r of list.slice(1)){if(npins(r)>2)continue;let key=hub+'>'+r;if(ghostSeen.has(key))continue;ghostSeen.add(key);
    cont.get(u).edges.push({id:'g'+(gid++),sources:[back?r:hub],targets:[back?hub:r],layoutOptions:{'elk.layered.priority.direction':'0'}})}}
 }
 const single=containers.length===1;let root;
 if(single)root={id:'root',children:containers[0].kids,edges:containers[0].edges,layoutOptions:schScaleOpts(schOpts(),S)};
 else root={id:'root',layoutOptions:schScaleOpts({'elk.algorithm':'rectpacking','elk.aspectRatio':'1.7','elk.spacing.nodeNode':'14','elk.padding':'[top=14,left=4,bottom=4,right=4]','elk.hierarchyHandling':'SEPARATE_CHILDREN'},S),
  children:containers.map(c=>({id:c.u.id,children:c.kids,edges:c.edges,layoutOptions:schScaleOpts({...schOpts(),'elk.padding':'[top=9,left=4,bottom=4,right=4]'},S)}))};
 const out=await new ELK().layout(root);
 // back to mm; parts first, so wire ends can be put on the exact pin positions
 const parts=new Map(),frames=[],wires=[],laid=[];
 if(single){for(let n of out.children)parts.set(n.id,{x:n.x/S,y:n.y/S,w:n.width/S,h:n.height/S});laid.push([out,0,0])}
 else for(let u of out.children){let c=containers.find(c=>c.u.id===u.id),ux=u.x/S,uy=u.y/S;frames.push({id:u.id,label:c.u.label,kind:c.u.kind,node:c.u.node,x:ux,y:uy,w:u.width/S,h:u.height/S});
  for(let n of u.children||[])parts.set(n.id,{x:ux+n.x/S,y:uy+n.y/S,w:n.width/S,h:n.height/S});laid.push([u,ux,uy])}
 const pinPt=id=>{let q=portOf.get(id),pos=q&&parts.get(q.ref),g=q&&G.get(q.ref);return pos?[pos.x+q.p.at[0]-g.bb[0],pos.y+q.p.at[1]-g.bb[1]]:null};
 let snapMax=0;const moved=new Map(); // net -> [[old pts, new pts]] for re-seating junctions
 for(let [g,ox,oy] of laid)for(let e of g.edges||[]){if(e.id[0]!=='e')continue;let net=edgeNet.get(e.id),secs=e.sections||[];
  secs.forEach((s,i)=>{let pts=[s.startPoint,...(s.bendPoints||[]),s.endPoint].map(p=>[ox+p.x/S,oy+p.y/S]);
   let a=pinPt(s.incomingShape||(i===0?e.sources[0]:null)),z=pinPt(s.outgoingShape||(i===secs.length-1?e.targets[0]:null));
   for(let [p,q] of [[a,pts[0]],[z,pts[pts.length-1]]])if(p)snapMax=Math.max(snapMax,Math.hypot(p[0]-q[0],p[1]-q[1]));
   let np=schSnapWire(pts,a,z);wires.push({net,pts:np});(moved.get(net)||moved.set(net,[]).get(net)).push([pts,np])})}
 // junctions sat on the unsnapped wires: move each by the shift of the segment it was on
 for(let [g,ox,oy] of laid)for(let e of g.edges||[]){if(e.id[0]!=='e')continue;let net=edgeNet.get(e.id);
  for(let j of e.junctionPoints||[])wires.push({net,junction:schReseat([ox+j.x/S,oy+j.y/S],moved.get(net)||[])})}
 return {w:out.width/S,h:out.height/S,parts,frames,wires,G,M,extraLabels,single,edges:eid,ghosts:gid,snapMax};
}
// A junction on segment i of an unsnapped wire -> the same fraction along the snapped wire's
// nearest segment (the snap moves points by <= 0.01 mm, a jog is only added to 2-point wires).
function schReseat(j,list){
 let best=null,bd=.05;
 for(let [o,n] of list)for(let i=0;i+1<o.length;i++){let a=o[i],b=o[i+1],dx=b[0]-a[0],dy=b[1]-a[1],l=dx*dx+dy*dy,t=l?Math.max(0,Math.min(1,((j[0]-a[0])*dx+(j[1]-a[1])*dy)/l)):0;
  let d=Math.hypot(a[0]+t*dx-j[0],a[1]+t*dy-j[1]);if(d<bd){bd=d;best=[o,n,i,t]}}
 if(!best)return j;let [o,n,i,t]=best;
 if(o.length!==n.length){ // wire got a jog or lost collinear points: nearest point on the snapped wire
  let q=j,qd=1e9;for(let k=0;k+1<n.length;k++){let a=n[k],b=n[k+1],dx=b[0]-a[0],dy=b[1]-a[1],l=dx*dx+dy*dy,u=l?Math.max(0,Math.min(1,((j[0]-a[0])*dx+(j[1]-a[1])*dy)/l)):0,p=[a[0]+u*dx,a[1]+u*dy],d=Math.hypot(p[0]-j[0],p[1]-j[1]);if(d<qd){qd=d;q=p}}return q}
 let a=n[i],b=n[i+1];return [a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])];
}

// ------------------------------------------------------------------ SVG build
function schArc(s,m,e){
 let [ax,ay]=s,[bx,by]=m,[cx,cy]=e,d=2*(ax*(by-cy)+bx*(cy-ay)+cx*(ay-by));if(Math.abs(d)<1e-9)return `M${ax},${ay}L${cx},${cy}`;
 let ux=((ax*ax+ay*ay)*(by-cy)+(bx*bx+by*by)*(cy-ay)+(cx*cx+cy*cy)*(ay-by))/d,uy=((ax*ax+ay*ay)*(cx-bx)+(bx*bx+by*by)*(ax-cx)+(cx*cx+cy*cy)*(bx-ax))/d;
 let r=Math.hypot(ax-ux,ay-uy),sweep=((bx-ax)*(cy-ay)-(by-ay)*(cx-ax))>0?1:0;return `M${ax},${ay}A${r},${r} 0 0 ${sweep} ${cx},${cy}`;
}
function schTierOf(c){return c.power?.tier||null}
function schBuild(key,data,L){
 schRoot.replaceChildren();
 const layer=n=>schEl('g',{class:n},schRoot);
 const gFrames=layer('frames'),gHalo=layer('halos'),gLoops=layer('loops'),gWires=layer('wires'),gParts=layer('parts'),gTop=layer('overlay');
 const idx={parts:new Map(),nets:new Map(),halos:new Map(),loops:new Map(),frames:new Map()};
 const netEl=(net,e)=>{if(!net)return;e.dataset.net=net;if(!idx.nets.has(net))idx.nets.set(net,[]);idx.nets.get(net).push(e)};
 // unit frames (board scope)
 for(let f of L.frames){let g=schEl('g',{class:'frame','data-unit':f.id,'data-group':f.node},gFrames);
  schEl('rect',{x:f.x,y:f.y,width:f.w,height:f.h,rx:2},g);
  // titles of small frames are clipped to the frame width (full name in the legend / tooltip)
  let cid='sch-fc-'+idx.frames.size,cp=schEl('clipPath',{id:cid},g);schEl('rect',{x:f.x,y:f.y-60,width:f.w,height:62},cp);
  let tt=schText(g,f.x+1.5,f.y-1.2,f.label,'ft');tt.setAttribute('clip-path',`url(#${cid})`);schEl('title',null,tt).textContent=f.label;idx.frames.set(f.id,g)}
 // parts
 for(let [ref,pos] of L.parts){
  const g=L.G.get(ref),c=L.M.comp.get(ref),ox=pos.x-g.bb[0],oy=pos.y-g.bb[1];
  const pg=schEl('g',{class:'p',transform:`translate(${+ox.toFixed(3)},${+oy.toFixed(3)})`},gParts);pg.dataset.ref=ref;
  const body=schEl('g',{class:'sym',transform:g.k?`rotate(${g.k})`:undefined},pg);
  for(let s of g.graphics){let cls='b'+(s.fill==='background'?' bg':s.fill==='outline'?' fo':'');
   if(s.t==='rect')schEl('rect',{x:Math.min(s.a[0],s.b[0]),y:Math.min(s.a[1],s.b[1]),width:Math.abs(s.b[0]-s.a[0]),height:Math.abs(s.b[1]-s.a[1]),class:cls},body);
   else if(s.t==='poly')schEl(s.fill==='none'?'polyline':'polygon',{points:s.pts.map(p=>p.join(',')).join(' '),class:cls},body);
   else if(s.t==='circle')schEl('circle',{cx:s.c[0],cy:s.c[1],r:s.r,class:cls},body);
   else if(s.t==='arc')schEl('path',{d:schArc(s.s,s.m,s.e),class:'b'},body);
   else if(s.t==='bezier'&&s.pts.length===4)schEl('path',{d:`M${s.pts[0]}C${s.pts[1]} ${s.pts[2]} ${s.pts[3]}`,class:cls},body);
   else if(s.t==='text')schText(body,s.at[0],s.at[1],s.text,'st','middle')}
  for(let p of g.pins){let ln=schEl('line',{x1:p.at[0],y1:p.at[1],x2:p.tip[0],y2:p.tip[1],class:'pin'},pg);ln.dataset.pin=p.number;netEl(p.net?.name,ln);
   let hit=schEl('circle',{cx:p.at[0],cy:p.at[1],r:.55,class:'ph'},pg);hit.dataset.pin=p.number;netEl(p.net?.name,hit)}
  for(let t of g.texts)schText(pg,t.x,t.y,t.t,t.cls,t.a,t.rot?{transform:`rotate(-90 ${+t.x.toFixed(3)} ${+t.y.toFixed(3)})`}:null);
  for(let d of g.decos){let p=d.p,o=schOut[p.side],[ax,ay]=p.at,n=p.net;
   if(d.t==='nc'){let s=.6;schEl('path',{d:`M${ax-s},${ay-s}L${ax+s},${ay+s}M${ax-s},${ay+s}L${ax+s},${ay-s}`,class:'nc'},pg);continue}
   let e=d.e,perp=[o[1],-o[0]];
   if(p.bus)netEl(n.name,schEl('line',{x1:p.bus[0][0],y1:p.bus[0][1],x2:p.bus[1][0],y2:p.bus[1][1],class:d.t==='gnd'?'gnd':'stub'},pg));
   if(d.t==='gnd'){let q=[e[0]+o[0]*1.1,e[1]+o[1]*1.1];netEl(n.name,schEl('path',{d:`M${ax},${ay}L${e[0]},${e[1]}M${e[0]+perp[0]*1.1},${e[1]+perp[1]*1.1}L${q[0]},${q[1]}L${e[0]-perp[0]*1.1},${e[1]-perp[1]*1.1}Z`,class:'gnd'},pg));continue}
   const grp=schEl('g',{class:d.t==='rail'?'rail':'lbl'},pg);netEl(n.name,grp);
   schEl('line',{x1:ax,y1:ay,x2:e[0],y2:e[1],class:'stub'},grp);
   let fs=SCH_FS.net,w=d.w;
   if(p.side==='W'||p.side==='E'){let dir=o[0],x0=e[0],x1=e[0]+dir*w,h=1.7;
    if(d.t==='rail'){schEl('path',{d:`M${x0},${ay}L${x0+dir*.9},${ay-h/2}L${x0+dir*.9},${ay+h/2}Z`,class:'rarrow'},grp);schText(grp,x0+dir*1.3,ay+fs*.36,n.label,'rt',dir>0?'start':'end')}
    else{schEl('path',{d:`M${x0},${ay}L${x0+dir*.7},${ay-h/2}L${x1},${ay-h/2}L${x1},${ay+h/2}L${x0+dir*.7},${ay+h/2}Z`,class:'lflag'},grp);schText(grp,x0+dir*1.0,ay+fs*.36,n.label,'lt',dir>0?'start':'end')}}
   else{let dir=o[1],ty=e[1]+dir*(dir<0?1.1:2.0);
    if(d.t==='rail'){schEl('path',{d:`M${ax-1},${e[1]}L${ax+1},${e[1]}`,class:'rbar'},grp);schText(grp,ax,ty,n.label,'rt','middle')}
    else schText(grp,ax,ty,n.label,'lt','middle')}
  }
  for(let id of L.extraLabels){if(!id.startsWith(ref+'/'))continue;let p=g.pins.find(q=>q.number===id.slice(ref.length+1));if(!p?.net)continue;
   let o=schOut[p.side],[ax,ay]=p.at,grp=schEl('g',{class:'lbl mini'},pg);netEl(p.net.name,grp);
   let anchor=p.side==='W'?'end':p.side==='E'?'start':'middle',tx=ax+o[0]*.5,ty=ay+(o[1]?o[1]*1.3:-.35);schText(grp,tx,ty,p.net.label,'lt',anchor)}
  schEl('rect',{x:g.bb[0]+.2,y:g.bb[1]+.2,width:g.bb[2]-g.bb[0]-.4,height:g.bb[3]-g.bb[1]-.4,rx:.6,class:'selbox'},pg);
  idx.parts.set(ref,{g:pg,pos,geom:g,halo:[pos.x+(g.body[0]-g.bb[0])-1.2,pos.y+(g.body[1]-g.bb[1])-1.2,pos.x+(g.body[2]-g.bb[0])+1.2,pos.y+(g.body[3]-g.bb[1])+1.2]});
 }
 // wires + junctions
 for(let w of L.wires){if(w.junction){netEl(w.net,schEl('circle',{cx:w.junction[0],cy:w.junction[1],r:.42,class:'jn'},gWires));continue}
  let pl=schEl('polyline',{points:w.pts.map(p=>p.map(v=>+v.toFixed(3)).join(',')).join(' '),class:'w'},gWires);netEl(w.net,pl)}
 // halos: power role (tier) and subcircuit (tree groups)
 const mkHalo=(cls,ref,color,id)=>{let h=idx.parts.get(L.M.R(ref))?.halo;if(!h)return;let r=schEl('rect',{x:h[0],y:h[1],width:h[2]-h[0],height:h[3]-h[1],rx:1.2,class:cls,fill:color,stroke:color},gHalo);r.dataset.ref=L.M.R(ref);if(id)r.dataset.group=id;return r};
 for(let c of data.components){let t=schTierOf(c);if(t&&L.M.R(c.ref)===c.ref)mkHalo('halo role',c.ref,SCH_TIER[t],'tier:'+t)}
 const groups=schGroups(data),seen=new Set();
 const unitNodes=new Set(L.frames.map(f=>f.node).filter(Boolean));
 for(let f of L.frames){let gr=groups.find(x=>x.id===f.node);if(gr)idx.frames.get(f.id)?.style.setProperty('--fc',gr.color)}
 for(let gr of groups){if(gr.depth===0&&unitNodes.has(gr.id))continue;for(let r of gr.refs){let d=L.M.R(r);if(seen.has(d+'|'+gr.depth))continue;seen.add(d+'|'+gr.depth);let el=mkHalo('halo grp d'+gr.depth,d,gr.color,gr.id);if(el)(idx.halos.get(gr.id)||idx.halos.set(gr.id,[]).get(gr.id)).push(el)}}
 // hot loops / conduction paths: badges on the member parts; the band through the
 // member centroids is shown when the loop is focused
 const badges=new Map();
 for(let h of data.highlights||[]){if(h.style!=='hot-loop'&&h.style!=='power-path')continue;
  let pts=(h.classes||[]).map(ms=>{let ps=[...new Set(ms.map(L.M.R))].map(r=>idx.parts.get(r)).filter(Boolean);if(!ps.length)return null;
   return [ps.reduce((s,p)=>s+(p.halo[0]+p.halo[2])/2,0)/ps.length,ps.reduce((s,p)=>s+(p.halo[1]+p.halo[3])/2,0)/ps.length]}).filter(Boolean);
  if(pts.length<2)continue;let g=schEl('g',{class:'loop '+(h.style==='hot-loop'?'hot':'path')},gLoops);g.dataset.hl=h.id;
  schEl('polygon',{points:pts.map(p=>p.map(v=>+v.toFixed(2)).join(',')).join(' ')},g);
  let cx=pts.reduce((s,p)=>s+p[0],0)/pts.length,cy=pts.reduce((s,p)=>s+p[1],0)/pts.length,n=+h.id.split(':')[1]+1;
  schEl('circle',{cx,cy,r:2.2,class:'lb'},g);schText(g,cx,cy+.9,'L'+n,'lbt','middle');idx.loops.set(h.id,g);
  for(let r of new Set(h.refs.map(L.M.R))){(badges.get(r)||badges.set(r,[]).get(r)).push({n,hot:h.style==='hot-loop',id:h.id})}}
 for(let [r,list] of badges){let p=idx.parts.get(r);if(!p)continue;let g0=p.geom,b=g0.body,w=2.9,vertical=!g0.big&&g0.k!==0;
  // vertical passives: under the ref/value/instance column; others: under the body, right-aligned
  let x=vertical?b[2]+.8:b[2]-list.length*(w+.3)+.3,y=vertical?(b[1]+b[3])/2+3:b[3]+.35;
  for(let bd of list){let g=schEl('g',{class:'lbadge '+(bd.hot?'hot':'path')},p.g);g.dataset.hl=bd.id;
   schEl('rect',{x,y,width:w,height:1.9,rx:.5},g);schText(g,x+w/2,y+1.4,'L'+bd.n,'lbt2','middle');x+=w+.3}}
 sch.built={key,idx,L,data,groups};sch.fitKey=null;sch.selRef=undefined;schNoteBadges();
}
// Colored subcircuit groups: block scope = top-level tree nodes; board scope = unit frames
// plus the subcircuits inside each module.
function schGroups(data){
 let out=[],ci=0,board=data.scope?.kind==='board';
 const push=(nd,depth,color,parent)=>{out.push({id:nd.id,label:nd.label||nd.key||nd.id,kind:nd.kind,refs:nd.refs,depth,color,parent,anchor:nd.anchor});return color};
 if(board){for(let nd of data.tree){let col=SCH_PALETTE[ci++%SCH_PALETTE.length];push(nd,0,col,null);
   if(nd.kind==='module'){let j=0;for(let ch of nd.children||[])push(ch,1,SCH_PALETTE[(ci+3+j++)%SCH_PALETTE.length],nd.id)}}}
 else for(let nd of data.tree){let col=SCH_PALETTE[ci++%SCH_PALETTE.length];push(nd,0,col,null);for(let ch of nd.children||[])push(ch,1,col,nd.id)}
 return out;
}

// ------------------------------------------------------------------ overlay, legend, highlighting
function schHighlights(){ // highlight entries: builder highlights + subcircuit groups + lane moves
 let d=sch.data;if(!d)return [];let list=[...(d.highlights||[])];
 for(let g of sch.built?.groups||[])list.push({id:g.id,label:g.label,style:'group',refs:g.refs,nets:[],color:g.color,depth:g.depth,parent:g.parent});
 let moves=(lane()?.moves||[]).map(m=>m.ref).filter(Boolean);if(moves.length)list.push({id:'moves',label:'Moved in this candidate',style:'moves',refs:moves,nets:[]});
 if(sch.overlay?.lane_refs&&d.scope?.kind==='board'){let blk=d.highlights.find(h=>h.style==='block'&&h.refs.length===sch.overlay.lane_refs.length&&h.refs.every(r=>sch.overlay.lane_refs.includes(r)));
  if(!blk)list.push({id:'lane-refs',label:'This trial\'s parts',style:'block',refs:sch.overlay.lane_refs,nets:[]})}
 return list;
}
function schLaneBlockId(){let d=sch.data,refs=sch.overlay?.lane_refs;if(!d||!refs)return null;
 let blk=(d.highlights||[]).find(h=>h.style==='block'&&h.refs.length===refs.length&&h.refs.every(r=>refs.includes(r)));return blk?blk.id:'lane-refs'}
function schApplyOverlay(){
 let d=sch.data,blockLane=!!sch.overlay?.lane_refs;
 $('sch-scope').hidden=!blockLane;for(let b of $('sch-scope').children)b.classList.toggle('active',b.dataset.s===sch.scopePref);
 if(!sch.colorByUser)sch.colorBy=d.scope.kind==='block'?'role':'group';
 let fk=sch.key+'|'+laneId;
 if(sch.focusKey!==fk){ // new payload or lane: pick the default highlight once
  sch.focusKey=fk;let hs=schHighlights(),keep=sch.focus&&hs.find(h=>h.id===sch.focus.id);
  if(sch.urlHl){sch.focus=hs.find(h=>h.id===sch.urlHl)||null;sch.urlHl=null}
  else if(blockLane&&d.scope.kind==='board'){let id=schLaneBlockId();sch.focus=hs.find(h=>h.id===id)||null}
  else sch.focus=keep||null;
 }
 schApplyClasses();schLegend();schSyncSelection(true);schAskApply(false);render();
}
function schFocusSet(){let f=sch.focus;if(!f)return null;let d=sch.data,L=sch.built?.L;return {refs:new Set(f.refs.map(r=>L?L.M.R(r):r)),raw:new Set(f.refs),nets:new Set(f.nets||[])}}
function schApplyClasses(){
 let b=sch.built;if(!b)return;
 schRoot.setAttribute('class',`color-${sch.colorBy||'role'}`+(sch.focus?' focusing':'')+(sch.net?' netting':''));
 for(let b2 of $('sch-color').children)b2.classList.toggle('active',b2.dataset.c===sch.colorBy);
 let F=schFocusSet();
 for(let [ref,p] of b.idx.parts)p.g.classList.toggle('in',!!F&&F.refs.has(ref));
 for(let [net,els] of b.idx.nets){let on=!!F&&F.nets.has(net),hl=sch.net===net;for(let e of els){e.classList.toggle('in',on);e.classList.toggle('hl',hl)}}
 for(let e of schRoot.querySelectorAll('.halos .halo'))e.classList.toggle('in',!!F&&F.refs.has(e.dataset.ref));
 for(let [id,g] of b.idx.loops)g.classList.toggle('in',sch.focus?.id===id);
 for(let el of schRoot.querySelectorAll('.lbadge'))el.classList.toggle('in',sch.focus?.id===el.dataset.hl);
 for(let [id,g] of b.idx.frames)g.classList.toggle('in',!!F&&!!sch.focus&&(sch.focus.id===id.slice(5)||sch.focus.id==='block:'+id.slice(5)));
 // wires: a wire is "in" when its net is a focus net or it joins two focused parts
 if(F)for(let el of schRoot.querySelectorAll('.wires .w,.wires .jn'))if(!F.nets.has(el.dataset.net))el.classList.toggle('in',schWireInside(el,F));
 let chip=$('sch-chip'),label=sch.focus?sch.focus.label:sch.net?'Net '+schNetLabel(sch.net):'';chip.hidden=!label;
 if(label){let cl=document.createElement('span');cl.className='chip-l';cl.textContent=label;chip.title='Highlighted in the schematic: '+label;chip.replaceChildren(cl);let x=document.createElement('button');x.textContent='×';x.title='Clear highlight';x.onclick=()=>{sch.focus=null;sch.net=null;schApplyClasses();schLegend();render()};
  let q=document.createElement('button');q.textContent='Ask';q.className='sch-ask';q.title='Add this highlight to the Ask context';q.onclick=()=>schAsk(sch.focus?{group:sch.focus}:{net:sch.net});chip.append(q,x)}
}
function schWireInside(el,F){let n=sch.data.nets.find(x=>x.name===el.dataset.net);if(!n)return false;let inside=n.pins.filter(([r])=>F.raw.has(r)).length;return inside>=2}
function schNetLabel(net){let n=sch.data?.nets.find(x=>x.name===net);return n?(n.alias?`${n.alias} (${n.name})`:n.name):net}
function schToggleFocus(h){sch.focus=sch.focus?.id===h.id?null:h;sch.net=null;if(sch.focus){schInspect(schGroupItem(h));if(viewHl)window.YapnrView.clear(true)}schApplyClasses();schLegend();if(sch.focus&&sch.focus.refs.length){schFit(true,true);schPcbFrame(sch.focus.refs)}render()}
// Bring highlighted parts into the PCB view only when some are off-screen (keeps the user's view otherwise).
function schPcbFrame(refs){let g=geo(),w=canvas.clientWidth,h=canvas.clientHeight;if(!g||w<80||h<80)return;
 let bs=(g.parts||[]).filter(p=>refs.includes(p.ref)).map(componentBounds).filter(Boolean);if(!bs.length)return;
 let b=[Math.min(...bs.map(x=>x[0])),Math.min(...bs.map(x=>x[1])),Math.max(...bs.map(x=>x[2])),Math.max(...bs.map(x=>x[3]))];
 let a=screen([b[0],b[3]]),c=screen([b[2],b[1]]);if(a[0]>=0&&a[1]>=0&&c[0]<=w&&c[1]<=h)return;
 let scale=Math.max(1,Math.min(120,(w-80)/Math.max(4,b[2]-b[0]),(h-80)/Math.max(4,b[3]-b[1])));
 view={scale,x:w/2-(b[0]+b[2])/2*scale,y:h/2+(b[1]+b[3])/2*scale};fitted=true}
function schPQ(h){ // routed hot-loop quality from the trial run dir, matched by class labels
 let pq=sch.overlay?.power_quality;if(!pq||!h.labels)return null;let key=h.labels.map(l=>l.split('|').sort().join('|')).sort().join('>');
 return pq.loops.find(l=>(l.labels||[]).map(x=>x.split('|').sort().join('|')).sort().join('>')===key)||null}
function schLegend(){
 const body=$('sch-legend-body'),d=sch.data;if(!d){body.replaceChildren();return}
 body.replaceChildren();const sec=(title,note)=>{let h=document.createElement('h3');h.textContent=title;body.append(h);if(note){let p=document.createElement('p');p.className='muted';p.textContent=note;body.append(p)}};
 const entry=(h,swatch,text,sub,indent)=>{let b=document.createElement('button');b.className='lg'+(sch.focus?.id===h.id?' on':'')+(indent?' ind':'');b.title=(h.refs||[]).join(' ')+'\nShift+click: add to the Ask context';
  let s=document.createElement('i');s.style.background=swatch;if(h.style==='power-path')s.className='dash';b.append(s);let t=document.createElement('span');t.textContent=text;b.append(t);
  if(sub){let u=document.createElement('em');u.textContent=sub;b.append(u)}b.onclick=e=>e.shiftKey?schAsk({group:h}):schToggleFocus(h);body.append(b);return b};
 const hs=schHighlights(),sc=d.scope,ov=sch.overlay||{};
 let head=document.createElement('div');head.className='lghead';
 let title=sc.kind==='block'?`Block ${sc.name}`:'Whole board',meta=sc.kind==='block'?`${sc.refs.length} parts`+(ov.template?` · template ${ov.template}`:''):`${d.components.length} parts · ${d.nets.length} nets`;
 head.innerHTML='<b></b><span></span>';head.children[0].textContent=title;head.children[1].textContent=meta+(sc.match==='approximate'?' · parts differ from the source block':'')+(ov.tag&&ov.lane_refs?` · trial ${ov.tag}`:'');body.append(head);
 if(sc.kind==='block'&&sc.ports?.length){let p=document.createElement('p');p.className='muted';p.textContent='External nets: '+sc.ports.join(', ');body.append(p)}
 if(sch.overlay?.lane_refs&&sc.kind==='board'){let id=schLaneBlockId(),h=hs.find(x=>x.id===id);if(h)entry(h,'#ffd166','This block trial: '+h.label.replace(/^Block /,''),h.refs.length+' parts')}
 if(d.power){
  sec('Power roles',sc.kind==='block'?'Tiers and hot loops of this block trial (pnr.power_topology).':'Tiers and hot loops across the board.');
  for(let h of hs.filter(h=>h.style==='tier'))entry(h,SCH_TIER[h.tier],h.label,h.refs.length+' parts');
  for(let h of hs.filter(h=>h.style==='hot-loop'||h.style==='power-path')){let q=schPQ(h),n=+h.id.split(':')[1]+1;
   let sub=(h.peak_a?h.peak_a+' A peak':'')+(q?(q.routed_mm!=null?` · routed ${q.routed_mm.toFixed(1)} mm`:' · not fully routed')+` · ${q.vias??'?'} vias`+(q.open_links?` · ${q.open_links} open link${q.open_links>1?'s':''}`:q.complete?' · complete':''):'');
   entry(h,h.style==='hot-loop'?SCH_HOT:SCH_PATH,`L${n} · `+h.label,sub)}
 }
 let groups=hs.filter(h=>h.style==='group');
 if(groups.length){sec('Subcircuits',sc.kind==='block'?'Hard placement groups and name families (atopile instance names).':'Modules, hard groups and families.');
  let arrays=new Map((d.arrays||[]).map(a=>[a.refs[0],a]));
  for(let h of groups){let tw=(d.arrays||[]).find(a=>a.refs.length===h.refs.length&&a.refs.every(r=>h.refs.includes(r)));
   let mod=d.modules?.find(m=>m.id===h.id),sub=h.refs.length+' parts'+(tw?' · identical twins':'')+(mod?.twins?.length?' · same type as '+mod.twins.map(x=>x.split('.').pop()).join(', '):'');
   let b=entry(h,h.color,h.label,sub,h.depth>0);if(mod?.doc)b.title=mod.doc+'\n'+b.title}}
 let blocks=hs.filter(h=>h.style==='block'&&sc.kind==='board'&&h.id!==schLaneBlockId());
 if(blocks.length){sec('Placement blocks','Blocks the hierarchical placer trials separately; twins share a template.');
  for(let h of blocks)entry(h,'#ffd166',h.label.replace(/^Block /,''),h.refs.length+' parts'+(h.twins?.length?' · twin of '+h.twins.join(', '):''))}
 let mv=hs.find(h=>h.id==='moves');if(mv){sec('Candidate');entry(mv,'#fff07d',mv.label,mv.refs.join(' '))}
 let w=[...(d.warnings||[]),...(d.issues||[]).map(i=>i.ref+': '+i.kind)];
 if(w.length){let det=document.createElement('details');det.innerHTML='<summary></summary><p class="muted"></p>';det.children[0].textContent=w.length+' data notes';det.children[1].textContent=w.join('\n');body.append(det)}
 let foot=document.createElement('p');foot.className='muted';foot.textContent=['Mechanical layout from the netlist'+(sch.layout?.ms>=1?` (${sch.layout.ms.toFixed(0)} ms)`:''),'click a part to find it on the PCB, a wire or label to trace its net, a group to highlight it.'].join(' · ');body.append(foot);
}

// ------------------------------------------------------------------ pan / zoom / fit
function schApplyView(){let {x,y,k}=sch.v;schRoot.setAttribute('transform',`translate(${(-x*k).toFixed(2)},${(-y*k).toFixed(2)}) scale(${k.toFixed(4)})`);
 schSvg.classList.toggle('lod0',k<2.6);schSvg.classList.toggle('lod1',k<4.2);schSvg.style.setProperty('--refs',Math.min(3.4,Math.max(1.8,10/k)).toFixed(2)+'px');schSvg.style.setProperty('--ft',Math.min(10,Math.max(3.2,10/k)).toFixed(2)+'px')}
function schFitBox(b){let w=schSvg.clientWidth,h=schSvg.clientHeight;if(w<40||h<40)return false;let pad=18,top=44;
 let lg=$('sch-legend');if(schSplitLike()&&lg.offsetWidth&&getComputedStyle(lg).position==='absolute')w=Math.max(120,w-lg.offsetWidth-12);
 let k=Math.min((w-2*pad)/(b[2]-b[0]),(h-top-pad)/(b[3]-b[1]));k=Math.max(.3,Math.min(40,k));
 sch.v={k,x:(b[0]+b[2])/2-w/2/k,y:(b[1]+b[3])/2-(h+top-pad)/2/k};schApplyView();return true}
function schFocusBox(){let F=schFocusSet(),b=sch.built;if(!F||!b)return null;let hs=[...F.refs].map(r=>b.idx.parts.get(r)?.halo).filter(Boolean);
 if(!hs.length)return null;return [Math.min(...hs.map(h=>h[0]))-4,Math.min(...hs.map(h=>h[1]))-6,Math.max(...hs.map(h=>h[2]))+4,Math.max(...hs.map(h=>h[3]))+4]}
function schFit(force,focusOnly){let L=sch.built?.L;if(!L)return;let b=(sch.focus&&(focusOnly||force))?schFocusBox():null;
 if(!b){b=[-2,-2,L.w+2,L.h+2];if(focusOnly)return}if(schFitBox(b))sch.fitKey=sch.key+'|'+schSvg.clientWidth+'x'+schSvg.clientHeight}
function schFitIfNeeded(){if(sch.built&&sch.mode!=='pcb'&&!sch.fitKey){if(sch.focus&&sch.data?.scope?.kind==='board')schFit(true,true);if(!sch.fitKey)schFit(false);
 let z=sch.url.get('zoom');if(z&&sch.fitKey){let [r,k]=z.split(':'),p=sch.built.idx.parts.get(sch.built.L.M.R(r));if(p){k=+k||10;let w=schSvg.clientWidth/k,h=schSvg.clientHeight/k;sch.v={k,x:(p.halo[0]+p.halo[2])/2-w/2,y:(p.halo[1]+p.halo[3])/2-h/2};schApplyView()}}}}
let schDrag=null;
schSvg.addEventListener('wheel',e=>{e.preventDefault();let r=schSvg.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,{x,y,k}=sch.v,wx=x+mx/k,wy=y+my/k;
 let k2=Math.max(.3,Math.min(60,k*Math.exp(-e.deltaY*.0015)));sch.v={k:k2,x:wx-mx/k2,y:wy-my/k2};schApplyView()},{passive:false});
schSvg.addEventListener('pointerdown',e=>{schSvg.setPointerCapture(e.pointerId);schDrag={x:e.clientX,y:e.clientY,v:{...sch.v},moved:false}});
schSvg.addEventListener('pointermove',e=>{
 if(schDrag){let dx=e.clientX-schDrag.x,dy=e.clientY-schDrag.y;if(Math.hypot(dx,dy)>3)schDrag.moved=true;if(schDrag.moved){sch.v={...schDrag.v,x:schDrag.v.x-dx/sch.v.k,y:schDrag.v.y-dy/sch.v.k};schApplyView()}return}
 schHover(e)});
schSvg.addEventListener('pointerup',e=>{let d=schDrag;schDrag=null;if(!d||d.moved)return;schClick(e)});
schSvg.addEventListener('pointerleave',()=>{$('sch-hover').style.display='none';schSetHoverNet(null)});
window.addEventListener('resize',()=>{if(sch.mode!=='pcb'&&sch.fitKey&&!sch.fitKey.endsWith(schSvg.clientWidth+'x'+schSvg.clientHeight)){sch.fitKey=null;schFitIfNeeded()}render()});

// ------------------------------------------------------------------ hover / click (schematic -> PCB)
function schTarget(e){let el=document.elementFromPoint(e.clientX,e.clientY);if(!el||!schSvg.contains(el))return {};
 let netEl=el.closest('[data-net]'),partEl=el.closest('.p'),halo=el.closest('[data-group]'),loop=el.closest('.loop');
 let nb=el.closest('.nbadge');if(nb)return {nbadge:nb.dataset.ref,refs:nb.dataset.refs.split(' '),ids:nb.dataset.ids.split(' ')};
 let badge=el.closest('.lbadge');return {net:netEl?.dataset.net,pin:el.dataset.pin,ref:badge?null:partEl?.dataset.ref,group:halo?.dataset.group,loop:(loop||badge)?.dataset.hl}}
function schDescribePart(ref){let d=sch.data,c=d.components.find(x=>x.ref===ref),L=sch.built.L,arr=L.M.members.get(ref);if(!c)return ref;
 let t=c.power?.tier,role=t?{1:'power stage',2:'controller',3:'support passive'}[t]+' (tier '+t+')':'';if(c.power?.controller&&t===1)role+=' · controller';
 return [`${arr?arr.join(', '):ref} · ${c.value}`,`${c.instance}`+(c.mpn&&c.mpn!==c.value?` · ${c.mpn}`:''),`${c.footprint}`+(role?' · '+role:''),
  Object.keys(c.power?.carrying||{}).length?'carrying '+Object.entries(c.power.carrying).map(([n,p])=>`${n}[${p.join(',')}]`).join(' '):''].filter(Boolean).join('\n')}
function schHover(e){let t=schTarget(e),box=$('sch-hover'),text='';
 if(t.nbadge){let N=window.YapnrNotes;text=t.ids.map(id=>{let n=N?.get?.(id);return n?`${id} · ${N.labels?.status?.[n.status]||n.status} ${(N.labels?.kind?.[n.kind]||n.kind||'').toLowerCase()} · ${n.title}`:id}).join('\n')+'\nClick: show in Notes'}
 else if(t.ref&&t.pin){let c=sch.data.components.find(x=>x.ref===t.ref),g=sch.built.idx.parts.get(t.ref)?.geom,p=g?.pins.find(q=>q.number===t.pin);text=`${t.ref}.${t.pin}`+(p?.name&&p.name!==t.pin?` ${p.name.replace(/~\{([^}]*)\}/g,'/$1')}`:'')+(t.net?`\nnet ${schNetLabel(t.net)}`+schSem(t.net):'\nnot connected')}
 else if(t.net){let n=sch.data.nets.find(x=>x.name===t.net);text=`net ${schNetLabel(t.net)}`+schSem(t.net)+`\n${n?n.kind+' · '+n.pins.length+' pins here'+(n.pins_total>n.pins.length?` of ${n.pins_total} on the board`:'')+(n.port?' · leaves this block':''):''}`}
 else if(t.ref)text=schDescribePart(t.ref);
 else if(t.loop){let h=sch.data.highlights.find(x=>x.id===t.loop),q=h&&schPQ(h);text=h?h.label+(h.peak_a?`\n${h.peak_a} A peak · nets ${h.nets.join(', ')}`:'')+(q?`\nrouted ${q.routed_mm?.toFixed(1)} mm · ${q.vias} vias`:''):''}
 else if(t.group){let g=schHighlights().find(x=>x.id===t.group);text=g?`${g.label} · ${g.refs.length} parts · click to highlight`:''}
 box.style.display=text?'block':'none';box.textContent=text;schSetHoverNet(t.net||null);
 if(text){let r=schSvg.getBoundingClientRect(),x=e.clientX-r.left+14,y=e.clientY-r.top+14;box.style.left=Math.min(x,r.width-box.offsetWidth-6)+'px';box.style.top=Math.min(y,r.height-box.offsetHeight-6)+'px'}}
function schClick(e){let t=schTarget(e);
 if(t.nbadge){let r=t.refs;if(e.shiftKey)schAsk({ref:r[0]});else window.YapnrNotes?.showFor?.(r.length===1?{kind:'component',ref:r[0]}:{kind:'group',id:'parallel:'+t.nbadge,label:r.join(' · '),refs:r});return}
 if(e.shiftKey){schAsk(t);return}
 if(t.net&&(t.pin||!t.ref)){sch.net=sch.net===t.net?null:t.net;if(sch.net&&viewHl)window.YapnrView.clear(true);schApplyClasses();render();schInspect(t.pin&&t.ref?{kind:'pad',ref:t.ref,pad:t.pin}:{kind:'net',name:t.net});return}
 if(t.ref){let g=geo(),geoPart=g?.parts?.find(p=>p.ref===t.ref);schInspect({kind:'component',ref:t.ref});
  // No checkpoint yet (lane just switched): queue the jump like the Jump box does.
  if(!g){schMarkSelected(t.ref);sch.selRef=selectedPartRef;jumpToComponent(t.ref);$('component-status').textContent='Loading checkpoint; will locate '+t.ref+'…';return}
  if(!geoPart){selectedPartRef=null;sch.selRef=null;schMarkSelected(t.ref);$('component-status').textContent=schOutside(t.ref);render();return}
  jumpToComponent(t.ref);return}
 if(t.loop){let h=sch.data.highlights.find(x=>x.id===t.loop);if(h)schToggleFocus(h);return}
 if(t.group){let h=schHighlights().find(x=>x.id===t.group);if(h){schToggleFocus(h);return}}
 if(sch.net){sch.net=null;schApplyClasses();render()}}

// ------------------------------------------------------------------ PCB -> schematic
function schMarkSelected(ref){let b=sch.built;if(!b)return;for(let el of schRoot.querySelectorAll('.p.sel'))el.classList.remove('sel');let d=ref&&b.L.M.R(ref),p=d&&b.idx.parts.get(d);if(p)p.g.classList.add('sel');return p}
function schSyncSelection(force){
 if(!sch.built)return;if(!force&&sch.selRef===selectedPartRef)return;sch.selRef=selectedPartRef;let p=schMarkSelected(selectedPartRef);
 if(p&&sch.mode!=='pcb'&&!force){let {x,y,k}=sch.v,w=schSvg.clientWidth/k,h=schSvg.clientHeight/k,[x0,y0,x1,y1]=p.halo;
  if(x0<x||y0<y+40/k||x1>x+w||y1>y+h){sch.v={k,x:(x0+x1)/2-w/2,y:(y0+y1)/2-h/2};schApplyView()}}
}
// The net the router is working on in this lane (lane.target, yellow on the PCB) glows in the schematic.
function schSyncTarget(){let t=phase==='live'&&!pinned?lane()?.target?.net||null:null,b=sch.built;if(!b||(sch.targetNet===t&&sch.targetKey===sch.key))return;
 for(let e of b.idx.nets.get(sch.targetNet)||[])e.classList.remove('routing');sch.targetNet=t;sch.targetKey=sch.key;for(let e of b.idx.nets.get(t)||[])e.classList.add('routing')}
function schSetHoverNet(net){if(sch.hoverNet===net)return;let b=sch.built;if(!b){sch.hoverNet=net;return}
 for(let e of b.idx.nets.get(sch.hoverNet)||[])e.classList.remove('hov');sch.hoverNet=net;for(let e of b.idx.nets.get(net)||[])e.classList.add('hov')}
const schMoveBefore=canvas.onpointermove;
canvas.onpointermove=e=>{schMoveBefore?.(e);if(sch.mode==='pcb'||!sch.built||drag)return;let h=$('hover');
 let first=h.style.display==='none'?'':h.textContent.split('\n')[0],m=first.match(/^(.+) · (?:F|B|In\d+)\.Cu$/)||first.match(/^\S+\.\S+ · (.+)$/);schSetHoverNet(m?m[1]:null)};

// ------------------------------------------------------------------ PCB highlight overlay (drawn only when asked)
function schPad(p,color){ctx.fillStyle=color;let [x,y]=screen(p.xy);
 if(p.polys?.length){ctx.beginPath();for(let poly of p.polys){poly.forEach((q,i)=>{let [sx,sy]=screen(q);i?ctx.lineTo(sx,sy):ctx.moveTo(sx,sy)});ctx.closePath()}ctx.fill();return}
 ctx.save();ctx.translate(x,y);ctx.rotate(-p.angle*Math.PI/180);if(p.shape==='circle'){ctx.beginPath();ctx.ellipse(0,0,p.size[0]*view.scale/2,p.size[1]*view.scale/2,0,0,Math.PI*2);ctx.fill()}else ctx.fillRect(-p.size[0]*view.scale/2,-p.size[1]*view.scale/2,p.size[0]*view.scale,p.size[1]*view.scale);ctx.restore()}
function schPcbOverlay(){
 let g=geo();if(!g||canvas.clientWidth<80||(!sch.focus&&!sch.net))return;
 const tracks=(nets,color,alpha)=>{ctx.globalAlpha=alpha;ctx.lineCap='round';for(let t of g.tracks||[])if(nets.has(t[0])&&layers.has(t[1])){ctx.strokeStyle=color;ctx.lineWidth=Math.max(1.5,t[4]*view.scale);ctx.beginPath();ctx.moveTo(...screen(t[2]));ctx.lineTo(...screen(t[3]));ctx.stroke()}ctx.globalAlpha=1};
 ctx.save();
 if(sch.focus){let F=new Set(sch.focus.refs),nets=new Set(sch.focus.nets||[]),col=sch.focus.style==='hot-loop'?SCH_HOT:sch.focus.style==='power-path'?SCH_PATH:sch.focus.style==='tier'?SCH_TIER[sch.focus.tier]:sch.focus.color||'#ffd166';
  ctx.save();ctx.setTransform(1,0,0,1,0,0);ctx.fillStyle='rgba(9,15,19,.62)';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.restore();
  tracks(nets,col,.55);let centers=new Map();
  for(let part of g.parts||[]){if(!F.has(part.ref))continue;for(let p of part.pads)if(p.layers.some(l=>layers.has(l)))schPad(p,p.net&&nets.has(p.net)?col:'#e6ebd8');
   let b=componentBounds(part);if(!b)continue;centers.set(part.ref,[(b[0]+b[2])/2,(b[1]+b[3])/2]);let a=screen([b[0]-.5,b[3]+.5]),c=screen([b[2]+.5,b[1]-.5]);
   ctx.strokeStyle=col;ctx.lineWidth=2;ctx.strokeRect(a[0],a[1],c[0]-a[0],c[1]-a[1]);ctx.fillStyle=col;ctx.font='bold 11px system-ui';ctx.fillText(part.ref,a[0],a[1]-4)}
  if(sch.focus.classes){let pts=sch.focus.classes.map(ms=>{let cs=ms.map(r=>centers.get(r)).filter(Boolean);return cs.length?[cs.reduce((s,p)=>s+p[0],0)/cs.length,cs.reduce((s,p)=>s+p[1],0)/cs.length]:null}).filter(Boolean);
   if(pts.length>1){ctx.strokeStyle=col;ctx.globalAlpha=.9;ctx.lineWidth=3;ctx.setLineDash([8,5]);ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(...screen(p)):ctx.moveTo(...screen(p)));ctx.closePath();ctx.stroke();ctx.setLineDash([]);ctx.globalAlpha=1}}
  let missing=sch.focus.refs.filter(r=>!(g.parts||[]).some(p=>p.ref===r)).length;
  schPcbLabel(sch.focus.label+(missing?` · ${missing} part(s) not on this board`:''),col,22)}
 if(sch.net){let nets=new Set([sch.net]);tracks(nets,'#ffe066',.95);for(let part of g.parts||[])for(let p of part.pads)if(p.net===sch.net&&p.layers.some(l=>layers.has(l)))schPad(p,'#ffe066');
  schPcbLabel('Net '+schNetLabel(sch.net),'#ffe066',sch.focus?44:22)}
 ctx.restore();
}
function schPcbLabel(text,col,y){ctx.font='bold 12px system-ui';let w=ctx.measureText(text).width;ctx.fillStyle='rgba(12,20,24,.85)';ctx.fillRect(8,y-15,w+14,21);ctx.fillStyle=col;ctx.fillText(text,15,y)}

// ------------------------------------------------------------------ render hook
const renderBeforeSchematic=render;
render=function(){
 renderBeforeSchematic();schPcbOverlay();window.YapnrNotes?.drawBadges?.(ctx,screen,geo(),laneId);
 if(sch.urlLane&&display()?.lanes?.[sch.urlLane]){let id=sch.urlLane;sch.urlLane=null;if(laneId!==id){select(id);return}}
 if(sch.urlRef&&geo()){let r=sch.urlRef;sch.urlRef=null;jumpToComponent(r)}
 if(sch.mode!=='pcb'){
  if(laneId!==sch.lane){sch.lane=laneId;sch.net=null;schEnsure()}else if(!sch.busy&&laneId)schEnsure();
  if(sch.built&&sch.key){schSyncSelection(false);schSyncTarget()}
 }else sch.lane=null;
};
(function schInit(){
 let q=sch.url,mode=q.get('view');if(!mode)try{mode=localStorage.getItem('pnr-view-mode')}catch(e){}
 if(q.get('lane'))sch.urlLane=q.get('lane');if(q.get('ref'))sch.urlRef=q.get('ref').toUpperCase();if(q.get('hl'))sch.urlHl=q.get('hl');
 if(q.get('scope')==='board')sch.scopePref='board';if(q.get('ghost')==='0')sch.noGhost=true;if(q.get('color')){sch.colorBy=q.get('color');sch.colorByUser=true}
 schStatus('');schSetMode(mode||'pcb',!q.get('view'));
})();

// ------------------------------------------------------------------ note badges (notes.js; optional)
// A sticky note on the top-left corner of each part (collapsed parallel parts: their representative) that has open,
// proposed or accepted notes; hover lists them, click opens them in the Notes tab, Shift+click adds the part to Ask.
const SCH_NOTE={proposed:'#ffd166',open:'#8ec5ff',accepted:'#9ee6d1'};
function schNoteBadges(){let b=sch.built;if(!b)return;schRoot.querySelector(':scope>.nbadges')?.remove();let N=window.YapnrNotes;if(!N?.badgesFor||N.badges?.()===false)return;
 let per=new Map();
 for(let x of N.badgesFor(laneId)){if(x.kind!=='component')continue;let d=b.L.M.R(x.ref),p=d&&b.idx.parts.get(d);if(!p)continue;
  let e=per.get(d);if(!e)per.set(d,e={p,ids:[],sts:new Set(),refs:new Set()});e.refs.add(x.ref);for(let id of x.ids)if(!e.ids.includes(id))e.ids.push(id);e.sts.add(x.status)}
 if(!per.size)return;let g=schEl('g',{class:'nbadges'},schRoot);
 for(let [ref,e] of per){let st=['proposed','open','accepted'].find(s=>e.sts.has(s)),s=2.2,f=.7,x=+(e.p.halo[0]-.5).toFixed(3),y=+(e.p.halo[1]-.9).toFixed(3);
  let bg=schEl('g',{class:'nbadge s-'+st},g);bg.dataset.ref=ref;bg.dataset.refs=[...e.refs].join(' ');bg.dataset.ids=e.ids.join(' ');
  schEl('path',{d:`M${x},${y}h${s-f}l${f},${f}v${s-f}h${-s}z`,fill:SCH_NOTE[st]},bg);schEl('path',{d:`M${x+s-f},${y}v${f}h${f}`,class:'nbf'},bg);
  schText(bg,x+s/2-.05,y+s*.76,e.ids.length>9?'9+':String(e.ids.length),'nbt','middle')}}
document.addEventListener('yapnr:notes',schNoteBadges);

// ------------------------------------------------------------------ Inspect / Ask (source.js, agent.js; both optional)
function schSem(net){let n=window.YapnrSource?.index?.()?.nets?.[net];return n&&n.low_info&&n.title&&n.title!==net?'\n'+n.title:''}
function schInspect(item){let S=window.YapnrSource;if(S?.inspect)S.inspect(item);else document.dispatchEvent(new CustomEvent('yapnr:select',{detail:item}))}
function schGroupItem(h){return {kind:'group',id:h.id,label:h.label,refs:[...new Set(h.refs||[])]}}
let schToastT=0;function schToast(msg){$('component-status').textContent=msg;let el=$('sch-toast');if(!el){el=document.createElement('div');el.id='sch-toast';$('sch-main').append(el)}el.textContent=msg;el.hidden=false;clearTimeout(schToastT);schToastT=setTimeout(()=>{el.hidden=true},2500)}
function schAsk(t){let A=window.YapnrAgent;if(!A){schToast('The Ask panel is not loaded.');return}
 let grp=t.group&&typeof t.group==='object'?t.group:t.loop?sch.data?.highlights?.find(x=>x.id===t.loop):typeof t.group==='string'?schHighlights().find(x=>x.id===t.group):null;
 let item=t.ref&&t.pin?{kind:'pad',ref:t.ref,pad:t.pin}:t.net?{kind:'net',name:t.net}:t.ref?{kind:'component',ref:t.ref}:grp?schGroupItem(grp):null;if(!item)return;
 A.addContext(item);window.YapnrDock?.badge?.('ask',true);
 schToast('Added '+(item.kind==='group'?item.label:item.kind==='net'?schNetLabel(item.name):item.kind==='pad'?item.ref+'.'+item.pad:item.ref)+' to the Ask context')}
// YapnrView.highlight (app.js) mirrored here: pink parts / pins / nets; pans the schematic when asked to frame.
function schAskApply(pan){let b=sch.built;if(!b)return;for(let el of schRoot.querySelectorAll('.ask'))el.classList.remove('ask');let H=viewHl;if(!H)return;
 let hit=new Set();const part=(r,mark)=>{let p=b.idx.parts.get(b.L.M.R(r));if(p){if(mark)p.g.classList.add('ask');hit.add(p)}return p};
 for(let r of H.refs)part(r,true);
 for(let x of H.pads){let i=x.indexOf('.'),p=part(x.slice(0,i),true);if(p)for(let el of p.g.querySelectorAll(`[data-pin="${CSS.escape(x.slice(i+1))}"]`))el.classList.add('ask')}
 for(let n of H.nets){for(let el of b.idx.nets.get(n)||[])el.classList.add('ask');for(let [r] of sch.data.nets.find(x=>x.name===n)?.pins||[])part(r,false)}
 if(!pan||sch.mode==='pcb'||!hit.size)return;
 let hs=[...hit].map(p=>p.halo),x0=Math.min(...hs.map(h=>h[0])),y0=Math.min(...hs.map(h=>h[1])),x1=Math.max(...hs.map(h=>h[2])),y1=Math.max(...hs.map(h=>h[3]));
 let {x,y,k}=sch.v,w=schSvg.clientWidth/k,h=schSvg.clientHeight/k;if(x0>=x&&y0>=y+40/k&&x1<=x+w&&y1<=y+h)return;
 if(x1-x0>w*.9||y1-y0>h*.8)schFitBox([x0-4,y0-6,x1+4,y1+4]);else{sch.v={k,x:(x0+x1)/2-w/2,y:(y0+y1)/2-h/2};schApplyView()}}
(function(){const V=window.YapnrView;if(!V)return;const hl=V.highlight,cl=V.clear;
 V.highlight=function(sel,opt){if(sch.focus||sch.net){sch.focus=null;sch.net=null;schApplyClasses();schLegend()}let r=hl.call(V,sel,opt);schAskApply(!opt||opt.frame!==false);return r};
 V.clear=function(keep){let r=cl.call(V,keep);schAskApply(false);return r}})();
