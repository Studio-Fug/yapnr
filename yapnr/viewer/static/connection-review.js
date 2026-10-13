'use strict';
(() => {
  const $ = id => document.getElementById(id), canvas = $('board'), ctx = canvas.getContext('2d');
  let cards = [], visible = [], current = null, geometry = null, layers = new Set(), index = 0;
  let bounds = [0, 0, 30, 30], transform = null, drawing = false, start = null, drag = null, boxes = [], busy = false, last = null, loadToken = 0, pan = null;
  const colors = {'F.Cu':'#e9956a','B.Cu':'#67b9e1','In1.Cu':'#a395d1','In2.Cu':'#62b788','In3.Cu':'#ceb66b','In4.Cu':'#c08dd1'};
  const text = (id, value) => { $(id).textContent = value ?? ''; };
  async function request(path, body) {
    const response = await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    return result;
  }
  const api = body => request('/api/connection-review/v1', body);
  function saveDraft() {
    if (!current) return;
    localStorage.setItem(`yapnr:connection-review:${current.id}`, JSON.stringify({text:$('feedback').value, boxes}));
  }
  function recoverDraft() {
    let value = {};
    try { value = JSON.parse(localStorage.getItem(`yapnr:connection-review:${current.id}`) || '{}'); } catch (_) { /* damaged local draft */ }
    $('feedback').value = value.text || ''; boxes = value.boxes || [];
  }
  function filtered() {
    const kind = $('filter').value;
    return cards.filter(c => kind === 'all' || (kind === 'assessed' ? !!c.annotation.hypothesis : kind === 'reviewed' ? ['agree','different'].includes(c.review_state) : ['unreviewed','outdated','skip'].includes(c.review_state)));
  }
  function list() {
    visible = filtered();
    const reviewed = cards.filter(c => ['agree','different'].includes(c.review_state)).length;
    text('counts', `${reviewed} / ${cards.length} reviewed · ${visible.length} shown`);
    const nav = $('cards'), scroll = nav.scrollTop; nav.replaceChildren();
    for (const [i,c] of visible.entries()) {
      const button = document.createElement('button'); button.className = current?.id === c.id ? 'selected' : '';
      button.textContent = `${c.annotation.hypothesis ? '◈' : '○'} ${c.net}`;
      const small = document.createElement('small'); small.textContent = `${c.lane} · ${c.review_state}`; button.append(small);
      button.addEventListener('click', () => show(i)); nav.append(button);
    }
    nav.scrollTop = scroll;
  }
  function layerControls() {
    $('layers').replaceChildren();
    const names = geometry.copper_layers || [...new Set((geometry.tracks || []).map(t => t[1]))];
    layers = new Set(names);
    for (const name of names) {
      const label = document.createElement('label'); label.className = 'check'; label.textContent = name;
      const box = document.createElement('input'); box.type = 'checkbox'; box.checked = true; box.setAttribute('aria-label', name);
      box.addEventListener('change', () => { box.checked ? layers.add(name) : layers.delete(name); render(); });
      label.append(box); $('layers').append(label);
    }
  }
  function resolution(card) {
    const item = card?.resolution;
    $('resolution').hidden = !item;
    if (!item) return;
    text('resolution-state', `${item.paused ? 'Paused · ' : ''}${item.state} · revision ${item.revision}`);
    text('resolution-next', item.invalidation || item.next_action);
    $('resolution-criteria').replaceChildren();
    const checks = item.verification?.checks || [];
    const historical = ["stale", "implementing", "ready", "needs_plan"].includes(item.state);
    for (const criterion of item.plan?.definition.criteria || []) {
      const check = checks.find(c => c.check === criterion.check);
      const result = check?.cases.find(c => c.id === criterion.id);
      const row = document.createElement('li');
      row.textContent = `${historical && result ? "Historical " : ""}${result ? result.passed ? 'pass' : 'failed' : 'Missing'} · ${criterion.id}${check?.error ? ' · '+check.error : ''}`;
      $('resolution-criteria').append(row);
    }
    if (item.verification?.error) {
      const row = document.createElement('li'); row.textContent = item.verification.error;
      $('resolution-criteria').append(row);
    }
    $('resolution-links').replaceChildren();
    if (item.submission) {
      const a = document.createElement('a'); a.href = item.submission.pr; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = `Implementation PR · ${item.submission.head.slice(0,12)}`; $('resolution-links').append(a);
    }
    for (const kind of ['before','after']) {
      const sha = item.verification?.[kind+'_visual_sha256'];
      if (!sha) continue;
      const figure = document.createElement('figure'), img = document.createElement('img'), label = document.createElement('figcaption');
      img.src = '/api/feedback/v1?artifact='+encodeURIComponent(sha); img.alt = kind+' worked example';
      label.textContent = `${historical ? "Historical " : ""}${kind} · artifact ${sha.slice(0,12)} · PR head ${item.verification.head.slice(0,12)}`; figure.append(img,label); $('resolution-links').append(figure);
    }
    text('resolution-attempts', item.attempts.map((a,i)=>`${i+1}. ${a.worker} · ${a.id.slice(0,12)} · ${a.verification ? a.verification.passed ? 'independently passed' : 'failed independent verification' : 'not verified'}`).join('\n') || 'No delegated implementation yet');
    $('resolution-pause').hidden = item.state === 'addressed';
    $('resolution-pause').textContent = item.paused ? 'Resume resolution' : 'Pause resolution';
  }
  $('resolution-pause').onclick = async () => {
    if (!current?.resolution) return;
    try {
      const item = current.resolution;
      current.resolution = await request('/api/feedback/v1', {action:item.paused?'resume':'pause',id:item.id,expect_revision:item.revision});
      resolution(current);
    } catch(e) { text('error',e.message); }
  };
  async function show(i) {
    saveDraft(); const token = ++loadToken;
    visible = filtered(); index = Math.max(0, Math.min(i,visible.length-1)); current = visible[index] || null;
    geometry = null; boxes = []; drawing = false; $('draw').setAttribute('aria-pressed','false');
    $('empty').hidden = !!current;
    for (const id of ['agree','different','skip','draw','previous']) $(id).disabled = !current || busy;
    list(); render(); resolution(current);
    if (!current) { text('net','Review complete'); text('lane','No matching cards'); for (const id of ['hypothesis','suggestion','question','confidence','prior','followup','history','facts','hash','position']) text(id,''); $('feedback').value=''; $('evidence').replaceChildren(); $('layers').replaceChildren(); return; }
    const card = current, a = card.annotation;
    text('lane', card.lane); text('net', card.net); text('position', `${index+1} / ${visible.length}`);
    text('hash', `${card.phase} · ${card.board_sha256.slice(0,12)}`);
    text('hypothesis', a.hypothesis || 'This failure is indexed but has not yet been assessed by the agent.');
    text('suggestion',a.suggestion); text('question',a.question);
    text('confidence', a.hypothesis ? `Agent hypothesis · ${a.confidence} confidence` : 'Awaiting agent assessment');
    $('agree').disabled = !a.hypothesis || busy;
    const f = card.facts;
    text('facts', `${f.unconnected_spans} unconnected island span${f.unconnected_spans===1?'':'s'} · shortest ${f.minimum_span_mm.toFixed(2)} mm · checkpoint: ${f.native_opens ?? '?'} native opens / ${f.native_violations ?? '?'} DRC findings`);
    $('evidence').replaceChildren();
    for (const e of a.evidence) { const li=document.createElement('li'); li.textContent=e; $('evidence').append(li); }
    text('prior',card.decision ? `${card.review_state}: ${card.decision.body || card.decision.verdict}${card.decision.rectangles.length ? ' · '+card.decision.rectangles.length+' saved rectangle(s)' : ''}` : '');
    text('followup',card.followup ? `Agent · ${card.followup.status === "tested" ? "tested (unverified claim)" : card.followup.status}: ${card.followup.body}` : '');
    $('json').href = `/api/geometry/${card.board_sha256}`;
    recoverDraft();
    try {
      const g = await request(`/api/geometry/${card.board_sha256}`);
      if (token !== loadToken) return;
      geometry=g; bounds=a.bounds.slice(); layerControls(); render();
      const history = await api({action:'history',id:card.id});
      if (token === loadToken) text('history',history.events.filter(e=>e.op!=='discover').map(e=>`${new Date(e.time*1000).toLocaleString()} · ${e.actor} · ${e.op}${e.decision ? ': '+e.decision.verdict+' '+e.decision.body : e.followup ? ': '+e.followup.status+' '+e.followup.body : ''}`).join('\n'));
    } catch(error) { if (token === loadToken) text('error',error.message); }
  }
  async function refresh(scan=true) {
    text('error',''); $('refresh').disabled=true;
    try {
      const data = await api({action:scan?'scan':'list',scope:$('scope').value.trim()});
      cards=data.cards; last=cards.filter(c=>c.decision).sort((a,b)=>b.revision-a.revision)[0]?.id||null; $('undo').disabled=!last; await show(0);
    } catch(e) { text('error',e.message); }
    finally { $('refresh').disabled=false; }
  }
  function point(p) { return [transform.x+p[0]*transform.s,transform.y-p[1]*transform.s]; }
  function world(e) { const r=canvas.getBoundingClientRect(); return [(e.clientX-r.left-transform.x)/transform.s, -(e.clientY-r.top-transform.y)/transform.s]; }
  function line(a,b,color,width=1,dash=[]) { const p=point(a),q=point(b);ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash);ctx.beginPath();ctx.moveTo(...p);ctx.lineTo(...q);ctx.stroke();ctx.setLineDash([]); }
  function polygon(paths,color) { ctx.fillStyle=color;ctx.beginPath();for(const path of paths){path.forEach((p,i)=>{const q=point(p);i?ctx.lineTo(...q):ctx.moveTo(...q);});ctx.closePath();}ctx.fill('evenodd'); }
  function rect(box,color,dash=[]) { const p=point([box[0],box[3]]),q=point([box[2],box[1]]);ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash(dash);ctx.strokeRect(...p,q[0]-p[0],q[1]-p[1]);ctx.setLineDash([]); }
  function render() {
    const size=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
    canvas.width=Math.max(1,size.width*dpr);canvas.height=Math.max(1,size.height*dpr);ctx.scale(dpr,dpr);
    ctx.fillStyle='#101b18';ctx.fillRect(0,0,size.width,size.height);
    if(!geometry || !current) return;
    const scale=Math.min((size.width-40)/(bounds[2]-bounds[0]),(size.height-40)/(bounds[3]-bounds[1]));
    transform={s:scale,x:size.width/2-(bounds[0]+bounds[2])*scale/2,y:size.height/2+(bounds[1]+bounds[3])*scale/2};
    rect([0,0,geometry.width,geometry.height],'#779386');
    const wanted=new Set(current.annotation.nets),refs=new Set(current.annotation.refs),dim=$('dim').checked;
    const opacity=(net)=>dim&&!wanted.has(net)?0.24:0.95;
    if($('zones').checked) for(const z of geometry.zones||[]) if(layers.has(z.layer)){ctx.globalAlpha=wanted.has(z.net)?.3:.07;polygon(z.paths||[],colors[z.layer]||'#87a9a0');}
    ctx.lineCap='round';
    for(const [net,layer,a,b,w] of geometry.tracks||[]) if(layers.has(layer)){ctx.globalAlpha=opacity(net);line(a,b,colors[layer]||'#aaa',Math.max(1,w*scale));}
    for(const via of geometry.vias||[]) if((via.layers||[]).some(l=>layers.has(l))){ctx.globalAlpha=opacity(via.net);const p=point(via.xy);ctx.fillStyle='#abcbbf';ctx.beginPath();ctx.arc(...p,via.diameter*scale/2,0,2*Math.PI);ctx.fill();ctx.fillStyle='#101b18';ctx.beginPath();ctx.arc(...p,Math.max(1,via.diameter*scale*.2),0,2*Math.PI);ctx.fill();}
    for(const part of geometry.parts||[]){
      for(const pad of part.pads||[]) if((pad.layers||[]).some(l=>layers.has(l))){
        ctx.globalAlpha=refs.has(part.ref)?0.95:opacity(pad.net);
        if(pad.polys?.length) polygon(pad.polys,wanted.has(pad.net)?'#dce8a5':'#c4cbb4');
        else {const p=point(pad.xy);ctx.save();ctx.translate(...p);ctx.rotate(-(pad.angle||0)*Math.PI/180);ctx.fillStyle='#c4cbb4';ctx.fillRect(-pad.size[0]*scale/2,-pad.size[1]*scale/2,pad.size[0]*scale,pad.size[1]*scale);ctx.restore();}
        if(scale>23){const p=point(pad.xy);ctx.globalAlpha=.85;ctx.fillStyle='#101b18';ctx.font='9px sans-serif';ctx.textAlign='center';ctx.fillText(pad.number,...p);}
      }
      const p=point(part.xy);ctx.globalAlpha=refs.has(part.ref)?1:.65;ctx.fillStyle=refs.has(part.ref)?'#b0efd2':'#b7cbc2';ctx.font=`${refs.has(part.ref)?'bold ':''}11px sans-serif`;ctx.textAlign='center';ctx.fillText(part.ref,p[0],p[1]-8);
    }
    ctx.globalAlpha=1;
    for(const span of current.spans)line(span.a,span.b,'#f2d968',2,[6,4]);
    rect(current.annotation.bounds,'#f3b36f',[7,4]);for(const box of current.decision?.rectangles||[])rect(box,'#a3f2d1',[3,3]);for(const box of boxes)rect(box,'#a3f2d1');
    if(drag)rect([Math.min(start[0],drag[0]),Math.min(start[1],drag[1]),Math.max(start[0],drag[0]),Math.max(start[1],drag[1])],'#fff');
  }
  async function decide(verdict) {
    if(!current||busy)return;
    if(verdict==='agree'&&!current.annotation.hypothesis)return;
    if(verdict==='different'&&!$('feedback').value.trim()&&!boxes.length){$('feedback').focus();text('saving','Add your conclusion or draw a rectangle, then submit Different finding.');return;}
    busy=true;text('error','');text('saving','Saving your review…');for(const id of ['agree','different','skip','previous','undo'])$(id).disabled=true;
    try {
      const old=current;
      const result=await request('/api/connection-review/decision',{id:old.id,expect_revision:old.revision,verdict,body:$('feedback').value,rectangles:boxes});
      cards=cards.map(c=>c.id===result.id?result:c);last=result.id;
      if(!window.matchMedia('(prefers-reduced-motion: reduce)').matches) await $('canvas-wrap').animate([{transform:'translateX(0)',opacity:1},{transform:`translateX(${verdict==='agree'?80:verdict==='different'?-80:0}px)`,opacity:0}],{duration:180,easing:'ease-in'}).finished;
      localStorage.removeItem(`yapnr:connection-review:${old.id}`);current=null;
      text('saving',verdict==='agree'?'Agreement saved':verdict==='different'?'Different finding saved':'Skipped');
      // Reviewed cards disappear from a pending queue; otherwise advance once.
      const next=$('filter').value==='pending'&&verdict!=='skip'?index:index+1;
      busy=false;await show(next);
    } catch(e){text('error',e.message);text('saving','Review not saved. Your feedback remains here.');}
    finally{busy=false;for(const id of ['different','skip','previous'])$(id).disabled=!current;$('undo').disabled=!last;$('agree').disabled=!current?.annotation.hypothesis;}
  }
  $('refresh').onclick=()=>refresh(true);$('filter').onchange=()=>show(0);$('feedback').oninput=saveDraft;
  $('agree').onclick=()=>decide('agree');$('different').onclick=()=>decide('different');$('skip').onclick=()=>decide('skip');$('previous').onclick=()=>show(index-1);
  $('undo').onclick=async()=>{if(!last||busy)return;try{const card=cards.find(c=>c.id===last);const result=await request('/api/connection-review/decision',{id:last,expect_revision:card.revision,verdict:'undo'});cards=cards.map(c=>c.id===last?result:c);visible=filtered();await show(visible.findIndex(c=>c.id===last));last=null;$('undo').disabled=true;text('saving','Last verdict undone');}catch(e){text('error',e.message);}};
  $('fit').onclick=()=>{if(current){bounds=current.annotation.bounds.slice();render();}};
  $('whole').onclick=()=>{if(geometry){bounds=[-1,-1,geometry.width+1,geometry.height+1];render();}};
  $('draw').onclick=()=>{drawing=!drawing;$('draw').setAttribute('aria-pressed',String(drawing));canvas.style.cursor=drawing?'crosshair':'grab';};
  $('clear-boxes').onclick=()=>{boxes=[];saveDraft();render();};$('zones').onchange=render;$('dim').onchange=render;
  canvas.addEventListener('pointerdown',e=>{if(!transform)return;start=world(e);canvas.setPointerCapture(e.pointerId);if(drawing){drag=start;}else{pan={x:e.clientX,y:e.clientY,bounds:bounds.slice(),scale:transform.s};}});
  canvas.addEventListener('pointermove',e=>{if(!geometry||!transform)return;const p=world(e);if(pan&&e.buttons){const dx=(e.clientX-pan.x)/pan.scale,dy=(e.clientY-pan.y)/pan.scale;bounds=[pan.bounds[0]-dx,pan.bounds[1]+dy,pan.bounds[2]-dx,pan.bounds[3]+dy];render();return;}if(drawing&&start&&e.buttons){drag=p;render();return;}const pads=(geometry.parts||[]).flatMap(f=>(f.pads||[]).map(q=>({ref:f.ref,...q})));const near=pads.sort((a,b)=>Math.hypot(a.xy[0]-p[0],a.xy[1]-p[1])-Math.hypot(b.xy[0]-p[0],b.xy[1]-p[1]))[0];$('hover').style.display=near?'block':'none';if(near)text('hover',`${near.ref}.${near.number} · ${near.net} · (${near.xy.map(x=>x.toFixed(2)).join(', ')}) mm`);});
  canvas.addEventListener('pointerup',()=>{if(drawing&&start&&drag&&Math.hypot(start[0]-drag[0],start[1]-drag[1])>.05){boxes.push([Math.min(start[0],drag[0]),Math.min(start[1],drag[1]),Math.max(start[0],drag[0]),Math.max(start[1],drag[1])]);saveDraft();}start=drag=pan=null;render();});
  canvas.addEventListener('pointercancel',()=>{start=drag=pan=null;render();});
  canvas.addEventListener('wheel',e=>{if(!transform)return;e.preventDefault();const p=world(e),f=e.deltaY>0?1.16:.86;bounds=[p[0]+(bounds[0]-p[0])*f,p[1]+(bounds[1]-p[1])*f,p[0]+(bounds[2]-p[0])*f,p[1]+(bounds[3]-p[1])*f];render();},{passive:false});
  let swipeStart=null;$('swipe').addEventListener('pointerdown',e=>{swipeStart=e.clientX;$('swipe').setPointerCapture(e.pointerId);});$('swipe').addEventListener('pointerup',e=>{if(swipeStart!==null&&Math.abs(e.clientX-swipeStart)>60)decide(e.clientX>swipeStart?'agree':'different');swipeStart=null;});
  document.addEventListener('keydown',e=>{if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)||busy)return;const action={ArrowRight:'agree',ArrowLeft:'different',ArrowDown:'skip'}[e.key];if(action){e.preventDefault();if(action!=='agree'||current?.annotation.hypothesis)decide(action);}});
  new ResizeObserver(render).observe($('canvas-wrap'));
  $('scope').value=new URLSearchParams(location.search).get('scope')||'';
  $('undo').disabled=true;
  refresh();
})();
