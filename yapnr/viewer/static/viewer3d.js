'use strict';
// 3D board view. Toolbar `3D` next to PCB | Split | Schematic; the pane stands alone or sits beside the PCB or the
// schematic (views 3d, 3d-pcb, 3d-sch: schematic.js schSetMode shows the panes and calls Yapnr3D.show).
// Extends app.js like schematic.js: shared globals (geo, laneId, phase, render, viewHl, HL, selectedPartRef, colors,
// componentBounds, padContains, inPoly, hoverText, askAdd, inspectItem, api) and window.YapnrView (mirrored here).
// three.js 0.186.1 is fetched pinned at build time (dist/vendor/three, import map in index.html) and loaded on first use.
// Board body, silkscreen, soldermask and footprint models: GET /api/3d -> /api/3d/glb/<key>, a headless KiCad GLB the
// server compacts into the PCB canvas frame (mm, x/y as the canvas, z up) with one node per ref. Copper, pads, vias and
// zones are built here from the geometry the PCB canvas draws: every item keeps its net (picking, highlighting) and the
// copper layers can be pulled apart (Explode). Without a GLB (export running / failed, placement previews) the board is
// a slab and parts are boxes from their pads; after a placement change the previous GLB stays for the parts that did
// not move (moved ones are boxes) until the new export is ready, and a running lane's live checkpoint auto-exports at
// most every 5 min. Picking: what is visible at the cursor (three.js rays for models, boxes and note badges; copper by
// intersecting each layer plane and testing a 2 mm grid of items, the board body occluding inner layers); Alt+click
// looks through models and boxes to the copper (the PCB canvas picks pads first).
(function(){
const D=document,Q=new URLSearchParams(location.search),V=()=>window.YapnrView,N3=()=>window.YapnrNotes;
const PREF_KEY='pnr-3d-prefs',EXPLODE_MM=10,CELL=2,BODY={z0:0,z1:1.51},CU=.035,LIVE_GAP=5*60e3,BADGE_PX=20,BADGE_MM=2.4;
// within a layer: zone < track < via < pad (mm above the layer plane, outward). Real offsets, not polygonOffset: a
// slope-scaled offset pulls inner layers through the 0.5 mm of board above them at iso angles or when zoomed out.
const ZOFF={zone:0,track:.01,via:.014,pad:.018,hl:.022};
const P=Object.assign({board:true,mask:false,silk:true,models:true,boxes:true,zones:true,vias:true,off:{},explode:0},(()=>{try{return JSON.parse(localStorage.getItem(PREF_KEY))||{}}catch(e){return {}}})());
for(let k of (Q.get('v3hide')||'').split(',').filter(Boolean))k in P&&typeof P[k]==='boolean'?P[k]=false:P.off[k]=true;
for(let k of (Q.get('v3show')||'').split(',').filter(Boolean))k in P&&typeof P[k]==='boolean'?P[k]=true:delete P.off[k];
if(Q.get('explode'))P.explode=Math.max(0,Math.min(100,+Q.get('explode')||0));
const marks={},mark=k=>{marks[k]??=Math.round(performance.now())};  // first-time timings (ms since navigation) for headless checks
const savePrefs=()=>{try{localStorage.setItem(PREF_KEY,JSON.stringify(P))}catch(e){}};
const S={visible:false,mode:null,boot:null,T:null,geo:undefined,sha:undefined,lane:undefined,req:0,pollT:0,status:null,key:null,glb:null,glbs:new Map(),
 layers:new Map(),stack:[],boxes:new Map(),models:new Map(),body:{...BODY},built:0,buildT:0,fitted:false,sel:undefined,hover:null,down:null,frameQ:false,
 stale:false,mv:null,solo:null,autoAt:{},toastHere:false,badgeBox:null};

// ------------------------------------------------------------------ DOM
const wrap=D.createElement('div');wrap.id='v3wrap';
wrap.innerHTML=`<div id="v3-main"><canvas id="v3-canvas"></canvas>
<div id="v3-bar"><span class="seg" id="v3-pair" title="The 3D view alone or beside another view"><button data-m="3d" title="3D view alone">Alone</button><button data-m="3d-pcb" title="3D beside the PCB">+ PCB</button><button data-m="3d-sch" title="3D beside the schematic">+ Sch</button></span>
<span class="seg" id="v3-cam"><button data-c="top" title="Look down on the top side">Top</button><button data-c="bottom" title="Look up at the bottom side (mirrored left-right, as when the board is flipped)">Bottom</button><button data-c="iso" title="Isometric">Iso</button><button data-c="fit" title="Fit the board, keeping the direction">Fit</button></span>
<label id="v3-explode-l" title="Pull the copper layers apart (up to ${EXPLODE_MM} mm between layers) to inspect inner-layer routing; the board body turns translucent">Explode <input id="v3-explode" type="range" min="0" max="100" step="1"></label>
<button id="v3-layers-btn" title="Layers and elements">Layers</button></div>
<div id="v3-layers" hidden></div><div id="v3-foot"><div id="v3-status"></div><div id="v3-hint">Drag: orbit · right-drag or Shift+drag: pan · wheel: zoom · click: inspect · Shift+click: add to Ask · Alt+click: copper under a part</div></div>
<div id="v3-toast" role="status" hidden></div><div id="v3-hover"></div></div>`;
$('views').append(wrap);
const btn3=D.createElement('button');btn3.dataset.v='3d';btn3.textContent='3D';btn3.title='3D board: KiCad models, copper by layer, exploded layers';$('viewswitch').append(btn3);
const pairPref=()=>{try{let m=localStorage.getItem('pnr-3d-pair');return /^3d(-pcb|-sch)?$/.test(m)?m:'3d'}catch(e){return '3d'}};
btn3.onclick=()=>schSetMode(/^3d/.test(D.body.dataset.view||'')?D.body.dataset.view:pairPref());
for(let b of $('v3-pair').children)b.onclick=()=>schSetMode(b.dataset.m);
for(let b of $('v3-cam').children)b.onclick=()=>camera(b.dataset.c);
$('v3-explode').value=P.explode;$('v3-explode').oninput=e=>{P.explode=+e.target.value;savePrefs();explode();draw()};
// Layers panel: opens below the toolbar's actual height (the bar wraps onto 2-3 rows in a half-width pane); closes on
// its x, the Layers button, Escape or a click elsewhere.
function placePanel(){let b=$('v3-bar'),p=$('v3-layers');p.style.top=(b.offsetTop+b.offsetHeight+6)+'px'}
function panel(open){let p=$('v3-layers');if(open)placePanel();p.hidden=!open;$('v3-layers-btn').classList.toggle('on',open);if(open&&S.badgeBox)S.badgeBox.checked=N3()?.badges?.()!==false}
$('v3-layers-btn').onclick=()=>panel($('v3-layers').hidden);
addEventListener('keydown',e=>{if(e.key==='Escape'&&S.visible&&!$('v3-layers').hidden&&!e.target?.closest?.('input[type=text],input:not([type]),textarea,select')){panel(false);e.stopImmediatePropagation()}},true);  // before app.js clears the highlight
D.addEventListener('pointerdown',e=>{if(!$('v3-layers').hidden&&!e.target.closest?.('#v3-layers,#v3-layers-btn'))panel(false)},true);
// status line; btn: true = Retry, or {label, title, fn}
function status(msg,kind,btn){S.stMsg=null;let el=$('v3-status');el.replaceChildren();el.className=kind||'';el.hidden=!msg;if(!msg)return;el.append(msg);
 if(btn===true)btn={label:'Retry',fn:()=>request3d(true)};if(btn){let b=D.createElement('button');b.textContent=btn.label;if(btn.title)b.title=btn.title;b.onclick=btn.fn;el.append(' ',b)}}
// a waiting status prefixed with what the stale GLB shows (redrawn by build when the geometry arrives)
function sstatus(msg,kind,btn){status(staleText()+msg+(S.stale?'':' · copper and part boxes meanwhile'),kind,btn);S.stMsg=[msg,kind,btn]}
let toastT=0;function toast3(msg){let el=$('v3-toast');el.textContent=msg;el.style.bottom=($('v3-foot').offsetHeight+20)+'px';el.hidden=false;clearTimeout(toastT);toastT=setTimeout(()=>{el.hidden=true},2600)}
function layersPanel(){let el=$('v3-layers');el.replaceChildren();
 let top=D.createElement('div'),x=D.createElement('button');top.className='v3-lh';top.append('Layers');x.className='v3-x';x.textContent='×';x.title='Close (Esc)';x.onclick=()=>panel(false);top.append(x);el.append(top);
 const row=(label,on,set,color,title,extra)=>{let l=D.createElement('label'),i=D.createElement('input');i.type='checkbox';i.checked=on;i.onchange=()=>{set(i.checked);savePrefs();vis();draw()};l.append(i,' '+label);if(color)l.style.color=color;if(title)l.title=title;
  if(extra){let w=D.createElement('div');w.className='v3-row';w.append(l,extra);el.append(w)}else el.append(l);return i};
 const head=t=>{let h=D.createElement('b');h.textContent=t;el.append(h)};
 head('Board');row('Board body',P.board,v=>P.board=v);row('Silkscreen',P.silk,v=>P.silk=v);row('Soldermask',P.mask,v=>P.mask=v,null,'KiCad soldermask faces (cover the copper, as on the real board)');
 head('Components');row('3D models',P.models,v=>P.models=v,null,'KiCad footprint models from the GLB export');row('Part boxes',P.boxes,v=>P.boxes=v,null,'Boxes from the pads: parts without a 3D model, or all parts while the models are off or exporting');
 head('Copper');for(let L of S.stack){let b=D.createElement('button'),on=S.solo===L;b.className='v3-solo'+(on?' on':'');b.textContent=on?'all':'only';
  b.title=on?'Show all layers again':`Only ${L}: hides the other copper layers, the board, parts, silkscreen and via barrels (inner-layer routing at a glance)`;b.onclick=e=>{e.preventDefault();solo(on?null:L)};
  row(L,!P.off[L],v=>{if(v)delete P.off[L];else P.off[L]=true},colors[L]||'#c9d3d8',null,b)}
 row('Zones',P.zones,v=>P.zones=v);row('Vias',P.vias,v=>P.vias=v);
 S.badgeBox=row('Note badges',N3()?.badges?.()!==false,v=>N3()?.badges?.(v),null,'Same switch as the PCB layer controls')}
function solo(L){S.solo=L&&S.stack.includes(L)?L:null;layersPanel();vis();draw()}

// ------------------------------------------------------------------ engine
let T,renderer,scene,cam,controls,loader,ray,G={};
function boot(){return S.boot||=(async()=>{
 status('Loading the 3D engine…');
 try{
  const [three,{OrbitControls},{GLTFLoader}]=await Promise.all([import('./vendor/three/build/three.module.js'),import('./vendor/three/examples/jsm/controls/OrbitControls.js'),import('./vendor/three/examples/jsm/loaders/GLTFLoader.js')]);
  T=S.T=three;const cv=$('v3-canvas');
  renderer=new T.WebGLRenderer({canvas:cv,antialias:true,preserveDrawingBuffer:Q.get('v3shot')==='1'});renderer.setPixelRatio(Math.min(2,devicePixelRatio||1));
  scene=new T.Scene();scene.background=new T.Color(0x0c1418);
  cam=new T.PerspectiveCamera(30,1,.05,5000);cam.up.set(0,0,1);
  controls=new OrbitControls(cam,cv);controls.screenSpacePanning=true;controls.zoomToCursor=true;controls.minDistance=1;controls.maxDistance=3000;controls.addEventListener('change',()=>{clip();pxAll();hideHover();draw()});
  controls.addEventListener('start',()=>{S.moved=true;if(!S.hinted){S.hinted=true;setTimeout(()=>$('v3-hint').classList.add('gone'),8000)}});
  scene.add(new T.HemisphereLight(0xeef4ff,0x2a3236,1.9));
  let d1=new T.DirectionalLight(0xffffff,2.3);d1.position.set(-60,-90,160);scene.add(d1);let d2=new T.DirectionalLight(0xdfe8ff,.9);d2.position.set(80,70,-140);scene.add(d2);
  for(let k of ['body','copper','top','bottom','barrels','hl'])scene.add(G[k]=new T.Group());
  loader=new GLTFLoader();ray=new T.Raycaster();
  // the pane changes size (Alone <-> beside a view, window): keep the board fitted until the user moves the camera
  new ResizeObserver(()=>{if(!$('v3-layers').hidden)placePanel();if(!S.moved&&S.fitted===true&&S.geo&&S.visible&&size())camera('fit');else draw()}).observe($('v3-main'));
  cv.addEventListener('pointerdown',e=>{S.down={x:e.clientX,y:e.clientY,b:e.button}});
  cv.addEventListener('pointerup',e=>{let d=S.down;S.down=null;if(!d||d.b!==0||Math.hypot(e.clientX-d.x,e.clientY-d.y)>4)return;click(e)});
  cv.addEventListener('pointermove',e=>{if(e.buttons){hideHover();return}S.hoverEv=e;if(!S.hoverQ){S.hoverQ=true;setTimeout(()=>{S.hoverQ=false;hover(S.hoverEv)},60)}});
  cv.addEventListener('pointerleave',hideHover);
  status('');mark('engine');return true;
 }catch(e){status('3D view unavailable: '+(e?.message||e)+(/webgl/i.test(String(e?.message||e))?' (WebGL is off in this browser)':''),'err');return false}})()}
let drawQ=false;
function draw(){if(drawQ||!renderer||!S.visible)return;drawQ=true;requestAnimationFrame(()=>{drawQ=false;if(!S.visible||!size())return;renderer.render(scene,cam);S.frames=(S.frames||0)+1;if(S.built)mark('frame');if(S.glb)mark('glbFrame')})}
function size(){let cv=$('v3-canvas'),w=cv.clientWidth,h=cv.clientHeight;if(w<4||h<4)return false;let s=renderer.getSize(new T.Vector2());if(s.x!==w||s.y!==h){renderer.setSize(w,h,false);cam.aspect=w/h;cam.updateProjectionMatrix();pxAll()}return true}
// depth precision follows the zoom: near = distance / 150 (fixed near planes left ~50 um per depth step at 550 mm, where
// the 35 um between F.Cu and the board top z-fought)
function clip(){if(!cam)return;let d=cam.position.distanceTo(controls.target),R=S.geo?Math.hypot(S.geo.width,S.geo.height):100;cam.near=Math.max(.01,d/150);cam.far=d*3+R*4+100;cam.updateProjectionMatrix()}
// screen-space sprites: the selection label keeps its size in CSS pixels at any zoom; a note badge is BADGE_MM wide on the
// board but never more than BADGE_PX on screen (zoomed in it stays a small tag, zoomed out it shrinks with the board)
function pxScale(px){return px/Math.max(1,renderer.getSize(S.v2||=new T.Vector2()).y)*2*Math.tan(cam.fov*Math.PI/360)}  // the drawn size: stays right while the pane is hidden
function pxSprite(s){let [w,h]=s.userData.px,k=1;if(s.userData.mm){let d=cam.position.distanceTo(s.getWorldPosition(S.v3||=new T.Vector3()));k=Math.min(1,s.userData.mm/Math.max(1e-6,d)/pxScale(w))}s.scale.set(pxScale(w)*k,pxScale(h)*k,1)}
function pxAll(){for(let g of [G.top,G.bottom])for(let o of g?.children||[])if(o.userData.px)pxSprite(o)}

// ------------------------------------------------------------------ geometry -> copper meshes
const C=hex=>new T.Color(hex);
function stackOf(g){let s=new Set();for(let t of g.tracks||[])s.add(t[1]);for(let z of g.zones||[])s.add(z.layer);
 return ['F.Cu',...[...s].filter(n=>/^In\d+\.Cu$/.test(n)).sort(natural),'B.Cu']}
function z0Of(L){let n=S.stack.length,i=S.stack.indexOf(L),{z0,z1}=S.body;return i===0?z1+CU:i===n-1?z0-CU:z1-(z1-z0)*i/(n-1)}
function Bld(){return {p:[],c:[],i:[],n:0,items:[]}}
function push(b,x,y,z,col){b.p.push(x,y,z);b.c.push(col.r,col.g,col.b);return b.n++}
function discInto(b,x,y,r,z,col,seg=12){let c0=push(b,x,y,z,col);for(let k=0;k<seg;k++){let a=k/seg*Math.PI*2;push(b,x+r*Math.cos(a),y+r*Math.sin(a),z,col)}for(let k=0;k<seg;k++)b.i.push(c0,c0+1+k,c0+1+(k+1)%seg)}
function polyInto(b,contour,holes,z,col){let c=contour.map(p=>new T.Vector2(p[0],p[1])),h=holes.map(hh=>hh.map(p=>new T.Vector2(p[0],p[1])));let f;try{f=T.ShapeUtils.triangulateShape(c,h)}catch(e){return}
 let base=b.n;for(let p of c.concat(...h))push(b,p.x,p.y,z,col);for(let t of f)b.i.push(base+t[0],base+t[1],base+t[2])}
function segInto(b,a,e,w,z,col){let dx=e[0]-a[0],dy=e[1]-a[1],l=Math.hypot(dx,dy),r=w/2;if(l>1e-6){let nx=-dy/l*r,ny=dx/l*r,k=b.n;push(b,a[0]+nx,a[1]+ny,z,col);push(b,a[0]-nx,a[1]-ny,z,col);push(b,e[0]-nx,e[1]-ny,z,col);push(b,e[0]+nx,e[1]+ny,z,col);b.i.push(k,k+1,k+2,k,k+2,k+3)}
 discInto(b,a[0],a[1],r,z,col,8);discInto(b,e[0],e[1],r,z,col,8)}
function item(b,it,fn){it.v0=b.n;it.i0=b.i.length;fn();it.v1=b.n;it.i1=b.i.length;b.items.push(it);return it}
function meshOf(b,mat,kind,layer,sg){let g=new T.BufferGeometry(),col=new Float32Array(b.c);g.setAttribute('position',new T.Float32BufferAttribute(b.p,3));g.setAttribute('color',new T.BufferAttribute(col,3));
 let nrm=new Float32Array(b.p.length);for(let k=2;k<nrm.length;k+=3)nrm[k]=1;g.setAttribute('normal',new T.BufferAttribute(nrm,3));g.setIndex(b.n>65535?new T.Uint32BufferAttribute(b.i,1):new T.Uint16BufferAttribute(b.i,1));
 let m=new T.Mesh(g,mat);m.userData={own:true,kind,layer,sg:sg||1,items:b.items,base:col.slice()};for(let it of b.items)it.mesh=m;return m}
function cuMat(){return new T.MeshStandardMaterial({vertexColors:true,side:T.DoubleSide,metalness:.05,roughness:.7})}
function build(){let g=S.geo,t0=performance.now();S.built=g?t0:0;clearTimeout(S.buildT);S.builds=(S.builds||0)+1;if(g)mark('build');
 for(let k of ['copper','barrels','top','bottom','hl'])for(let o of [...G[k].children])if(k==='copper'||k==='barrels'||o.userData.own||o.userData.box||o.userData.badge||o.userData.selbox){G[k].remove(o);o.traverse(x=>{x.geometry?.dispose();if(x.userData.own)x.material?.dispose?.()})}
 for(let o of [...G.body.children])if(o.userData.slab){G.body.remove(o);o.geometry.dispose()}
 S.layers.clear();S.boxes.clear();
 if(!g){S.stack=[];S.panelKey=null;layersPanel();status(!laneId?'Waiting for experiment events…':V()?.boardSha?.()?'Loading the board geometry…':'No board checkpoint for this lane yet (placement probes only so far).');draw();return}
 S.stack=stackOf(g);if(S.stack.join()!==S.panelKey){S.panelKey=S.stack.join();layersPanel()}  // live rebuilds leave an open panel alone
 const padCol=C('#cdd2be'),viaCol=C('#b3c1c9');
 for(let [i,L] of S.stack.entries()){let sg=i===S.stack.length-1?-1:1,base=C(colors[L]||'#c9a27a'),zc=base.clone().multiplyScalar(.5),grp=new T.Group(),bz=Bld(),bc=Bld(),bv=Bld();grp.userData={own:false,layer:L};
  for(let z of g.zones||[])if(z.layer===L&&z.paths?.length)item(bz,{kind:'zone',net:z.net,obj:z,bb:bbox(z.paths[0])},()=>polyInto(bz,z.paths[0],z.paths.slice(1),ZOFF.zone*sg,zc));
  for(let t of g.tracks||[])if(t[1]===L)item(bc,{kind:'track',net:t[0],obj:t,bb:[Math.min(t[2][0],t[3][0])-t[4]/2,Math.min(t[2][1],t[3][1])-t[4]/2,Math.max(t[2][0],t[3][0])+t[4]/2,Math.max(t[2][1],t[3][1])+t[4]/2]},()=>segInto(bc,t[2],t[3],t[4],ZOFF.track*sg,base));
  for(let part of g.parts||[])for(let p of part.pads||[])if(p.layers?.includes(L)){let it={kind:'pad',ref:part.ref,pad:p.number,net:p.net,obj:p,part};
   let pz=ZOFF.pad*sg;item(bc,it,()=>{if(p.polys?.length)for(let poly of p.polys)polyInto(bc,poly,[],pz,padCol);else if(p.shape==='circle')discInto(bc,p.xy[0],p.xy[1],p.size[0]/2,pz,padCol,16);else{let a=(p.angle||0)*Math.PI/180,w=p.size[0]/2,h=p.size[1]/2,cs=Math.cos(a),sn=Math.sin(a);polyInto(bc,[[-w,-h],[w,-h],[w,h],[-w,h]].map(([x,y])=>[p.xy[0]+x*cs-y*sn,p.xy[1]+x*sn+y*cs]),[],pz,padCol)}});
   let pts=p.polys?.length?p.polys.flat():[[p.xy[0]-p.size[0],p.xy[1]-p.size[1]],[p.xy[0]+p.size[0],p.xy[1]+p.size[1]]];it.bb=bbox(pts)}
  for(let v of g.vias||[])item(bv,{kind:'via',net:v.net,obj:v,bb:[v.xy[0]-v.diameter/2,v.xy[1]-v.diameter/2,v.xy[0]+v.diameter/2,v.xy[1]+v.diameter/2]},()=>discInto(bv,v.xy[0],v.xy[1],v.diameter/2,ZOFF.via*sg,viaCol,12));
  let ms={};for(let [k,b] of [['zones',bz],['copper',bc],['vias',bv]])if(b.n){ms[k]=meshOf(b,cuMat(),k,L,sg);grp.add(ms[k])}
  let items=[...bc.items.filter(x=>x.kind==='pad'),...bv.items,...bc.items.filter(x=>x.kind==='track'),...bz.items];
  let ol=new T.LineLoop(new T.BufferGeometry().setFromPoints([[0,0],[g.width,0],[g.width,g.height],[0,g.height]].map(p=>new T.Vector3(p[0],p[1],0))),new T.LineBasicMaterial({color:base,transparent:true,opacity:.7}));ol.userData={own:true,outline:true};grp.add(ol);
  S.layers.set(L,{group:grp,meshes:ms,z0:z0Of(L),grid:grid(items,g),outline:ol});G.copper.add(grp)}
 // via barrels: unit height, stretched between the outer layers (explode)
 let bb=Bld();for(let v of g.vias||[])item(bb,{kind:'via',net:v.net,obj:v},()=>{let r=Math.max(.1,v.diameter*.3),k=bb.n,n=10;for(let j=0;j<n;j++){let a=j/n*Math.PI*2;push(bb,v.xy[0]+r*Math.cos(a),v.xy[1]+r*Math.sin(a),0,viaCol);push(bb,v.xy[0]+r*Math.cos(a),v.xy[1]+r*Math.sin(a),1,viaCol)}for(let j=0;j<n;j++){let a=k+2*j,c=k+2*((j+1)%n);bb.i.push(a,c,a+1,c,c+1,a+1)}});
 if(bb.n){let m=meshOf(bb,new T.MeshStandardMaterial({vertexColors:true,side:T.DoubleSide,metalness:.4,roughness:.45}),'barrels',null,1);m.geometry.computeVertexNormals();G.barrels.add(m)}
 // board slab (until the GLB body arrives) and part boxes
 const M=S.mats||={slab:new T.MeshStandardMaterial({color:0x2c4a36,roughness:.8,metalness:0}),box:new T.MeshStandardMaterial({color:0x56666e,roughness:.7,metalness:.1}),nomodel:new T.MeshStandardMaterial({color:0x8fa3ad,roughness:.7,metalness:.1,transparent:true,opacity:.3,depthWrite:false}),edge:new T.LineBasicMaterial({color:0x9db3bd})};  // shared across rebuilds
 let slab=new T.Mesh(new T.BoxGeometry(g.width,g.height,S.body.z1-S.body.z0),M.slab);slab.position.set(g.width/2,g.height/2,(S.body.z0+S.body.z1)/2);slab.userData={slab:true};G.body.add(slab);
 const boxMat=M.box,edgeMat=M.edge;
 for(let part of g.parts||[]){let b=componentBounds(part);if(!b)continue;let smd=new Set((part.pads||[]).filter(p=>p.layers?.length===1).map(p=>p.layers[0])),bottom=smd.has('B.Cu')&&!smd.has('F.Cu');
  let w=Math.max(.2,b[2]-b[0]),d=Math.max(.2,b[3]-b[1]),h=Math.min(1.6,Math.max(.4,.3+.22*Math.min(w,d))),geo=new T.BoxGeometry(w,d,h),m=new T.Mesh(geo,boxMat),e=new T.LineSegments(new T.EdgesGeometry(geo),edgeMat),grp=new T.Group();
  grp.add(m,e);grp.position.set((b[0]+b[2])/2,(b[1]+b[3])/2,bottom?S.body.z0-CU-h/2:S.body.z1+CU+h/2);grp.userData={box:true,ref:part.ref,side:bottom?'B':'F',bbox:[b[0],b[1],bottom?S.body.z0-CU-h:S.body.z1+CU,b[2],b[3],bottom?S.body.z0-CU:S.body.z1+CU+h]};m.userData={ref:part.ref,m0:boxMat};
  S.boxes.set(part.ref,grp);(bottom?G.bottom:G.top).add(grp)}
 if(S.glb&&S.stale&&!sameOutline())detach();if(S.glb){attach(S.glb);S.mv=null}badges();vis();explode();hl(false);selection();if(S.stMsg)sstatus(...S.stMsg);
 if(S.fitted!==true){camera(S.fitted||Q.get('cam')||'iso');S.fitted=true}
 S.buildMs=Math.round(performance.now()-t0);draw()}
function bbox(pts){let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;for(let p of pts){if(p[0]<x0)x0=p[0];if(p[0]>x1)x1=p[0];if(p[1]<y0)y0=p[1];if(p[1]>y1)y1=p[1]}return [x0,y0,x1,y1]}
function grid(items,g){let nx=Math.max(1,Math.ceil(g.width/CELL)),ny=Math.max(1,Math.ceil(g.height/CELL)),cells=new Map();
 items.forEach((it,k)=>{if(!it.bb)return;let a=Math.max(0,Math.floor(it.bb[0]/CELL)),b=Math.min(nx-1,Math.floor(it.bb[2]/CELL)),c=Math.max(0,Math.floor(it.bb[1]/CELL)),d=Math.min(ny-1,Math.floor(it.bb[3]/CELL));
  for(let x=a;x<=b;x++)for(let y=c;y<=d;y++){let key=x*100000+y;(cells.get(key)||cells.set(key,[]).get(key)).push(k)}});
 return {items,cells,nx,ny}}

// ------------------------------------------------------------------ GLB (board body, silk, mask, models)
// First ask with peek=1 (never enqueues): a board shown only briefly (lane / phase flicking, a viewer replaying its
// events after a restart, the default lane before the URL's) costs no export; enqueue once it stayed ~2 s. A running
// lane's live checkpoint changes every few minutes: auto-export it at most every LIVE_GAP per lane (the pane offers
// "Export now" meanwhile), and keep the previous GLB for the parts that did not move until the new one is ready.
async function request3d(retry,enqueue){let my=++S.req,sha=S.sha;clearTimeout(S.pollT);
 if(!sha){S.status=null;detach();if(S.geo)status('This checkpoint has no native KiCad board (placement preview): copper and part boxes only.');return}  // no geometry: build() said why
 let r;try{r=await api(`/api/3d?lane=${encodeURIComponent(laneId||'')}&phase=${encodeURIComponent(phase)}&sha=${sha}`+(retry?'&retry=1':enqueue||S.enq===sha?'':'&peek=1'))}
 catch(e){if(my===S.req){status('3D export status failed: '+e.message,'err');S.pollT=setTimeout(()=>S.visible&&my===S.req&&request3d(),6000)}return}
 if(my!==S.req)return;S.status=r;
 const enq=()=>{if(phase==='live')S.autoAt[laneId]=Date.now();request3d(false,true)},now=()=>S.visible&&my===S.req&&enq();
 if(r.status==='idle'){keepStale(r);let wait=phase==='live'?LIVE_GAP-(Date.now()-(S.autoAt[laneId]||-1e15)):0;
  if(wait>0){sstatus(`Live checkpoint: KiCad 3D models refresh at most every ${LIVE_GAP/60e3} min per lane (${Math.ceil(wait/60e3)} min left)`,'busy',{label:'Export now',title:'Export the 3D models of this placement now (~30 s, ~2 GB)',fn:enq});S.pollT=setTimeout(()=>S.visible&&my===S.req&&request3d(),wait+500);return}
  sstatus('New placement: the KiCad 3D export starts in a moment','busy');S.pollT=setTimeout(now,2000);return}
 if(r.status==='queued'||r.status==='exporting')S.enq=sha;
 if(r.status==='ready'){if(r.key===S.key){if(S.stale){S.stale=false;S.mv=null;vis();selection();badges();draw()}return status(readyText(r))}return load(r,my)}
 if(r.status==='queued'||r.status==='exporting'){keepStale(r);
  sstatus((r.status==='queued'?'3D models queued'+(r.ahead?` (${r.ahead} ahead)`:''):'KiCad is exporting the 3D models'+(r.stage==='compact'?' (compacting)':''))+` · ${Math.round(r.elapsed||0)} s`+(r.expected?` of ~${Math.round(r.expected)} s`:''),'busy');
  S.pollT=setTimeout(()=>S.visible&&my===S.req&&request3d(),1500);return}
 detach();status(r.status==='failed'?'3D models unavailable: '+(r.error||'export failed')+' · showing copper and part boxes':'3D models: '+(r.error||'unavailable'),'err',r.status==='failed')}
function readyText(r){let m=r.meta||{},nm=(m.no_model||[]);return `KiCad models · ${Object.keys(m.refs||{}).length} parts`+(nm.length?` · ${nm.length} as boxes (${nm.slice(0,4).join(', ')}${nm.length>4?'…':''})`:'')}
// A new placement of the same board (outline unchanged): keep the current GLB, its models only for parts that did not move.
// (Geometry still loading, e.g. right after a lane switch: decided again when it arrives, in build.)
const sameOutline=()=>{let b=S.glb?.meta?.board,g=S.geo;return !!(b&&g&&Math.abs(b.width-g.width)<=.05&&Math.abs(b.height-g.height)<=.05)};
function keepStale(r){if(!S.glb||r.key===S.key)return;if(S.geo&&!sameOutline()){detach();return}
 if(!S.stale){S.stale=true;S.mv=null;vis();selection();badges();draw()}}
function staleText(){let m=S.stale&&movedSet();if(!m)return '';let n=[...S.models.keys()].filter(r=>!m.has(r)).length;
 return !m.size?'Placement unchanged apart from routing · ':!n?'Another placement: part boxes and the previous board body · ':`${m.size} moved part${m.size>1?'s':''} as boxes (${[...m].slice(0,3).join(', ')}${m.size>3?'…':''}), ${n} models from the previous export · `}
const pose=p=>[p.xy?.map(v=>(+v).toFixed(3)),p.pads?.[0]?.xy?.map(v=>(+v).toFixed(3)),p.pads?.[0]?.layers?.[0]].join();
function poses(g){let m=new Map();for(let p of g.parts||[])m.set(p.ref,pose(p));return m}
// refs whose model is out of place while a stale GLB is shown (all of them if its placement is unknown), else null
function movedSet(){if(!S.stale||!S.glb||!S.geo)return null;if(S.mv?.g===S.geo&&S.mv.e===S.glb)return S.mv.s;let s=new Set(),P0=S.glb.pose;
 for(let p of S.geo.parts||[])if(!P0||P0.get(p.ref)!==pose(p))s.add(p.ref);S.mv={g:S.geo,e:S.glb,s};return s}
async function load(r,my){let e=S.glbs.get(r.key);
 if(!e){status(staleText()+`Loading the 3D models (${((r.meta?.gz_bytes||r.meta?.bytes||0)/1e6).toFixed(1)} MB)…`,'busy');
  mark('glbFetch');try{let g=await loader.loadAsync(r.url);mark('glbParsed');e=prep(r,g.scene)}catch(err){if(my===S.req)status('Loading the 3D models failed: '+(err?.message||err),'err',true);return}
  S.glbs.set(r.key,e);for(let k of S.glbs.keys()){if(S.glbs.size<=3)break;if(k!==r.key&&k!==S.key){dispose(S.glbs.get(k));S.glbs.delete(k)}}}
 if(my!==S.req)return;e.sha=r.sha;
 let b=r.meta?.board;if(b&&(Math.abs(b.z0-S.body.z0)>1e-3||Math.abs(b.z1-S.body.z1)>1e-3)){detach();S.body={z0:b.z0,z1:b.z1};S.glb=e;S.key=r.key;build()}else attach(e);
 status(readyText(r));vis();explode();hl(false);selection();badges();draw()}
function prep(r,sceneObj){let root=sceneObj.children[0]||sceneObj,objs=[...root.children],e={key:r.key,meta:r.meta,objs,meshes:[]};
 for(let o of objs){let u=o.userData||{};o.traverse(m=>{if(!m.isMesh)return;m.userData.ref=u.ref;m.userData.kind=u.kind;m.userData.m0=m.material;e.meshes.push(m);
  if(u.kind==='board'){m.material.transparent=false;m.material.opacity=1;m.material.depthWrite=true}
  if(u.kind==='silk'||u.kind==='mask'){m.material.depthWrite=false;m.renderOrder=2}})}
 return e}
function attach(e){if(S.glb&&S.glb!==e)detach();S.glb=e;S.key=e.key;S.models.clear();if(!e.pose&&S.geo&&e.sha&&e.sha===S.sha)e.pose=poses(S.geo);
 for(let o of e.objs){let u=o.userData||{},top=u.side!=='B';if(u.kind==='component'){S.models.set(u.ref,o);(top?G.top:G.bottom).add(o)}else if(u.kind==='board')G.body.add(o);else(top?G.top:G.bottom).add(o)}}
function detach(){S.stale=false;S.mv=null;let e=S.glb;if(!e)return;for(let o of e.objs)o.parent?.remove(o);S.glb=null;S.key=null;S.models.clear();vis();draw()}
function dispose(e){let ids=new Set();for(let m of e.meshes){m.geometry.dispose();if(m.userData.m0){ids.add(m.userData.m0.uuid);m.userData.m0.dispose?.()}}
 for(let [k,v] of variants)if(ids.has(k.slice(0,36))){v.dispose();variants.delete(k)}}

// ------------------------------------------------------------------ visibility, explode, highlight, selection
const effVisible=o=>{for(let x=o;x;x=x.parent)if(!x.visible)return false;return true};
const modelShown=ref=>P.models&&S.models.has(ref)&&!movedSet()?.has(ref)&&!S.solo;
function vis(){let glb=!!S.glb,so=S.solo,mv=movedSet(),badgesOn=N3()?.badges?.()!==false&&!so;
 // exploded: no board body (it would sit between the inner layers); each layer shows the board outline instead
 let open=P.explode>0,body=P.board&&!open&&!so;for(let o of G.body.children)o.visible=body&&(o.userData.slab?!glb:true);
 // a stale GLB (previous placement): its silkscreen / soldermask would be off under the moved parts
 for(let o of S.glb?.objs||[]){let k=o.userData.kind;o.visible=k==='silk'?P.silk&&!so&&!mv?.size:k==='mask'?P.mask&&!so&&!mv?.size:k==='component'?modelShown(o.userData.ref):k==='board'?body:!so}
 for(let [L,l] of S.layers)if(l.outline)l.outline.visible=open||so===L;
 // a part box is a stand-in body while the models export (solid), or for a part that moved since the shown GLB (solid);
 // next to the KiCad models it marks a part KiCad has no model for (TP1: a pogo-pad array), translucent so its pads show
 let swap=false;for(let [ref,b] of S.boxes){b.visible=P.boxes&&!so&&!modelShown(ref);let m=b.children[0],m0=glb&&P.models&&!S.models.has(ref)?S.mats.nomodel:S.mats.box;if(m.userData.m0!==m0){m.userData.m0=m0;swap=true}}
 if(swap)queueMicrotask(()=>{if(curHl())hl(false);else paintParts();draw()});
 for(let [L,l] of S.layers){l.group.visible=so?so===L:!P.off[L];if(l.meshes.zones)l.meshes.zones.visible=P.zones;if(l.meshes.vias)l.meshes.vias.visible=P.vias}
 for(let o of G.barrels.children)o.visible=P.vias&&!so&&S.stack.some(L=>!P.off[L]);
 for(let g of [G.top,G.bottom])for(let o of g.children)if(o.userData.badge)o.visible=badgesOn}
function layerZ(L){let n=S.stack.length,i=S.stack.indexOf(L),s=P.explode/100*EXPLODE_MM;return z0Of(L)+((n-1)/2-i)*s}
function explode(){if(!T)return;let n=S.stack.length,s=P.explode/100*EXPLODE_MM,lift=(n-1)/2*s;
 for(let [L,l] of S.layers)l.group.position.z=layerZ(L);
 G.top.position.z=lift;G.bottom.position.z=-lift;
 let zb=S.stack.length?layerZ(S.stack[n-1]):0,zf=S.stack.length?layerZ(S.stack[0]):1;for(let o of G.barrels.children){o.position.z=zb;o.scale.z=Math.max(1e-3,zf-zb)}
 let open=P.explode>0;
 // exploded: planes turn faint (a GND pour would hide the layer below it) and the via barrels, stretched over the whole
 // stack, turn into a light haze, so inner-layer routing reads through them
 for(let [,l] of S.layers){let m=l.meshes.zones?.material;if(m&&m.transparent!==open){m.transparent=open;m.opacity=open?.12:1;m.depthWrite=!open;m.needsUpdate=true}}
 for(let o of G.barrels.children){let m=o.material;if(m.transparent!==open){m.transparent=open;m.opacity=open?.22:1;m.depthWrite=!open;m.needsUpdate=true}}
 vis();$('v3-explode-l').classList.toggle('on',open)}
const variants=new Map();
// material variants: hl (emissive), sel (selected part: white glow), dim (ghost), body (darker: linear colour under strong
// lights, .12 reads ~40 %)
function variant(m0,kind,col){let k=m0.uuid+kind+(col||''),m=variants.get(k);if(!m){m=m0.clone();if(kind==='hl'){m.emissive=C(col||HL);m.emissiveIntensity=.7}else if(kind==='sel'){m.emissive=C('#ffffff');m.emissiveIntensity=.32}else if(kind==='body')m.color?.multiplyScalar(.12);else{m.transparent=true;m.opacity=Math.min(m0.opacity,.14);m.depthWrite=false}variants.set(k,m)}return m}
// The active highlight: YapnrView's (pink) or, without one, the schematic's own net / group focus (schematic.js sch.net in
// yellow, sch.focus in its colour), which the PCB mirrors in its overlay too.
function schKey(){return typeof sch!=='undefined'?(sch.net||'')+'|'+(sch.focus?.id||''):''}
function curHl(){if(typeof viewHl!=='undefined'&&viewHl)return {H:viewHl,col:HL};let s=typeof sch!=='undefined'?sch:null,f=s?.focus;if(!s||!(s.net||f))return null;
 let nets=new Set(f?.nets||[]);if(s.net)nets.add(s.net);
 return {H:{refs:new Set(f?.refs||[]),nets,pads:new Set(),region:null},col:s.net?'#ffe066':f.style==='hot-loop'?SCH_HOT:f.style==='power-path'?SCH_PATH:f.style==='tier'?SCH_TIER[f.tier]:f.color||'#ffd166'}}
function partHitFn(H){if(!H)return ()=>false;let padRefs=new Set([...H.pads].map(p=>p.slice(0,p.lastIndexOf('.'))));return ref=>H.refs.has(ref)||padRefs.has(ref)}
// models and boxes: highlighted (glow in the highlight colour), others ghosted; without a highlight the selected part glows white
function paintParts(){let cur=curHl(),H=cur?.H||null,col=cur?.col||HL,on=!!H,hit=partHitFn(H),sel=selectedPartRef;
 const paint=(o,ref)=>o.traverse(m=>{if(!m.isMesh)return;let m0=m.userData.m0||(m.userData.m0=m.material);m.material=on?(hit(ref)?variant(m0,'hl',col):variant(m0,'dim')):ref===sel?variant(m0,'sel'):m0});
 for(let [ref,o] of S.models)paint(o,ref);for(let [ref,o] of S.boxes)paint(o.children[0],ref);
 for(let g of [G.top,G.bottom])for(let o of g.children)if(o.userData.badge){o.material.opacity=on&&!hit(o.userData.ref)?.22:1}}  // badges of other parts fade too
function hl(frame){if(!T)return;S.hk=schKey();let cur=curHl(),H=cur?.H||null,col=cur?.col||HL,on=!!H,dim=.07;
 // copper: the rest dimmed through the vertex colours, the highlighted items redrawn on top unlit (bright at any angle)
 for(let [,l] of S.layers)for(let m of Object.values(l.meshes))recolor(m,H,dim,col);for(let m of G.barrels.children)recolor(m,H,dim,col);
 paintParts();
 // board body, silkscreen and soldermask darken too (as the PCB canvas dims under a highlight): the highlight stands out
 const body=(o,k)=>o.traverse(m=>{if(!m.isMesh)return;let m0=m.userData.m0||(m.userData.m0=m.material);m.material=on?variant(m0,k):m0});
 for(let o of G.body.children)body(o,'body');for(let o of S.glb?.objs||[])if(o.userData.kind==='silk'||o.userData.kind==='mask')body(o,'dim');
 for(let o of [...G.hl.children])if(o.userData.own){G.hl.remove(o);o.geometry.dispose();o.material.dispose()}
 if(H?.region){let [x0,y0,x1,y1]=H.region,z=S.body.z1+CU+.05+((S.stack.length-1)/2)*P.explode/100*EXPLODE_MM,g=new T.BufferGeometry().setFromPoints([[x0,y0],[x1,y0],[x1,y1],[x0,y1]].map(p=>new T.Vector3(p[0],p[1],z)));
  let l=new T.LineLoop(g,new T.LineBasicMaterial({color:0xffb347}));l.userData.own=true;G.hl.add(l)}
 if(frame&&on&&S.visible)frameOn(H);draw()}
const hlMats=new Map();
// Highlighted tracks, pads and vias: the same triangles again, unlit, drawn after the lit copper (depth LessEqual plus a
// one-step constant bias, no slope-scaled offset: that pulled a highlighted In1.Cu pour through the board top).
// Highlighted zones are not filled (as on the PCB canvas): their fill keeps its colour and gets an outline.
function recolor(m,H,dim,hc){let col=m.geometry.attributes.color,a=col.array,base=m.userData.base,pos=m.geometry.attributes.position.array,idx=m.geometry.index.array,out=[],lines=[],zl=[];
 for(let o of [...m.children])if(o.userData.hlo){m.remove(o);o.geometry.dispose()}
 if(!H){a.set(base);col.needsUpdate=true;return}
 for(let it of m.userData.items){let hit=(it.net&&H.nets.has(it.net))||(it.kind==='pad'&&(H.pads.has(it.ref+'.'+it.pad)||H.refs.has(it.ref)));
  for(let v=it.v0*3;v<it.v1*3;v+=3){let k=hit?1:dim;a[v]=base[v]*k;a[v+1]=base[v+1]*k;a[v+2]=base[v+2]*k}
  if(!hit)continue;
  if(it.kind==='zone'){let z=pos[it.v0*3+2]+(ZOFF.hl-ZOFF.zone)*m.userData.sg;for(let path of it.obj.paths)for(let i=0;i<path.length;i++){let p=path[i],q=path[(i+1)%path.length];zl.push(p[0],p[1],z,q[0],q[1],z)}continue}
  for(let k=it.i0;k<it.i1;k++){let v=idx[k]*3;out.push(pos[v],pos[v+1],pos[v+2])}
  if(it.kind==='track'){let t=it.obj,z=pos[it.v0*3+2];lines.push(t[2][0],t[2][1],z,t[3][0],t[3][1],z)}}  // centre lines: a 0.2 mm trace stays visible from afar
 col.needsUpdate=true;
 let M=hlMats.get(hc);if(!M)hlMats.set(hc,M={line:new T.LineBasicMaterial({color:C(hc),depthTest:false,transparent:true,opacity:.85}),zone:new T.LineBasicMaterial({color:C(hc),transparent:true,opacity:.9}),mesh:new T.MeshBasicMaterial({color:C(hc),side:T.DoubleSide,polygonOffset:true,polygonOffsetFactor:0,polygonOffsetUnits:-1})});
 const add=(g,mat,ro)=>{let o=g.isLine?g:new T.Mesh(g,mat);o.userData={hlo:true};o.renderOrder=ro;m.add(o)};
 if(lines.length){let lg=new T.BufferGeometry();lg.setAttribute('position',new T.Float32BufferAttribute(lines,3));add(new T.LineSegments(lg,M.line),null,6)}  // x-ray: the whole net shows through parts and layers
 if(zl.length){let zg=new T.BufferGeometry();zg.setAttribute('position',new T.Float32BufferAttribute(zl,3));add(new T.LineSegments(zg,M.zone),null,4)}  // depth-tested: an inner pour's outline stays inside the board
 if(out.length){let g=new T.BufferGeometry();g.setAttribute('position',new T.Float32BufferAttribute(out,3));add(g,M.mesh,3)}}
// a part's box in the engine frame: its KiCad model's (unless it moved since that GLB), else the pad box
function partBox(ref){let m=S.glb?.meta?.refs?.[ref];if(m&&!movedSet()?.has(ref))return m.bbox;return S.boxes.get(ref)?.userData.bbox||null}
function frameOn(H){let g=S.geo,pts=[];for(let r of H.refs){let b=partBox(r);if(b)pts.push([b[0],b[1]],[b[3],b[4]])}
 for(let part of g?.parts||[])for(let p of part.pads||[])if(H.pads.has(part.ref+'.'+p.number)||(p.net&&H.nets.has(p.net)))pts.push(p.xy);
 for(let t of g?.tracks||[])if(H.nets.has(t[0]))pts.push(t[2],t[3]);if(H.region)pts.push(H.region.slice(0,2),H.region.slice(2));if(!pts.length)return;
 let b=bbox(pts),c=new T.Vector3((b[0]+b[2])/2,(b[1]+b[3])/2,(S.body.z0+S.body.z1)/2),off=cam.position.clone().sub(controls.target),need=Math.max(14,Math.hypot(b[2]-b[0],b[3]-b[1]))/2/Math.sin(cam.fov*Math.PI/360)*1.2;
 off.setLength(need);S.moved=true;controls.target.copy(c);cam.position.copy(c).add(off);controls.update();clip()}  // zoom to it (a small part fills a good part of the pane), keeping the direction
// The selected part: a white glow on its model, a white box drawn through everything and a label with its ref that keeps
// its size on screen (the PCB's mint box would vanish among the mint note badges at fit zoom).
const labelTex=new Map();
function labelTexture(txt){let t=labelTex.get(txt);if(t)return t;let c=D.createElement('canvas'),x=c.getContext('2d'),f='700 26px Inter,system-ui,sans-serif';x.font=f;let w=Math.ceil(x.measureText(txt).width)+28,h=58;c.width=w;c.height=h;
 x.font=f;x.fillStyle='#ffffff';x.beginPath();x.roundRect(2,2,w-4,40,8);x.fill();x.beginPath();x.moveTo(w/2-9,41);x.lineTo(w/2+9,41);x.lineTo(w/2,56);x.closePath();x.fill();
 x.fillStyle='#0b1216';x.textAlign='center';x.textBaseline='middle';x.fillText(txt,w/2,23);t=new T.CanvasTexture(c);t.colorSpace=T.SRGBColorSpace;t.userData={w:w/2,h:h/2};labelTex.set(txt,t);return t}
function selection(){if(!T)return;for(let g of [G.top,G.bottom])for(let o of [...g.children])if(o.userData.selbox){g.remove(o);o.geometry?.dispose();if(o.isSprite||o.isLine)o.material.dispose()}
 paintParts();
 let ref=selectedPartRef,b=ref&&partBox(ref);if(!b)return;let side=(S.glb?.meta?.refs?.[ref]?.side)||S.boxes.get(ref)?.userData.side||'F',p=.35,grp=side==='B'?G.bottom:G.top;
 let h=new T.Box3Helper(new T.Box3(new T.Vector3(b[0]-p,b[1]-p,b[2]-.1),new T.Vector3(b[3]+p,b[4]+p,b[5]+.1)),0xffffff);h.material.depthTest=false;h.material.transparent=true;h.material.opacity=.95;h.renderOrder=7;h.userData.selbox=true;grp.add(h);
 let tex=labelTexture(ref),s=new T.Sprite(new T.SpriteMaterial({map:tex,sizeAttenuation:false,depthTest:false,transparent:true}));s.center.set(.5,0);s.renderOrder=8;
 s.position.set((b[0]+b[3])/2,(b[1]+b[4])/2,side==='B'?b[2]-.1:b[5]+.1);s.userData={selbox:true,px:[tex.userData.w,tex.userData.h]};pxSprite(s);grp.add(s)}
// note badges (notes.js): a sticky note by each part with open / proposed / accepted notes, its lower-right corner at the
// part's upper-left corner
const BADGE={proposed:'#ffd166',open:'#8ec5ff',accepted:'#9ee6d1'},badgeTex=new Map();
function badgeTexture(st,n){let k=st+n,t=badgeTex.get(k);if(t)return t;let c=D.createElement('canvas');c.width=c.height=64;let x=c.getContext('2d'),f=18;
 x.fillStyle='#0b1216';x.fillRect(2,2,60,60);x.fillStyle=BADGE[st]||'#8ec5ff';x.beginPath();x.moveTo(6,6);x.lineTo(58-f,6);x.lineTo(58,6+f);x.lineTo(58,58);x.lineTo(6,58);x.closePath();x.fill();
 x.fillStyle='#10202a';x.font='700 30px Inter,system-ui,sans-serif';x.textAlign='center';x.textBaseline='middle';x.fillText(n>9?'9+':String(n),30,35);t=new T.CanvasTexture(c);t.colorSpace=T.SRGBColorSpace;badgeTex.set(k,t);return t}
function badges(){if(!T)return;for(let g of [G.top,G.bottom])for(let o of [...g.children])if(o.userData.badge){g.remove(o);o.material.dispose()}
 let NS=N3();if(!NS?.badgesFor||!S.geo)return;
 for(let b of NS.badgesFor(laneId)){if(b.kind!=='component')continue;let bb=partBox(b.ref);if(!bb)continue;let side=(S.glb?.meta?.refs?.[b.ref]?.side)||S.boxes.get(b.ref)?.userData.side||'F';
  let s=new T.Sprite(new T.SpriteMaterial({map:badgeTexture(b.status,b.count),sizeAttenuation:false,transparent:true}));s.center.set(1,0);s.renderOrder=5;s.position.set(bb[0],bb[4],side==='B'?bb[2]:bb[5]);
  s.userData={badge:true,ref:b.ref,item:b.item,ids:b.ids,px:[BADGE_PX,BADGE_PX],mm:BADGE_MM};(side==='B'?G.bottom:G.top).add(s);s.updateWorldMatrix(true,false);pxSprite(s)}
 vis();paintParts()}

// ------------------------------------------------------------------ camera
function camera(kind){if(!T||!S.geo)return;let g=S.geo,c=new T.Vector3(g.width/2,g.height/2,(S.body.z0+S.body.z1)/2),dir;
 if(kind==='top')dir=new T.Vector3(0,-1e-4,1);else if(kind==='bottom')dir=new T.Vector3(0,1e-4,-1);else if(kind==='fit'){dir=cam.position.clone().sub(controls.target);if(dir.length()<1e-6)dir.set(0,-1e-4,1)}else dir=new T.Vector3(-.5,-.85,.8);
 dir.normalize();S.moved=false;let d=fitDistance(dir,c);controls.target.copy(c);cam.position.copy(c).addScaledVector(dir,d);clip();controls.update();draw()}
// Exact fit: the distance along dir at which every corner of the board box (plus part heights) is inside the frustum.
function fitDistance(dir,c){let g=S.geo,f=dir.clone().negate(),r=new T.Vector3().crossVectors(f,cam.up);if(r.lengthSq()<1e-12)r.set(1,0,0);r.normalize();let u=new T.Vector3().crossVectors(r,f).normalize();
 let tv=Math.tan(cam.fov*Math.PI/360),th=tv*cam.aspect,d=0,v=new T.Vector3();
 for(let x of [0,g.width])for(let y of [0,g.height])for(let z of [S.body.z0-3,S.body.z1+5]){v.set(x,y,z).sub(c);let a=v.dot(dir);d=Math.max(d,a+Math.abs(v.dot(r))/th,a+Math.abs(v.dot(u))/tv)}
 return d*1.06}

// ------------------------------------------------------------------ picking
// What is visible at the cursor: a note badge or a part (model or box) in front, else the copper. Alt looks through the
// parts to the copper under them (the PCB canvas picks a pad under a part first; in 3D the part body is what you see).
function pickAt(ev){if(!T||!S.geo)return null;let cv=$('v3-canvas'),rc=cv.getBoundingClientRect();
 ray.setFromCamera(new T.Vector2((ev.clientX-rc.left)/rc.width*2-1,-(ev.clientY-rc.top)/rc.height*2+1),cam);let o=ray.ray.origin,dir=ray.ray.direction,best=null;
 let objs=[];const add=x=>{if(effVisible(x))objs.push(x)};
 if(!ev.altKey){for(let g of [G.top,G.bottom])for(let x of g.children)if(x.userData.badge)add(x);
  for(let [,m] of S.models)m.traverse(x=>x.isMesh&&add(x));for(let [,b] of S.boxes)add(b.children[0])}
 for(let h of ray.intersectObjects(objs,false)){let u=h.object.userData;if(u.badge){best={d:h.distance,h:{kind:'badge',item:u.item,ids:u.ids}};break}if(u.ref){best={d:h.distance,h:{kind:'component',ref:u.ref}};break}}
 let occ=Infinity;if(P.board&&P.explode===0&&!S.solo){let bh=ray.intersectObjects(G.body.children.filter(effVisible),true)[0];if(bh)occ=bh.distance}
 let layers=[...S.layers].filter(([L,l])=>effVisible(l.group)).map(([L,l])=>({L,l,t:Math.abs(dir.z)<1e-9?-1:(l.group.position.z-o.z)/dir.z})).filter(x=>x.t>0&&x.t<occ).sort((a,b)=>a.t-b.t);
 let px=2*Math.tan(cam.fov*Math.PI/360)/rc.height;
 for(let {L,l,t} of layers){if(best&&best.d<t)break;let q=[o.x+dir.x*t,o.y+dir.y*t],tol=2*px*t,h=hit2d(l,q,tol);if(h){best={d:t,h:{...h,layer:L}};break}}
 return best?.h||null}
// PCB order (app.js boardHit): pad > numberless pad (its part) > track > via > zone; tol = 2 px at that depth
function hit2d(l,q,tol){let gr=l.grid,cx=Math.floor(q[0]/CELL),cy=Math.floor(q[1]/CELL);if(cx<0||cy<0||cx>=gr.nx||cy>=gr.ny)return null;
 let ids=new Set();for(let dx=-1;dx<=1;dx++)for(let dy=-1;dy<=1;dy++)for(let k of gr.cells.get((cx+dx)*100000+cy+dy)||[])ids.add(k);
 let cand=[...ids].sort((a,b)=>a-b).map(k=>gr.items[k]).filter(it=>effVisible(it.mesh));
 for(let it of cand){if(it.kind==='pad'&&it.pad&&(it.obj.polys?.length?it.obj.polys.some(p=>inPoly(q,p)):padContains(it.obj,q,tol)))return {kind:'pad',ref:it.ref,pad:it.pad,net:it.net,obj:it.obj}}
 for(let it of cand){if(it.kind==='pad'&&!it.pad&&(it.obj.polys?.length?it.obj.polys.some(p=>inPoly(q,p)):padContains(it.obj,q,tol)))return {kind:'component',ref:it.ref}}
 for(let it of cand)if(it.kind==='track'&&it.net){let t=it.obj,a=t[2],b=t[3],dx=b[0]-a[0],dy=b[1]-a[1],f=Math.max(0,Math.min(1,((q[0]-a[0])*dx+(q[1]-a[1])*dy)/(dx*dx+dy*dy||1)));if(Math.hypot(q[0]-a[0]-f*dx,q[1]-a[1]-f*dy)<tol+t[4]/2)return {kind:'track',net:it.net,obj:t}}
 for(let it of cand)if(it.kind==='via'&&it.net&&Math.hypot(q[0]-it.obj.xy[0],q[1]-it.obj.xy[1])<it.obj.diameter/2+tol)return {kind:'via',net:it.net,obj:it.obj};
 for(let it of cand)if(it.kind==='zone'&&it.net&&it.obj.paths.filter(p=>inPoly(q,p)).length%2)return {kind:'zone',net:it.net,obj:it.obj};
 return null}
// Shift+click adds to Ask: its confirmation toast shows in this pane (app.js boardToast would put it over the PCB,
// which is hidden in the 3D and 3D + schematic views)
function askHere(item,where){S.toastHere=true;try{askAdd(item,where)}finally{S.toastHere=false}}
function click(ev){let h=pickAt(ev);if(!h)return;
 if(h.kind==='badge'){if(ev.shiftKey)askHere(h.item,' (note badge)');else N3()?.showFor?.(h.item);return}
 let item=h.kind==='pad'?{kind:'pad',ref:h.ref,pad:h.pad}:h.kind==='component'?{kind:'component',ref:h.ref}:{kind:'net',name:h.net};
 if(ev.shiftKey){askHere(item,' (3D)');return}
 inspectItem(item);
 if(h.kind==='pad'||h.kind==='component'){selectedPartRef=h.ref;$('component-query').value=h.ref;$('component-status').textContent=h.ref+' selected'}
 if(h.kind==='component')V().clear(true);else V().highlight(h.kind==='pad'?{pads:[h.ref+'.'+h.pad],nets:h.net?[h.net]:[]}:{nets:[h.net]},{frame:false,label:h.kind==='pad'?h.ref+'.'+h.pad+(h.net?' · '+(netTitle(h.net)||h.net):''):null});
 render()}
function hover(ev){if(!S.visible||!ev||S.down)return;let h=pickAt(ev),el=$('v3-hover'),cv=$('v3-canvas');cv.style.cursor=h?'pointer':'grab';
 if(!h){el.style.display='none';return}
 let txt=h.kind==='badge'?h.ids.map(id=>id+' · '+(N3()?.get?.(id)?.title||'')).join('\n')+'\nClick: show in Notes · Shift+click: add to Ask':hoverText(h)+(h.kind==='component'?'\nAlt+click: the copper under it':'');
 let r=$('v3-main').getBoundingClientRect();el.textContent=txt;el.style.display='block';let x=ev.clientX-r.left+14,y=ev.clientY-r.top+14;if(x+el.offsetWidth>r.width-6)x=Math.max(4,ev.clientX-r.left-14-el.offsetWidth);if(y+el.offsetHeight>r.height-6)y=Math.max(4,ev.clientY-r.top-14-el.offsetHeight);el.style.left=x+'px';el.style.top=y+'px'}
function hideHover(){let el=$('v3-hover');if(el)el.style.display='none'}

// ------------------------------------------------------------------ sync with app.js / YapnrView
function sync(force){if(!S.visible||!T)return;let g=geo()||null,sha=V()?.boardSha?.()||null;if(g)mark('geo');
 if(laneId!==S.lane&&S.lane!==undefined)S.fitted='fit';  // another lane (another board size): fit again, same direction; phases of one lane keep the camera
 if(g!==S.geo||force&&!S.built){S.geo=g;let wait=1000-(performance.now()-S.built);clearTimeout(S.buildT);if(wait>0&&S.built&&!force)S.buildT=setTimeout(build,wait);else build()}
 if(force||sha!==S.sha||laneId!==S.lane){S.sha=sha;S.lane=laneId;request3d()}
 if(selectedPartRef!==S.sel){S.sel=selectedPartRef;selection();draw()}
 if(schKey()!==S.hk)hl(false)}
function show(mode){S.visible=!!mode;S.mode=mode;if(!mode){hideHover();clearTimeout(S.pollT);panel(false);return}
 try{localStorage.setItem('pnr-3d-pair',mode)}catch(e){}
 for(let b of $('v3-pair').children)b.classList.toggle('active',b.dataset.m===mode);
 boot().then(ok=>{if(ok&&S.visible)requestAnimationFrame(()=>{size();sync(true);draw()})})}
const renderBefore3D=render;render=function(){renderBefore3D();if(S.visible)sync()};
// Jump to component / a part clicked in the schematic: bring it into view here too (the PCB canvas recentres itself;
// with the PCB hidden, schematic.js only records the selection)
const jumpBefore3D=jumpToComponent;jumpToComponent=function(q){let ok=jumpBefore3D(q);
 if(ok&&S.visible&&T&&S.geo&&selectedPartRef){let ref=selectedPartRef;frameOn({refs:new Set([ref]),nets:new Set(),pads:new Set(),region:null});sync();draw();
  if(canvas.clientWidth<80)$('component-status').textContent=ref+' selected · shown in 3D'}
 return ok};
const toastBefore3D=boardToast;boardToast=function(msg){if(S.visible&&(S.toastHere||canvas.clientWidth<80))return toast3(msg);return toastBefore3D(msg)};
(function(){const VV=V();if(!VV)return;const h0=VV.highlight,c0=VV.clear;
 VV.highlight=function(sel,opt){let r=h0.call(VV,sel,opt);hl(!opt||opt.frame!==false);return r};
 VV.clear=function(keep){let r=c0.call(VV,keep);hl(false);return r}})();
D.addEventListener('yapnr:notes',()=>{if(S.badgeBox)S.badgeBox.checked=N3()?.badges?.()!==false;if(S.visible&&T){badges();draw()}});
window.Yapnr3D={show,visible:()=>S.visible,camera,status:()=>S.status,ready:()=>!!S.glb,layerZ,solo:L=>solo(L||null),panel,autoAt:S.autoAt,  // autoAt: lane -> last live auto-export (ms), for headless checks

 explode(v){P.explode=Math.max(0,Math.min(100,+v||0));$('v3-explode').value=P.explode;explode();draw()},
 set(k,v){if(k in P&&typeof P[k]==='boolean')P[k]=!!v;else if(v)delete P.off[k];else P.off[k]=true;layersPanel();vis();draw()},
 badgePx:()=>{let out=[];for(let g of [G.top,G.bottom])for(let o of g.children)if(o.userData.badge&&effVisible(o))out.push(+(o.scale.x/pxScale(1)).toFixed(1));return out},  // on-screen badge widths (headless checks)
 pick:(x,y,mods)=>{let r=$('v3-canvas').getBoundingClientRect();return pickAt({clientX:r.left+x,clientY:r.top+y,...(mods||{})})},
 project(x,y,z){if(!T)return null;let v=new T.Vector3(x,y,z).project(cam),r=$('v3-canvas').getBoundingClientRect();return [r.left+(v.x+1)/2*r.width,r.top+(1-v.y)/2*r.height]},  // world mm -> page px (headless checks)
 matStats:()=>{let out={};for(let o of [...(S.glb?.objs||[]),...G.body.children])o.traverse(m=>{if(!m.isMesh)return;let k=(o.userData.kind||(o.userData.slab?'slab':'?'))+':'+(m.material===m.userData.m0||!m.userData.m0?'base':m.material.transparent?'ghost':'variant');out[k]=(out[k]||0)+1});return out},
 hlStats:()=>[...S.layers].map(([L,l])=>[L,Object.entries(l.meshes).map(([k,m])=>[k,m.children.filter(o=>o.userData.hlo).reduce((n,o)=>n+o.geometry.attributes.position.count/(o.isLine?2:3),0)])]),
 stats:()=>({layers:S.stack,models:S.models.size,shown:[...S.models.keys()].filter(modelShown).length,boxes:[...S.boxes.values()].filter(b=>b.visible).length,key:S.key,frames:S.frames||0,stale:S.stale,moved:movedSet()?.size??null,solo:S.solo,
  triangles:renderer?.info.render.triangles,calls:renderer?.info.render.calls,camera:cam&&cam.position.toArray().map(v=>+v.toFixed(2)),near:cam&&+cam.near.toFixed(3),
  mem:renderer&&{...renderer.info.memory},sel:S.sel,builds:S.builds||0,buildMs:S.buildMs,marks:{...marks},sha:S.sha,lane:S.lane})};
if(/^3d/.test(D.body.dataset.view||'')){btn3.classList.add('active');for(let b of $('viewswitch').children)if(b!==btn3)b.classList.remove('active');show(D.body.dataset.view)}
})();
