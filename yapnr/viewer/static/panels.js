'use strict';
// Dockable side panels: the Experiment lanes column (#lanes-panel) and the Exploration column
// (#explore-panel) each gain a close button and a header toolbar toggle, on top of the existing
// Inspect/Source/Ask/Notes dock (dock.css + source.js's window.YapnrDock, unchanged here except
// for a toolbar button that delegates to it). All three can be closed independently so the board
// can fill the window; state persists per browser (localStorage, wrapped in try/catch) with a
// Reset. At <=900px (phone and small-tablet widths -- a laptop still keeps both columns) every
// panel is a fixed overlay drawer instead of a grid column (style.css), closed by default, so a
// phone always opens on the board maximized; a panel a user explicitly opened on a wide screen
// stays remembered, matching how the dock already behaves (see source.js: `open = wide() &&
// pref.open !== false`). On a wide screen each panel can also collapse to a 34px rail (mirroring
// the Inspect dock's own collapse) instead of closing outright, and either panel can dock to the
// left or right edge of an overlay drawer, independent of the other.
(function(){
// Embedded views are ordinary workbench tabs; the parent owns their drawers.
if(window.parent!==window&&new URLSearchParams(location.search).get('workspace')==='1')return;
const D=document,store={get(k,d){try{let v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const KEY='yapnr-panels-v1',wide=()=>innerWidth>900,pref=store.get(KEY,{});
const lanesEl=D.getElementById('lanes-panel'),exploreEl=D.getElementById('explore-panel');
if(!lanesEl&&!exploreEl)return; // a stripped-down static/ checkout without these ids: nothing to wire
let lanesOpen=wide()&&pref.lanes!==false,exploreOpen=wide()&&pref.explore!==false;
let lanesRail=!!pref.lanesRail,exploreRail=!!pref.exploreRail;
let lanesSide=pref.lanesSide==='right'?'right':'left',exploreSide=pref.exploreSide==='left'?'left':'right';
function save(){store.set(KEY,{lanes:lanesOpen,explore:exploreOpen,lanesRail,exploreRail,lanesSide,exploreSide})}
function setPanel(el,open,rail,side){if(!el)return;el.classList.toggle('sp-open',open);el.classList.toggle('sp-closed',!open);
 el.classList.toggle('sp-rail',!!rail);el.classList.toggle('sp-side-left',side==='left');el.classList.toggle('sp-side-right',side!=='left');
 el.toggleAttribute('inert',!open)}
function focusInto(el){if(!el)return;cancelAnimationFrame(focusInto.t);focusInto.t=requestAnimationFrame(()=>{
 if(!el.classList.contains('sp-open'))return;
 let target=el.querySelector('button,[href],input,select,textarea,[tabindex]')||el;
 if(target===el&&!el.hasAttribute('tabindex'))el.setAttribute('tabindex','-1');
 try{target.focus()}catch(e){}})}
function apply(){
 D.documentElement.style.setProperty('--lanes-w',!lanesOpen?'0px':(wide()&&lanesRail?'34px':'245px'));
 D.documentElement.style.setProperty('--explore-w',!exploreOpen?'0px':(wide()&&exploreRail?'34px':'310px'));
 setPanel(lanesEl,lanesOpen,wide()&&lanesRail,lanesSide);setPanel(exploreEl,exploreOpen,wide()&&exploreRail,exploreSide);
 scrim.hidden=wide()||!(lanesOpen||exploreOpen);
 syncButtons();cancelAnimationFrame(apply.kt);apply.kt=requestAnimationFrame(()=>dispatchEvent(new Event('resize')))}
function setLanes(v,focusBack){let was=lanesOpen;lanesOpen=!!v;save();apply();
 if(focusBack&&!lanesOpen)bLanes.focus();else if(!was&&lanesOpen&&!wide())focusInto(lanesEl)}
function setExplore(v,focusBack){let was=exploreOpen;exploreOpen=!!v;save();apply();
 if(focusBack&&!exploreOpen)bExplore.focus();else if(!was&&exploreOpen&&!wide())focusInto(exploreEl)}
const toggleLanes=()=>setLanes(!lanesOpen),toggleExplore=()=>setExplore(!exploreOpen);
function setLanesRail(v){lanesRail=!!v;save();apply()}
function setExploreRail(v){exploreRail=!!v;save();apply()}
function setLanesSide(s){lanesSide=s==='right'?'right':'left';save();apply()}
function setExploreSide(s){exploreSide=s==='left'?'left':'right';save();apply()}

function closeBtn(label,fn){let b=D.createElement('button');b.type='button';b.className='sp-close';b.title='Close '+label;b.setAttribute('aria-label','Close '+label);b.textContent='×';b.onclick=()=>fn(false,true);return b}
function railBtn(label,getRail,setRail){let b=D.createElement('button');b.type='button';b.className='sp-rail-btn';
 const sync=()=>{let on=getRail();b.title=(on?'Expand ':'Collapse ')+label;b.setAttribute('aria-label',b.title);b.textContent=on?'»':'«'};
 b.onclick=()=>{setRail(!getRail());sync()};sync();return b}
function sideBtn(label,getSide,setSide){let b=D.createElement('button');b.type='button';b.className='sp-side-btn';
 const sync=()=>{let s=getSide();b.title='Dock '+label+' to the '+(s==='left'?'right':'left');b.setAttribute('aria-label',b.title)};
 b.textContent='⇄';b.onclick=()=>{setSide(getSide()==='left'?'right':'left');sync()};sync();return b}
if(lanesEl){lanesEl.prepend(sideBtn('Experiments',()=>lanesSide,setLanesSide),railBtn('Experiments',()=>lanesRail,setLanesRail),closeBtn('Experiments',setLanes))}
if(exploreEl){exploreEl.prepend(sideBtn('Exploration',()=>exploreSide,setExploreSide),railBtn('Exploration',()=>exploreRail,setExploreRail),closeBtn('Exploration',setExplore))}

// A scrim behind the overlay drawers on narrow screens: without it the board underneath still
// reads as part of the same surface as the drawer, even once the drawer itself is opaque, and a
// tap anywhere outside the drawer should close it. Hidden (not just transparent) on wide screens,
// where lanes/explore are grid columns rather than overlays, so it never intercepts clicks there.
const scrim=D.createElement('div');scrim.id='sp-scrim';scrim.hidden=true;
scrim.onclick=()=>{if(lanesOpen)setLanes(false,true);if(exploreOpen)setExplore(false,true)};
D.body.append(scrim);

const bar=D.createElement('div');bar.id='panel-bar';bar.setAttribute('role','toolbar');bar.setAttribute('aria-label','Panels: show or hide, reset layout');
function tbtn(label,title,onclick){let b=D.createElement('button');b.type='button';b.textContent=label;b.title=title;b.setAttribute('aria-pressed','false');b.onclick=onclick;return b}
const bLanes=lanesEl?tbtn('Experiments','Show or hide the experiment lanes panel',toggleLanes):null;
const bDock=tbtn('Inspect · Ask','Show or hide the Inspect / Source / Ask / Notes panel',()=>window.YapnrDock?.toggle());
const bExplore=exploreEl?tbtn('Exploration','Show or hide the exploration panel (tree, costs, events)',toggleExplore):null;
const bReset=tbtn('Reset layout','Reset every panel (open/closed, width, side) to its default',()=>{
 try{localStorage.removeItem(KEY);localStorage.removeItem('yapnr-dock')}catch(e){}
 location.reload()});
bReset.className='reset';
for(let b of [bLanes,bDock,bExplore,bReset])if(b)bar.append(b);
const hr=D.querySelector('.header-right');if(hr)hr.insertBefore(bar,hr.firstChild);

function syncButtons(){
 bLanes?.setAttribute('aria-pressed',String(lanesOpen));
 bExplore?.setAttribute('aria-pressed',String(exploreOpen));
 bDock.setAttribute('aria-pressed',String(!!window.YapnrDock?.isOpen?.()));
}
D.addEventListener('yapnr:dock',syncButtons);

let wasWide=wide();
addEventListener('resize',()=>{let w=wide();if(w!==wasWide){wasWide=w;apply()}});
// Esc closes whichever overlay panel is open, on a narrow screen, regardless of where focus
// currently is -- a drawer sitting open over the board is the thing to dismiss either way.
addEventListener('keydown',e=>{if(e.key!=='Escape'||wide())return;
 if(lanesOpen){setLanes(false,true);return}
 if(exploreOpen)setExplore(false,true)});

apply();
window.YapnrPanels={lanesOpen:()=>lanesOpen,exploreOpen:()=>exploreOpen,setLanes,setExplore,toggleLanes,toggleExplore,
 lanesRail:()=>lanesRail,exploreRail:()=>exploreRail,setLanesRail,setExploreRail,
 lanesSide:()=>lanesSide,exploreSide:()=>exploreSide,setLanesSide,setExploreSide,
 reset:()=>bReset.onclick()};
})();
