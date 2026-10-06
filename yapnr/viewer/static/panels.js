'use strict';
// Dockable side panels: the Experiment lanes column (#lanes-panel) and the Exploration column
// (#explore-panel) each gain a close button and a header toolbar toggle, on top of the existing
// Inspect/Source/Ask/Notes dock (dock.css + source.js's window.YapnrDock, unchanged here except
// for a toolbar button that delegates to it). All three can be closed independently so the board
// can fill the window; state persists per browser (localStorage, wrapped in try/catch) with a
// Reset. At <=1360px (the breakpoint the dock already uses) every panel is a fixed overlay drawer
// instead of a grid column (style.css), closed by default, so a phone always opens on the board
// maximized; a panel a user explicitly opened on a wide screen stays remembered, matching how the
// dock already behaves (see source.js: `open = wide() && pref.open !== false`).
(function(){
const D=document,store={get(k,d){try{let v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const KEY='yapnr-panels-v1',wide=()=>innerWidth>1360,pref=store.get(KEY,{});
const lanesEl=D.getElementById('lanes-panel'),exploreEl=D.getElementById('explore-panel');
if(!lanesEl&&!exploreEl)return; // a stripped-down static/ checkout without these ids: nothing to wire
let lanesOpen=wide()&&pref.lanes!==false,exploreOpen=wide()&&pref.explore!==false;
function save(){store.set(KEY,{lanes:lanesOpen,explore:exploreOpen})}
function setPanel(el,open){if(!el)return;el.classList.toggle('sp-open',open);el.classList.toggle('sp-closed',!open);el.toggleAttribute('inert',!open)}
function apply(){
 D.documentElement.style.setProperty('--lanes-w',lanesOpen?'245px':'0px');
 D.documentElement.style.setProperty('--explore-w',exploreOpen?'310px':'0px');
 setPanel(lanesEl,lanesOpen);setPanel(exploreEl,exploreOpen);
 syncButtons();cancelAnimationFrame(apply.kt);apply.kt=requestAnimationFrame(()=>dispatchEvent(new Event('resize')))}
function setLanes(v,focusBack){lanesOpen=!!v;save();apply();if(focusBack&&!lanesOpen)bLanes.focus()}
function setExplore(v,focusBack){exploreOpen=!!v;save();apply();if(focusBack&&!exploreOpen)bExplore.focus()}
const toggleLanes=()=>setLanes(!lanesOpen),toggleExplore=()=>setExplore(!exploreOpen);

function closeBtn(label,fn){let b=D.createElement('button');b.type='button';b.className='sp-close';b.title='Close '+label;b.setAttribute('aria-label','Close '+label);b.textContent='×';b.onclick=()=>fn(false,true);return b}
if(lanesEl)lanesEl.prepend(closeBtn('Experiments',setLanes));
if(exploreEl)exploreEl.prepend(closeBtn('Exploration',setExplore));

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
// Esc closes whichever overlay panel currently has focus (the dock already does this itself).
addEventListener('keydown',e=>{if(e.key!=='Escape'||wide())return;
 if(lanesOpen&&lanesEl?.contains(D.activeElement)){setLanes(false,true);return}
 if(exploreOpen&&exploreEl?.contains(D.activeElement))setExplore(false,true)});

apply();
window.YapnrPanels={lanesOpen:()=>lanesOpen,exploreOpen:()=>exploreOpen,setLanes,setExplore,toggleLanes,toggleExplore,
 reset:()=>bReset.onclick()};
})();
