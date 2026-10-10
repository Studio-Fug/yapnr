'use strict';
// Right-hand dock (Inspect | Source | Ask | Notes) and the atopile source browser / inspector.
// Data: GET /api/source/index, GET /api/source/file?path=. Talks to app.js only through
// window.YapnrView (highlight/clear/lane/phase/view/netInfo/componentInfo), to agent.js
// through window.YapnrAgent and to notes.js through window.YapnrNotes (Inspect notes, Add note,
// Source gutter markers), all optional. Exposes window.YapnrDock and window.YapnrSource.
(function(){
const D=document,store={get(k,d){try{let v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const nat=(a,b)=>String(a).localeCompare(String(b),undefined,{numeric:true});
const V=()=>window.YapnrView||null,AG=()=>window.YapnrAgent||null;
function h(t,a,...k){let e=D.createElement(t);if(a)for(let [x,v] of Object.entries(a)){if(v==null||v===false)continue;if(x==='class')e.className=v;else if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v===true?'':v)}for(let c of k.flat(9))if(c!=null&&c!==false)e.append(c.nodeType?c:String(c));return e}

// ------------------------------------------------------------------ dock
const DOCK=window.YapnrDock||(function(){
 const TABS=[['inspect','Inspect'],['source','Source'],['ask','Ask'],['notes','Notes'],['timing','Timing']],pref=store.get('yapnr-dock',{}),wide=()=>innerWidth>1360;
 let cur=TABS.some(t=>t[0]===pref.tab)?pref.tab:'inspect',open=wide()&&pref.open!==false,w=+pref.w||380,kt=0;
 const el=h('aside',{id:'dock','aria-label':'Inspect, source, assistant and notes'},
  h('div',{class:'dk-grip',title:'Drag to resize'}),
  h('div',{class:'dk-head'},h('div',{class:'dk-tabs',role:'tablist'},TABS.map(([k,l])=>h('button',{'data-tab':k,role:'tab'},l,h('i',{class:'dk-dot'})))),h('button',{class:'dk-x',title:'Collapse panel'},'»')),
  h('div',{class:'dk-rail'},h('button',{class:'dk-exp',title:'Expand panel'},'«'),TABS.map(([k,l])=>h('button',{'data-tab':k},l,h('i',{class:'dk-dot'})))),
  TABS.map(([k])=>h('section',{class:'dk-panel','data-p':k,role:'tabpanel'})));
 const fab=h('button',{id:'dk-fab',title:'Open the Inspect / Source / Ask / Notes panel'},'Inspect · Source · Ask · Notes');
 (D.querySelector('main')||D.body).append(el);D.body.append(fab);D.body.classList.add('dk-on');
 const maxW=()=>wide()?Math.max(300,innerWidth-245-310-400):Math.round(innerWidth*.94),clampW=v=>Math.round(Math.max(280,Math.min(maxW(),v)));
 const save=()=>store.set('yapnr-dock',{tab:cur,open:wide()?open:(pref.open!==false),w});
 // resize only when the layout really changes: a plain tab switch must not make the schematic re-fit
 let lastLay='';const kick=()=>{let lay=open+'|'+clampW(w)+'|'+wide();if(lay===lastLay)return;lastLay=lay;cancelAnimationFrame(kt);kt=requestAnimationFrame(()=>dispatchEvent(new Event('resize')))};
 function apply(resize=true){D.body.classList.toggle('dk-min',!open);D.documentElement.style.setProperty('--dk-w',clampW(w)+'px');
  for(let b of el.querySelectorAll('[data-tab]')){b.classList.toggle('on',b.dataset.tab===cur);b.setAttribute('aria-selected',b.dataset.tab===cur)}
  for(let p of el.querySelectorAll('.dk-panel'))p.hidden=p.dataset.p!==cur;if(open)api.badge(cur,false);if(resize)kick()}
 const fire=()=>D.dispatchEvent(new CustomEvent('yapnr:dock',{detail:{tab:cur,open}}));
 const api={el:n=>el.querySelector(`.dk-panel[data-p="${n}"]`),root:el,current:()=>cur,isOpen:()=>open,
  tab(n,show=true){if(n&&TABS.some(t=>t[0]===n))cur=n;if(show)open=true;save();apply(show);fire();return api.el(cur)},
  open(){open=true;save();apply();fire()},close(){open=false;save();apply();fire()},toggle(){open?api.close():api.open()},
  badge(n,on){let vis=open&&cur===n;for(let d of el.querySelectorAll(`[data-tab="${n}"] .dk-dot`))d.classList.toggle('on',!!on&&!vis);fab.classList.toggle('dot',!!el.querySelector('.dk-dot.on'))},
  count(n){fab.dataset.count=n?String(n):'';fab.title='Open the Inspect / Source / Ask / Notes panel'+(n?' · '+n+' item'+(n>1?'s':'')+' in the Ask context':'')}};
 for(let b of el.querySelectorAll('[data-tab]'))b.onclick=()=>api.tab(b.dataset.tab);
 el.querySelector('.dk-x').onclick=api.close;el.querySelector('.dk-exp').onclick=api.open;fab.onclick=api.open;
 const grip=el.querySelector('.dk-grip');let drag=null;
 grip.onpointerdown=e=>{grip.setPointerCapture(e.pointerId);drag={x:e.clientX,w:el.getBoundingClientRect().width};D.body.classList.add('dk-resizing')};
 grip.onpointermove=e=>{if(!drag)return;w=clampW(drag.w+drag.x-e.clientX);apply()};
 grip.onpointerup=grip.onpointercancel=()=>{if(!drag)return;drag=null;D.body.classList.remove('dk-resizing');save()};
 grip.ondblclick=()=>{w=380;save();apply()};
 let wasWide=wide();addEventListener('resize',()=>{let now=wide();if(now!==wasWide){wasWide=now;if(!now&&open){open=false;apply(false)}}D.documentElement.style.setProperty('--dk-w',clampW(w)+'px')});
 addEventListener('keydown',e=>{if(e.key==='Escape'&&open&&!wide()&&!el.contains(D.activeElement))api.close()});
 apply(false);lastLay=open+'|'+clampW(w)+'|'+wide();return window.YapnrDock=api;
})();

// ------------------------------------------------------------------ state + index
const S={idx:null,p:null,err:null,at:0,look:null,files:new Map(),file:null,sel:null,marks:new Set(),hist:[],item:null,ihist:[],needScroll:0};
const P={insp:DOCK.el('inspect'),src:DOCK.el('source')},NT=()=>window.YapnrNotes||null;
if(DOCK.el('notes')&&!DOCK.el('notes').childNodes.length)DOCK.el('notes').append(h('div',{class:'dk-empty'},'Notes panel not loaded (dist/notes.js).'));
// /api/about says whether this server has a source index (features.source): without one the index is
// never requested (its 503 would only add console errors), and the Source tab shows the reason.
let about=null;const features=()=>about||(about=fetch('/api/about').then(r=>r.ok?r.json():null).then(a=>a?.features||{}).catch(()=>({})));
function load(force){
 if(S.p&&!force)return S.p;
 S.p=features().then(f=>{if(f.source===false)throw Error(f.source_reason||'no atopile source configured');return fetch('/api/source/index')})
  .then(async r=>{let j=await r.json().catch(()=>null);if(!r.ok||!j)throw Error(j?.error||'HTTP '+r.status);return j})
  .then(j=>{S.err=null;S.at=Date.now();if(!S.idx||S.idx.sha!==j.sha||force==='always'){S.idx=j;prep(j);onIndex()}return S.idx})
  .catch(e=>{S.err=e.message;S.p=null;onIndex();throw e});
 return S.p;
}
function prep(x){
 for(let k of ['modules','components','nets'])x[k]=x[k]||{};x.files=x.files||[];
 const L=S.look={inst:new Map(),alias:new Map(),line:new Map(),prefix:new Map()};
 const add=(f,l,k,v)=>{if(!f||!l)return;let key=f+':'+l,o=L.line.get(key);if(!o)L.line.set(key,o={refs:new Set(),nets:new Set()});o[k].add(v)};
 for(let [ref,c] of Object.entries(x.components)){c.ref=ref;if(c.instance)L.inst.set(c.instance,ref);if(c.address&&c.address!==c.instance)L.inst.set(c.address,ref);
  add(c.file,c.line,'refs',ref);for(let e of c.chain||[])add(e.file,e.line,'refs',ref);for(let s of c.statements||[])add(s.file,s.line,'refs',ref)}
 for(let [name,n] of Object.entries(x.nets)){n.name=n.name||name;for(let a of n.aliases||[]){if(a.path&&!L.alias.has(a.path))L.alias.set(a.path,name);add(a.file,a.line,'nets',name)}for(let s of n.statements||[])add(s.file,s.line,'nets',name)}
 // the server's line map also covers @pnr-current / @pnr-plane-access lines (contracts resolved to ref + net)
 for(let [f,ls] of Object.entries(x.lines||{}))for(let [l,o] of Object.entries(ls||{})){for(let r of o.refs||[])add(f,+l,'refs',r);for(let n of o.nets||[])add(f,+l,'nets',n)}
 L.instances=[...L.inst.keys()];
}
const IX=()=>S.idx,comp=r=>S.idx?.components?.[r]||null,net=n=>S.idx?.nets?.[n]||null;
function fileRec(p){if(!p||!S.idx)return null;p=String(p).replace(/\\/g,'/');let fs=S.idx.files;return fs.find(f=>f.path===p)||fs.find(f=>p.endsWith('/'+f.path))||fs.find(f=>f.path.split('/').pop()===p.split('/').pop())||null}
const fileName=p=>{let s=String(p||'').split('/');return s[0]==='parts'&&s.length>2?s[s.length-1]:s.join('/')};
// Net labels: low-information atopile names ('hv', '10', 'board-1-3') are shown with their semantic title.
function netLabel(name,long){let n=net(name);if(!n||!n.title||n.title===name)return String(name);if(long)return n.title;if(!n.low_info)return String(name);let t=n.title.replace(/\s*\([^()]*\)\s*$/,'')||n.title;return t===name?t:t+' ('+name+')'}
function netTip(name){let n=net(name);return n?[n.title||name,n.title&&n.title!==name?'net '+name:'',n.summary||''].filter(Boolean).join('\n'):'net '+name}
function refTip(r){let c=comp(r);return c?[r+' · '+(c.type||c.part||''),c.instance||c.address,c.doc||''].filter(Boolean).join('\n'):r}

// ------------------------------------------------------------------ resolution: source token -> refs / nets / pads
function modAt(file,line){let best=null;for(let [name,m] of Object.entries(S.idx?.modules||{}))if(m.file===file&&m.line<=line&&line<=(m.end||1e9)&&(!best||m.line>best.line))best={...m,name};return best}
function prefixes(file,line){
 let m=modAt(file,line),key=m?m.name:'@'+file;if(S.look.prefix.has(key))return S.look.prefix.get(key);
 let out=new Set(),re=m&&new RegExp('=\\s*new\\s+'+m.name.replace(/\W/g,'\\$&')+'\\b');
 if(m)for(let c of Object.values(S.idx.components))for(let e of [...(c.chain||[]),{address:c.instance,file:c.file,line:c.line,text:c.text}]){if(!e.address)continue;
  if(e.text&&re.test(e.text))out.add(e.address);else if(e.file===m.file&&e.line>=m.line&&e.line<=(m.end||1e9)){let i=e.address.lastIndexOf('.');out.add(i<0?'':e.address.slice(0,i))}}
 let entry=String(S.idx.entry||'').split(':').pop();if(!out.size&&(!m||m.name===entry))out.add('');
 let r=[...out];S.look.prefix.set(key,r);return r}
function resolve(file,line,tok){
 const L=S.look,r={refs:new Set(),nets:new Set(),pads:new Set()};if(!L||!tok)return r;
 tok=tok.replace(/^\.+/,'');
 for(let segs=tok.split('.');segs.length&&!r.refs.size&&!r.nets.size;segs.pop()){
  let t=segs.join('.');
  for(let p of prefixes(file,line)){let full=p?p+'.'+t:t,n=L.alias.get(full),ref=L.inst.get(full);if(n)r.nets.add(n);
   if(ref){r.refs.add(ref);continue}
   let pre=full+'.',kids=L.instances.filter(i=>i.startsWith(pre));for(let i of kids)r.refs.add(L.inst.get(i));
   if(kids.length)continue;
   if(!n){let seen=0;for(let [a,nn] of L.alias)if(a.startsWith(pre)){r.nets.add(nn);if(++seen>40)break}}
   let ps=full.split('.');for(let k=ps.length-1;k>0;k--){let rf=L.inst.get(ps.slice(0,k).join('.'));if(!rf)continue;let pin=ps.slice(k).join('.'),c=comp(rf);r.refs.add(rf);
    for(let [pad,v] of Object.entries(c.pins||{}))if(v.pin===pin||pad===pin||(n&&v.net===n)){r.pads.add(rf+'.'+pad);if(v.net)r.nets.add(v.net)}break}
  }
 }
 if(!r.refs.size&&!r.nets.size){let o=L.line.get(file+':'+line);if(o)for(let x of o.nets)r.nets.add(x)}
 return r}
function lineHits(file,a,b){let r={refs:new Set(),nets:new Set()};if(!S.look)return r;for(let l=a;l<=(b||a);l++){let o=S.look.line.get(file+':'+l);if(o){o.refs.forEach(x=>r.refs.add(x));o.nets.forEach(x=>r.nets.add(x))}}return r}
function highlight(sel){let v=V();try{v?.highlight?.({refs:[...(sel.refs||[])],nets:[...(sel.nets||[])],pads:[...(sel.pads||[])]})}catch(e){console.warn('YapnrView.highlight',e)}}

// ------------------------------------------------------------------ chips (shared with agent.js)
function srcParse(v){let m=/^(.*?):(\d+)(?:\s*[-–]\s*(\d+))?$/.exec(String(v).trim());return m?{file:m[1],line:+m[2],end:m[3]?+m[3]:+m[2]}:{file:String(v),line:1,end:1}}
function chipLabel(k,v){if(k==='net')return netLabel(v);if(k==='src'){let s=srcParse(v);return fileName(s.file)+':'+s.line+(s.end>s.line?'–'+s.end:'')}return String(v)}
function chipTip(k,v){if(k==='net')return netTip(v);if(k==='ref')return refTip(v);if(k==='pad'){let i=v.lastIndexOf('.'),c=comp(v.slice(0,i)),p=c?.pins?.[v.slice(i+1)];return v+(p?.pin?' · '+p.pin:'')+(p?.net?'\nnet '+netLabel(p.net,true):'')}if(k==='src')return 'Open '+v+' in Source';return v}
function chip(k,v,label,opt){return h('button',{class:'dk-chip k-'+k,'data-k':k,'data-v':v,title:chipTip(k,v),onclick:e=>{e.stopPropagation();act(k,v,opt)}},label??chipLabel(k,v))}
function act(k,v,opt={}){
 if(k==='src'){let s=srcParse(v);return open(s.file,s.line,s.end)}
 if(k==='ref'){highlight({refs:[v]});return inspect({kind:'component',ref:v},opt)}
 if(k==='net'){highlight({nets:[v]});return inspect({kind:'net',name:v},opt)}
 if(k==='pad'){let i=v.lastIndexOf('.'),ref=v.slice(0,i),pad=v.slice(i+1);highlight({pads:[v]});return inspect({kind:'pad',ref,pad},opt)}
}

// ------------------------------------------------------------------ syntax highlighting (.ato)
const KW=new Set(['module','component','interface','new','signal','pin','import','from','trait','pragma','to','within','assert','is','pass','for','in','if']);
function hlComment(t){let ann=/@pnr-[\w-]+/.exec(t);if(!ann)return [`<span class="t-com">${esc(t)}</span>`,false];
 let i=ann.index,rest=t.slice(i+ann[0].length);
 return [`<span class="t-com">${esc(t.slice(0,i))}</span><span class="t-ann" data-ann="${esc(rest.trim())}">${esc(ann[0])}</span><span class="t-annj">${esc(rest)}</span>`,true]}
function hlLine(line,st){
 let out='',i=0,n=line.length,prev=null,plain=false,ann=false;
 if(st.doc){let j=line.indexOf(st.doc);if(j<0)return [`<span class="t-doc">${esc(line)}</span>`,false];out=`<span class="t-doc">${esc(line.slice(0,j+3))}</span>`;i=j+3;st.doc=null}
 while(i<n){let c=line[i],rest=line.slice(i),m;
  if(c==='#'){if(/^#pragma\b/.test(rest)){out+=`<span class="t-kw">#pragma</span>${esc(rest.slice(7))}`}else{let [s,a]=hlComment(rest);out+=s;ann=a}break}
  if(rest.startsWith('"""')||rest.startsWith("'''")){let q=rest.slice(0,3),j=line.indexOf(q,i+3);if(j<0){out+=`<span class="t-doc">${esc(rest)}</span>`;st.doc=q;break}out+=`<span class="t-doc">${esc(line.slice(i,j+3))}</span>`;i=j+3;continue}
  if(c==='"'||c==="'"){let j=line.indexOf(c,i+1);if(j<0)j=n-1;let s=line.slice(i,j+1),f=/\.ato$/.test(s.slice(1,-1));out+=f?`<span class="t-str t-file" data-f="${esc(s.slice(1,-1))}">${esc(s)}</span>`:`<span class="t-str">${esc(s)}</span>`;i=j+1;continue}
  if((m=/^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*/.exec(rest))){let w=m[0];
   if(KW.has(w)){out+=`<span class="t-kw">${w}</span>`;prev=w;if(w==='trait'||w==='import'||w==='pin')plain=true}
   else if(prev==='new'||prev==='module'||prev==='component'||prev==='interface'||prev==='import'||(prev==='from'&&!plain))out+=`<span class="t-ty">${esc(w)}</span>`;
   else if(w==='True'||w==='False'||w==='None')out+=`<span class="t-num">${w}</span>`;
   else out+=plain?`<span class="t-pl">${esc(w)}</span>`:`<span class="t-id">${esc(w)}</span>`;
   i+=w.length;continue}
  if((m=/^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?:[a-zA-Zµ%Ω]+)?/.exec(rest))){out+=`<span class="t-num">${esc(m[0])}</span>`;i+=m[0].length;continue}
  if(rest.startsWith('+/-')){out+='<span class="t-op">+/-</span>';i+=3;continue}
  if(c==='~'||c==='='){out+=`<span class="t-op">${c}</span>`;i++;continue}
  out+=esc(c);i++}
 return [out,ann]}

// ------------------------------------------------------------------ Source tab
P.src.innerHTML='';
const SB={bar:h('div',{class:'src-bar'}),list:h('div',{class:'src-list',hidden:true}),code:h('div',{class:'src-code',tabindex:'0'}),res:h('div',{class:'src-res',hidden:true})};
const btnFiles=h('button',{class:'src-files',title:'Browse source files','aria-expanded':'false'},'Files ▾'),btnBack=h('button',{class:'src-back',title:'Back to the previous location',disabled:true},'‹'),nameEl=h('b',{class:'src-name'},'Source'),metaEl=h('span',{class:'src-meta'});
SB.bar.append(btnFiles,btnBack,h('span',{class:'src-title'},nameEl,metaEl));P.src.append(h('div',{class:'src'},SB.bar,SB.list,SB.code,SB.res));
SB.code.append(h('div',{class:'dk-empty'},'Loading source index…'));
btnFiles.onclick=()=>toggleList();btnBack.onclick=()=>{let b=S.hist.pop();if(b)open(b.file,b.line,b.end,{back:true});btnBack.disabled=!S.hist.length};
function toggleList(force){let show=force??SB.list.hidden;SB.list.hidden=!show;btnFiles.setAttribute('aria-expanded',show);btnFiles.classList.toggle('on',show);if(show)renderList()}
function renderList(){
 let x=IX();SB.list.replaceChildren();if(!x){SB.list.append(h('div',{class:'dk-empty'},S.err?'Source index unavailable: '+S.err:'Loading…'));return}
 let q=h('input',{class:'dk-input',placeholder:'Filter files and modules…',spellcheck:'false'}),body=h('div');
 const mods=f=>Object.entries(x.modules).filter(([,m])=>m.file===f.path).sort((a,b)=>a[1].line-b[1].line);
 function fill(){let s=q.value.trim().toLowerCase(),hit=f=>!s||f.path.toLowerCase().includes(s)||mods(f).some(([n])=>n.toLowerCase().includes(s));body.replaceChildren();
  let design=x.files.filter(f=>f.kind!=='part'&&hit(f)).sort((a,b)=>nat(a.path,b.path)),parts=x.files.filter(f=>f.kind==='part'&&hit(f)).sort((a,b)=>nat(a.path,b.path));
  const row=f=>h('div',{class:'src-f'+(f.path===S.file?' on':'')},h('button',{class:'src-fb',onclick:()=>{toggleList(false);open(f.path)}},h('span',null,fileName(f.path)),h('em',null,f.lines+' lines')),
   f.kind==='part'?null:h('div',{class:'src-mods'},mods(f).map(([n,m])=>h('button',{title:m.doc||n,onclick:()=>{toggleList(false);open(f.path,m.line,m.line)}},n))));
  body.append(h('h3',null,'Design · '+design.length),...design.map(row));
  let det=h('details',{open:!!s||undefined},h('summary',null,'Parts · '+parts.length),parts.map(row));body.append(det);
  let v=x.validation;if(v)body.append(h('p',{class:'dk-muted'},`Netlist cross-check: ${v.nets_matched}/${v.nets_total} nets matched`+(v.mismatches?.length?` · ${v.mismatches.length} mismatch${v.mismatches.length>1?'es':''}`:'')+(v.notes?' · '+(Array.isArray(v.notes)?v.notes.join(' '):v.notes):'')))}
 q.oninput=fill;SB.list.append(q,body);fill();setTimeout(()=>q.focus(),0)}
async function fetchFile(path){let rec=fileRec(path),p=rec?.path||path,c=S.files.get(p);if(c&&(!rec?.sha||c.sha===rec.sha))return c;
 let r=await fetch('/api/source/file?path='+encodeURIComponent(p)),j=await r.json().catch(()=>null);if(!r.ok||!j)throw Error(j?.error||'HTTP '+r.status);
 j.path=j.path||p;j.rows=null;S.files.set(p,j);return j}
function renderCode(f){
 if(f.rows)return f.rows;let st={doc:null},rows=String(f.text??'').replace(/\r\n?/g,'\n').split('\n');if(rows.length>1&&rows[rows.length-1]==='')rows.pop();
 let html=rows.map((t,k)=>{let [s,a]=hlLine(t,st);return `<div class="ln${a?' ann':''}" data-l="${k+1}"><i>${k+1}</i><code>${s||' '}</code></div>`}).join('');
 return f.rows={html,n:rows.length}}
let openTok=0;
async function open(file,line,end,opt={}){
 DOCK.tab('source',opt.focus!==false);let tok=++openTok;
 try{if(!S.idx)await load().catch(()=>null);let rec=fileRec(file),path=rec?.path||String(file);
  if(!opt.back&&S.file&&(S.file!==path||S.sel?.[0]!==line))S.hist.push({file:S.file,line:S.sel?.[0],end:S.sel?.[1]}),S.hist.length>40&&S.hist.shift();btnBack.disabled=!S.hist.length;
  if(S.file!==path||!SB.code.querySelector('.ln')){SB.code.replaceChildren(h('div',{class:'dk-empty'},'Loading '+fileName(path)+'…'));let f=await fetchFile(path);if(tok!==openTok)return;
   let r=renderCode(f);S.file=f.path;SB.code.innerHTML=`<div class="src-lines">${r.html}</div>`;nameEl.textContent=fileName(f.path);nameEl.title=f.path;markNotes()}
  S.marks=new Set(opt.marks||[]);setSel(line?+line:null,end?+end:line?+line:null,{scroll:true,auto:true,quiet:!!opt.result});
  if(opt.result)showRes(opt.result)}
 catch(e){if(tok===openTok)SB.code.replaceChildren(h('div',{class:'dk-empty dk-err'},'Cannot open '+file+': '+e.message))}}
function setSel(a,b,o={}){
 S.sel=a?[a,b||a]:null;let m=a?modAt(S.file,a):null;metaEl.textContent=(a?' · L'+a+(b>a?'–'+b:''):'')+(m?' · '+m.name:'');
 for(let el of SB.code.querySelectorAll('.ln.hl,.ln.mk'))el.classList.remove('hl','mk');
 for(let l of S.marks)SB.code.querySelector(`.ln[data-l="${l}"]`)?.classList.add('mk');
 if(a)for(let l=a;l<=(b||a);l++)SB.code.querySelector(`.ln[data-l="${l}"]`)?.classList.add('hl');
 if(a&&o.scroll){S.needScroll=a;requestAnimationFrame(scrollTo)}
 let hits=a&&!o.quiet?lineHits(S.file,a,b):null;if(hits&&(!o.auto||hits.refs.size||hits.nets.size))showRes({title:'L'+a+(b>a?'–'+b:''),hits,range:[a,b||a]});else if(!o.quiet)SB.res.hidden=true}
function scrollTo(){let a=S.needScroll,el=a&&SB.code.querySelector(`.ln[data-l="${a}"]`);if(!el||!SB.code.clientHeight)return;SB.code.scrollTop=Math.max(0,el.offsetTop-SB.code.clientHeight*.3);S.needScroll=0}
function showRes({title,hits,range,note}){
 let refs=[...(hits.refs||[])].sort(nat),nets=[...(hits.nets||[])].sort(nat),pads=[...(hits.pads||[])].sort(nat),r=range||S.sel||[1,1],cap=(list,n,k)=>list.length>n?[list.slice(0,n).map(x=>chip(k,x)),h('span',{class:'dk-muted'},' +'+(list.length-n)+' more')]:list.map(x=>chip(k,x));
 SB.res.replaceChildren(h('div',{class:'src-rh'},h('b',null,title),note?h('span',{class:'dk-muted'},note):null,h('span',{class:'dk-sp'}),
  refs.length||nets.length||pads.length?h('button',{onclick:()=>highlight({refs,nets,pads}),title:'Highlight on the board'},'Show'):null,
  NT()&&NT().available?.()!==false?h('button',{title:'New note about these lines',onclick:()=>NT().newNote({targets:[{kind:'source',file:S.file,line:r[0],end:r[1]}]})},'Note'):null,
  h('button',{class:'dk-ask ask-entry',title:'Add these lines to the Ask context',onclick:()=>askAbout({kind:'source',file:S.file,line:r[0],end:r[1]})},'Ask'),h('button',{class:'dk-xs',title:'Close',onclick:()=>{SB.res.hidden=true}},'×')),
  h('div',{class:'src-rc'},refs.length||nets.length||pads.length?[cap(refs,24,'ref'),cap(pads,12,'pad'),cap(nets,12,'net')]:h('span',{class:'dk-muted'},'No board objects map to this location.')));
 SB.res.hidden=false}
SB.code.addEventListener('click',e=>{
 let t=e.target,ln=t.closest('.ln');if(!ln||!S.file)return;let l=+ln.dataset.l;
 if(t.classList.contains('nmk')){let ids=(t.dataset.ids||'').split(' ').filter(Boolean),N=NT();if(N&&ids.length)ids.length===1?N.open(ids[0]):N.showFor({kind:'source',file:S.file,line:l,end:l});return}
 if(t.tagName==='I'){if(e.shiftKey&&S.sel)setSel(Math.min(S.sel[0],l),Math.max(S.sel[1],l));else setSel(l,l);let hits=lineHits(S.file,S.sel[0],S.sel[1]);if(hits.refs.size||hits.nets.size)highlight(hits);return}
 if(t.classList.contains('t-file'))return open(t.dataset.f,1,1);
 if(t.classList.contains('t-ty')){let m=S.idx?.modules?.[t.textContent];if(m)open(m.file,m.line,m.line);return}
 if(t.classList.contains('t-ann')||t.classList.contains('t-annj')){let a=ln.querySelector('.t-ann'),j=null;try{j=JSON.parse(a.dataset.ann)}catch(e){}
  if(j?.target){let full=S.look.inst.has('board.'+j.target)?'board.'+j.target:j.target,ref=S.look.inst.get(full),c=comp(ref),hits={refs:new Set(ref?[ref]:[]),nets:new Set(),pads:new Set()};
   for(let p of j.pads||[]){hits.pads.add(ref+'.'+p);let n=c?.pins?.[p]?.net;if(n)hits.nets.add(n)}
   if(ref)highlight({refs:[ref],pads:hits.pads});setSel(l,l,{quiet:true});showRes({title:a.textContent+' → '+j.target,hits,note:ref?'':'target not found'})}return}
 if(t.classList.contains('t-id')){let r=resolve(S.file,l,t.textContent);if(!S.sel||l<S.sel[0]||l>S.sel[1])setSel(l,l,{quiet:true});if(r.refs.size||r.nets.size)highlight(r);showRes({title:t.textContent+' · L'+l,hits:r});return}
});
SB.code.addEventListener('mouseover',e=>{let t=e.target;if(!t.classList.contains('t-id')||t.title||!S.look)return;let l=+t.closest('.ln').dataset.l,r=resolve(S.file,l,t.textContent),refs=[...r.refs],nets=[...r.nets];
 t.title=(refs.length?(refs.length>6?refs.length+' parts':refs.join(', ')):'')+(nets.length?(refs.length?'\n':'')+'net '+nets.slice(0,4).map(n=>netLabel(n)).join(', ')+(nets.length>4?' …':''):'')||'No board object';});
function showComponent(ref){let c=comp(ref);if(!c)return load().then(()=>comp(ref)&&showComponent(ref)).catch(()=>{});
 let marks=(c.statements||[]).filter(s=>s.file===c.file).map(s=>s.line);return open(c.file,c.line,c.line,{marks})}
function showNet(name){let n=net(name);if(!n)return load().then(()=>net(name)&&showNet(name)).catch(()=>{});
 let locs=[...(n.statements||[]),...(n.aliases||[])].filter(s=>s.file&&s.line);if(!locs.length)return;
 let by=new Map();for(let s of locs)by.set(s.file,(by.get(s.file)||0)+1);let f=[...by].sort((a,b)=>b[1]-a[1])[0][0],first=locs.filter(s=>s.file===f).sort((a,b)=>a.line-b.line)[0];
 return open(f,first.line,first.line,{marks:locs.filter(s=>s.file===f).map(s=>s.line)})}

// ------------------------------------------------------------------ Inspect tab
P.insp.innerHTML='';
const IB={head:h('form',{class:'insp-find'}),body:h('div',{class:'insp-body'})},findIn=h('input',{class:'dk-input',list:'dk-find-list',placeholder:'Find part, net or pad (C1, GND, 3V3, U1.2)…',spellcheck:'false',autocomplete:'off'}),findList=h('datalist',{id:'dk-find-list'}),btnIBack=h('button',{type:'button',class:'insp-back',title:'Previous item',disabled:true},'‹');
IB.head.append(btnIBack,findIn,findList,h('button',{type:'submit'},'Go'));P.insp.append(IB.head,IB.body);
btnIBack.onclick=()=>{let it=S.ihist.pop();if(it){S.item=null;inspect(it,{nohist:true,focus:true})}btnIBack.disabled=!S.ihist.length};
IB.head.onsubmit=e=>{e.preventDefault();let q=findIn.value.trim();if(!q)return;let x=IX(),Q=q.toUpperCase(),lq=q.toLowerCase();
 if(!x)return load().then(()=>IB.head.onsubmit(e)).catch(()=>{});
 let ref=Object.keys(x.components).find(r=>r.toUpperCase()===Q);if(ref)return act('ref',ref,{focus:true});
 let nm=Object.keys(x.nets).find(n=>n===q)||Object.keys(x.nets).find(n=>n.toLowerCase()===lq);if(nm)return act('net',nm,{focus:true});
 let m=/^([A-Za-z]+\d+)\.(\w+)$/.exec(q);if(m&&x.components[m[1].toUpperCase()])return act('pad',m[1].toUpperCase()+'.'+m[2],{focus:true});
 let hits=rankNets(x,q);if(hits.length){act('net',hits[0].name,{focus:true});if(hits.length>1)IB.body.prepend(h('div',{class:'dk-alt'},h('span',{class:'dk-muted'},'Also matching “'+q+'”: '),hits.slice(1,9).map(n=>chip('net',n.name,netLabel(n.name,true).replace(/\s+—.*$/,'')+' ('+n.name+')',{focus:true})),hits.length>9?h('span',{class:'dk-muted'},' +'+(hits.length-9)):null));return}
 let byInst=Object.values(x.components).find(c=>(c.instance||'').toLowerCase().endsWith(lq));if(byInst)return act('ref',byInst.ref,{focus:true});
 IB.body.prepend(h('p',{class:'dk-muted dk-err'},'Nothing named “'+q+'” in the source index.'))};
// Net search by meaning: an alias/rail name ('p5v', 'vsys') first, then a voltage stated as the rail's own ('5V ±5%', '5V rail'),
// then the voltage at the start of a range ('5V to 21V'), then any other title/alias match; power nets break ties.
function rankNets(x,q){let lq=q.toLowerCase().trim(),re=new RegExp('(^|[^\\w.])'+lq.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?![\\w.])','i'),out=[];
 for(let n of Object.values(x.nets)){let t=(n.title||'').toLowerCase(),al=(n.aliases||[]).map(a=>(a.path||'').toLowerCase()),s=0;
  if(al.some(a=>a===lq||a.endsWith('.'+lq)||a.split('.').includes(lq)))s=100;
  else if((n.voltage||'').toLowerCase().replace(/\s/g,'').startsWith(lq.replace(/\s/g,''))&&!/\bto\b/i.test(n.voltage||''))s=80;
  else{let m=re.exec(t);if(m){let after=t.slice(m.index+m[0].length);s=/^\s*(±|\+\/-|rail\b|supply\b|—|$)/.test(after)?70:/^\s*(to|-|–)\s*\d/.test(after)?35:50}
   else if(!/^[\d.]/.test(lq)&&t.includes(lq))s=30;else if(al.some(a=>a.endsWith(lq)))s=25;else if(re.test(n.llm?.label||''))s=20}
  if(s)out.push({name:n.name,s:s+(n.kind==="power"?5:0)})}
 return out.sort((a,b)=>b.s-a.s||nat(a.name,b.name))}
function fillFind(){let x=IX();if(!x)return;let o=[];for(let [r,c] of Object.entries(x.components).sort((a,b)=>nat(a[0],b[0])))o.push(h('option',{value:r,label:(c.type||'')+' · '+(c.instance||'')}));
 for(let [n,v] of Object.entries(x.nets).sort((a,b)=>nat(a[0],b[0])))o.push(h('option',{value:n,label:v.title&&v.title!==n?v.title:'net'}));findList.replaceChildren(...o)}
const rich=t=>String(t??'').split(/(`[^`\n]+`)/).map((p,i)=>i%2?h('code',null,p.slice(1,-1)):p);
const sec=(title,...k)=>h('div',{class:'dk-sec'},h('h3',null,title),...k);
const loc=(f,l,end,text,tip)=>h('button',{class:'dk-loc',title:tip||('Open '+f+':'+l+' in Source'),onclick:()=>open(f,l,end||l)},h('b',null,fileName(f)+':'+l+(end&&end>l?'–'+end:'')),text?h('span',null,String(text).trim()):null);
function more(list,n,fn,label){let out=list.slice(0,n).map(fn);if(list.length>n){let b=h('button',{type:'button',class:'dk-more',onclick:()=>b.replaceWith(...list.slice(n).map(fn))},(label||'Show all')+' · '+list.length);out.push(b)}return out}
function moreRows(list,n,fn,cols,label){let out=list.slice(0,n).map(fn);if(list.length>n){let tr=h('tr',{class:'dk-morer'},h('td',{colspan:cols},h('button',{type:'button',class:'dk-more',onclick:()=>tr.replaceWith(...list.slice(n).map(fn))},(label||'Show all')+' · '+list.length)));out.push(tr)}return out}
function actions(item,extra){let hl=item.kind==='component'?{refs:[item.ref]}:item.kind==='net'?{nets:[item.name]}:item.kind==='pad'?{pads:[item.ref+'.'+item.pad]}:{refs:item.refs||[],nets:item.nets||[]};
 return h('div',{class:'dk-actions'},h('button',{type:'button',onclick:()=>highlight(hl)},'Show on board'),extra,h('span',{class:'dk-sp'}),noteBtn(item),h('button',{type:'button',class:'dk-ask ask-entry',onclick:()=>askAbout(item)},'Ask about this'))}
const noteBtn=item=>NT()&&NT().available?.()!==false?h('button',{type:'button',class:'dk-note',title:'New note about this (you can edit targets, kind and status before saving)',onclick:()=>NT().newNote({targets:[JSON.parse(JSON.stringify(item))]})},'Add note'):null;
function workspaceSelection(item,type='yapnr-selection'){if(window.parent===window)return false;try{const origin=new URL(document.referrer).origin;window.parent.postMessage({type,selection:item,record:item?.kind==='component'?comp(item.ref):item?.kind==='net'?net(item.name):null,scope:{board_sha256:V()?.boardSha?.()||null,layout_sha256:(phase==='live'?lane():lane()?.frames?.[Number(phase)])?.layout_sha256||null,lane:V()?.lane?.(),phase:V()?.phase?.(),source_revision:'unknown'}},origin);return true}catch{return false}}
function askAbout(item){if(workspaceSelection(item,'yapnr-ask-selection'))return;let a=AG();if(!a){alert('The Ask panel (agent.js) is not loaded.');return}if(a.enabled?.()===false){DOCK.tab('ask');return}a.addContext(item);a.open?a.open():DOCK.tab('ask')}
function currentsTable(list){if(!list.length)return null;return h('table',{class:'dk-tab'},h('thead',null,h('tr',null,['Target','Pads','RMS','Peak','Scope',''].map(x=>h('th',null,x)))),
 h('tbody',null,list.map(c=>h('tr',null,h('td',{class:'mono'},c.target||''),h('td',{class:'mono'},(c.pads||[]).join(', ')),h('td',null,c.rms_current_a!=null?c.rms_current_a+' A':'—'),h('td',null,c.peak_current_a!=null?c.peak_current_a+' A':'—'),h('td',null,c.scope||''),h('td',null,c.file?h('button',{class:'dk-lnk',title:(c.file+':'+c.line),onclick:()=>open(c.file,c.line,c.line)},'L'+c.line):'')))))}
function boardInfo(o){if(!o||typeof o!=='object')return null;let b=[],f=v=>typeof v==='number'?(Math.abs(v)>=100?v.toFixed(0):+v.toFixed(2)):v,xy=o.xy||o.position;
 if(Array.isArray(xy))b.push(xy.map(f).join(', ')+' mm');for(let k of ['side','layer','rot','angle','footprint','value','tracks','vias','length_mm'])if(o[k]!=null&&typeof o[k]!=='object')b.push((k==='rot'||k==='angle'?f(o[k])+'°':k==='length_mm'?f(o[k])+' mm':k==='tracks'||k==='vias'?o[k]+' '+k:String(o[k])));
 if(Array.isArray(o.pads))b.push(o.pads.length+' pads');return b.length?h('p',{class:'dk-muted'},'On board: '+b.join(' · ')):null}
// contracts are resolved server-side to a ref (target 'converter.fb_top' and 'board.fb_top' alike): match on that, never on the target text
function componentCurrents(c){if(c.currents)return c.currents;let out=[],seen=new Set();for(let p of Object.values(c.pins||{}))for(let x of net(p.net)?.currents||[]){let k=x.file+':'+x.line;if(x.ref===c.ref&&!seen.has(k)){seen.add(k);out.push(x)}}return out}
function badges(...b){return h('div',{class:'dk-badges'},b.filter(Boolean).map(([t,cls,tip])=>h('span',{class:'dk-badge '+(cls||''),title:tip||null},t)))}
function aiBox(llm){if(!llm||!(llm.summary||llm.label))return null;return h('div',{class:'dk-ai',title:llm.generated_at?'Generated '+llm.generated_at+' · not verified':'Not verified'},h('div',{class:'dk-ai-h'},'AI summary · '+(llm.model||'model')),llm.label?h('b',null,llm.label):null,llm.summary?h('p',null,llm.summary):null)}
function partLinks(c){
 let links=c.part_links||{},out=[];
 for(let [key,label] of [['octopart_url','Octopart'],['easyeda_url','EasyEDA'],['datasheet_url','Datasheet']]){
  let url;try{url=new URL(links[key]||c[key]);if(!['https:','http:'].includes(url.protocol)||url.username||url.password)continue}catch(e){continue}
  out.push(h('a',{href:url.href,target:'_blank',rel:'noopener noreferrer',class:'dk-lnk'},label));
 }
 return sec('Part references',out.length?h('div',{class:'dk-row'},out):h('p',{class:'dk-muted'},'Source-page and datasheet URLs have not been recorded for this part.'));
}
function rInspect(item){
 let x=IX(),body=[];const hd=(t,sub,tip)=>h('div',{class:'insp-h'},h('div',{class:'insp-t',title:tip||null},t),sub?h('div',{class:'insp-sub'},sub):null);
 if(item.kind==='component'){let c=comp(item.ref);
  if(!c){body.push(hd(item.ref,x?'No source record in the index for this part.':S.err?'Source index unavailable: '+S.err:'Loading source index…'),boardInfo(V()?.componentInfo?.(item.ref)),actions(item));return body}
  let typeDoc=x.modules?.[c.type]?.doc,doc=c.doc||typeDoc;
  body.push(hd([item.ref,h('span',{class:'insp-ty'},c.type||c.part||'')],c.instance||c.address),badges(c.part&&c.part!==c.type?[c.part,'mono','Package component (part file)']:null));
  if(doc)body.push(h('p',{class:'insp-doc'},rich(doc)));body.push(boardInfo(V()?.componentInfo?.(item.ref)));
  body.push(actions(item,c.file?h('button',{type:'button',onclick:()=>showComponent(item.ref)},'Source'):null));
  body.push(partLinks(c));
  if(c.file)body.push(sec('Defined at',loc(c.file,c.line,c.line,c.text)));
  if(c.chain?.length>1)body.push(sec('Instantiated via',h('div',{class:'dk-chain'},c.chain.map((e,i)=>h('button',{class:'dk-crumb',title:(e.text||'').trim()+'\n'+e.file+':'+e.line,onclick:()=>open(e.file,e.line,e.line)},i?h('i',null,'›'):null,e.address.split('.').pop()||e.address,(t=>t?h('em',null,t):null)(/new\s+(\w+)/.exec(e.text||'')?.[1]||e.module))))));
  if(c.statements?.length)body.push(sec('Statements · '+c.statements.length,more(c.statements,10,s=>loc(s.file,s.line,s.line,s.text))));
  let pins=Object.entries(c.pins||{}).sort((a,b)=>nat(a[0],b[0]));
  if(pins.length)body.push(sec('Pins · '+pins.length,h('table',{class:'dk-tab dk-pins'},h('thead',null,h('tr',null,h('th',null,'Pad'),h('th',null,'Pin'),h('th',null,'Net'))),h('tbody',null,moreRows(pins,40,([pad,p])=>h('tr',null,h('td',null,chip('pad',item.ref+'.'+pad,pad)),h('td',{class:'mono'},p.pin||''),h('td',null,p.net?chip('net',p.net):h('span',{class:'dk-muted'},'unconnected'))),3,'Show all pins')))));
  let cur=componentCurrents(c);if(cur.length)body.push(sec('Current contracts',currentsTable(cur)));
  return body}
 if(item.kind==='net'){let n=net(item.name);
  if(!n){body.push(hd(item.name,x?'No source record in the index for this net.':S.err?'Source index unavailable: '+S.err:'Loading source index…'),boardInfo(V()?.netInfo?.(item.name)),actions(item));return body}
  let kc={power:'k-pow',ground:'k-gnd',signal:'k-sig',unconnected:'k-nc'}[n.kind]||'';
  body.push(hd(n.title||n.name,null,n.title),badges(['net '+n.name,'mono'],n.kind?[n.kind,kc]:null,n.voltage?[n.voltage,'']:null,n.net_class?['class '+n.net_class,'','Net class from rules.json']:null,n.low_info?['auto-named','k-low','The netlist name was generated by atopile; the title is derived from the source']:null));
  if(n.summary)body.push(h('p',{class:'insp-doc'},rich(n.summary)));body.push(aiBox(n.llm),boardInfo(V()?.netInfo?.(item.name)));
  body.push(actions(item,(n.statements?.length||n.aliases?.length)?h('button',{type:'button',onclick:()=>showNet(item.name)},'Source'):null));
  if(n.statements?.length)body.push(sec('Connections · '+n.statements.length,more(n.statements,8,s=>loc(s.file,s.line,s.line,s.text))));
  if(n.aliases?.length)body.push(sec('Source names · '+n.aliases.length,more(n.aliases,6,a=>h('div',{class:'dk-alias'},h('span',{class:'mono'},a.path),a.file?h('button',{class:'dk-lnk',title:a.file+':'+a.line,onclick:()=>open(a.file,a.line,a.line)},fileName(a.file)+':'+a.line):null))));
  if(n.currents?.length)body.push(sec('Current contracts',currentsTable(n.currents)));
  if(n.comments?.length)body.push(sec('Comments',n.comments.map(c=>h('p',{class:'dk-com'},typeof c==='string'?c:(c.text||JSON.stringify(c))))));
  let pins=(n.pins||[]).slice().sort((a,b)=>nat(a.ref,b.ref)||nat(a.pad,b.pad));
  if(pins.length)body.push(sec('Pins · '+pins.length+' on '+new Set(pins.map(p=>p.ref)).size+' parts',h('table',{class:'dk-tab dk-pins'},h('thead',null,h('tr',null,h('th',null,'Pad'),h('th',null,'Pin'),h('th',null,'Instance'))),
   h('tbody',null,moreRows(pins,30,p=>h('tr',null,h('td',null,chip('pad',p.ref+'.'+p.pad)),h('td',{class:'mono'},p.pin||''),h('td',{class:'mono dk-dim',title:p.address||''},(p.address||comp(p.ref)?.instance||'').replace(/^board\./,'').replace(/\._p$/,''))),3,'Show all pins')))));
  return body}
 if(item.kind==='pad'){let c=comp(item.ref),p=c?.pins?.[item.pad],n=p?.net&&net(p.net);
  body.push(hd([item.ref+'.'+item.pad,p?.pin?h('span',{class:'insp-ty'},p.pin):null],c?(c.type||'')+' · '+(c.instance||''):(x?'No source record for '+item.ref:'Loading source index…')));
  body.push(h('div',{class:'dk-row'},chip('ref',item.ref,item.ref+(c?.type?' · '+c.type:'')),p?.net?chip('net',p.net):null));
  if(n){if(n.summary)body.push(h('p',{class:'insp-doc'},h('b',null,netLabel(p.net,true)+'. '),rich(n.summary)));body.push(aiBox(n.llm))}
  body.push(actions(item,c?h('button',{type:'button',onclick:()=>showComponent(item.ref)},'Source'):null));
  let cur=c?componentCurrents(c).filter(k=>k.ref===item.ref&&(k.pads||[]).map(String).includes(String(item.pad))):[];if(cur.length)body.push(sec('Current contracts',currentsTable(cur)));
  if(c?.statements?.length){let pin=p?.pin,rel=c.statements.filter(s=>pin&&new RegExp('\\.'+pin.replace(/\W/g,'\\$&')+'\\b').test(s.text||''));if(rel.length)body.push(sec('Statements for '+pin,rel.map(s=>loc(s.file,s.line,s.line,s.text))))}
  return body}
 if(item.kind==='region'){let b=item.bbox||[0,0,0,0],refs=(item.refs||[]).slice().sort(nat),nets=(item.nets||[]).slice().sort(nat);
  body.push(hd('Board region',`${Math.abs(b[2]-b[0]).toFixed(1)} × ${Math.abs(b[3]-b[1]).toFixed(1)} mm at ${(+b[0]).toFixed(1)}, ${(+b[1]).toFixed(1)}`+(item.lane?' · '+item.lane:'')),actions(item));
  body.push(sec('Parts · '+refs.length,h('div',{class:'dk-row'},more(refs,60,r=>chip('ref',r)))),sec('Nets · '+nets.length,h('div',{class:'dk-row'},more(nets,40,n=>chip('net',n)))));return body}
 if(item.kind==='group'){let refs=(item.refs||[]).slice().sort(nat);body.push(hd(item.label||item.id||'Group',refs.length+' parts'),actions(item),sec('Parts',h('div',{class:'dk-row'},more(refs,80,r=>chip('ref',r)))));return body}
 if(item.kind==='source'){body.push(hd(fileName(item.file)+':'+item.line+(item.end>item.line?'–'+item.end:''),modAt(item.file,item.line)?.name||''),h('div',{class:'dk-actions'},h('button',{type:'button',onclick:()=>open(item.file,item.line,item.end)},'Open in Source'),h('span',{class:'dk-sp'}),noteBtn(item),h('button',{type:'button',class:'dk-ask ask-entry',onclick:()=>askAbout(item)},'Ask about this')));
  let hits=lineHits(item.file,item.line,item.end||item.line);if(hits.refs.size||hits.nets.size)body.push(sec('Maps to',h('div',{class:'dk-row'},[...hits.refs].sort(nat).slice(0,60).map(r=>chip('ref',r)),[...hits.nets].sort(nat).slice(0,30).map(n=>chip('net',n)))));return body}
 if(item.kind==='lane'){body.push(hd('Lane '+item.lane,'Experiment lane'),h('div',{class:'dk-actions'},h('button',{type:'button',onclick:()=>V()?.selectLane?.(item.lane)},'Show lane'),h('span',{class:'dk-sp'}),noteBtn(item),h('button',{type:'button',class:'dk-ask ask-entry',onclick:()=>askAbout(item)},'Ask about this')));return body}
 if(item.kind==='event'||item.kind==='probe'){let pretty=t=>{try{return JSON.stringify(JSON.parse(t),null,1)}catch(e){return t||''}};
  body.push(hd(item.kind==='event'?'Event · '+(item.event_kind||item.id):item.label,[item.lane?'lane '+item.lane:'',item.kind==='event'?' · id '+item.id:''].join('')),h('div',{class:'dk-actions'},item.lane?h('button',{type:'button',onclick:()=>V()?.selectLane?.(item.lane)},'Show lane'):null,h('span',{class:'dk-sp'}),noteBtn(item),h('button',{type:'button',class:'dk-ask ask-entry',onclick:()=>askAbout(item)},'Ask about this')),
   h('pre',{class:'dk-pre'},pretty(item.summary)));return body}
 body.push(hd('Unknown selection',JSON.stringify(item)));return body}
function emptyInspect(){IB.body.replaceChildren(h('div',{class:'dk-empty'},h('b',null,'Nothing selected'),h('p',null,'Select a part on the board or schematic, click a chip in an answer or a name in Source, or search above.'),
 S.err?h('p',{class:'dk-err'},'Source index unavailable: '+S.err):!S.idx?h('p',null,'Loading source index…'):h('p',{class:'dk-muted'},`${Object.keys(S.idx.components).length} parts · ${Object.keys(S.idx.nets).length} nets · ${S.idx.files.length} files`+(S.idx.sha?' · source '+String(S.idx.sha).slice(0,10):''))))}
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
// focus: true = show the Inspect tab (opening the dock); undefined = show it only if the dock is open and not on Ask; false = badge only.
function inspect(item,opt={}){
 if(!item){S.item=null;workspaceSelection(null);emptyInspect();return}
 let f=opt.focus,show=()=>{if(f===true||(f===undefined&&DOCK.isOpen()&&DOCK.current()!=='ask'))DOCK.tab('inspect');else if(DOCK.current()!=='inspect'||!DOCK.isOpen())DOCK.badge('inspect',true)};
 if(S.item&&same(S.item,item)&&!opt.force){show();return}
 if(S.item&&!opt.nohist){S.ihist.push(S.item);if(S.ihist.length>30)S.ihist.shift()}btnIBack.disabled=!S.ihist.length;
 S.item=item;workspaceSelection(item);IB.body.replaceChildren(...withNotes(item));IB.body.scrollTop=0;show();
 if(!S.idx&&!S.err)load().then(()=>{if(S.item===item)IB.body.replaceChildren(...withNotes(item))}).catch(()=>{})}
// notes.js: a 'Notes · N' section right after the action row, re-rendered (scroll kept) only when this item's notes change
const noteSig=it=>(NT()?.forItem?.(it)||[]).map(n=>n.id+':'+n.status+':'+n.updated+':'+n.title).join('|');
function withNotes(item){let body=rInspect(item).filter(Boolean),ns=NT()?.inspectSection?.(item);S.noteSig=noteSig(item);if(ns){let i=body.findIndex(e=>e.classList?.contains('dk-actions'));body.splice(i<0?body.length:i+1,0,ns)}return body}
// Source gutter: a dot on lines targeted by notes (source ranges; a part's defining line for part/pad targets)
function markNotes(){if(!S.file)return;for(let el of SB.code.querySelectorAll('.ln.nm')){el.classList.remove('nm');el.querySelector('.nmk')?.remove()}let N=NT();if(!N?.sourceLines)return;
 for(let [l,ns] of N.sourceLines(S.file)){let ln=SB.code.querySelector(`.ln[data-l="${l}"]`);if(!ln)continue;let sts=ns.map(n=>n.status||'open'),top=['proposed','open','accepted','applied','resolved','rejected'].find(x=>sts.includes(x))||'open';
  ln.classList.add('nm');ln.querySelector('i')?.append(h('span',{class:'nmk s-'+top,'data-ids':ns.map(n=>n.id).join(' '),title:ns.map(n=>n.id+' · '+(n.status||'open')+' · '+n.title).join('\n')+'\nClick: show in Notes'}))}}
function onIndex(){fillFind();if(S.item)IB.body.replaceChildren(...withNotes(S.item));else emptyInspect();
 if(!SB.list.hidden)renderList();if(!S.file){SB.code.replaceChildren(h('div',{class:'dk-empty'},S.err?h('span',{class:'dk-err'},'Source index unavailable: '+S.err):[h('b',null,'Atopile source'),h('p',null,'Open a file from Files, or use Source on a part or net in Inspect. Click a name in the code to find it on the board; click line numbers to select lines (Shift extends).'),
  S.idx?.entry?h('button',{onclick:()=>{let e=String(S.idx.entry).split(':');let m=S.idx.modules?.[e[1]];open(e[0],m?.line||1,m?.line||1)}},'Open entry '+S.idx.entry):null]))}}

// ------------------------------------------------------------------ wiring
D.addEventListener('yapnr:select',e=>{if(e.detail)inspect(e.detail,{focus:false})});
D.addEventListener('yapnr:lane',()=>{if(Date.now()-S.at>30000)load(true).catch(()=>{})});
D.addEventListener('yapnr:dock',e=>{if(e.detail?.open&&e.detail.tab==='source'&&S.needScroll)requestAnimationFrame(scrollTo)});
D.addEventListener('yapnr:notes',()=>{if(S.item&&noteSig(S.item)!==S.noteSig){let top=IB.body.scrollTop;IB.body.replaceChildren(...withNotes(S.item));IB.body.scrollTop=top}markNotes()});
emptyInspect();setTimeout(()=>load().catch(()=>{}),0);
window.YapnrSource={open,showComponent,showNet,inspect,act,chip,resolve,netLabel,netTip,reload:()=>load(true),ready:()=>load(),index:()=>S.idx,item:()=>S.item,
 escape:esc,srcParse,chipLabel,chipTip};
})();
