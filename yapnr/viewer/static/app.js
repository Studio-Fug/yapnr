'use strict';
const $=id=>document.getElementById(id), canvas=$('board'),ctx=canvas.getContext('2d');
let state=null,pinned=null,pinId=null,laneId=null,phase='live',phaseGeo=null,annotations=[],drawing=false,drag=null,preview=null,view={scale:8,x:40,y:500},fitted=false,revision=-1;
// Gesture performance (see docs/viewer.md "Rendering and performance"): settleTimer fires a full
// render() shortly after a pan/pinch/zoom/wheel gesture goes idle; gestureCache is an offscreen
// raster of the last full render(), blitted with a transform while a gesture is live instead of
// repainting thousands of vias/tracks every pointermove; gestureCacheDirty marks it stale (set by
// every full render()) so it is rebuilt lazily, once, at the start of the next gesture.
let settleTimer=null,gestureCache=null,gestureCacheDirty=true;
const colors={'F.Cu':'#e89a73','In1.Cu':'#aa9de9','In2.Cu':'#dfbd62','B.Cu':'#6eb6e5'},layers=new Set(Object.keys(colors));
for(const [l,col] of Object.entries(colors)){let lab=document.createElement('label'),inp=document.createElement('input');inp.type='checkbox';inp.checked=true;inp.onchange=()=>{inp.checked?layers.add(l):layers.delete(l);render()};lab.append(inp,document.createTextNode(l));lab.style.color=col;$('layers').append(lab)}
const display=()=>pinned||state, lane=()=>display()?.lanes[laneId], geo=()=>phase==='live'?lane()?.geometry:phaseGeo;
const point=(x,y)=>[(x-view.x)/view.scale,(view.y-y)/view.scale];
const screen=p=>[view.x+p[0]*view.scale,view.y-p[1]*view.scale];
async function api(url,body){let r=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}
function resize(c){let w=c.clientWidth,h=c.clientHeight,d=window.devicePixelRatio||1;if(c.width!==Math.round(w*d)||c.height!==Math.round(h*d)){c.width=Math.round(w*d);c.height=Math.round(h*d)}let x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);return [x,w,h]}
function fit(){let g=geo();if(!g)return;view.scale=Math.min((canvas.clientWidth-60)/g.width,(canvas.clientHeight-60)/g.height);view.x=(canvas.clientWidth-g.width*view.scale)/2;view.y=(canvas.clientHeight+g.height*view.scale)/2;fitted=true;render()}
let selectedPartRef=null,componentOptionsGeometry=null,pendingComponentRef=null;
function componentBounds(part){
 let points=[part.xy].filter(p=>p?.length===2&&p.every(Number.isFinite));
 for(let pad of part.pads||[]){if(!pad.xy?.every(Number.isFinite)||!pad.size?.every(Number.isFinite))continue;let t=(pad.angle||0)*Math.PI/180,co=Math.abs(Math.cos(t)),si=Math.abs(Math.sin(t)),dx=(co*pad.size[0]+si*pad.size[1])/2,dy=(si*pad.size[0]+co*pad.size[1])/2;points.push([pad.xy[0]-dx,pad.xy[1]-dy],[pad.xy[0]+dx,pad.xy[1]+dy]);}
 if(!points.length)return null;return [Math.min(...points.map(p=>p[0])),Math.min(...points.map(p=>p[1])),Math.max(...points.map(p=>p[0])),Math.max(...points.map(p=>p[1]))];
}
function refreshComponentOptions(g){if(componentOptionsGeometry===g)return;componentOptionsGeometry=g;let refs=[...new Set((g.parts||[]).map(p=>p.ref))].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));$('component-options').replaceChildren(...refs.map(ref=>new Option(ref,ref)));if(selectedPartRef&&!g.parts?.some(p=>p.ref===selectedPartRef))$('component-status').textContent=selectedPartRef+' is absent from this checkpoint.';}
function jumpToComponent(query){let g=geo(),ref=String(query??'').trim().toUpperCase();if(!g){pendingComponentRef=ref;$('component-status').textContent=ref?'Loading checkpoint; will locate '+ref+'…':'Wait for a board checkpoint to load.';return false;}pendingComponentRef=null;let part=(g.parts||[]).find(p=>p.ref.toUpperCase()===ref);if(!part){$('component-status').textContent=ref?'No component '+ref+' in this checkpoint.':'Enter a component reference.';return false;}let b=componentBounds(part);if(!b){$('component-status').textContent=part.ref+' has no display geometry.';return false;}let cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2,scale=Math.max(1,Math.min(120,canvas.clientWidth/Math.max(12,b[2]-b[0]+8),canvas.clientHeight/Math.max(12,b[3]-b[1]+8)));view={scale,x:canvas.clientWidth/2-cx*scale,y:canvas.clientHeight/2+cy*scale};fitted=true;selectedPartRef=part.ref;$('component-query').value=part.ref;let hidden=part.pads?.length&&!part.pads.some(p=>p.layers.some(l=>layers.has(l)));$('component-status').textContent=part.ref+' · '+part.xy.map(v=>v.toFixed(2)).join(', ')+' mm'+(hidden?' · pad layers hidden':'');render();return true;}
$('component-find').onsubmit=e=>{e.preventDefault();jumpToComponent($('component-query').value);};
$('component-clear').onclick=()=>{selectedPartRef=null;pendingComponentRef=null;$('component-status').textContent='';render();};
function line(a,b,color,width=1,dash=[],round=false){ctx.lineCap=round?'round':'butt';ctx.lineJoin=round?'round':'miter';ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash);ctx.beginPath();ctx.moveTo(...screen(a));ctx.lineTo(...screen(b));ctx.stroke();ctx.setLineDash([])}
function circle(p,r,color){ctx.fillStyle=color;ctx.beginPath();ctx.arc(...screen(p),r,0,Math.PI*2);ctx.fill()}
const key=t=>JSON.stringify(t);
// ------------------------------------------------------------------ spatial grid (board mm units)
// One uniform grid per geometry object (WeakMap: a fresh g from the server -- a new lane, frame or
// poll update -- gets a fresh grid for free; nothing is ever invalidated by hand). Cell size aims
// for a handful of vias+tracks per cell. Used both to cull drawing to the visible viewport (paintBoard)
// and to narrow hit-testing (boardHit) from "every via/track on the board" to "the few near the
// point" -- the fix for the linear scans this file used to do per hover/click, same as the
// per-frame via/track draw loops they mirror.
const geomGrids=new WeakMap();
function gridFor(g){
 let idx=geomGrids.get(g);if(idx)return idx;
 let n=(g.vias||[]).length+(g.tracks||[]).length,cell=Math.max(.5,Math.min(10,Math.sqrt((g.width*g.height)/Math.max(1,n))*2));
 let cellOf=(x,y)=>[Math.floor(x/cell),Math.floor(y/cell)],k=(cx,cy)=>cx+','+cy;
 let viaCells=new Map(),trackCells=new Map();
 const add=(map,key,i)=>{let a=map.get(key);if(!a)map.set(key,a=[]);a.push(i)};
 (g.vias||[]).forEach((v,i)=>{let [cx,cy]=cellOf(v.xy[0],v.xy[1]);add(viaCells,k(cx,cy),i)});
 (g.tracks||[]).forEach((t,i)=>{ // stamp every cell the segment's bbox touches: a track can span several
  let [ax,ay]=t[2],[bx,by]=t[3],x0=Math.min(ax,bx),x1=Math.max(ax,bx),y0=Math.min(ay,by),y1=Math.max(ay,by);
  let [cx0,cy0]=cellOf(x0,y0),[cx1,cy1]=cellOf(x1,y1);
  for(let cx=cx0;cx<=cx1;cx++)for(let cy=cy0;cy<=cy1;cy++)add(trackCells,k(cx,cy),i);
 });
 idx={cell,cellOf,key:k,viaCells,trackCells};geomGrids.set(g,idx);return idx;
}
function cellsInRect(idx,x0,y0,x1,y1){
 let [cx0,cy0]=idx.cellOf(x0,y0),[cx1,cy1]=idx.cellOf(x1,y1),out=[];
 for(let cx=cx0;cx<=cx1;cx++)for(let cy=cy0;cy<=cy1;cy++)out.push(idx.key(cx,cy));
 return out;
}
function gridCandidates(idx,x0,y0,x1,y1){
 let keys=cellsInRect(idx,x0,y0,x1,y1),vias=new Set(),tracks=new Set();
 for(let kk of keys){let a=idx.viaCells.get(kk);if(a)for(let i of a)vias.add(i);let b=idx.trackCells.get(kk);if(b)for(let i of b)tracks.add(i)}
 return {vias,tracks};
}
// ------------------------------------------------------------------ board painting (static layers)
// paintBoard draws everything that only depends on the geometry, the view and the layer/toggle
// checkboxes -- background, grid lines, zones, tracks, vias, pads and labels, and the board border.
// It takes its ctx/view/size explicitly (never the globals) so the exact same function paints the
// live canvas during a full render() *and* paints the offscreen gesture-raster cache at a shifted
// view -- see buildGestureCache(). Everything else (selection, routing target, air wires, cost
// dots, move previews, the view highlight, the drag/annotation rectangles) is "dynamic": it depends
// on more than the board alone and must stay live every frame, so it is drawn by renderOverlay()
// against the real canvas and current view, never cached.
//
// Vias are the hot path this exists for (thousands of ground-stitching vias on a dense board): they
// are culled to the viewport via the grid above, then split by their true on-screen diameter (no
// artificial per-via minimum) into three tiers, each drawn with a single Path2D fill/stroke rather
// than two beginPath/arc/fill calls and a fillStyle change per via:
//  - >= 1.5px diameter: full detail (outer ring + hole), one Path2D each.
//  - 0.5-1.5px: a plain dot at the true diameter (indistinguishable from a ring at that size).
//  - < 0.5px: too small to resolve individually; one representative dot per occupied grid cell
//    stands in for the whole cluster (a cheap stand-in for a pre-rendered texture tile -- the
//    measured win is almost entirely from cutting the draw count, not from the exact shape).
// Hit-testing (boardHit) always uses each via's true diameter regardless of this tier, so a via
// that is barely a cell-cluster fleck on screen is still exactly as clickable as it always was.
function paintBoard(c,v,w,h,g,dpr){
 c.setTransform(dpr,0,0,dpr,0,0);c.clearRect(0,0,w,h);c.fillStyle='#10191e';c.fillRect(0,0,w,h);
 const scr=p=>[v.x+p[0]*v.scale,v.y-p[1]*v.scale];
 const ln=(a,b,color,width=1,dash=[],round=false)=>{c.lineCap=round?'round':'butt';c.lineJoin=round?'round':'miter';c.strokeStyle=color;c.lineWidth=width;c.setLineDash(dash);c.beginPath();c.moveTo(...scr(a));c.lineTo(...scr(b));c.stroke();c.setLineDash([])};
 let step=v.scale<7?5:1;c.lineWidth=.4;for(let x=0;x<=g.width;x+=step)ln([x,0],[x,g.height],'#23343c',.4);for(let y=0;y<=g.height;y+=step)ln([0,y],[g.width,y],'#23343c',.4);
 for(let z of g.zones||[]){if(!layers.has(z.layer))continue;c.fillStyle=colors[z.layer]+'25';c.beginPath();for(let path of z.paths){path.forEach((p,i)=>i?c.lineTo(...scr(p)):c.moveTo(...scr(p)));c.closePath()}c.fill('evenodd')}
 // Viewport (world/board mm) rect, padded by one grid cell so nothing just outside pops in late.
 let idx=gridFor(g),corners=[[0,0],[w,0],[0,h],[w,h]].map(p=>[(p[0]-v.x)/v.scale,(v.y-p[1])/v.scale]);
 let xs=corners.map(p=>p[0]),ys=corners.map(p=>p[1]),pad=idx.cell;
 let vx0=Math.min(...xs)-pad,vx1=Math.max(...xs)+pad,vy0=Math.min(...ys)-pad,vy1=Math.max(...ys)+pad;
 // When the viewport already covers the whole board (fit-to-screen, or just zoomed out past it)
 // there is nothing to cull: walking every grid cell to rebuild the same "everything" candidate
 // set, every frame, via a Map lookup and a Set insert per via/track, is pure overhead in that
 // case (measured: slower than not culling at all on a very dense board). A plain index range
 // costs none of that and is exactly equivalent once nothing is actually excluded.
 let cand=(vx0<=0&&vy0<=0&&vx1>=g.width&&vy1>=g.height)
  ?{vias:g.vias.map((_,i)=>i),tracks:g.tracks.map((_,i)=>i)}
  :gridCandidates(idx,vx0,vy0,vx1,vy1);
 let previous=phase==='live'?lane()?.previous:null,old=new Set((previous?.tracks||[]).map(key)),now=new Set((g.tracks||[]).map(key)),changesOn=$('changes').checked;
 for(let i of cand.tracks){let t=g.tracks[i];if(!layers.has(t[1]))continue;let col=changesOn&&previous&&!old.has(key(t))?'#62ffad':(colors[t[1]]||'#aaa');ln(t[2],t[3],col,Math.max(.6,t[4]*v.scale),[],true)}
 // Diff/draft overlays are opt-in and bounded by the previous checkpoint's own track count, not
 // the current viewport, so they are left uncull (they are also drawn against a *different*
 // geometry object -- `previous` -- which has no grid of its own here).
 if(changesOn&&previous)for(let t of previous.tracks||[])if(!now.has(key(t)))ln(t[2],t[3],'#ff566b',Math.max(.6,t[4]*v.scale),[4,3],true);
 if(phase==='live')for(let ts of Object.values(lane()?.draft||{}))for(let t of ts)ln(t[2],t[3],'#d39af6',Math.max(.6,t[4]*v.scale),[4,2],true);
 let full=[],dot=[],cellDot=new Map();
 for(let i of cand.vias){let via=g.vias[i],d=via.diameter*v.scale;if(d>=1.5)full.push(via);else if(d>=.5)dot.push(via);else{let [cx,cy]=idx.cellOf(via.xy[0],via.xy[1]),ck=idx.key(cx,cy);if(!cellDot.has(ck))cellDot.set(ck,via)}}
 if(full.length||dot.length||cellDot.size){
  c.save();c.setTransform(v.scale*dpr,0,0,-v.scale*dpr,v.x*dpr,v.y*dpr);
  if(full.length){let outer=new Path2D(),hole=new Path2D();for(let via of full){let [x,y]=via.xy,r=via.diameter/2,rh=via.diameter/5;outer.moveTo(x+r,y);outer.arc(x,y,r,0,Math.PI*2);hole.moveTo(x+rh,y);hole.arc(x,y,rh,0,Math.PI*2)}c.fillStyle='#b3c1c9';c.fill(outer);c.fillStyle='#15252c';c.fill(hole)}
  if(dot.length){let p=new Path2D();for(let via of dot){let [x,y]=via.xy,r=via.diameter/2;p.moveTo(x+r,y);p.arc(x,y,r,0,Math.PI*2)}c.fillStyle='#b3c1c9';c.fill(p)}
  if(cellDot.size){let p=new Path2D(),r=idx.cell*.18;for(let via of cellDot.values()){let [x,y]=via.xy;p.moveTo(x+r,y);p.arc(x,y,r,0,Math.PI*2)}c.fillStyle='#8a97a0';c.fill(p)}
  c.restore();
 }
 for(let part of g.parts||[]){for(let p of part.pads){if(!p.layers.some(l=>layers.has(l)))continue;let [x,y]=scr(p.xy);if(p.polys?.length){c.fillStyle='#cdd2be';c.beginPath();for(let poly of p.polys){poly.forEach((q,i)=>{let [sx,sy]=scr(q);i?c.lineTo(sx,sy):c.moveTo(sx,sy)});c.closePath()}c.fill()}else{c.save();c.translate(x,y);c.rotate(-p.angle*Math.PI/180);c.fillStyle='#cdd2be';if(p.shape==='circle'){c.beginPath();c.ellipse(0,0,p.size[0]*v.scale/2,p.size[1]*v.scale/2,0,0,Math.PI*2);c.fill()}else c.fillRect(-p.size[0]*v.scale/2,-p.size[1]*v.scale/2,p.size[0]*v.scale,p.size[1]*v.scale);c.restore();}if($('labels').checked&&v.scale>14){c.fillStyle='#07141a';c.font='9px system-ui';c.fillText(p.number,x-3,y+3)}}if($('labels').checked){let p=scr(part.xy);c.fillStyle='#fff';c.font='10px system-ui';c.fillText(part.ref,p[0]+3,p[1]-5)}}
 ln([0,0],[g.width,0],'#acc7ce');ln([g.width,0],[g.width,g.height],'#acc7ce');ln([g.width,g.height],[0,g.height],'#acc7ce');ln([0,g.height],[0,0],'#acc7ce');
}
// ------------------------------------------------------------------ dynamic overlay (every frame)
function renderOverlay(g){
 if(phase==='live'&&lane()?.target){let target=lane().target,loc=id=>{let i=id.lastIndexOf('.'),part=g.parts.find(p=>p.ref===id.slice(0,i));return part?.pads.find(p=>p.number===id.slice(i+1))?.xy},a=loc(target.source),b=loc(target.target);if(a&&b){line(a,b,'#ffde68',2,[7,4]);for(let p of [a,b]){ctx.strokeStyle='#ffde68';ctx.lineWidth=2;ctx.beginPath();ctx.arc(...screen(p),8,0,7);ctx.stroke()}ctx.fillStyle='#ffde68';ctx.fillText('Routing '+target.net,...screen(a));}}
 if($('air').checked){let nets={};for(let p of (g.parts||[]).flatMap(c=>c.pads)){if(p.net)(nets[p.net]??=[]).push(p.xy)}for(let ps of Object.values(nets)){let rest=ps.slice(1),tree=[ps[0]];while(rest.length){let best=[Infinity,0,null];rest.forEach((p,i)=>tree.forEach(q=>{let d=Math.hypot(p[0]-q[0],p[1]-q[1]);if(d<best[0])best=[d,i,q]}));let p=rest.splice(best[1],1)[0];line(p,best[2],'#8da4ba55',.6,[2,4]);tree.push(p)}}}
 let search=display()?.search[String(lane()?.iteration)]||{},probes=search.probes||Object.values(display()?.lanes[`r${String(lane()?.iteration).padStart(2,'0')}/search`]?.costs||{});
 if($('costs').checked)for(let probe of probes){let costs=probe.candidates.map(p=>p.cost),min=Math.min(...costs),max=Math.max(...costs);for(let p of probe.candidates){let t=(p.cost-min)/(max-min||1);circle(p.position,2,`hsla(${150-t*150},75%,65%,.65)`)} }
 for(let m of (preview?.moves||lane()?.moves||[])){line(m.original,m.position,'#fff07d',2,[5,3]);circle(m.position,5,'#fff07d');ctx.fillStyle='#fff07d';ctx.fillText(m.ref,...screen(m.position))}
 let selected=(g.parts||[]).find(p=>p.ref===selectedPartRef),box=selected&&componentBounds(selected);if(box){let a=screen([box[0]-.6,box[3]+.6]),b=screen([box[2]+.6,box[1]-.6]);ctx.strokeStyle='#79ffe0';ctx.lineWidth=2;ctx.setLineDash([6,3]);ctx.strokeRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.setLineDash([]);ctx.fillStyle='#79ffe0';ctx.font='bold 13px system-ui';ctx.fillText(selected.ref,a[0],a[1]-7);}
 drawViewHl(g);if(drag?.region){let a=screen(drag.world),b=drag.last;ctx.fillStyle='#ffb3471a';ctx.strokeStyle='#ffb347';ctx.lineWidth=1.5;ctx.setLineDash([6,4]);ctx.fillRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.strokeRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.setLineDash([]);ctx.fillStyle='#ffb347';ctx.font='11px system-ui';ctx.fillText('Ask about region',Math.min(a[0],b[0]),Math.min(a[1],b[1])-5)}
 let rects=annotations.filter(r=>r.lane===laneId&&r.phase===phase);if(drag?.draw)rects=[...rects,{bounds:[...drag.world,...point(drag.last[0],drag.last[1])],text:'New annotation'}];for(let r of rects){let a=screen(r.bounds.slice(0,2)),b=screen(r.bounds.slice(2));ctx.fillStyle='#ffe38b17';ctx.strokeStyle='#ffe38b';ctx.lineWidth=2;ctx.fillRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.strokeRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.fillStyle='#ffe38b';ctx.fillText(r.text.slice(0,45),Math.min(a[0],b[0]),Math.min(a[1],b[1])-5)}
}
function render(){
 if(settleTimer!==null){clearTimeout(settleTimer);settleTimer=null}
 let [c,w,h]=resize(canvas),g=geo();
 if(!g){c.clearRect(0,0,w,h);c.fillStyle='#10191e';c.fillRect(0,0,w,h);c.fillStyle='#8fa7b3';c.fillText('Waiting for native placement…',30,40);gestureCache=null;renderSearch();return}
 if(pendingComponentRef){let requested=pendingComponentRef;pendingComponentRef=null;jumpToComponent(requested);return;}
 refreshComponentOptions(g);
 paintBoard(c,view,w,h,g,window.devicePixelRatio||1);
 renderOverlay(g);
 renderSearch();
 gestureCacheDirty=true; // next gesture frame rebuilds the raster against this latest content
}
// ------------------------------------------------------------------ gesture fast path
// Render the static board layers into an offscreen canvas once, at the view a gesture started
// from plus a margin (so a modest pan/pinch stays inside it); every frame of that gesture then
// just blits the cache with a scale+translate (no via/track work at all) and redraws the small,
// cheap dynamic overlay on top. A plain full render() ~150ms after the last gesture input restores
// full quality and refreshes the cache for the next one (buildGestureCache runs lazily, once, the
// first time it is needed rather than on every full render -- most render() calls are not followed
// by a gesture, e.g. the live poll or a lane switch, and would otherwise pay for a cache nobody uses).
function buildGestureCache(g){
 let w=canvas.clientWidth,h=canvas.clientHeight,dpr=window.devicePixelRatio||1;
 if(w<10||h<10){gestureCache=null;return}
 // Snap the margin so the offscreen canvas's backing store is an exact integer number of
 // physical pixels for the CSS size we then draw it back at (cssW,cssH below) -- otherwise
 // drawImage has to resample by a sub-pixel amount even when blitting back unchanged (k=1), which
 // showed up as a faint, otherwise pointless blur/edge-shift versus a full render() of the same view.
 let marginGuess=Math.min(320,Math.max(w,h)*.3);
 let pw=Math.round((w+2*marginGuess)*dpr),ph=Math.round((h+2*marginGuess)*dpr);
 let cssW=pw/dpr,cssH=ph/dpr,marginX=(cssW-w)/2,marginY=(cssH-h)/2;
 let off=gestureCache?.canvas||document.createElement('canvas');
 if(off.width!==pw||off.height!==ph){off.width=pw;off.height=ph}
 let octx=off.getContext('2d'),shifted={scale:view.scale,x:view.x+marginX,y:view.y+marginY};
 paintBoard(octx,shifted,cssW,cssH,g,dpr);
 gestureCache={canvas:off,scale0:view.scale,x0:view.x,y0:view.y,marginX,marginY,cssW,cssH};
}
function scheduleSettle(){if(settleTimer!==null)clearTimeout(settleTimer);settleTimer=setTimeout(()=>{settleTimer=null;render()},150)}
function fastFrame(){
 let g=geo();if(!g){render();return}
 if(gestureCacheDirty||!gestureCache){buildGestureCache(g);gestureCacheDirty=false}
 if(!gestureCache){render();return}
 let [c,w,h]=resize(canvas);c.clearRect(0,0,w,h);c.fillStyle='#10191e';c.fillRect(0,0,w,h);
 let k=view.scale/gestureCache.scale0,dx=view.x-(gestureCache.x0+gestureCache.marginX)*k,dy=view.y-(gestureCache.y0+gestureCache.marginY)*k;
 c.save();c.translate(dx,dy);c.scale(k,k);c.drawImage(gestureCache.canvas,0,0,gestureCache.cssW,gestureCache.cssH);c.restore();
 renderOverlay(g);
 scheduleSettle();
}
let treeHits=[],spaceHits=[];
function renderSearch(){let s=display();if(!s)return;let [c,w,h]=resize($('tree'));c.clearRect(0,0,w,h);treeHits=[];let all=Object.values(s.lanes).filter(l=>!l.id.endsWith('/search')),rounds=[...new Set(all.map(l=>l.iteration))].slice(-6);rounds.forEach((r,ri)=>{let ls=all.filter(l=>l.iteration===r),y=22+ri*25;c.fillStyle='#8da9b6';c.font='10px system-ui';c.fillText('R'+r,4,y+3);ls.forEach((l,i)=>{let x=60+i*49;c.strokeStyle='#405965';c.beginPath();c.moveTo(30,y);c.lineTo(x,y);c.stroke();c.fillStyle=l.status==='accepted'?'#9ee6d1':l.status==='failed'?'#ed6d70':l.status==='complete'?'#719be8':l.status==='queued'?'#53636d':'#f3c875';c.beginPath();c.arc(x,y,l.id===laneId?7:5,0,7);c.fill();treeHits.push({x,y,id:l.id})})});
 let [d,sw,sh]=resize($('space'));d.clearRect(0,0,sw,sh);spaceHits=[];let audit=s.search[String(lane()?.iteration)]||Object.values(s.search).slice(-1)[0];if(!audit)return;let options=audit.alternatives||[],bad=audit.rejections||[],items=[...options,...bad],costs=options.map(x=>x.cost),mi=Math.min(...costs),ma=Math.max(...costs),sampled=new Set((audit.sampled||[]).map(JSON.stringify));let columns=Math.ceil(Math.sqrt(items.length||1));items.forEach((p,i)=>{let x=9+(i%columns)*(sw-18)/columns,y=9+Math.floor(i/columns)*(sh-18)/columns;d.fillStyle=p.reason?'#52616a':`hsl(${150-(p.cost-mi)/(ma-mi||1)*150},70%,65%)`;d.beginPath();d.arc(x,y,2,0,7);d.fill();if(sampled.has(JSON.stringify(p.indices))){d.strokeStyle='#fff';d.beginPath();d.arc(x,y,4,0,7);d.stroke()}spaceHits.push({x,y,p})});$('spaceinfo').textContent=`${audit.enumerated} / ${audit.combinatorial_space} combinations · ${audit.legal_configurations} legal · ${(audit.sampled||[]).length} sampled. Held out: ${(audit.held_out||[]).join(', ')}`;}
function select(id){laneId=id;revision=-1;phase='live';phaseGeo=null;preview=null;updateControls();render();if(!fitted)fit();document.dispatchEvent(new CustomEvent('yapnr:lane',{detail:{lane:id}}))}
function updateControls(){let s=display();if(!s)return;let ls=Object.values(s.lanes);if(!laneId&&ls.length)laneId=ls.find(l=>!l.id.endsWith('/search'))?.id||ls[0].id;$('lanes').replaceChildren();for(let l of ls.slice(-24)){let b=document.createElement('button');b.className='lane'+(l.id===laneId?' active':'');b.textContent=l.id+' · '+(l.status||l.kind);let sub=document.createElement('span');sub.textContent=(l.phase||'placement probes')+(l.opens!=null?` · ${l.opens} opens / ${l.violations??'?'} violations`:'');b.append(sub);b.title='Click: show this lane · Shift+click: add it to the Ask context';b.onclick=e=>e.shiftKey?askAdd({kind:'lane',lane:l.id}):select(l.id);$('lanes').append(b)}let ph=$('phase');ph.replaceChildren(new Option('Live transaction','live'));for(let [i,f] of (lane()?.frames||[]).entries())ph.add(new Option(`${f.name}${f.opens==null?' · native DRC not run':' · '+f.opens+' opens'}`,String(i)));ph.value=phase;$('headline').textContent=`${laneId||'Waiting'} · ${phase==='live'?(lane()?.phase||'initialization'):(lane()?.frames[Number(phase)]?.name||'selected checkpoint')}`;$('headline').title=$('headline').textContent;$('detail').textContent=`Revision ${s.revision} · ${pinned?'PINNED':'LIVE'} · ${lane()?.kind||''} · ${annotations.length} rectangles`;$('events').replaceChildren();for(let e of s.events.slice(-35).reverse()){let div=document.createElement('div');div.className='event';div.textContent=new Date(e.time*1000).toLocaleTimeString()+' '+e.candidate+' '+e.kind+' '+JSON.stringify(e.data).slice(0,190);div.title='Click: inspect · Shift+click: add to the Ask context';div.onclick=ev=>{let it=eventItem(e);ev.shiftKey?askAdd(it):inspectItem(it)};$('events').append(div)}}
let pinPromise=null,draftRevision=0,draftSavedRevision=-1,finalizedRevision=-1,draftTimer=null,draftRestored=false;
const hasAnnotation=()=>annotations.length>0||$('note').value.trim().length>0;
const draftKey=run=>'pnr-annotation-draft-v1:'+run;
function annotationBody(){return {pin_id:pinId,draft_revision:draftRevision,annotations:JSON.parse(JSON.stringify(annotations)),view:{lane:laneId,phase,viewport:{...view},layers:[...layers],changes:$('changes').checked,labels:$('labels').checked,air:$('air').checked,costs:$('costs').checked},note:$('note').value};}
function backupDraft(){let run=display()?.run;if(!run)return;try{window.localStorage?.setItem(draftKey(run),JSON.stringify({...annotationBody(),run}));}catch(e){$('saved').textContent='Browser backup unavailable; keep this tab open until the server confirms the draft.';}}
async function pin(){if(pinId)return;if(pinPromise)return pinPromise;
 pinPromise=(async()=>{let r=await api('/api/pin',{});pinId=r.pin_id;pinned=r.state;$('pause').textContent='Resume live';if(hasAnnotation())backupDraft();updateControls();render()})();
 try{await pinPromise}finally{pinPromise=null}}
async function saveDraft(){await pin();let body=annotationBody();if(body.draft_revision<=finalizedRevision)return;backupDraft();try{await api('/api/draft',body);if(pinId===body.pin_id&&draftRevision===body.draft_revision&&body.draft_revision>finalizedRevision){draftSavedRevision=body.draft_revision;$('saved').textContent='Draft saved on this server. Save structured snapshot to create the full review bundle.';}}catch(e){if(pinId===body.pin_id&&draftRevision===body.draft_revision)$('saved').textContent='Draft not synced: '+e.message+' · browser backup retained.';throw e;}}
function annotationChanged(){draftRevision++;backupDraft();$('saved').textContent='Saving annotation draft…';if(draftTimer!==null)clearTimeout(draftTimer);draftTimer=setTimeout(()=>{draftTimer=null;saveDraft().catch(()=>{});},500);}
async function restoreDraft(){if(draftRestored||!state?.run)return;draftRestored=true;
 let raw;try{raw=window.localStorage?.getItem(draftKey(state.run));}catch(e){return}if(!raw)return;
 try{let body=JSON.parse(raw);if(body.run!==state.run)return;
  if(!body.pin_id){$('note').value=body.note||'';$('saved').textContent='Recovered a note without a pinned board. Select its checkpoint before saving.';return}
  let recovered=await api('/api/pins/'+body.pin_id);if(recovered.run!==state.run)throw Error('draft belongs to a different run');
  let selected=body.view,entry=recovered.lanes[selected.lane];if(!entry)throw Error('draft lane is missing');
  let geometry=selected.phase==='live'?null:await api('/api/geometry/'+(entry.frames[Number(selected.phase)].board_sha256||entry.frames[Number(selected.phase)].layout_sha256));
  pinned=recovered;pinId=body.pin_id;laneId=selected.lane;phase=selected.phase;phaseGeo=geometry;annotations=body.annotations||[];$('note').value=body.note||'';draftRevision=body.draft_revision||0;view={...selected.viewport};fitted=true;
  layers.clear();for(let l of selected.layers||Object.keys(colors))layers.add(l);for(let [i,l] of Object.keys(colors).entries())$('layers').children[i].children[0].checked=layers.has(l);
  for(let id of ['changes','labels','air','costs'])$(id).checked=selected[id];$('pause').textContent='Resume live';updateControls();render();$('saved').textContent='Recovered annotation draft on its original pinned board.';
 }catch(e){$('saved').textContent='Draft recovery failed: '+e.message+' · browser backup retained.';}}
$('note').oninput=()=>{annotationChanged();pin().catch(e=>{$('saved').textContent='Cannot pin annotation: '+e.message;});};
if(window.addEventListener)window.addEventListener('beforeunload',e=>{if(hasAnnotation()&&draftSavedRevision<draftRevision){backupDraft();e.preventDefault();e.returnValue='';}});
$('pause').onclick=async()=>{try{if(pinned){let version=draftRevision;if(hasAnnotation())await saveSnapshot();if(version!==draftRevision){$('saved').textContent='Annotation changed while saving. Remain pinned to save the newer draft.';return;}if(draftTimer!==null){clearTimeout(draftTimer);draftTimer=null;}pinned=null;pinId=null;$('note').value='';drawing=false;$('rect').textContent='Draw rectangle · pins state';$('pause').textContent='Pause / pin';annotations=[];updateControls();render()}else await pin()}catch(e){$('saved').textContent=e.message}};
$('rect').onclick=async()=>{try{await pin();drawing=!drawing;$('rect').textContent=drawing?'Draw on board…':'Draw rectangle · pins state';canvas.style.cursor=drawing?'crosshair':'grab'}catch(e){$('saved').textContent=e.message}};
$('undo').onclick=()=>{annotations.pop();annotationChanged();render()};$('fit').onclick=fit;
$('phase').onchange=async()=>{phase=$('phase').value;phaseGeo=null;if(phase!=='live')phaseGeo=await api('/api/geometry/'+(lane().frames[Number(phase)].board_sha256||lane().frames[Number(phase)].layout_sha256));updateControls();render()};
for(let id of ['changes','labels','air','costs'])$(id).onchange=render;
async function saveSnapshot(){await pin();let body=annotationBody();backupDraft();let r=await api('/api/snapshot',body);if(pinId===body.pin_id&&draftRevision===body.draft_revision){draftSavedRevision=draftRevision;finalizedRevision=draftRevision;if(draftTimer!==null){clearTimeout(draftTimer);draftTimer=null;}try{window.localStorage?.removeItem(draftKey(pinned.run));}catch(e){}}let a=document.createElement('a');a.href=r.url;a.download=r.id+'.json';a.textContent='Download snapshot JSON';$('saved').replaceChildren(a,document.createElement('br'),document.createTextNode(r.path));}
$('snapshot').onclick=async()=>{try{await saveSnapshot()}catch(e){$('saved').textContent='Snapshot failed: '+e.message}};
canvas.onwheel=e=>{e.preventDefault();let r=canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,p=point(x,y),s=Math.max(1,Math.min(250,view.scale*Math.exp(-e.deltaY*.001)));view={scale:s,x:x-p[0]*s,y:y+p[1]*s};fastFrame()};
// Touch: Pointer Events fire once per finger (touch-action:none on #board keeps the page itself
// from scrolling/zooming), so a second finger landing is a pinch, not a second single-finger drag.
// bTouch tracks every active touch pointer; bPinchPrev is the previous frame's distance/midpoint
// (incremental, like onwheel is one step per event) so pinch-zoom and two-finger pan compose in
// one gesture, anchored at the live pinch midpoint. One-finger drag keeps its existing meaning
// (pans, or draws the armed rectangle/region tool); double-tap zooms in, or back out if already
// zoomed in, centred on the tap.
let bTouch=new Map(),bTouchDown=new Map(),bPinchPrev=null,bPinchQ=false,bPinchRaf=0,bDtap=new DoubleTap();
// Two simultaneous touchmoves arrive as two separate pointermove events (one per finger), so
// reading both fingers' positions on the first of the two would see one finger already moved and
// the other stale -- a spurious, momentary distance change. Bookkeeping (bTouch.set) happens on
// every pointermove; the gesture itself is computed at most once per animation frame, by which
// point both fingers' events for that frame have already landed.
function bApplyPinch(){bPinchQ=false;if(bTouch.size<2)return;let d=touchDist(bTouch),m=touchMid(bTouch);
 if(bPinchPrev){view.x+=m[0]-bPinchPrev.mid[0];view.y+=m[1]-bPinchPrev.mid[1];
  let wp=point(...m),s=Math.max(1,Math.min(250,view.scale*(bPinchPrev.d>0?d/bPinchPrev.d:1)));
  view={scale:s,x:m[0]-wp[0]*s,y:m[1]+wp[1]*s}}
 bPinchPrev={d,mid:m};fastFrame()}
canvas.onpointerdown=e=>{canvas.setPointerCapture(e.pointerId);let r=canvas.getBoundingClientRect(),p=[e.clientX-r.left,e.clientY-r.top];
 if(e.pointerType==='touch'){bTouch.set(e.pointerId,p);bTouchDown.set(e.pointerId,p);
  if(bTouch.size>=2){drag=null;bPinchPrev={d:touchDist(bTouch),mid:touchMid(bTouch)};return}} // a second finger lands: baseline now, so the very first move already has one to measure against
 drag={start:p,last:p,world:point(...p),view:{...view},draw:drawing,region:regionMode&&!drawing}};
canvas.onpointermove=e=>{let r=canvas.getBoundingClientRect(),p=[e.clientX-r.left,e.clientY-r.top];
 if(e.pointerType==='touch'&&bTouch.has(e.pointerId)){bTouch.set(e.pointerId,p);
  if(bTouch.size>=2){if(!bPinchQ){bPinchQ=true;bPinchRaf=requestAnimationFrame(bApplyPinch)}return}}
 let q=point(...p);$('coords').textContent=q.map(v=>v.toFixed(2)).join(', ')+' mm';if(drag){drag.last=p;if(!drag.draw&&!drag.region){view.x=drag.view.x+p[0]-drag.start[0];view.y=drag.view.y+p[1]-drag.start[1]}fastFrame();return}if(!geo())return;let nb=window.YapnrNotes?.badgeAt?.(p[0],p[1]);if(nb&&canvas.style.cursor!=='pointer'){canvas.dataset.cur=canvas.style.cursor;canvas.style.cursor='pointer'}else if(!nb&&canvas.style.cursor==='pointer')canvas.style.cursor=canvas.dataset.cur||'';if(nb){$('hover').style.display='block';$('hover').textContent=nb.text;return}let hit=hoverText(boardHit(q));$('hover').style.display=hit?'block':'none';$('hover').textContent=hit||''};
canvas.addEventListener('pointerleave',()=>{if(!drag)$('hover').style.display='none'});  // the hover box (part, pad or note badge) does not outlive the pointer
function bTouchEnd(e){if(e.pointerType!=='touch')return false;
 if(bPinchQ){cancelAnimationFrame(bPinchRaf);bApplyPinch()} // flush a still-queued gesture frame before this finger (and its bTouch entry) goes away
 let was=bTouch.size,down=bTouchDown.get(e.pointerId),r=canvas.getBoundingClientRect(),up=[e.clientX-r.left,e.clientY-r.top];
 bTouch.delete(e.pointerId);bTouchDown.delete(e.pointerId);
 if(was>=2){bPinchPrev=null;drag=null;if(bTouch.size<2)render();return true} // ending a pinch/two-finger gesture: never a click or a draw
 if(was===1&&bTouch.size===0&&down&&bDtap.hit(down,up)){let s=view.scale<40?Math.min(120,view.scale*2.5):8,wp=point(...up);view={scale:s,x:up[0]-wp[0]*s,y:up[1]+wp[1]*s};drag=null;render();return true}
 return false}
canvas.addEventListener('pointercancel',e=>bTouchEnd(e));
canvas.onpointerup=e=>{if(bTouchEnd(e))return;if(drag&&!drag.draw&&!drag.region&&Math.hypot(drag.last[0]-drag.start[0],drag.last[1]-drag.start[1])<3){let nb=window.YapnrNotes?.badgeAt?.(drag.last[0],drag.last[1]);if(nb){if(e.shiftKey)askAdd(nb.item,' (note badge)');else window.YapnrNotes.showFor(nb.item);drag=null;render();return}}if(drag?.region)finishRegion();else if(drag&&!drag.draw&&Math.hypot(drag.last[0]-drag.start[0],drag.last[1]-drag.start[1])<3)boardClick(point(...drag.last),e.shiftKey);if(drag?.draw){let a=drag.world,b=point(...drag.last);if(Math.hypot(a[0]-b[0],a[1]-b[1])>.05)annotations.push({bounds:[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])],text:$('note').value||`Issue ${annotations.length+1}`,lane:laneId,phase,board_sha256:phase==='live'?lane()?.board_sha256:lane()?.frames[Number(phase)]?.board_sha256,event_id:lane()?.event_id});annotationChanged();}drag=null;render()};
$('tree').onclick=e=>{let r=$('tree').getBoundingClientRect(),p=treeHits.find(p=>Math.hypot(p.x-e.clientX+r.left,p.y-e.clientY+r.top)<10);if(p)e.shiftKey?askAdd({kind:'lane',lane:p.id},' (Shift+click)'):select(p.id)};
$('space').onmousemove=e=>{let r=$('space').getBoundingClientRect(),p=spaceHits.find(p=>Math.hypot(p.x-e.clientX+r.left,p.y-e.clientY+r.top)<5);if(p)$('space').title=JSON.stringify(p.p)};
$('space').onclick=e=>{let r=$('space').getBoundingClientRect(),p=spaceHits.find(p=>Math.hypot(p.x-e.clientX+r.left,p.y-e.clientY+r.top)<5);if(e.shiftKey){if(p)askAdd(probeItem(p.p));return}preview=p?.p.moves?p.p:null;render()};
window.onresize=render;
async function poll(){try{let fresh=await api('/api/state?since='+revision+'&run='+encodeURIComponent(state?.run||'')+(laneId?'&lane='+encodeURIComponent(laneId):''));if(!fresh.unchanged)state=fresh;await restoreDraft();showControls();$('health').textContent=state.errors.length?'TELEMETRY ERROR · '+state.errors.at(-1).error:`● LOCAL LIVE · ${state.revision} events`;$('health').style.color=state.errors.length?'#ff9292':'#9ee6d1';if(!pinned&&revision!==state.revision){revision=state.revision;updateControls();render();if(!fitted&&geo())fit()}}catch(e){$('health').textContent='DISCONNECTED · '+e.message}setTimeout(poll,1200)}poll();

const controlKeys=['route_workers','candidate_workers','samples','k','n'];let controlRevision=null,controlMessage=null;
function showControls(){let requested=state?.controls;if(!requested)return;
 if(controlRevision!==requested.revision){for(let k of controlKeys)$('cfg-'+k).value=requested.values[k];controlRevision=requested.revision}
 let active=state.active_controls,v=active?.values;
 const selected=lane(),phaseName=selected?.phase||'initializing';
 const running=Object.values(state.lanes||{}).filter(l=>l.status==='start'&&!l.id.endsWith('/search')).length;
 const batchedElectrical=selected?.worker_config?.native_batch_modes?.includes('power');
 const serial= (batchedElectrical?/pair|usb|coalesc|refill|audit|placement/i:/power|plane|pair|usb|coalesc|refill|audit|placement/i).test(phaseName);
 const workerText=serial?`Current phase ${phaseName}: serial. Coupled transactions and final validation remain serial.`:`Route-worker acknowledgement: ${selected?.worker_config?.route_workers??'pending'} (a limit, not a busy-worker count).`;
 $('control-active').textContent=`${running} candidate(s) currently active. `+(active?.round===1?'Round 1 is a single baseline; candidate parallelism begins in round 2. ':'')+(v?`Round limits: ${v.candidate_workers} candidates, K${v.k}/N${v.n}, ${v.samples} alternatives. `:'')+workerText;
 const values=controlKeys.map(k=>`${k}=${requested.values[k]}`).join(', ');
 $('control-status').textContent=controlMessage||`Saved revision ${requested.revision}: ${values}. `+(active?.revision===requested.revision?'Round settings acknowledged.':'Pending next placement round.');
 if(state.restart_status&&requested.apply_mode==='restart_round')$('control-status').textContent+=' Restart: '+state.restart_status.status+(state.restart_status.error?' — '+state.restart_status.error:'');

}
let pendingControls=null;
$('apply-controls').onclick=()=>{try{
 pendingControls=Object.fromEntries(controlKeys.map(k=>[k,Number($('cfg-'+k).value)]));
 if(pendingControls.route_workers*pendingControls.candidate_workers>16)throw Error('route workers × concurrent candidates must be at most 16');
 $('restart-choice').hidden=false;controlMessage='Choose when to apply. Settings have not been saved yet.';
}catch(e){pendingControls=null;controlMessage='Apply failed — '+e.message;}showControls()};
async function saveControls(mode){if(!pendingControls)return;try{
 const r=await api('/api/controls',{values:pendingControls,expected_revision:controlRevision,apply_mode:mode});
 controlMessage=`Saved revision ${r.revision} for this and future runs. `+(mode==='restart_round'?'Restart requested: unfinished candidates will be archived and rerun.':'Applying at the next safe boundary.');
 state.controls=r;controlRevision=null;revision=-1;pendingControls=null;$('restart-choice').hidden=true;
}catch(e){controlMessage='Apply failed — '+e.message;}showControls()}
$('controls-boundary').onclick=()=>saveControls('boundary');
$('controls-restart').onclick=()=>saveControls('restart_round');
$('controls-cancel').onclick=()=>{pendingControls=null;$('restart-choice').hidden=true;controlMessage='Cancelled; saved settings unchanged.';showControls()};

// ------------------------------------------------------------------ Inspect / Source / Ask integration
// window.YapnrView for dist/source.js and dist/agent.js (both optional). Board clicks select into
// Inspect, Shift+click adds to the Ask context, the region tool adds a {kind:'region'} item, and
// highlight() draws a pink selection (dimming the rest) that schematic.js mirrors.
let viewHl=null,regionMode=false;
const SRC=()=>window.YapnrSource||null,AGENT=()=>window.YapnrAgent||null,natural=(a,b)=>String(a).localeCompare(String(b),undefined,{numeric:true});
const HL='#ff7ad9';
function netTitle(name){let n=SRC()?.index?.()?.nets?.[name];return n&&n.low_info&&n.title&&n.title!==name?n.title:null}
function pinName(ref,pad){let p=SRC()?.index?.()?.components?.[ref]?.pins?.[pad]?.pin;return p&&p!==pad?p:null}
function hoverExtra(net,pin){let t=[pin,net&&netTitle(net)].filter(Boolean).join(' · ');return t?t+'\n':''}
const boardTools=$('pause').parentElement,regionBtn=document.createElement('button'),viewChip=document.createElement('span');
regionBtn.id='region-ask';regionBtn.className='ask-entry';regionBtn.title='Drag a rectangle on the board: its parts and nets become Ask context';regionBtn.textContent='Ask region';
viewChip.id='view-chip';viewChip.hidden=true;boardTools.prepend(viewChip,regionBtn);
function setRegionMode(on){regionMode=on;if(on&&drawing){drawing=false;$('rect').textContent='Draw rectangle · pins state'}regionBtn.classList.toggle('on',on);regionBtn.textContent=on?'Drag… (Esc)':'Ask region';canvas.style.cursor=on?'crosshair':'grab'}
regionBtn.onclick=()=>setRegionMode(!regionMode);
$('rect').addEventListener('click',()=>{if(regionMode)setRegionMode(false)});
addEventListener('keydown',e=>{if(e.key!=='Escape'||e.target?.closest?.('input,textarea,select'))return;if(regionMode){setRegionMode(false);drag=null;render()}else if(viewHl)window.YapnrView.clear()});
function padContains(p,q,tol){let t=-(p.angle||0)*Math.PI/180,dx=q[0]-p.xy[0],dy=q[1]-p.xy[1],x=dx*Math.cos(t)-dy*Math.sin(t),y=dx*Math.sin(t)+dy*Math.cos(t);return Math.abs(x)<=p.size[0]/2+tol&&Math.abs(y)<=p.size[1]/2+tol}
function inPoly(q,path){let inside=false;for(let i=0,j=path.length-1;i<path.length;j=i++){let a=path[i],b=path[j];if((a[1]>q[1])!==(b[1]>q[1])&&q[0]<(b[0]-a[0])*(q[1]-a[1])/(b[1]-a[1])+a[0])inside=!inside}return inside}
// The one hit test for hover, click, Shift+click and the cost panel: pad > part outline > track > via > filled zone.
// Numberless pads (mechanical holes, shield tabs) are not selectable pads: they fall through to their part.
const boundsCache=new WeakMap(),partBounds=part=>{if(!boundsCache.has(part))boundsCache.set(part,componentBounds(part));return boundsCache.get(part)};
function boardHit(q){let g=geo();if(!g)return null;let tol=2/view.scale,vis=p=>p.layers.some(l=>layers.has(l)),best=null;
 for(let part of g.parts||[])for(let p of part.pads||[])if(p.number&&vis(p)&&p.size?.every(Number.isFinite)&&padContains(p,q,tol))return {kind:'pad',ref:part.ref,pad:p.number,net:p.net,obj:p};
 for(let part of g.parts||[]){if(part.pads?.length&&!part.pads.some(vis))continue;let b=partBounds(part);if(b&&q[0]>=b[0]-.3&&q[0]<=b[2]+.3&&q[1]>=b[1]-.3&&q[1]<=b[3]+.3){let area=(b[2]-b[0])*(b[3]-b[1]);if(!best||area<best.area)best={kind:'component',ref:part.ref,area,obj:part}}}
 if(best)return best;
 // Grid-accelerated: same exact math as a linear scan over every track/via, over only the
 // handful near q -- padded by a whole cell plus tol so nothing a full scan would have found is
 // ever missed (a track's bbox is stamped into every cell it touches; see gridFor()).
 let idx=gridFor(g),padq=idx.cell+tol,cand=gridCandidates(idx,q[0]-padq,q[1]-padq,q[0]+padq,q[1]+padq);
 // Sorted back to original array order so "first match wins" (two tracks/vias both covering q,
 // e.g. a crossing) picks the exact same one a full linear scan would have.
 for(let i of [...cand.tracks].sort((a,b)=>a-b)){let t=g.tracks[i];if(!t[0]||!layers.has(t[1]))continue;let a=t[2],b=t[3],dx=b[0]-a[0],dy=b[1]-a[1],f=Math.max(0,Math.min(1,((q[0]-a[0])*dx+(q[1]-a[1])*dy)/(dx*dx+dy*dy||1)));if(Math.hypot(q[0]-a[0]-f*dx,q[1]-a[1]-f*dy)<tol+t[4]/2)return {kind:'track',net:t[0],obj:t}}
 for(let i of [...cand.vias].sort((a,b)=>a-b)){let v=g.vias[i];if(v.net&&Math.hypot(q[0]-v.xy[0],q[1]-v.xy[1])<v.diameter/2+tol)return {kind:'via',net:v.net,obj:v}}
 for(let l of ['F.Cu','B.Cu','In1.Cu','In2.Cu'])if(layers.has(l))for(let z of g.zones||[])if(z.layer===l&&z.net&&z.paths.filter(path=>inPoly(q,path)).length%2)return {kind:'zone',net:z.net,obj:z};
 return null}
function hoverText(h){if(!h)return '';let o=h.obj;
 if(h.kind==='pad')return `${h.ref}.${h.pad} · ${h.net||'no net'}\n`+hoverExtra(h.net,pinName(h.ref,h.pad))+`${o.layers.join(', ')} · pad ${o.size.join(' × ')} mm`;
 if(h.kind==='component'){let c=SRC()?.index?.()?.components?.[h.ref];return h.ref+(c?' · '+(c.type||c.part||'')+'\n'+(c.instance||c.address||''):'')+'\nClick: inspect this part'}
 if(h.kind==='track')return `${h.net} · ${o[1]}\n`+hoverExtra(h.net)+`Native trace width ${o[4].toFixed(3)} mm`;
 if(h.kind==='via')return `${h.net} · via\n`+hoverExtra(h.net)+`Diameter ${(+o.diameter).toFixed(2)} mm`;
 return `${h.net} · ${o.layer} zone\n`+hoverExtra(h.net).trimEnd()}
function itemText(it){return it.kind==='component'?it.ref:it.kind==='pad'?it.ref+'.'+it.pad:it.kind==='net'?(SRC()?.netLabel?.(it.name)||it.name):it.kind==='lane'?'lane '+it.lane:it.kind==='event'?'event '+it.event_kind:it.kind==='probe'?it.label:it.kind}
function inspectItem(item){let S=SRC();if(S?.inspect)S.inspect(item);else document.dispatchEvent(new CustomEvent('yapnr:select',{detail:item}))}
// Short confirmation over the board: the status line sits far down the sidebar and the dock may be collapsed.
let toastT=0;function boardToast(msg){let el=$('board-toast');if(!el){el=document.createElement('div');el.id='board-toast';el.setAttribute('role','status');$('boardwrap').append(el)}el.textContent=msg;el.hidden=false;clearTimeout(toastT);toastT=setTimeout(()=>{el.hidden=true},2600)}
function askAdd(item,where){let A=AGENT();if(!A){boardToast('The Ask panel is not loaded.');return}if(A.enabled?.()===false){boardToast(A.offText);return}A.addContext(item);window.YapnrDock?.badge?.('ask',true);let n=A.context?.().length,msg='Added '+itemText(item)+' to the Ask context'+(where||'')+(n?' · '+n+' item'+(n>1?'s':''):'');$('component-status').textContent=msg+'.';boardToast(msg)}
function eventItem(e){return {kind:'event',id:String(e.id),lane:e.candidate||undefined,event_kind:e.kind,summary:JSON.stringify({time:e.time,iteration:e.iteration,data:e.data}).slice(0,1500)}}
function probeItem(p){return {kind:'probe',label:'Placement alternative · '+(p.reason?'rejected':'cost '+(+p.cost).toFixed(3)),lane:laneId||undefined,summary:JSON.stringify(p).slice(0,1500)}}
function boardClick(q,shift){let h=boardHit(q);if(!h)return null;
 let item=h.kind==='pad'?{kind:'pad',ref:h.ref,pad:h.pad}:h.kind==='component'?{kind:'component',ref:h.ref}:{kind:'net',name:h.net};
 if(shift){askAdd(item);return h}
 inspectItem(item);
 // pads and parts also drive the cost panel (cost-inspector.js): one hit test, one selected part
 if(h.kind==='pad'||h.kind==='component'){selectedPartRef=h.ref;$('component-query').value=h.ref;$('component-status').textContent=h.ref+' selected'}
 if(h.kind==='component')window.YapnrView.clear(true);
 else window.YapnrView.highlight(h.kind==='pad'?{pads:[h.ref+'.'+h.pad],nets:h.net?[h.net]:[]}:{nets:[h.net]},{frame:false,label:h.kind==='pad'?h.ref+'.'+h.pad+(h.net?' · '+(netTitle(h.net)||h.net):''):null});
 return h}
function finishRegion(){let a=drag.world,b=point(...drag.last);setRegionMode(false);if(Math.hypot(a[0]-b[0],a[1]-b[1])<.3)return;
 let bb=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])].map(v=>+v.toFixed(3)),g=geo(),inside=p=>p&&p[0]>=bb[0]&&p[0]<=bb[2]&&p[1]>=bb[1]&&p[1]<=bb[3],refs=new Set(),nets=new Set();
 for(let part of g?.parts||[]){let hit=inside(part.xy);for(let p of part.pads||[])if(inside(p.xy)){hit=true;if(p.net)nets.add(p.net)}if(hit)refs.add(part.ref)}
 for(let t of g?.tracks||[])if(t[0]&&layers.has(t[1])&&(inside(t[2])||inside(t[3])))nets.add(t[0]);
 for(let v of g?.vias||[])if(v.net&&inside(v.xy))nets.add(v.net);
 let item={kind:'region',lane:laneId,bbox:bb,refs:[...refs].sort(natural).slice(0,400),nets:[...nets].sort(natural).slice(0,400)};
 window.YapnrView.highlight({refs:item.refs,nets:[],pads:[],bbox:bb},{frame:false,label:`Region · ${item.refs.length} parts · ${item.nets.length} nets`});
 let A=AGENT();if(A?.enabled?.()===false)$('component-status').textContent=A.offText;else if(A){A.addContext(item);A.open?.()}else $('component-status').textContent='The Ask panel is not loaded.'}
function padShape(p,color){ctx.fillStyle=color;if(p.polys?.length){ctx.beginPath();for(let poly of p.polys){poly.forEach((q,i)=>{let [sx,sy]=screen(q);i?ctx.lineTo(sx,sy):ctx.moveTo(sx,sy)});ctx.closePath()}ctx.fill();return}
 let [x,y]=screen(p.xy);ctx.save();ctx.translate(x,y);ctx.rotate(-p.angle*Math.PI/180);if(p.shape==='circle'){ctx.beginPath();ctx.ellipse(0,0,p.size[0]*view.scale/2,p.size[1]*view.scale/2,0,0,Math.PI*2);ctx.fill()}else ctx.fillRect(-p.size[0]*view.scale/2,-p.size[1]*view.scale/2,p.size[0]*view.scale,p.size[1]*view.scale);ctx.restore()}
function drawViewHl(g){let H=viewHl;if(!H||!g)return;
 ctx.save();ctx.setTransform(1,0,0,1,0,0);ctx.fillStyle='rgba(9,15,19,.55)';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.restore();
 if(H.region){let a=screen([H.region[0],H.region[3]]),b=screen([H.region[2],H.region[1]]);ctx.fillStyle='#ffb34712';ctx.strokeStyle='#ffb347';ctx.lineWidth=1.5;ctx.setLineDash([6,4]);ctx.fillRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.strokeRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.setLineDash([])}
 if(H.nets.size){for(let t of g.tracks||[])if(H.nets.has(t[0])&&layers.has(t[1]))line(t[2],t[3],HL,Math.max(1.5,t[4]*view.scale),[],true);for(let v of g.vias||[])if(H.nets.has(v.net))circle(v.xy,Math.max(2.5,v.diameter*view.scale/2),HL)}
 for(let part of g.parts||[]){let mine=H.refs.has(part.ref);
  for(let p of part.pads||[]){if(!p.layers.some(l=>layers.has(l)))continue;let on=H.pads.has(part.ref+'.'+p.number);if(on||(p.net&&H.nets.has(p.net)))padShape(p,HL);else if(mine)padShape(p,'#f1e4ec');
   if(on){ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.beginPath();ctx.arc(...screen(p.xy),Math.max(7,Math.max(...p.size)*view.scale*.75),0,7);ctx.stroke()}}
  let b=mine&&componentBounds(part);if(b){let a=screen([b[0]-.8,b[3]+.8]),c=screen([b[2]+.8,b[1]-.8]);ctx.strokeStyle=HL;ctx.lineWidth=2;ctx.strokeRect(a[0],a[1],c[0]-a[0],c[1]-a[1]);ctx.fillStyle=HL;ctx.font='bold 12px system-ui';ctx.fillText(part.ref,a[0],a[1]-5)}}}
function viewHlBounds(g,H){let pts=[];
 for(let part of g.parts||[]){if(H.refs.has(part.ref)){let b=componentBounds(part);if(b)pts.push(b.slice(0,2),b.slice(2))}for(let p of part.pads||[])if(H.pads.has(part.ref+'.'+p.number)||(p.net&&H.nets.has(p.net)))pts.push(p.xy)}
 for(let t of g.tracks||[])if(H.nets.has(t[0]))pts.push(t[2],t[3]);if(H.region)pts.push(H.region.slice(0,2),H.region.slice(2));
 return pts.length?[Math.min(...pts.map(p=>p[0])),Math.min(...pts.map(p=>p[1])),Math.max(...pts.map(p=>p[0])),Math.max(...pts.map(p=>p[1]))]:null}
// Frame only when something highlighted is off-screen; keeps the user's view otherwise.
function frameViewHl(){let H=viewHl,g=geo(),w=canvas.clientWidth,h=canvas.clientHeight;if(!H?.pendingFrame||!g||w<80||h<80)return false;H.pendingFrame=false;
 let b=viewHlBounds(g,H);if(!b){$('component-status').textContent=H.label+' · not on this board';return false}
 let a=screen([b[0],b[3]]),c=screen([b[2],b[1]]);if(a[0]>=0&&a[1]>=0&&c[0]<=w&&c[1]<=h)return false;
 let scale=Math.max(1,Math.min(120,(w-80)/Math.max(4,b[2]-b[0]),(h-80)/Math.max(4,b[3]-b[1])));view={scale,x:w/2-(b[0]+b[2])/2*scale,y:h/2+(b[1]+b[3])/2*scale};fitted=true;return true}
function hlLabel(refs,nets,pads){let all=[...refs,...pads,...[...nets].map(n=>{let t=netTitle(n);return t?t.split(' — ')[0]+' ('+n+')':n})];return all.slice(0,3).join(', ')+(all.length>3?` +${all.length-3}`:'')}
function updateViewChip(){viewChip.hidden=!viewHl;if(!viewHl)return;let x=document.createElement('button');x.textContent='×';x.title='Clear this highlight';x.onclick=()=>window.YapnrView.clear();let l=document.createElement('span');l.className='chip-l';l.textContent=viewHl.label;viewChip.replaceChildren(l,x);viewChip.title='Highlighted on the board: '+viewHl.label}
const renderBeforeView=render;render=function(){renderBeforeView();if(viewHl?.pendingFrame&&frameViewHl())renderBeforeView();if(viewHl&&!viewHl.named&&SRC()?.index?.()){viewHl.named=true;if(!viewHl.fixed){viewHl.label=hlLabel(viewHl.refs,viewHl.nets,viewHl.pads);updateViewChip()}}};
window.YapnrView={
 highlight(sel={},opt={}){let refs=new Set(sel.refs||[]),nets=new Set(sel.nets||[]),pads=new Set(sel.pads||[]);if(!refs.size&&!nets.size&&!pads.size&&!sel.bbox)return window.YapnrView.clear();
  viewHl={refs,nets,pads,region:sel.bbox||null,label:opt.label||hlLabel(refs,nets,pads),fixed:!!opt.label,named:!!SRC()?.index?.(),pendingFrame:opt.frame!==false};updateViewChip();
  let one=refs.size===1&&!nets.size&&!pads.size?[...refs][0]:pads.size===1&&!refs.size?[...pads][0].split('.')[0]:null;
  if(one&&opt.frame!==false){viewHl.pendingFrame=false;jumpToComponent(one)}else render();return true},
 clear(keep){viewHl=null;updateViewChip();if(!keep)render()},
 boardSha:()=>phase==='live'?lane()?.board_sha256:lane()?.frames?.[Number(phase)]?.board_sha256,
 lane:()=>laneId,selectLane:id=>display()?.lanes?.[id]?(select(id),true):false,phase:()=>phase,view:()=>document.body.dataset.view||'pcb',current:()=>viewHl&&{refs:[...viewHl.refs],nets:[...viewHl.nets],pads:[...viewHl.pads],bbox:viewHl.region},
 componentInfo(ref){let part=geo()?.parts?.find(p=>p.ref===ref);if(!part)return null;let smd=new Set((part.pads||[]).filter(p=>p.layers.length===1).map(p=>p.layers[0])),side=smd.has('F.Cu')&&smd.has('B.Cu')?'both sides':smd.has('B.Cu')?'bottom':smd.has('F.Cu')?'top':'through-hole';return {xy:part.xy,side,pads:(part.pads||[]).map(p=>p.number)}},
 netInfo(name){let g=geo();if(!g)return null;let ts=(g.tracks||[]).filter(t=>t[0]===name),pads=(g.parts||[]).flatMap(part=>(part.pads||[]).filter(p=>p.net===name).map(p=>part.ref+'.'+p.number));
  return {tracks:ts.length,vias:(g.vias||[]).filter(v=>v.net===name).length,length_mm:ts.reduce((s,t)=>s+Math.hypot(t[3][0]-t[2][0],t[3][1]-t[2][1]),0),pads}}};
// URL parameters for sharing / headless checks: dock=inspect|source|ask, src=<file>:<line>[-end],
// inspect=ref:<REF>|net:<NAME>|pad:<REF.PAD>, ask=<comma list of ref:/net:/pad:/src: items>.
function urlState(){let q=new URLSearchParams(location.search),S=SRC(),A=AGENT(),K=window.YapnrDock;
 const item=(k,v)=>{let i=v.indexOf('.');return k==='ref'||k==='component'?{kind:'component',ref:v.toUpperCase()}:k==='net'?{kind:'net',name:v}:k==='pad'&&i>0?{kind:'pad',ref:v.slice(0,i).toUpperCase(),pad:v.slice(i+1)}:k==='src'&&S?.srcParse?(s=>({kind:'source',file:s.file,line:s.line,end:s.end}))(S.srcParse(v)):null};
 const parse=s=>{let i=s.indexOf(':');return i>0?item(s.slice(0,i).trim(),s.slice(i+1).trim()):null};
 for(let s of (q.get('ask')||'').split(',')){let it=s.trim()&&parse(s);if(it)A?.addContext(it)}
 let it=q.get('inspect')&&parse(q.get('inspect')),dock=q.get('dock');
 if(it){S?.inspect?.(it,{focus:!dock&&!q.get('src')});window.YapnrView.highlight(it.kind==='component'?{refs:[it.ref]}:it.kind==='net'?{nets:[it.name]}:it.kind==='pad'?{pads:[it.ref+'.'+it.pad],nets:[]}:{})}
 let src=q.get('src')&&S?.srcParse?.(q.get('src'));
 if(src)S.open(src.file,src.line,src.end);else if(it&&dock==='source')it.kind==='net'?S?.showNet?.(it.name):it.kind!=='source'&&S?.showComponent?.(it.ref);
 dock=dock||(q.get('ask')?'ask':null);if(dock&&K)K.tab(dock)}
document.addEventListener('DOMContentLoaded',urlState);
// notes.js: badges are drawn last by the outermost render wrapper (schematic.js); redraw when the notes set or the toggle changes
document.addEventListener('yapnr:notes',()=>render());
// About / Source (AGPL-3.0 section 13): the configured title, and a link to the source tree of the exact revision
// the server runs ("modified": the checkout has uncommitted changes, so that tree is not all of it).
fetch('/api/about').then(r=>r.json()).then(a=>{let b=$('brand'),s=$('about');if(a.title){if(b)b.textContent=String(a.title).toUpperCase();document.title=a.title+' · Live routing laboratory'}if(s){let url=a.source_link||a.source_url;if(url)s.href=url;s.textContent='Source'+(a.revision?' · '+a.revision.slice(0,7)+(a.modified?' (modified)':''):'');s.title=['yapnr',a.version,a.revision,a.modified?'with uncommitted changes':null,a.license,'source code'].filter(Boolean).join(' · ')}if(a.missing_assets?.length)console.warn('viewer: third-party files missing from this server:',a.missing_assets.join(', '))}).catch(()=>{});
