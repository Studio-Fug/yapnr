'use strict';
// Notes tab: design notes shared by the user and the assistant, fed back into the design loop
// (design-notes.md). Data: GET /api/notes?since=<rev> (polled every 4 s, 1.5 s while an Ask turn runs),
// POST /api/notes (create), POST /api/notes/<id> {fields:{…}, comment?:'…', expect_rev?} (edit / status / comment; 409 = changed meanwhile),
// POST /api/notes/<id>/delete. Every button here is a human action: only the UI accepts, rejects,
// marks applied, resolves or deletes; the assistant can only create, comment and edit open notes of its own conversation.
// Status buttons send the rev the card shows (expect_rev): a note the assistant edited meanwhile answers 409 and is redrawn
// for a fresh decision; accepting a proposal with a diff first shows the diff and asks once more.
// Exposes window.SplancNotes (list/get/open/newNote/forItem/badgesFor/drawBadges/badgeAt/…) and fires
// 'splanc:notes' on document when the set changes (board badges, Inspect, Source gutter, Ask cards).
(function(){
const D=document,V=()=>window.SplancView||null,SS=()=>window.SplancSource||null,AG=()=>window.SplancAgent||null,DK=()=>window.SplancDock||null;
const store={get(k,d){try{let v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
function h(t,a,...k){let e=D.createElement(t);if(a)for(let [x,v] of Object.entries(a)){if(v==null||v===false)continue;if(x==='class')e.className=v;else if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v===true?'':v)}for(let c of k.flat(9))if(c!=null&&c!==false)e.append(c.nodeType?c:String(c));return e}
const clone=o=>o==null?o:JSON.parse(JSON.stringify(o)),enc=encodeURIComponent;
const tms=v=>{if(v==null||v==='')return NaN;if(typeof v==='number')return v<1e12?v*1000:v;let n=+v;return isFinite(n)&&!/[-:T]/.test(v)?(n<1e12?n*1000:n):Date.parse(v)};
function ago(v){let t=tms(v);if(!isFinite(t))return '';let s=(Date.now()-t)/1000;if(s<45)return 'now';if(s<3600)return Math.round(s/60)+' min';if(s<86400)return Math.round(s/3600)+' h';if(s<6*86400)return Math.round(s/86400)+' d';return new Date(t).toLocaleDateString(undefined,{month:'short',day:'numeric'})}
const when=v=>{let t=tms(v);return isFinite(t)?new Date(t).toLocaleString():''};

const KINDS=['observation','question','requirement','decision','todo','proposal'],KL={observation:'Observation',question:'Question',requirement:'Requirement',decision:'Decision',todo:'To-do',proposal:'Proposal'};
const STATUSES=['open','proposed','accepted','rejected','applied','resolved'],SL={open:'Open',proposed:'Proposed',accepted:'Accepted',rejected:'Rejected',applied:'Applied',resolved:'Resolved'};
const GROUPS=[['proposed','Awaiting your decision'],['open','Open'],['accepted','Accepted · to apply'],['applied','Applied'],['resolved','Resolved'],['rejected','Rejected']];
const PTYPES=['ato','pnr-annotation','constraint','engine','other'],PL={ato:'atopile source','pnr-annotation':'PnR annotation',constraint:'Constraint',engine:'Engine',other:'Other'};
const BADGE={proposed:'#ffd166',open:'#8ec5ff',accepted:'#9ee6d1'},LIVE=['open','proposed','accepted'];
const N={list:[],by:new Map(),rev:-1,ok:null,err:null,timer:0,tok:0,fast:false,loaded:false,removed:new Set(),seen:new Set(),open:new Set(),form:{},drafts:{},msg:{},
 flt:Object.assign({status:'all',kind:'',author:''},store.get('splanc-notes-filter',{}),{q:''}),forItem:null,ui:null,ed:null,hits:[],dirty:false,badges:store.get('splanc-notes-badges',true)!==false};
const st=n=>n?.status||'open',upd=n=>tms(n.updated)||tms(n.created)||0;
// as the assistant left it: nobody decided, edited or answered it yet (the inline Undo of a live Ask turn is only offered then)
const pristine=n=>!!n&&n.author==='agent'&&(st(n)==='open'||st(n)==='proposed')&&!n.status_by&&n.updated_by?.kind!=='user'&&!(n.comments||[]).some(c=>c.author==='user');

// ------------------------------------------------------------------ store API
async function req(url,body){let o=body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
 let r=await fetch(url,o),j=await r.json().catch(()=>null);
 if(!r.ok||!j||j.ok===false)throw Object.assign(Error(j?.error||(r.status===404?'the notes endpoint is not available on this server':'HTTP '+r.status)),{status:r.status});return j}
const noteOf=j=>j?.note&&typeof j.note==='object'?j.note:j&&j.id&&j.title!=null?j:null;
function after(j){let n=noteOf(j);if(n)upsert(n);else refresh().catch(()=>{});if(j?.rev!=null&&n)setTimeout(()=>refresh().catch(()=>{}),50);return n}
async function create(f){let j=await req('/api/notes',f),n=after(j);return n||{id:j.id}}
async function update(id,fields,extra){return after(await req('/api/notes/'+enc(id),{fields,...(extra||{})}))}
async function comment(id,text){return after(await req('/api/notes/'+enc(id),{comment:text}))}
async function remove(id){await req('/api/notes/'+enc(id)+'/delete',{});N.removed.add(id);let m=new Map(N.by);m.delete(id);commit(m);refresh().catch(()=>{})}
const setStatus=(id,status,extra,body)=>update(id,{status,...(extra||{})},body);
function upsert(n){if(!n?.id)return;let m=new Map(N.by);m.set(n.id,n);commit(m)}

// ------------------------------------------------------------------ polling
function schedule(){clearTimeout(N.timer);N.timer=setTimeout(poll,N.ok===false?30000:N.fast?1500:D.hidden?15000:4000)}
async function poll(){clearTimeout(N.timer);let tok=++N.tok;
 try{let j=await req('/api/notes?since='+enc(N.rev));if(tok!==N.tok)return;N.ok=true;N.err=null;ingest(j)}
 catch(e){if(tok!==N.tok)return;N.err=e.message;if(e.status===404||e.status===405)N.ok=false;let first=!N.loaded;N.loaded=true;if(first)render();else renderState()}
 finally{if(tok===N.tok)schedule()}}
const refresh=()=>poll();
// {rev, notes:[…]} replaces the set; {rev, unchanged:true} (or no notes array) keeps it; {delta:true, notes, deleted:[ids]} merges.
function ingest(j){let first=!N.loaded;N.loaded=true;
 if(!j||j.unchanged||!Array.isArray(j.notes)){if(j?.rev!=null)N.rev=j.rev;if(first)render();else renderState();return}
 let m=new Map(j.delta?N.by:[]);if(j.delta)for(let id of j.deleted||[])m.delete(id);for(let n of j.notes)if(n&&n.id)m.set(n.id,n);
 if(j.rev!=null)N.rev=j.rev;commit(m,first)}
function commit(m,first){let changed=[],fresh=[];
 for(let [id,n] of m){let o=N.by.get(id);if(!o||JSON.stringify(o)!==JSON.stringify(n)){changed.push(id);if(!o&&!first&&!N.seen.has(id))fresh.push(n)}}
 for(let id of N.by.keys())if(!m.has(id))changed.push(id);
 for(let id of m.keys())N.seen.add(id);
 if(!changed.length&&!first){renderState();return}
 N.by=m;N.list=[...m.values()];
 if(fresh.some(n=>n.author==='agent'))DK()?.badge?.('notes',true);
 render();D.dispatchEvent(new CustomEvent('splanc:notes',{detail:{rev:N.rev,changed}}))}

// ------------------------------------------------------------------ matching (targets use the Ask context item schema)
const sameFile=(a,b)=>{a=String(a||'');b=String(b||'');return !!a&&!!b&&(a===b||a.endsWith('/'+b)||b.endsWith('/'+a))};
const padNet=t=>SS()?.index?.()?.components?.[t.ref]?.pins?.[t.pad]?.net;
const overlap=(a,b)=>Array.isArray(a)&&Array.isArray(b)&&Math.min(a[0],a[2])<=Math.max(b[0],b[2])&&Math.max(a[0],a[2])>=Math.min(b[0],b[2])&&Math.min(a[1],a[3])<=Math.max(b[1],b[3])&&Math.max(a[1],a[3])>=Math.min(b[1],b[3]);
function hit(t,x){if(!t||!x)return false;
 switch(x.kind){
  case 'component':return (t.kind==='component'||t.kind==='pad')&&t.ref===x.ref||(t.kind==='region'||t.kind==='group')&&(t.refs||[]).includes(x.ref);
  case 'pad':return t.kind==='pad'&&t.ref===x.ref&&String(t.pad)===String(x.pad)||t.kind==='component'&&t.ref===x.ref;
  case 'net':return t.kind==='net'&&t.name===x.name||t.kind==='pad'&&padNet(t)===x.name;
  case 'source':{if(t.kind==='source')return sameFile(t.file,x.file)&&+t.line<=+(x.end||x.line)&&+(t.end||t.line)>=+x.line;
   let c=(t.kind==='component'||t.kind==='pad')&&SS()?.index?.()?.components?.[t.ref];return !!c&&sameFile(c.file,x.file)&&+c.line>=+x.line&&+c.line<=+(x.end||x.line)}
  case 'region':case 'group':{let refs=new Set(x.refs||[]);return (t.kind==='component'||t.kind==='pad')&&refs.has(t.ref)||t.kind==='region'&&x.kind==='region'&&(!t.lane||!x.lane||t.lane===x.lane)&&overlap(t.bbox,x.bbox)||t.kind==='group'&&x.kind==='group'&&(t.id||t.label)===(x.id||x.label)}
  case 'lane':return (t.kind==='lane'||t.kind==='region')&&t.lane===x.lane;
  case 'event':return t.kind==='event'&&String(t.id)===String(x.id);
  case 'probe':return t.kind==='probe'&&t.label===x.label&&(t.lane||'')===(x.lane||'');
 }return false}
const matches=(n,x)=>(n.targets||[]).some(t=>hit(t,x));
function forItem(item){return item?N.list.filter(n=>matches(n,item)).sort(order):[]}
const RANK=Object.fromEntries(GROUPS.map(([s],i)=>[s,i]));
const order=(a,b)=>(RANK[st(a)]??9)-(RANK[st(b)]??9)||upd(b)-upd(a);
// Source gutter: line -> notes (source targets; component/pad targets on the part's defining line)
function sourceLines(file){let out=new Map(),ix=SS()?.index?.();const add=(l,n)=>{let a=out.get(l);if(!a)out.set(l,a=[]);if(!a.includes(n))a.push(n)};
 for(let n of N.list)for(let t of n.targets||[]){
  if(t.kind==='source'&&sameFile(t.file,file)){let a=Math.max(1,+t.line||1),b=Math.min(Math.max(a,+(t.end||t.line)||a),a+200);for(let l=a;l<=b;l++)add(l,n)}
  else if((t.kind==='component'||t.kind==='pad')&&ix){let c=ix.components?.[t.ref];if(c&&c.line&&sameFile(c.file,file))add(+c.line,n)}}
 for(let a of out.values())a.sort(order);return out}

// ------------------------------------------------------------------ board badges (drawn by app.js render() through drawBadges)
function badgesFor(lane){let comp=new Map(),out=[];const top=s=>LIVE.slice().sort((a,b)=>RANK[a]-RANK[b]).find(x=>s.has(x));
 for(let n of N.list){let s=st(n);if(!BADGE[s])continue;
  for(let t of n.targets||[]){
   if((t.kind==='component'||t.kind==='pad')&&t.ref){let b=comp.get(t.ref);if(!b)comp.set(t.ref,b={kind:'component',ref:t.ref,ids:[],sts:new Set(),item:{kind:'component',ref:t.ref}});if(!b.ids.includes(n.id))b.ids.push(n.id);b.sts.add(s)}
   else if(t.kind==='region'&&Array.isArray(t.bbox)&&t.bbox.length===4&&(!t.lane||!lane||t.lane===lane))out.push({kind:'region',bbox:t.bbox,lane:t.lane||null,ids:[n.id],sts:new Set([s]),item:clone(t)})}}
 return [...comp.values(),...out].map(({sts,...b})=>({...b,status:top(sts),count:b.ids.length}))}
function partBounds(p){let pts=[];if(Array.isArray(p.xy))pts.push(p.xy);
 for(let q of p.pads||[]){if(!q.xy||!q.size)continue;let a=(q.angle||0)*Math.PI/180,co=Math.abs(Math.cos(a)),si=Math.abs(Math.sin(a)),dx=(co*q.size[0]+si*q.size[1])/2,dy=(si*q.size[0]+co*q.size[1])/2;pts.push([q.xy[0]-dx,q.xy[1]-dy],[q.xy[0]+dx,q.xy[1]+dy])}
 pts=pts.filter(p=>p.every(Number.isFinite));return pts.length?[Math.min(...pts.map(p=>p[0])),Math.min(...pts.map(p=>p[1])),Math.max(...pts.map(p=>p[0])),Math.max(...pts.map(p=>p[1]))]:null}
// c: the board 2d context (CSS-pixel transform), screen: world mm -> CSS px, g: geometry {parts}, lane: lane id
function drawBadges(c,screen,g,lane){N.hits=[];if(!N.badges||!c||!g||!N.list.length)return 0;let bs=badgesFor(lane);if(!bs.length)return 0;
 const parts=new Map((g.parts||[]).map(p=>[p.ref,p])),W=c.canvas.clientWidth||c.canvas.width,H=c.canvas.clientHeight||c.canvas.height;let n=0;
 c.save();c.setLineDash([]);c.font='700 9.5px Inter,system-ui,sans-serif';c.textAlign='center';c.textBaseline='middle';
 for(let b of bs){let x,y,col=BADGE[b.status]||'#8ec5ff';
  if(b.kind==='component'){let p=parts.get(b.ref),bb=p&&partBounds(p);if(!bb)continue;[x,y]=screen([bb[0],bb[3]]);x-=15;y-=1} // top-left corner: app.js writes the ref label to the upper right
  else{let [x0,y0,x1,y1]=b.bbox,a=screen([Math.min(x0,x1),Math.max(y0,y1)]),z=screen([Math.max(x0,x1),Math.min(y0,y1)]);
   c.strokeStyle=col;c.globalAlpha=.9;c.lineWidth=1.5;c.setLineDash([3,3]);c.strokeRect(a[0],a[1],z[0]-a[0],z[1]-a[1]);c.setLineDash([]);c.globalAlpha=1;x=z[0];y=a[1]}
  if(x<-20||y<-20||x>W+20||y>H+20)continue;
  // a sticky note: 15×15 with a folded corner, count inside
  let s=15,x0=Math.round(x-2),y0=Math.round(y-s+2),f=5;
  c.fillStyle='#0b1216';c.beginPath();c.moveTo(x0-1,y0-1);c.lineTo(x0+s-f+1,y0-1);c.lineTo(x0+s+1,y0+f-1);c.lineTo(x0+s+1,y0+s+1);c.lineTo(x0-1,y0+s+1);c.closePath();c.fill();c.strokeStyle='rgba(244,248,250,.85)';c.lineWidth=1;c.stroke(); // light rim: stands out on copper of the same hue
  c.fillStyle=col;c.beginPath();c.moveTo(x0,y0);c.lineTo(x0+s-f,y0);c.lineTo(x0+s,y0+f);c.lineTo(x0+s,y0+s);c.lineTo(x0,y0+s);c.closePath();c.fill();
  c.fillStyle='#0b1216';c.globalAlpha=.45;c.beginPath();c.moveTo(x0+s-f,y0);c.lineTo(x0+s-f,y0+f);c.lineTo(x0+s,y0+f);c.closePath();c.fill();c.globalAlpha=1;
  c.fillStyle='#0b1216';c.fillText(b.count>9?'9+':String(b.count),x0+s/2-.5,y0+s/2+1);
  N.hits.push({x0:x0-3,y0:y0-3,x1:x0+s+3,y1:y0+s+3,b});n++}
 c.restore();return n}
function badgeAt(x,y){for(let i=N.hits.length-1;i>=0;i--){let q=N.hits[i];if(x>=q.x0&&x<=q.x1&&y>=q.y0&&y<=q.y1){let b=q.b;
 return {ids:b.ids.slice(),item:b.item,status:b.status,text:b.ids.map(id=>{let n=N.by.get(id);return n?`${id} · ${SL[st(n)]} ${(KL[n.kind]||n.kind||'').toLowerCase()} · ${n.title}`:id}).join('\n')+'\nClick: show in Notes'}}}return null}
function badges(on){if(on!==undefined){N.badges=!!on;store.set('splanc-notes-badges',N.badges);let el=D.getElementById('note-badges');if(el)el.checked=N.badges;D.dispatchEvent(new CustomEvent('splanc:notes',{detail:{rev:N.rev,changed:[],badges:true}}))}return N.badges}
function wireToggle(){let el=D.getElementById('note-badges');
 if(!el){let costs=D.getElementById('costs')?.closest('label');if(!costs)return;el=h('input',{type:'checkbox',id:'note-badges'});costs.after(h('label',{title:'Sticky-note badges on parts and regions with open, proposed or accepted notes; click one to show its notes'},el,' Note badges'))}
 el.checked=N.badges;el.addEventListener('change',()=>badges(el.checked))}

// ------------------------------------------------------------------ small parts
const tLabel=t=>AG()?.label?AG().label(t):t.kind==='component'?t.ref:t.kind==='net'?t.name:t.kind==='pad'?t.ref+'.'+t.pad:t.kind==='source'?String(t.file).split('/').pop()+':'+t.line:t.kind==='lane'?'Lane '+t.lane:t.label||t.id||t.kind;
// cards use short chip labels (full label in the tooltip); the editor and filter row use the Ask context chips
const CK={component:'ref',net:'net',pad:'pad',source:'src',region:'region',group:'group',lane:'lane',event:'event',probe:'probe'};
function shortLabel(t){let nn=t.kind==='net'&&SS()?.index?.()?.nets?.[t.name],b=t.bbox||[0,0,0,0];
 return t.kind==='component'?t.ref:t.kind==='pad'?t.ref+'.'+t.pad:t.kind==='net'?(nn&&nn.low_info&&nn.title&&nn.title!==t.name?(x=>x.length>30?x.slice(0,29)+'…':x)(nn.title.replace(/\s*\([^()]*\)\s*$/,'')):t.name)
  :t.kind==='source'?String(t.file).split('/').pop()+':'+t.line+((t.end||t.line)>t.line?'–'+t.end:''):t.kind==='region'?`Region ${Math.abs(b[2]-b[0]).toFixed(0)}×${Math.abs(b[3]-b[1]).toFixed(0)} mm · ${(t.refs||[]).length} parts`:tLabel(t)}
function cChip(t){return h('button',{class:'dk-chip k-'+(CK[t.kind]||'ref'),type:'button',title:tLabel(t)+'\nClick: show on the board / in Inspect',onclick:e=>{e.stopPropagation();let A=AG();if(A?.show)return A.show(t);let k=CK[t.kind];if(SS()?.act&&t.kind==='component')SS().act('ref',t.ref,{focus:false});else if(SS()?.act&&t.kind==='net')SS().act('net',t.name,{focus:false})}},shortLabel(t))}
function tChip(t,onRemove){let A=AG();if(A?.chip)return A.chip(t,onRemove);return h('span',{class:'dk-chip ctx'},h('button',{type:'button',class:'ctx-l'},tLabel(t)),onRemove?h('button',{type:'button',class:'ctx-x',onclick:onRemove},'×'):null)}
function mdEl(text,cls){let el=h('div',{class:'ask-md '+(cls||'')}),A=AG();if(A?.markdown)el.innerHTML=A.markdown(String(text||''));else{el.style.whiteSpace='pre-wrap';el.textContent=text||''}return el}
function chipClick(e){let b=e.target.closest('button.dk-chip');if(!b||!b.dataset.k)return;e.stopPropagation();let k=b.dataset.k,v=b.dataset.v;if(k==='note')return openNote(v);let S=SS();if(S?.act)S.act(k,v,{focus:false})}
const who=a=>a==='agent'?'Claude':a==='user'?'You':String(a||'?');
function diffEl(diff){let pre=h('pre',{class:'nt-diff'});for(let l of String(diff).split('\n'))pre.append(h('span',{class:/^\+(?!\+\+)/.test(l)?'da':/^-(?!--)/.test(l)?'dd':/^@@/.test(l)?'dh':null},l+'\n'));return pre}
const host=u=>{try{return new URL(u).host.replace(/^www\./,'')}catch(e){return u}};
function srcLinks(list){return list.filter(s=>/^https?:\/\//i.test(s?.url||'')).map(s=>h('a',{href:s.url,target:'_blank',rel:'noopener noreferrer',title:s.url,class:'nt-sl'},s.title||host(s.url),s.title?h('em',null,host(s.url)):null))}
function prov(n,big){let p=n.provenance||{},bits=[];if(p.lane)bits.push('lane '+p.lane);if(p.phase!=null&&p.phase!=='')bits.push('phase '+p.phase);if(p.board_sha)bits.push('board '+String(p.board_sha).slice(0,big?16:8));if(big&&p.viewer_port)bits.push('viewer :'+p.viewer_port);return bits}
// status buttons (human decisions): a question is resolved or rejected; everything else can also be accepted (-> "to apply")
function flow(n){let s=st(n),dec=n.kind==='proposal'||!!n.proposal,b=[];
 if(s==='proposed'||s==='open'){if(n.kind!=='question')b.push(['accepted','Accept','ok','Accept: part of the design intent from now on (design-notes.md “Accepted · to apply”)']);
  b.push(['rejected','Reject…','no','Reject, with an optional reason (kept as a comment)'],['resolved','Resolve','','Resolved: answered or no action needed'])}
 if(s==='accepted')b.push(['applied','Mark applied…','ok','Mark applied, optionally naming where (commit, file, run)']);
 if(!LIVE.includes(s)||s==='accepted')b.push([dec&&n.kind==='proposal'?'proposed':'open','Reopen','','Reopen this note']);
 return b}

// ------------------------------------------------------------------ note card (Notes list, Ask inline, Inspect)
function card(x,opt={}){let id=typeof x==='string'?x:x?.id,n=typeof x==='string'?N.by.get(x):x;
 if(!n)return h('div',{class:'nt-card nt-gone'+(opt.compact?' compact':''),'data-id':id||''},h('span',{class:'nt-id'},id||'note'),N.removed.has(id)?' removed':N.loaded?' deleted or not visible':' loading…');
 const s=st(n),big=!opt.compact&&N.open.has(n.id),el=h('div',{class:`nt-card s-${s}`+(opt.compact?' compact':'')+(big?' open':''),'data-id':n.id});el.addEventListener('click',chipClick);
 const rer=()=>{let e2=card(n.id,opt);el.replaceWith(e2);return e2},form=opt.compact?null:N.form[n.id];
 el.append(h('div',{class:'nt-h'},opt.caption?h('span',{class:'nt-cap'},opt.caption):null,h('button',{class:'nt-id',type:'button',title:opt.compact?'Open in Notes':'Copy reference '+n.id,onclick:e=>{e.stopPropagation();if(opt.compact)openNote(n.id);else copyRef(n,e.currentTarget)}},n.id),
  h('span',{class:'nt-kind'},KL[n.kind]||n.kind||'note'),n.author==='agent'&&!opt.caption?h('span',{class:'nt-by',title:'Written by the assistant'},'Claude'):null,h('span',{class:'dk-sp'}),
  h('span',{class:'nt-st s-'+s},SL[s]||s),opt.compact?null:h('span',{class:'nt-when',title:'Created '+when(n.created)+(n.updated&&n.updated!==n.created?'\nUpdated '+when(n.updated):'')},ago(n.updated||n.created))));
 el.append(h(opt.compact?'div':'button',{class:'nt-t',type:opt.compact?null:'button',title:opt.compact?n.title:big?'Collapse':'Expand',onclick:opt.compact?()=>openNote(n.id):()=>{big?N.open.delete(n.id):N.open.add(n.id);rer()}},n.title||'(untitled)'));
 if(!opt.compact&&n.body){let long=n.body.length>230||n.body.split('\n').length>3,b=mdEl(n.body,'nt-body'+(big||!long?'':' clamp'));el.append(b);if(!big&&long)el.append(h('button',{class:'dk-more nt-mo',type:'button',onclick:()=>{N.open.add(n.id);rer()}},'More'))}
 let tg=n.targets||[],cap=opt.compact?4:big?400:12;
 if(tg.length)el.append(h('div',{class:'nt-tg'},tg.slice(0,cap).map(cChip),tg.length>cap?h('button',{class:'dk-more',type:'button',onclick:()=>opt.compact?openNote(n.id):(N.open.add(n.id),rer())},'+'+(tg.length-cap)):null));
 if(!opt.compact&&n.tags?.length)el.append(h('div',{class:'nt-tags'},n.tags.map(t=>h('button',{class:'nt-tag',type:'button',title:'Filter by tag',onclick:()=>{N.flt.q=t;N.ui.q.value=t;renderList(true)}},'#'+t))));
 if(n.proposal&&!opt.compact){let p=n.proposal,dl=p.diff?String(p.diff).split('\n').length:0,show=big||N.form['diff:'+n.id];
  el.append(h('div',{class:'nt-prop'},h('div',{class:'nt-ph'},'Proposal · '+(PL[p.type]||p.type||'other'),dl&&!big?h('button',{class:'dk-lnk',type:'button',onclick:()=>{N.form['diff:'+n.id]=!show;rer()}},show?'hide diff':'diff · '+dl+' line'+(dl>1?'s':'')):null),p.summary?mdEl(p.summary,'nt-ps'):null,p.diff&&show?diffEl(p.diff):null))}
 else if(n.proposal&&opt.compact&&n.proposal.summary){let ps=String(n.proposal.summary).replace(/`|\*\*|\[\[\w+:([^\]]+)\]\]/g,(m,x)=>x||'');el.append(h('div',{class:'nt-cps',title:ps},'Proposal · '+(PL[n.proposal.type]||n.proposal.type||'')+': '+ps))}
 if(!opt.compact&&n.sources?.length)el.append(h('div',{class:'nt-src'},h('span',{class:'nt-lb'},'Sources'),srcLinks(n.sources)));
 if(!opt.compact&&n.links?.length)el.append(h('div',{class:'nt-src'},n.links.map(l=>h('span',{class:'nt-link'},l.kind==='applied_in'?'Applied in ':'Related ',/^N-\d+$/.test(l.ref||'')?h('button',{class:'dk-chip k-note','data-k':'note','data-v':l.ref,type:'button'},l.ref):h('code',null,l.ref),l.note?' '+l.note:''))));
 // meta: author, provenance, conversation, comments
 if(!opt.compact){let p=n.provenance||{},nc=(n.comments||[]).length,A=AG();
  let ub=n.updated_by||{};
  el.append(h('div',{class:'nt-meta'},h('span',null,who(n.author)),ub.kind==='agent'&&n.updated!==n.created?h('span',{class:'nt-edby',title:'Last changed by the assistant'+(ub.session?' (session '+String(ub.session).slice(0,8)+')':'')+' '+when(n.updated)+'. Review before deciding.'},'edited by Claude'):null,prov(n,big).map(b=>h('span',null,b)),p.session&&A?.load?h('button',{class:'dk-lnk',type:'button',title:'Reopen the Ask conversation this came from (session '+p.session+(p.turn?' · turn '+p.turn:'')+')',onclick:()=>{DK()?.tab('ask');A.load(p.session)}},'conversation'+(p.turn?' · turn '+p.turn:'')):null,
   h('button',{class:'dk-lnk',type:'button',title:big?'Collapse':'Show and add comments',onclick:()=>{if(big)N.open.delete(n.id);else{N.open.add(n.id);N.form[n.id]={type:'comment'}}rer()}},nc?nc+' comment'+(nc>1?'s':''):'Comment')))}
 if(big){let cs=n.comments||[];if(cs.length)el.append(h('div',{class:'nt-cs'},cs.map(c=>h('div',{class:'nt-c'},h('div',{class:'nt-ch'},h('b',null,who(c.author)),h('span',{title:when(c.ts)},ago(c.ts))),mdEl(c.text,'nt-ct')))));
  let ta=h('textarea',{class:'dk-input nt-cta',rows:'2',maxlength:'4000',placeholder:'Add a comment…  (Ctrl/⌘+Enter)'});ta.value=N.drafts['c:'+n.id]||'';
  const go=async()=>{let v=ta.value.trim();if(!v)return;btn.disabled=true;try{await comment(n.id,v);N.drafts['c:'+n.id]='';N.form[n.id]=null;renderList(true)}catch(err){btn.disabled=false;errEl.textContent='Comment failed: '+err.message}};
  let btn=h('button',{type:'button',onclick:go},'Comment'),errEl=h('span',{class:'dk-err'});
  ta.oninput=()=>{N.drafts['c:'+n.id]=ta.value};ta.onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();go()}};
  el.append(h('div',{class:'nt-cf'},ta,h('div',{class:'nt-cfr'},errEl,h('span',{class:'dk-sp'}),btn)));if(form?.type==='comment')setTimeout(()=>ta.focus(),0)}
 // actions
 if(opt.compact){el.append(h('div',{class:'nt-act'},h('button',{class:'dk-lnk',type:'button',onclick:()=>openNote(n.id)},'Open in Notes'),
   opt.undo&&pristine(n)?h('button',{class:'dk-lnk nt-undo',type:'button',title:'Delete this note the assistant just recorded (your action)',onclick:async e=>{let b=e.currentTarget;b.disabled=true;b.textContent='Removing…';try{await remove(n.id)}catch(err){b.disabled=false;b.textContent='Undo failed: '+err.message}}},'Undo'):null,
   opt.discuss&&AG()?h('button',{class:'dk-lnk',type:'button',title:'Discuss this note in Ask',onclick:()=>discuss(n)},'Discuss'):null));return el}
 if(form&&form.type!=='comment'){let q=form.type==='delete'||form.type==='accept',inp=q?null:h('input',{class:'dk-input',maxlength:'400',placeholder:form.type==='rejected'?'Reason (optional, kept as a comment)':'Applied in: commit, file or run (optional)'}),errEl=h('span',{class:'dk-err'});
  const rev={expect_rev:n.rev}; // the version this card shows: 409 if it changed since
  const go=async()=>{try{ok.disabled=true;if(form.type==='delete')await remove(n.id);else if(form.type==='accept')await setStatus(n.id,'accepted',null,rev);
    else if(form.type==='applied'){let r=inp.value.trim(),links=(n.links||[]).slice();if(r)links.push({kind:'applied_in',ref:r});await setStatus(n.id,'applied',r?{links}:null,rev)}
    else{let r=inp.value.trim();await setStatus(n.id,'rejected',null,r?{...rev,comment:'Rejected: '+r}:rev)}N.form[n.id]=null;renderList(true)}catch(err){if(err.status===409)return stale(n);ok.disabled=false;errEl.textContent=err.message}};
  let ok=h('button',{type:'button',class:form.type==='delete'?'nt-danger':form.type==='rejected'?'nt-sb no':'nt-ok',onclick:go},{delete:'Delete',rejected:'Reject',accept:'Accept',applied:'Mark applied'}[form.type]);
  if(inp)inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();go()}else if(e.key==='Escape'){N.form[n.id]=null;rer()}};
  el.append(h('div',{class:'nt-mini'},form.type==='delete'?h('span',{class:'nt-q'},'Delete '+n.id+' permanently?'):form.type==='accept'?h('span',{class:'nt-q ok'},'Accept '+n.id+' with the diff shown above?'):inp,ok,h('button',{type:'button',onclick:()=>{N.form[n.id]=null;rer()}},'Cancel'),errEl));if(inp)setTimeout(()=>inp.focus(),0)}
 else el.append(h('div',{class:'nt-act'},flow(n).map(([to,l,cls,tip])=>h('button',{type:'button',class:'nt-sb '+cls,title:tip,onclick:()=>statusAction(n,to,rer)},l)),h('span',{class:'dk-sp'}),
  AG()?h('button',{class:'dk-lnk',type:'button',title:'Discuss this note in Ask (adds its targets to the context)',onclick:()=>discuss(n)},'Discuss'):null,
  h('button',{class:'dk-lnk',type:'button',onclick:()=>editor(n,n.id)},'Edit'),h('button',{class:'dk-lnk nt-del',type:'button',onclick:()=>{N.form[n.id]={type:'delete'};rer()}},'Delete')));
 if(opt.err||N.msg[n.id])el.append(h('div',{class:'dk-err nt-e'},opt.err||N.msg[n.id]));
 return el}
async function statusAction(n,to,rer){N.msg[n.id]=null;if(to==='rejected'||to==='applied'){N.form[n.id]={type:to};N.open.delete(n.id);return rer()}
 if(to==='accepted'&&n.proposal?.diff){N.form['diff:'+n.id]=true;N.form[n.id]={type:'accept'};return rer()} // decide on the diff you see
 try{await setStatus(n.id,to,null,{expect_rev:n.rev});N.form[n.id]=null;renderList(true)}catch(e){if(e.status===409)return stale(n);N.msg[n.id]='Status change failed: '+e.message;renderList(true)}}
// 409: the note changed after this card was drawn (typically the assistant edited it): redraw and ask for a fresh decision
async function stale(n){N.form[n.id]=null;try{await refresh()}catch(e){}let m=N.by.get(n.id),ub=m?.updated_by?.kind==='agent'?' by Claude':'';
 N.msg[n.id]=`${n.id} changed${ub} after you opened it; nothing was decided. Review the current version and decide again.`;N.open.add(n.id);renderList(true)}
function copyRef(n,btn){let t=`[[note:${n.id}]] ${n.title}`;if(!navigator.clipboard){prompt('Note reference (plain http page: copy it by hand)',t);return}navigator.clipboard.writeText(t).then(()=>{btn.textContent='copied';setTimeout(()=>{btn.textContent=n.id},900)},()=>prompt('Note reference',t))}
function discuss(n){let A=AG();if(!A)return;for(let t of (n.targets||[]).slice(0,40))A.addContext(t);A.draft(`About note ${n.id} (“${n.title}”): `)}

// ------------------------------------------------------------------ Notes tab
function textOf(n){return [n.id,n.title,n.body,KL[n.kind],SL[st(n)],(n.tags||[]).join(' '),(n.targets||[]).map(t=>[tLabel(t),t.ref,t.name,t.file].join(' ')).join(' '),(n.comments||[]).map(c=>c.text).join(' '),n.proposal?.summary,n.author==='agent'?'claude agent':'you user',(n.sources||[]).map(s=>s.title+' '+s.url).join(' ')].join('\n').toLowerCase()}
function filtered(){let f=N.flt,w=f.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
 return N.list.filter(n=>(f.status==='all'||(f.status==='active'?LIVE.includes(st(n)):st(n)===f.status))&&(!f.kind||n.kind===f.kind)&&(!f.author||n.author===f.author)&&(!N.forItem||matches(n,N.forItem))&&(!w.length||(t=>w.every(x=>t.includes(x)))(textOf(n))))}
function build(){const K=DK(),panel=K?.el('notes');if(!panel||N.ui)return;panel.innerHTML='';
 const sel=(v,opts,title)=>{let s=h('select',{class:'dk-input nt-sel',title},opts.map(([k,l])=>h('option',{value:k,selected:k===v||undefined},l)));return s};
 const u=N.ui={q:h('input',{class:'dk-input',type:'search',placeholder:'Search notes, parts, nets, tags…',spellcheck:'false'}),newb:h('button',{class:'dk-ask',type:'button',title:'New note (targets: the Ask context, else the inspected item)'},'New note'),
  st:sel(N.flt.status,[['all','All statuses'],['active','Open · proposed · accepted'],...STATUSES.map(s=>[s,SL[s]])],'Status'),kind:sel(N.flt.kind,[['','All kinds'],...KINDS.map(k=>[k,KL[k]])],'Kind'),
  au:sel(N.flt.author,[['','Anyone'],['user','You'],['agent','Claude']],'Author'),cnt:h('span',{class:'nt-cnt'}),state:h('div',{class:'nt-state',hidden:true}),forRow:h('div',{class:'nt-for',hidden:true}),
  list:h('div',{class:'nt-list'}),ed:h('div',{class:'nt-ed',hidden:true})};
 u.exp=h('span',{class:'nt-exp'},'Export ',h('a',{href:'/api/notes/export?format=md',target:'_blank',rel:'noopener',title:'design-notes.md: accepted (to apply), proposed, open, applied, rejected — the feed for the design loop'},'md'),' · ',h('a',{href:'/api/notes/export?format=json',target:'_blank',rel:'noopener',title:'All notes as JSON'},'json'));
 u.head=h('div',{class:'nt-bar'},h('div',{class:'nt-r1'},u.q,u.newb),h('div',{class:'nt-r2'},u.st,u.kind,u.au),h('div',{class:'nt-r3'},u.cnt,h('span',{class:'dk-sp'}),u.exp));
 panel.append(h('div',{class:'nt'},u.head,u.forRow,u.state,u.list,u.ed));
 const saveF=()=>store.set('splanc-notes-filter',{status:N.flt.status,kind:N.flt.kind,author:N.flt.author});
 u.q.oninput=()=>{N.flt.q=u.q.value;renderList(true)};u.st.onchange=()=>{N.flt.status=u.st.value;saveF();renderList(true)};u.kind.onchange=()=>{N.flt.kind=u.kind.value;saveF();renderList(true)};u.au.onchange=()=>{N.flt.author=u.au.value;saveF();renderList(true)};
 u.newb.onclick=()=>newNote();
 u.list.addEventListener('focusout',()=>setTimeout(()=>{if(N.dirty&&!u.list.contains(D.activeElement))renderList()},0));
 D.addEventListener('splanc:dock',e=>{if(e.detail?.tab==='notes'&&e.detail.open)refresh().catch(()=>{})});
 D.addEventListener('visibilitychange',()=>{if(!D.hidden)refresh().catch(()=>{})});
 D.addEventListener('keydown',e=>{if(!N.ed||!u.ed.contains(e.target))return;if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();u.ed.querySelector('.nt-save')?.click()}else if(e.key==='Escape'){e.preventDefault();closeEditor()}});
 render()}
function renderState(){let u=N.ui;if(!u)return;let msg=N.ok===false?'Notes are not available on this server ('+(N.err||'no /api/notes')+'). Restart the viewer with the notes store enabled.':N.err?'Cannot reach the notes store: '+N.err+' · retrying':'';
 u.state.hidden=!msg;u.state.textContent=msg;u.state.className='nt-state'+(N.ok===false?' off':'');u.newb.disabled=N.ok===false;tabCount()}
function tabCount(){let root=DK()?.root;if(!root)return;let p=N.list.filter(n=>st(n)==='proposed').length;
 for(let b of root.querySelectorAll('[data-tab="notes"]')){let em=b.querySelector('.dk-n');if(!em){em=h('em',{class:'dk-n'});b.insertBefore(em,b.querySelector('.dk-dot'))}em.textContent=p?String(p):'';em.hidden=!p;b.title=p?p+' note'+(p>1?'s':'')+' awaiting your decision':'Notes'}}
function render(){renderState();if(!N.ed)renderList()}
function renderList(force){let u=N.ui;if(!u||N.ed)return;let a=D.activeElement;if(!force&&a&&u.list.contains(a)&&a.matches('input,textarea,select')){N.dirty=true;return}N.dirty=false;
 let top=u.list.scrollTop,out=[];
 u.forRow.hidden=!N.forItem;if(N.forItem)u.forRow.replaceChildren(h('span',{class:'nt-lb'},'Notes for'),tChip(N.forItem),h('span',{class:'dk-sp'}),h('button',{class:'dk-xs',type:'button',title:'Show all notes',onclick:()=>{N.forItem=null;renderList(true)}},'×'));
 let all=filtered().sort(order);u.cnt.textContent=N.loaded?(all.length===N.list.length?N.list.length+' note'+(N.list.length===1?'':'s'):all.length+' of '+N.list.length+' notes')+(N.list.some(n=>st(n)==='proposed')?' · '+N.list.filter(n=>st(n)==='proposed').length+' awaiting decision':''):'';
 if(!N.loaded)out.push(h('div',{class:'dk-empty'},'Loading notes…'));
 else if(!N.list.length){if(N.ok!==false)out.push(h('div',{class:'dk-empty'},h('b',null,'No notes yet'),h('p',null,'Notes keep what you and the assistant conclude about the circuit — observations, questions, requirements, decisions, to-dos and proposals — tied to parts, nets, pads, regions and source lines.'),
  h('p',null,'Ask the assistant to “record this as a note”, use Save as note under an answer, Add note in Inspect, or New note above. Accepted notes are the input for the next design pass (Export md).'),h('button',{type:'button',class:'dk-ask',onclick:()=>newNote()},'New note')))}
 else if(!all.length)out.push(h('div',{class:'dk-empty'},'No notes match these filters. ',h('button',{type:'button',onclick:()=>{N.flt={status:'all',kind:'',author:'',q:''};N.forItem=null;u.q.value='';u.st.value='all';u.kind.value='';u.au.value='';renderList(true)}},'Clear filters')));
 else{let groups=[...GROUPS,...[...new Set(all.map(st))].filter(s=>!(s in RANK)).map(s=>[s,s])];
  for(let [s,label] of groups){let g=all.filter(n=>st(n)===s);if(!g.length)continue;out.push(h('h3',{class:'nt-gh s-'+s},h('i'),label+' · '+g.length),...g.map(n=>card(n)))}}
 u.list.replaceChildren(...out);u.list.scrollTop=top}
function openNote(id){let K=DK();if(N.ed&&N.ed.dirty){K?.tab('notes');return}N.ed=null;if(N.ui){N.ui.ed.hidden=true;N.ui.list.hidden=false;N.ui.head.hidden=false}
 let n=N.by.get(id);if(n&&!filtered().includes(n)){N.flt={status:'all',kind:'',author:'',q:''};N.forItem=null;let u=N.ui;if(u){u.q.value='';u.st.value='all';u.kind.value='';u.au.value=''}}
 if(n)N.open.add(id);K?.tab('notes');renderList(true);K?.badge?.('notes',false);
 requestAnimationFrame(()=>{let el=N.ui?.list.querySelector(`.nt-card[data-id="${CSS.escape(id)}"]`);if(!el){if(!n&&!N.loaded)refresh().then(()=>N.by.has(id)&&openNote(id)).catch(()=>{});return}el.scrollIntoView({block:'nearest'});el.classList.add('flash');setTimeout(()=>el.classList.remove('flash'),1400)})}
function showFor(item){N.forItem=item?clone(item):null;N.ed=null;if(N.ui){N.ui.ed.hidden=true;N.ui.list.hidden=false;N.ui.head.hidden=false}DK()?.tab('notes');renderList(true)}

// ------------------------------------------------------------------ editor (new / edit; the user is the author of new notes)
function defaultTargets(){let c=AG()?.context?.()||[];if(c.length)return clone(c.slice(0,40));let it=SS()?.item?.();return it?[clone(it)]:[]}
function newNote(pre={}){pre=clone(pre)||{};if(pre.targets===undefined)pre.targets=defaultTargets();editor(pre,null)}
function editor(pre,id){let base=id?N.by.get(id):null;if(id&&!base)return;
 let f={kind:'observation',status:'',title:'',body:'',targets:[],tags:[],proposal:null,sources:[],...clone(id?base:pre)};
 if(!KINDS.includes(f.kind))f.kind='observation';if(!f.status)f.status=f.kind==='proposal'||f.proposal?'proposed':'open';
 N.ed={id,f,orig:id?clone(base):null,dirty:false,err:null,prop:!!f.proposal||f.kind==='proposal'};build();DK()?.tab('notes');renderEditor()}
function closeEditor(force){let E=N.ed;if(!E)return;if(E.dirty&&!force&&!confirm('Discard this unsaved note?'))return;N.ed=null;let u=N.ui;u.ed.hidden=true;u.list.hidden=false;u.head.hidden=false;renderList(true)}
function renderEditor(){let u=N.ui,E=N.ed;if(!u||!E)return;let f=E.f,base=E.id?N.by.get(E.id):null;u.head.hidden=true;u.forRow.hidden=true;u.list.hidden=true;u.ed.hidden=false;
 const dirty=()=>{E.dirty=true},lab=(t,...k)=>h('label',{class:'nt-fl'},h('span',null,t),...k);
 let kind=h('select',{class:'dk-input'},KINDS.map(k=>h('option',{value:k,selected:k===f.kind||undefined},KL[k]))),status=h('select',{class:'dk-input'},STATUSES.map(s=>h('option',{value:s,selected:s===f.status||undefined},SL[s])));
 let title=h('input',{class:'dk-input',maxlength:'120',placeholder:'One line: what is this about?',value:f.title||''}),tcnt=h('em',{class:'nt-cc'}),body=h('textarea',{class:'dk-input nt-bta',rows:'8',maxlength:'8000',placeholder:'Details (markdown). [[ref:C17]] [[net:hv]] [[pad:U5.13]] [[src:system_5v.ato:112-118]] become chips.'}),bcnt=h('em',{class:'nt-cc'});
 body.value=f.body||'';const cc=()=>{tcnt.textContent=title.value.length+'/120';bcnt.textContent=body.value.length+'/8000'};cc();
 let tags=h('input',{class:'dk-input',placeholder:'power, layout, datasheet…',value:(f.tags||[]).join(', ')});
 let srcs=h('textarea',{class:'dk-input nt-mono nt-srcs',rows:'2',placeholder:'https://… Optional title after a space; one per line'});srcs.value=(f.sources||[]).map(s=>s.url+(s.title?' '+s.title:'')).join('\n');
 let tgRow=h('div',{class:'nt-tg nt-edtg'});
 const drawT=()=>{let A=AG(),ctx=A?.context?.()||[],it=SS()?.item?.(),key=x=>A?.key?A.key(x):JSON.stringify(x),have=new Set(f.targets.map(key));
  tgRow.replaceChildren(...[...f.targets.map((t,i)=>tChip(t,()=>{f.targets.splice(i,1);dirty();drawT()})),f.targets.length?null:h('span',{class:'dk-muted'},'No targets: the note is not tied to the board. '),
   ctx.some(x=>!have.has(key(x)))?h('button',{class:'dk-more',type:'button',title:'Add the items in the Ask context',onclick:()=>{for(let x of ctx)if(!have.has(key(x))&&f.targets.length<40)f.targets.push(clone(x));dirty();drawT()}},'+ Ask context ('+ctx.length+')'):null,
   it&&!have.has(key(it))?h('button',{class:'dk-more',type:'button',title:'Add the item shown in Inspect',onclick:()=>{f.targets.push(clone(it));dirty();drawT()}},'+ Inspect: '+shortLabel(it)):null].filter(Boolean))};drawT();
 let pOn=h('input',{type:'checkbox',checked:E.prop||undefined}),ptype=h('select',{class:'dk-input'},PTYPES.map(p=>h('option',{value:p,selected:p===(f.proposal?.type||'ato')||undefined},PL[p]))),
  psum=h('input',{class:'dk-input',maxlength:'400',placeholder:'What should change, in one sentence',value:f.proposal?.summary||''}),pdiff=h('textarea',{class:'dk-input nt-mono',rows:'5',wrap:'off',placeholder:'Optional diff or snippet (unified diff renders coloured)'});pdiff.value=f.proposal?.diff||'';
 let pBox=h('div',{class:'nt-pbox',hidden:!E.prop},lab('Change in',ptype),lab('Summary',psum),lab('Diff',pdiff));
 pOn.onchange=()=>{E.prop=pOn.checked;pBox.hidden=!E.prop;if(E.prop&&status.value==='open'){status.value='proposed'}dirty()};
 kind.onchange=()=>{if(kind.value==='proposal'&&!E.prop){pOn.checked=true;pOn.onchange()}if(kind.value==='proposal'&&status.value==='open')status.value='proposed';dirty()};
 for(let el of [title,body,tags,srcs,psum,pdiff])el.addEventListener('input',()=>{dirty();cc()});status.onchange=ptype.onchange=dirty;
 let err=h('div',{class:'dk-err nt-e'}),save=h('button',{class:'primary nt-save',type:'button'},E.id?'Save changes':'Save note');
 save.onclick=async()=>{let out={kind:kind.value,status:status.value,title:title.value.trim(),body:body.value.replace(/\s+$/,''),targets:f.targets.slice(0,40),tags:[...new Set(tags.value.split(/[,\n]/).map(t=>t.trim().replace(/^#/,'')).filter(Boolean))].slice(0,20),sources:[]};
  for(let l of srcs.value.split('\n').map(x=>x.trim()).filter(Boolean)){let m=/^(\S+)\s*(.*)$/.exec(l);if(!/^https?:\/\/\S+$/i.test(m[1])){err.textContent='Sources must be http(s) URLs: '+m[1];return}out.sources.push(m[2]?{url:m[1],title:m[2].slice(0,200)}:{url:m[1]})}
  if(!out.title){err.textContent='A title is required.';title.focus();return}
  if(E.prop){if(!psum.value.trim()){err.textContent='A proposal needs a summary (or untick “Attach a proposal”).';psum.focus();return}out.proposal={type:ptype.value,summary:psum.value.trim(),...(pdiff.value.trim()?{diff:pdiff.value.replace(/\s+$/,'')}:{})}}else out.proposal=null;
  err.textContent='';save.disabled=true;save.textContent='Saving…';
  try{let n;if(E.id){let o=E.orig||{},fields={};for(let k of ['kind','status','title','body','targets','tags','sources','proposal'])if(JSON.stringify(out[k]??null)!==JSON.stringify(o[k]??(k==='proposal'?null:k==='body'||k==='title'?'':[])))fields[k]=out[k];
    n=Object.keys(fields).length?await update(E.id,fields,typeof o.rev==='number'?{expect_rev:o.rev}:null):base}
   else{let v=V(),call=g=>{try{return g?.()??undefined}catch(e){return undefined}},p={lane:call(v?.lane),phase:call(v?.phase),board_sha:call(v?.boardSha),viewer_port:+location.port||undefined};
    for(let k of Object.keys(p))if(p[k]==null||p[k]==='')delete p[k];if(!out.proposal)delete out.proposal;n=await create({...out,provenance:{...p,...(f.provenance||{})}})}
   let id=n?.id||E.id;closeEditor(true);if(id)openNote(id)}
  catch(e){save.disabled=false;save.textContent=E.id?'Save changes':'Save note';err.textContent='Save failed: '+e.message+(e.status===409?' (someone changed it: copy your text, Cancel and edit again)':'')}};
 let cancel=h('button',{type:'button',onclick:()=>closeEditor()},'Cancel');
 u.ed.replaceChildren(h('div',{class:'nt-eh'},h('b',null,E.id?'Edit '+E.id:'New note'),E.id&&base?.author==='agent'?h('span',{class:'nt-by'},'written by Claude'):h('span',{class:'dk-muted'},E.id?'':'you are the author'),h('span',{class:'dk-sp'}),h('button',{class:'dk-xs',type:'button',title:'Close (Esc)',onclick:()=>closeEditor()},'×')),
  h('div',{class:'nt-eb'},h('div',{class:'nt-frow'},lab('Kind',kind),lab('Status',status)),lab('Title',title,tcnt),lab('Body',body,bcnt),h('div',{class:'nt-fl'},h('span',null,'Targets'),tgRow),lab('Tags',tags),
   h('label',{class:'nt-chk'},pOn,' Attach a proposal (a change for a human to apply: atopile source, PnR annotation, constraint or engine)'),pBox,lab('Web sources',srcs),
   h('p',{class:'dk-muted'},'Accepted, rejected and applied are your decisions; the assistant can only propose. Accepted notes feed the next design pass through design-notes.md.')),
  h('div',{class:'nt-ef'},err,h('span',{class:'dk-sp'}),cancel,save));
 setTimeout(()=>(E.id?body:title).focus(),0)}

// ------------------------------------------------------------------ Inspect section (source.js calls SplancNotes.inspectSection(item))
function inspectSection(item){if(!item)return null;let list=forItem(item),live=list.filter(n=>LIVE.includes(st(n)));if(!list.length)return null;
 let show=list.slice(0,4);
 return h('div',{class:'dk-sec nt-insp'},h('h3',null,'Notes · '+list.length+(live.length&&live.length<list.length?' ('+live.length+' open)':'')),show.map(n=>card(n,{compact:true,discuss:true})),
  list.length>show.length?h('button',{class:'dk-more',type:'button',onclick:()=>showFor(item)},'Show all '+list.length+' in Notes'):null)}

// ------------------------------------------------------------------ init
function start(){build();wireToggle();poll();
 let q=new URLSearchParams(location.search).get('note');if(q){let tries=0,go=()=>{if(N.by.has(q))openNote(q);else if(tries++<20)setTimeout(go,300)};go()}}
window.SplancNotes={list:()=>N.list.slice(),get:id=>N.by.get(id)||null,ids:()=>[...N.by.keys()],rev:()=>N.rev,available:()=>N.ok!==false,
 open:openNote,newNote,edit:id=>editor(null,id),showFor,forItem,matches,sourceLines,badgesFor,drawBadges,badgeAt,badges,
 create,update,setStatus,comment,remove,refresh,upsert,fast(on){N.fast=!!on;schedule()},card,inspectSection,labels:{kind:KL,status:SL}};
if(DK())start();else D.addEventListener('DOMContentLoaded',()=>DK()?start():console.warn('notes.js: window.SplancDock missing (load source.js before notes.js)'));
})();
