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
const ALL_STATES=['queued','running','stalled','done','failed','rejected'];
let run=null,expandOverride={},filterText='',filterStates=new Set(ALL_STATES);
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
function anyFiltering(){return !!filterText||filterStates.size<ALL_STATES.length}

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
const STATE_COLOR={queued:'#53636d',running:'#f3c875',stalled:'#c98a3e',done:'#9ee6d1',failed:'#ed6d70',rejected:'#8da9b6'};
// Priority order for "what is this group's overall state": prefer whatever's still actively
// happening (running, then stalled) over a terminal outcome, mirroring lane_tree.py's
// _STATE_PRIORITY -- a single failed/rejected leaf in an otherwise-running group must not read
// as the group being "failed".
const STATE_PRIORITY=['running','stalled','failed','rejected','queued','done'];
function dominantState(node){
 let c=node.counts;
 for(let s of STATE_PRIORITY)if(c[s])return s;
 return 'running';
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
 for(let s of ['running','stalled','failed','rejected','queued'])if(counts[s])bits.push(counts[s]+' '+s);
 return `${counts.done} of ${counts.total} done`+(bits.length?' · '+bits.join(', '):'');
}

// ------------------------------------------------------------------ DOM
let rows=[],focusIndex=-1,onSelect=()=>{};
// Row elements, keyed by node id and reused across renders -- a live poll fires every 1.2s
// (app.js's poll() -> updateControls() -> YapnrTree.update()), and a full rebuild each time
// (the old body.replaceChildren(...rows.map(buildRow))) tore down and recreated every row's DOM
// node on every single poll. That (a) detached whatever had focus, so the row-refocus below had
// to run on every render just to get back to where it already was, and a focus() call scrolls
// its container to reveal the focused element by default -- yanking a user's manual scroll back
// to the focused row on every update even though that row never moved; and (b) discarded hover
// state and any per-row affordance tied to the DOM node itself. Keeping the same element for a
// node across renders (created once, then mutated in place by updateRowContent()) means an
// unaffected row's focus/hover simply isn't touched at all, so most updates need no refocus and
// no scroll adjustment of any kind.
let elByNodeId=new Map();

function rowText(r){
 let base=r.node.name||'(root)';
 if(!r.lane)return base;
 return base;
}
// Creates a row's DOM skeleton once; everything that can change between renders of the *same*
// node (label text, bar, badge, status, expanded/active state, title, index-derived id/tabIndex)
// is applied by updateRowContent() instead, both right after creation and on every later render.
function buildRow(r,i){
 let el=document.createElement('div');
 el.className='tr-row';
 el.setAttribute('role','treeitem');
 el.onclick=e=>{
  let rr=rows[Number(el.dataset.index)];if(!rr)return;
  if(e.shiftKey&&rr.node.lane_id&&typeof askAdd==='function'){askAdd({kind:'lane',lane:rr.node.lane_id});return}
  activate(Number(el.dataset.index));
 };
 let main=document.createElement('div');main.className='tr-row-main';
 let tw=document.createElement('button');
 tw.type='button';tw.className='tr-twirl';tw.tabIndex=-1;
 tw.onclick=e=>{
  let rr=rows[Number(el.dataset.index)];
  if(!rr||!hasChildren(rr.node))return; // a leaf's twirl is just a spacer: let the click bubble to the row (select), as a plain <span> there would have
  e.stopPropagation();setExpanded(rr.node,rr.depth,!isExpanded(rr.node,rr.depth));renderBody();
 };
 main.append(tw);
 let label=document.createElement('span');label.className='tr-label';
 main.append(label);
 let badge=document.createElement('span');badge.className='tr-count';
 main.append(badge);
 let barWrap=document.createElement('div');barWrap.className='tr-bar';
 let barFill=document.createElement('div');barFill.className='tr-bar-fill';
 barWrap.append(barFill);
 main.append(barWrap);
 el.append(main);
 let status=document.createElement('div');status.className='tr-status';
 el.append(status);
 el._refs={tw,label,badge,barWrap,barFill,status};
 updateRowContent(el,r,i);
 return el;
}
// Mutates an existing row element (new or reused) to match `r` at index `i` -- never recreates
// or reorders its children, so an element that already has focus or is mid-:hover keeps it.
function updateRowContent(el,r,i){
 let refs=el._refs;
 el.setAttribute('aria-level',String(r.depth+1));
 el.style.paddingLeft=(6+r.depth*14)+'px';
 el.dataset.index=String(i);
 el.id='tr-row-'+i;
 let kids=hasChildren(r.node);
 if(kids){
  el.setAttribute('aria-expanded',String(r.open));
  refs.tw.textContent=r.open?'▾':'▸';
  refs.tw.title=(r.open?'Collapse ':'Expand ')+r.node.id;
  refs.tw.classList.remove('tr-twirl-spacer');
 } else {
  el.removeAttribute('aria-expanded');
  refs.tw.textContent='';refs.tw.title='';
  refs.tw.classList.add('tr-twirl-spacer');
 }
 refs.label.textContent=rowText(r);
 refs.label.title=r.node.id;
 if(r.node.counts.total&&kids){
  refs.badge.hidden=false;
  refs.badge.textContent=countBadge(r.node.counts);refs.badge.title=countDetail(r.node.counts);
 } else refs.badge.hidden=true;
 let state=r.lane?(r.lane.progress?.state||'running'):dominantState(r.node);
 refs.barWrap.title=r.lane?r.lane.status_text||'':countDetail(r.node.counts);
 refs.barFill.style.width=(Math.max(0,Math.min(1,r.node.fraction))*100).toFixed(1)+'%';
 refs.barFill.style.background=STATE_COLOR[state]||STATE_COLOR.running;
 if(r.lane){
  refs.status.hidden=false;
  refs.status.textContent=r.lane.status_text||'';refs.status.title=`${r.lane.id}\n${r.lane.phase||''}`;
 } else refs.status.hidden=true;
 el.tabIndex=i===focusIndex?0:-1;
 el.classList.toggle('tr-active',r.node.lane_id===laneId);
 el.title=r.lane?'Click: show this lane · Shift+click: add it to the Ask context':'Click: expand/select';
}

// Keyed reconciliation of `body`'s children against `rows` (keyed by node.id): reuses each row's
// existing element when its node is still present (updating it in place), creates one only for a
// node that just appeared, and removes only the elements for nodes no longer present -- the same
// approach a keyed virtual-DOM diff uses, so an update's DOM churn is proportional to what
// actually changed instead of the whole visible list. Nodes that persist keep their element (and
// therefore keep focus, hover and any open native tooltip) even when their position in the list
// shifts; insertBefore() is only called for elements that are actually out of place, and moving
// a node that already has focus does not blur or scroll it (it stays connected throughout).
function reconcileRows(){
 let next=new Map();
 for(let i=0;i<rows.length;i++){
  let r=rows[i],el=elByNodeId.get(r.node.id);
  if(el)updateRowContent(el,r,i);else el=buildRow(r,i);
  next.set(r.node.id,el);
  if(body.childNodes[i]!==el)body.insertBefore(el,body.childNodes[i]||null);
 }
 while(body.childNodes.length>rows.length)body.removeChild(body.lastChild);
 elByNodeId=next;
}

function renderBody(){
 let s=display&&display();
 let tree=s?.tree,lanes=s?.lanes||{};
 let hadFocus=!!(document.activeElement&&body.contains(document.activeElement));
 let focusedId=rows[focusIndex]?.node.id;
 if(!tree){body.textContent='Waiting for experiment events…';rows=[];elByNodeId.clear();return}
 rows=flatten(tree,lanes);
 if(!rows.length){body.textContent=filterText||filterStates.size<ALL_STATES.length?'No lanes match this filter.':'No experiments yet.';elByNodeId.clear();return}
 if(focusedId){let idx=rows.findIndex(r=>r.node.id===focusedId);if(idx>=0)focusIndex=idx}
 if(focusIndex<0||focusIndex>=rows.length)focusIndex=rows.findIndex(r=>r.node.lane_id===laneId);
 if(focusIndex<0)focusIndex=0;
 // Every scroll container this panel has (the row list itself, and the Experiments panel/drawer
 // around it, both `overflow:auto`) keeps its exact scroll offset across the update: this is a
 // belt-and-suspenders restore on top of the keyed reconciliation above, in case anything about
 // the DOM mutation nudges either container (a browser clamping scrollTop mid-mutation, say).
 let panel=document.getElementById('lanes-panel');
 let bodyTop=body.scrollTop,panelTop=panel?panel.scrollTop:null;
 reconcileRows();
 body.scrollTop=bodyTop;
 if(panel)panel.scrollTop=panelTop;
 // The focused row's *element* survives the reconcile above untouched when its node persisted,
 // so it is still the active element already -- no focus() call, and so no scroll, needed. Only
 // when the previously-focused node is gone (filtered out, collapsed away, pruned) do we need to
 // move focus ourselves, and this is a programmatic refocus following a live update, not user
 // navigation, so it must never scroll the row into view on its own (preventScroll:true) --
 // scrolling here is reserved for arrow keys, click and activate (moveFocus/activate below).
 if(hadFocus&&document.activeElement!==document.getElementById('tr-row-'+focusIndex)){
  document.getElementById('tr-row-'+focusIndex)?.focus({preventScroll:true});
 }
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
 for(let s of ALL_STATES){
  let b=document.createElement('button');b.type='button';b.className='tr-chip tr-chip-'+s;
  b.textContent=s;b.setAttribute('aria-pressed','true');
  b.onclick=()=>{filterStates.has(s)?filterStates.delete(s):filterStates.add(s);
   if(!filterStates.size)filterStates=new Set(ALL_STATES);
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
 // a node's current row element, so a test can confirm the same DOM node survives an update
 // (reconcileRows() reuses it) rather than being torn down and recreated.
 elementFor:id=>elByNodeId.get(id),
};
})();
