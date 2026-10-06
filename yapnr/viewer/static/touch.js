'use strict';
// Small touch-gesture math shared by the PCB canvas (app.js) and the schematic SVG
// (schematic.js): pinch distance/midpoint and double-tap detection. Pure functions only -- each
// view wires Pointer Events itself (its own Map of active touch pointers) and calls these from its
// existing pointerdown/move/up handlers, so mouse and single-finger behaviour stay exactly as they
// were and there is only ever one listener per event. Both surfaces already set CSS
// touch-action:none (style.css #board, schematic.css #sch-svg), which is what stops the browser
// page itself from scrolling/zooming during a touch gesture -- this file only computes the numbers.
function touchMid(pts){let a=[...pts.values()],n=a.length||1;return [a.reduce((s,p)=>s+p[0],0)/n,a.reduce((s,p)=>s+p[1],0)/n]}
function touchDist(pts){let a=[...pts.values()];return a.length<2?0:Math.hypot(a[0][0]-a[1][0],a[0][1]-a[1][1])}
class DoubleTap{
 // moveTol: how far a single tap may drift (down -> up) and still count as a tap, not a drag.
 // gapMs/posTol: how soon and how close the second tap must land.
 constructor(moveTol=12,gapMs=350,posTol=28){this.moveTol=moveTol;this.gapMs=gapMs;this.posTol=posTol;this.last=null}
 // down/up: [x,y] in the element's local CSS-pixel coordinates, for one finger's whole touch.
 // Returns true on the second qualifying tap (and resets, so a third tap starts a fresh pair).
 hit(down,up){let moved=Math.hypot(down[0]-up[0],down[1]-up[1]);if(moved>=this.moveTol){this.last=null;return false}
  let now=Date.now(),last=this.last;this.last={t:now,x:up[0],y:up[1]};
  return !!(last&&now-last.t<this.gapMs&&Math.hypot(up[0]-last.x,up[1]-last.y)<this.posTol)}}
window.touchMid=touchMid;window.touchDist=touchDist;window.DoubleTap=DoubleTap;
// Safari (iOS) fires its own non-standard gesturestart/change/end for a two-finger pinch in
// addition to Pointer/Touch events, and uses it to zoom the whole page regardless of
// touch-action. touch-action:none on the drawing surfaces already stops this on iOS 13+, but
// the gesture events still reach the page on older/odd WebKit builds, so suppress them too --
// cheap insurance, never the primary mechanism (pinch math above still drives the real zoom).
for(const ev of ['gesturestart','gesturechange','gestureend'])
 document.addEventListener(ev,e=>e.preventDefault());
