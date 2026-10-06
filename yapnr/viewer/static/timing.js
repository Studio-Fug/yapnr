'use strict';
// Timing tab: GET /api/timing[?scope=<tree path>] -- per-stage summary table, log-x histograms,
// stacked stage-share bars per group, and a Gantt timeline with a concurrency line. Scope follows
// the currently selected lane's top-level ancestor (window.YapnrView.lane()); "" (whole campaign)
// when nothing is selected. Clicking a histogram bin or a Gantt bar selects those lanes through
// window.YapnrView.selectLane, the same hook the tree panel uses. Phone-width safe: the Gantt and
// histograms scroll horizontally inside their own box rather than widening the page. No charting
// library -- everything here is inline SVG.
(function(){
const D=document,DK=()=>window.YapnrDock||null,V=()=>window.YapnrView||null;
if(!DK())return; // a stripped-down static/ checkout without the dock: nothing to mount
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
function h(t,a,...k){let e=D.createElement(t);if(a)for(let [x,v] of Object.entries(a)){if(v==null||v===false)continue;if(x==='class')e.className=v;else if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v===true?'':v)}for(let c of k.flat(9))if(c!=null&&c!==false)e.append(c.nodeType?c:String(c));return e}
const SVG_NS='http://www.w3.org/2000/svg';
function svgEl(t,a,...k){let e=D.createElementNS(SVG_NS,t);if(a)for(let [x,v] of Object.entries(a)){if(v==null||v===false)continue;if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v)}for(let c of k.flat(9))if(c!=null&&c!==false)e.append(c.nodeType?c:D.createTextNode(String(c)));return e}
const fmt=s=>{if(s==null)return '—';if(s<1)return (s*1000).toFixed(0)+'ms';if(s<90)return s.toFixed(1)+'s';if(s<5400)return (s/60).toFixed(1)+'m';return (s/3600).toFixed(1)+'h'};
const STAGE_COLORS=['#5b8def','#54b399','#d6bf57','#da7a58','#b36ad3','#4fb6c6','#e6756f','#8a9bb0','#7fbf7f','#c98bd9','#6ea8d8'];
const colorFor=(stage,order)=>STAGE_COLORS[Math.max(0,order.indexOf(stage))%STAGE_COLORS.length]||'#8a9bb0';

// source.js builds the dock's tab/panel markup synchronously from its own TABS list, which must
// include ['timing','Timing'] (a one-line addition there) for this element to exist.
const panel=DK().el('timing');
if(!panel)return; // a dock build with no 'timing' tab registered: nothing to mount into
panel.classList.add('timing-panel');

let state={loading:false,data:null,error:null,scope:''};

function currentScope(){
 let lane=V()?.lane?.();
 if(!lane)return '';
 let seg=lane.split('/');
 return seg.length>1?seg[0]:'';
}

function load(){
 let scope=currentScope();
 state.loading=true;state.scope=scope;renderStatus();
 fetch('/api/timing'+(scope?('?scope='+encodeURIComponent(scope)):''))
  .then(r=>r.ok?r.json():Promise.reject(new Error('HTTP '+r.status)))
  .then(data=>{state.loading=false;state.data=data;state.error=null;renderAll()})
  .catch(e=>{state.loading=false;state.error=e.message;renderAll()});
}

function renderStatus(){
 if(!panel.querySelector('.tm-status'))return;
 panel.querySelector('.tm-status').textContent=state.loading?'Loading…':'';
}

function histogramSvg(stage,stats,order){
 let {edges,counts}=stats.histogram||{};
 if(!edges||!edges.length)return h('div',{class:'tm-empty'},'no data');
 let w=260,hh=56,barW=w/counts.length,maxC=Math.max(1,...counts);
 let bars=counts.map((c,i)=>{
  let bh=hh*(c/maxC);
  return svgEl('rect',{x:i*barW+1,y:hh-bh,width:Math.max(1,barW-2),height:bh,fill:colorFor(stage,order),
   onclick:()=>selectByDuration(stage,edges[i],edges[i+1]),class:'tm-hist-bar'},
   svgEl('title',{},fmt(edges[i])+'–'+fmt(edges[i+1])+': '+c+' lane(s)'));
 });
 return svgEl('svg',{width:w,height:hh,viewBox:`0 0 ${w} ${hh}`,class:'tm-hist'},...bars);
}

function selectByDuration(stage,lo,hi){
 let lanes=(state.data?.timeline||[]).filter(l=>l.spans.some(s=>s.stage===stage&&s.seconds>=lo&&s.seconds<hi));
 if(lanes[0])V()?.selectLane?.(lanes[0].candidate);
}

function summaryTable(){
 let d=state.data;
 if(!d)return h('div');
 let rows=d.stage_order.map(stage=>{
  let s=d.stages[stage];
  return h('tr',{},
   h('td',{class:'tm-stage',style:`--c:${colorFor(stage,d.stage_order)}`},h('i'),stage),
   h('td',{},s.count),
   h('td',{},fmt(s.total)),
   h('td',{},fmt(s.mean)),
   h('td',{},fmt(s.median)),
   h('td',{},fmt(s.p90)),
   h('td',{},fmt(s.max)),
   h('td',{},(s.share*100).toFixed(0)+'%'),
   h('td',{},histogramSvg(stage,s,d.stage_order)));
 });
 return h('table',{class:'tm-table'},
  h('thead',{},h('tr',{},h('th',{},'stage'),h('th',{},'n'),h('th',{},'total'),h('th',{},'mean'),
   h('th',{},'median'),h('th',{},'p90'),h('th',{},'max'),h('th',{},'share'),h('th',{},'distribution'))),
  h('tbody',{},...rows));
}

function groupBars(){
 let d=state.data;
 if(!d||!d.groups.length)return h('div');
 let rows=d.groups.map(g=>{
  let segs=d.stage_order.filter(s=>g.shares[s]).map(s=>
   h('span',{class:'tm-seg',style:`--c:${colorFor(s,d.stage_order)};--w:${(g.shares[s]*100).toFixed(2)}%`},
    h('title',{},s+': '+(g.shares[s]*100).toFixed(0)+'%')));
  return h('div',{class:'tm-grouprow'},
   h('span',{class:'tm-groupname',title:g.group},g.group||'(root)'),
   h('span',{class:'tm-groupbar'},...segs),
   h('span',{class:'tm-grouptotal'},fmt(g.total)));
 });
 return h('div',{class:'tm-groups'},...rows);
}

function ganttSvg(){
 let d=state.data;
 if(!d||!d.timeline.length)return h('div',{class:'tm-empty'},'No lanes in scope yet.');
 let lanes=[...d.timeline].sort((a,b)=>a.start-b.start);
 let t0=Math.min(...lanes.map(l=>l.start)),t1=Math.max(...lanes.map(l=>l.end));
 let span=Math.max(1,t1-t0),rowH=16,w=900,pxPerS=w/span;
 let x=t=>(t-t0)*pxPerS;
 let segRects=[];
 lanes.forEach((lane,i)=>{
  let segs=lane.spans.length?lane.spans:[{stage:'other',start:lane.start,end:lane.end,seconds:lane.end-lane.start,estimated:true}];
  for(let s of segs)segRects.push(svgEl('rect',{x:x(s.start),y:i*rowH+1,width:Math.max(0.5,x(s.end)-x(s.start)),height:rowH-2,
   fill:colorFor(s.stage,d.stage_order),opacity:s.estimated?0.55:1,
   onclick:()=>V()?.selectLane?.(lane.candidate),class:'tm-gantt-seg'},
   svgEl('title',{},lane.candidate+' · '+s.stage+' · '+fmt(s.seconds)+(s.estimated?' (estimated)':''))));
  if(lane.running)segRects.push(svgEl('rect',{x:x(lane.end)-1,y:i*rowH+1,width:2,height:rowH-2,fill:'#fff',class:'tm-running-edge'}));
 });
 let concurrency=d.concurrency||[];
 let maxRun=Math.max(1,...concurrency.map(p=>p.running));
 let concH=lanes.length*rowH;
 let line=concurrency.map(p=>`${x(p.time)},${concH-(p.running/maxRun)*concH}`).join(' ');
 return h('div',{class:'tm-gantt-wrap'},
  svgEl('svg',{width:w,height:concH+4,viewBox:`0 0 ${w} ${concH+4}`,class:'tm-gantt'},
   ...segRects,
   concurrency.length?svgEl('polyline',{points:line,fill:'none',stroke:'#fff',['stroke-opacity']:0.5,['stroke-width']:1.5,class:'tm-concurrency'}):null));
}

function render404(){
 return h('div',{class:'tm-empty'},'No live events yet. The Timing panel fills in once the campaign starts emitting events.');
}

function renderAll(){
 panel.textContent='';
 let d=state.data;
 if(state.error){panel.append(h('div',{class:'tm-empty'},'Timing unavailable: '+esc(state.error)));return}
 if(!d||d.mode==='empty'){panel.append(render404());return}
 let modeNote=d.mode==='estimated'
  ?'Estimated from event timestamps (this campaign has no stage events).'
  :d.mode==='mixed'?'Mixed: some lanes observed, some estimated from event timestamps.':'Observed from stage events.';
 panel.append(
  h('div',{class:'tm-head'},
   h('span',{class:'tm-status'},state.loading?'Loading…':''),
   h('span',{class:'tm-scope'},state.scope?('Scope: '+state.scope):'Whole campaign'),
   h('button',{class:'tm-refresh',onclick:load,title:'Refresh'},'↻')),
  h('div',{class:'tm-mode'},modeNote+' · '+d.lane_count+' lane(s)'+(d.running_count?(', '+d.running_count+' running'):'')),
  h('h3',{},'Per-stage'),
  summaryTable(),
  h('h3',{},'Per-group breakdown'),
  groupBars(),
  h('h3',{},'Timeline'),
  ganttSvg(),
  h('h3',{},'Slowest lanes'),
  h('ol',{class:'tm-slowest'},...d.slowest.map(l=>h('li',{onclick:()=>V()?.selectLane?.(l.candidate),class:'tm-slowest-row'},
   h('span',{class:'tm-slowest-time'},fmt(l.seconds)),h('span',{},l.candidate),l.running?h('em',{}, ' running'):null))));
}

// Load when the Timing tab is first opened, and whenever it becomes the active tab again
// (another poll is cheap: the server caches incrementally, see yapnr/viewer/timing.py).
let lastScope=null;
D.addEventListener('yapnr:dock',()=>{
 if(DK().current()!=='timing'||!DK().isOpen())return;
 let scope=currentScope();
 if(scope!==lastScope||!state.data){lastScope=scope;load()}
});
window.YapnrTiming={reload:load};
})();
