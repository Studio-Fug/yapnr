'use strict';
// The experiment browser: a collapsible, directory-like tree of every lane the live directory
// has, replacing the old flat "last 24 lanes" button list in #lanes. The hierarchy itself is
// built once, server-side (yapnr/viewer/lane_tree.py, from the lane ids -- see docs/viewer.md),
// and shipped as state.tree; this file only renders it, so there is exactly one place that knows
// how a campaign's ids nest ("one place in code", the same rule the phase model follows). Each
// lane's progress bar and status text are likewise computed once server-side
// (yapnr/viewer/progress.py) and shipped on the lane itself (lane.progress, lane.status_text);
// this file reads them, it never re-derives a phase from raw telemetry.
//
// Shares app.js's top-level bindings ($, select, laneId) the same way schematic.js/cost-
// inspector.js do; its own state (expand/collapse, filters) stays in this IIFE and is published
// only as window.YapnrTree, mirroring panels.js.
(function(){
const ROOT=$('lanes');
if(!ROOT)return; // a stripped-down static/ checkout without the lanes panel: nothing to mount

const store={
 get(k,d){try{let v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},
 set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}},
};
let run=null,expandOverride={},filterText='',filterStates=new Set(['queued','running','done','failed']);
function expandKey(){return 'yapnr-tree-expand-v1:'+(run||'')}
function loadExpand(){expandOverride=store.get(expandKey(),{})}
function saveExpand(){store.set(expandKey(),expandOverride)}

// Depth 0/1 (a campaign's arms, and their immediate cases) open by default so a fresh page shows
// useful structure at a glance; anything deeper defaults closed, a large campaign's hundreds of
// seed/candidate leaves start out of the way until asked for.
function defaultExpanded(depth){return depth<2}
function isExpanded(node,depth){return node.id in expandOverride?expandOverride[node.id]:defaultExpanded(depth)}
function setExpanded(node,depth,on){
 if(on===defaultExpanded(depth))delete expandOverride[node.id];else expandOverride[node.id]=on;
 saveExpand();
}
function hasChildren(node){return node.order.length>0}

// ------------------------------------------------------------------ filtering
// A node is a "match" when its own name/id or (for a leaf) its status text hits the search text
// and its leaf state (if any) is one of the checked state filters. A non-matching group still
// shows if any descendant matches, so a hit stays reachable without silently hiding its ancestry;
// those forced-open ancestors are a *display* override only, independent of the persisted
// expand/collapse map, so filtering never overwrites what a user chose to leave collapsed.
function matches(node){
 let hay=(node.id+' '+(node.lane?.status_text||'')).toLowerCase();
 if(filterText&&!hay.includes(filterText))return false;
 if(node.lane_id){
  let state=node.lane?.progress?.state||'running';
  if(!filterStates.has(state))return false;
 }
 return true;
}
function anyFiltering(){return !!filterText||filterStates.size<4}

// ------------------------------------------------------------------ rows (flat, visible-order)
// Walks the tree once per render into a flat array of {node, depth, lane, visible, forceOpen,
// matched}, in display order -- used both to build the DOM and to drive keyboard navigation
// (arrow up/down just moves between consecutive rows in this array; left/right expand/collapse).
function flatten(tree, lanes){
 let rows=[];
 const filtering=anyFiltering();
 function visit(node, depth){
  let lane=node.lane_id?lanes[node.lane_id]:null;
  let annotated={...node, lane};
  let selfMatch=matches(annotated);
  let childRows=[];
  let anyChildMatch=false;
  for(let seg of node.order){
   let sub=visit(node.children[seg], depth+1);
   if(sub.length&&(sub[0].matched||sub[0].descendantMatch))anyChildMatch=true;
   childRows.push(...sub);
  }
  let matched=selfMatch||anyChildMatch;
  if(filtering&&!matched)return []; // neither this node nor anything under it matches: prune
  let forceOpen=filtering&&anyChildMatch;
  let open=hasChildren(node)&&(isExpanded(node,depth)||forceOpen);
  let out=[{node,depth,lane,matched:selfMatch,descendantMatch:anyChildMatch,open,forceOpen}];
  if(open)out.push(...childRows);
  return out;
 }
 for(let seg of tree.order)rows.push(...visit(tree.children[seg],0));
 return rows;
}

// ------------------------------------------------------------------ progress bar + status text
const STATE_COLOR={queued:'#53636d',running:'#f3c875',done:'#9ee6d1',failed:'#ed6d70'};
function dominantState(node){
 let c=node.counts;
 if(c.failed)return 'failed';
 if(c.running)return 'running';
 if(c.queued&&!c.done)return 'queued';
 if(c.done===c.total&&c.total>0)return 'done';
 return c.running||c.queued?'running':'done';
}
function bar(fraction,color,title){
 let wrap=document.createElement('div');wrap.className='tr-bar';wrap.title=title;
 let fill=document.createElement('div');fill.className='tr-bar-fill';
 fill.style.width=(Math.max(0,Math.min(1,fraction))*100).toFixed(1)+'%';
 fill.style.background=color;
 wrap.append(fill);return wrap;
}
function countBadge(counts){return `${counts.done}/${counts.total}`}
function countDetail(counts){
 let bits=[];
 for(let s of ['running','failed','queued'])if(counts[s])bits.push(counts[s]+' '+s);
 return `${counts.done} of ${counts.total} done`+(bits.length?' · '+bits.join(', '):'');
}

// ------------------------------------------------------------------ DOM
let rows=[],focusIndex=-1,onSelect=()=>{};

function rowText(r){
 let base=r.node.name||'(root)';
 if(!r.lane)return base;
 return base;
}
function buildRow(r,i){
 let el=document.createElement('div');
 el.className='tr-row';
 el.setAttribute('role','treeitem');
 el.setAttribute('aria-level',String(r.depth+1));
 el.style.paddingLeft=(6+r.depth*14)+'px';
 el.dataset.index=String(i);
 el.id='tr-row-'+i;
 let main=document.createElement('div');main.className='tr-row-main';
 if(hasChildren(r.node)){
  el.setAttribute('aria-expanded',String(r.open));
  let tw=document.createElement('button');
  tw.type='button';tw.className='tr-twirl';tw.tabIndex=-1;
  tw.textContent=r.open?'▾':'▸';
  tw.title=(r.open?'Collapse ':'Expand ')+r.node.id;
  tw.onclick=e=>{e.stopPropagation();setExpanded(r.node,r.depth,!isExpanded(r.node,r.depth));renderBody()};
  main.append(tw);
 } else main.append(spacer());
 let label=document.createElement('span');label.className='tr-label';label.textContent=rowText(r);
 label.title=r.node.id;
 main.append(label);
 if(r.node.counts.total&&hasChildren(r.node)){
  let badge=document.createElement('span');badge.className='tr-count';
  badge.textContent=countBadge(r.node.counts);badge.title=countDetail(r.node.counts);
  main.append(badge);
 }
 let state=r.lane?(r.lane.progress?.state||'running'):dominantState(r.node);
 main.append(bar(r.node.fraction,STATE_COLOR[state]||STATE_COLOR.running,
  r.lane?r.lane.status_text||'':countDetail(r.node.counts)));
 el.append(main);
 if(r.lane){
  let st=document.createElement('div');st.className='tr-status';
  st.textContent=r.lane.status_text||'';st.title=`${r.lane.id}\n${r.lane.phase||''}`;
  el.append(st);
 }
 el.tabIndex=i===focusIndex?0:-1;
 el.classList.toggle('tr-active',r.node.lane_id===laneId);
 el.title=r.lane?'Click: show this lane · Shift+click: add it to the Ask context':'Click: expand/select';
 el.onclick=e=>{
  if(e.shiftKey&&r.node.lane_id&&typeof askAdd==='function'){askAdd({kind:'lane',lane:r.node.lane_id});return}
  activate(i);
 };
 return el;
}
function spacer(){let s=document.createElement('span');s.className='tr-twirl tr-twirl-spacer';return s}

function renderBody(){
 let s=display&&display();
 let tree=s?.tree,lanes=s?.lanes||{};
 if(!tree){body.textContent='Waiting for experiment events…';rows=[];return}
 rows=flatten(tree,lanes);
 if(!rows.length){body.textContent=filterText||filterStates.size<4?'No lanes match this filter.':'No experiments yet.';return}
 if(focusIndex<0||focusIndex>=rows.length)focusIndex=rows.findIndex(r=>r.node.lane_id===laneId);
 if(focusIndex<0)focusIndex=0;
 body.replaceChildren(...rows.map((r,i)=>buildRow(r,i)));
}

function activate(i){
 let r=rows[i];if(!r)return;
 focusIndex=i;
 let target=r.node.lane_id||r.node.best_leaf;
 if(target)onSelect(target);
 if(hasChildren(r.node)&&!r.node.lane_id)setExpanded(r.node,r.depth,!isExpanded(r.node,r.depth));
 renderBody();
 requestAnimationFrame(()=>document.getElementById('tr-row-'+focusIndex)?.focus());
}

function moveFocus(delta){
 if(!rows.length)return;
 focusIndex=Math.max(0,Math.min(rows.length-1,focusIndex+delta));
 body.querySelectorAll('.tr-row').forEach(e=>e.tabIndex=-1);
 let el=document.getElementById('tr-row-'+focusIndex);
 if(el){el.tabIndex=0;el.focus();el.scrollIntoView({block:'nearest'})}
}
function onKey(e){
 if(!rows.length)return;
 let r=rows[focusIndex];
 if(e.key==='ArrowDown'){e.preventDefault();moveFocus(1)}
 else if(e.key==='ArrowUp'){e.preventDefault();moveFocus(-1)}
 else if(e.key==='ArrowRight'){e.preventDefault();
  if(r&&hasChildren(r.node)){if(!isExpanded(r.node,r.depth)){setExpanded(r.node,r.depth,true);renderBody();moveFocus(0)}else moveFocus(1)}}
 else if(e.key==='ArrowLeft'){e.preventDefault();
  if(r&&hasChildren(r.node)&&isExpanded(r.node,r.depth)){setExpanded(r.node,r.depth,false);renderBody()}
  else if(r&&r.depth>0){ // jump to the parent row: the nearest earlier row at depth-1
   for(let xi=focusIndex-1;xi>=0;xi--)if(rows[xi].depth<r.depth){focusIndex=xi;break}
   moveFocus(0)}}
 else if(e.key==='Enter'||e.key===' '){e.preventDefault();activate(focusIndex)}
}

// ------------------------------------------------------------------ toolbar (built once)
let body,search,chips={};
function buildToolbar(){
 let toolbar=document.createElement('div');toolbar.className='tr-toolbar';
 search=document.createElement('input');search.type='search';search.placeholder='Filter by name…';
 search.setAttribute('aria-label','Filter experiments by name');
 search.oninput=()=>{filterText=search.value.trim().toLowerCase();renderBody()};
 let chipWrap=document.createElement('div');chipWrap.className='tr-chips';
 for(let s of ['running','failed','done','queued']){
  let b=document.createElement('button');b.type='button';b.className='tr-chip tr-chip-'+s;
  b.textContent=s;b.setAttribute('aria-pressed','true');
  b.onclick=()=>{filterStates.has(s)?filterStates.delete(s):filterStates.add(s);
   if(!filterStates.size)filterStates=new Set(['queued','running','done','failed']);
   b.setAttribute('aria-pressed',String(filterStates.has(s)));renderBody()};
  chips[s]=b;chipWrap.append(b);
 }
 let btns=document.createElement('div');btns.className='tr-btns';
 let expandAll=document.createElement('button');expandAll.type='button';expandAll.textContent='Expand all';
 expandAll.onclick=()=>{walkAll(tree=>{expandOverride[tree.id]=true});saveExpand();renderBody()};
 let collapseAll=document.createElement('button');collapseAll.type='button';collapseAll.textContent='Collapse all';
 collapseAll.onclick=()=>{walkAll(tree=>{expandOverride[tree.id]=false});saveExpand();renderBody()};
 btns.append(expandAll,collapseAll);
 toolbar.append(search,chipWrap,btns);
 body=document.createElement('div');body.className='tr-body';body.setAttribute('role','tree');
 body.setAttribute('aria-label','Experiments');
 body.addEventListener('keydown',onKey);
 ROOT.replaceChildren(toolbar,body);
}
function walkAll(fn){
 let s=display&&display();if(!s?.tree)return;
 (function walk(n){if(hasChildren(n)){fn(n);for(let seg of n.order)walk(n.children[seg])}})(s.tree);
}

let built=false;
function update(){
 let s=display&&display();
 if(s&&s.run!==run){run=s.run;loadExpand()}
 if(!built){buildToolbar();built=true}
 renderBody();
}
onSelect=id=>{if(typeof select==='function')select(id)};

window.YapnrTree={update,
 // test/e2e hooks: exact internal state, read-only from outside this module.
 rows:()=>rows,isExpanded:(id,depth)=>{let n=rows.find(r=>r.node.id===id);return n?isExpanded(n.node,n.depth):isExpanded({id},depth||0)},
 setFilterText:t=>{filterText=t.toLowerCase();if(search)search.value=t;renderBody()},
 setFilterStates:arr=>{filterStates=new Set(arr);for(let s in chips)chips[s].setAttribute('aria-pressed',String(filterStates.has(s)));renderBody()},
};
})();
