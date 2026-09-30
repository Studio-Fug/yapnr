'use strict';
// Ask tab: design assistant backed by POST /api/agent/chat (server-sent events: session, delta, tool,
// note, done, error). It reads the design, may search/fetch the public web (per-conversation Web toggle,
// body.web) and may record notes through the notes MCP server; it never edits design files.
// Context chips come from YapnrAgent.addContext(item). Answer markup [[ref:C17]] [[net:hv]] [[pad:U5.13]]
// [[src:file.ato:12-18]] [[note:N-0003]] becomes buttons, [text](https://…) links open in a new tab.
// History: GET /api/agent/conversations[/<session>]. Notes created in a turn (SSE 'note', done.notes_created,
// or new agent notes carrying this session in provenance, seen through window.YapnrNotes) appear inline; Undo only on a
// live turn while the note is untouched. Tool lines carry the full WebFetch URL / WebSearch query (tool.full) in the link and
// tooltip, with a length marker when it is long. Save as note opens the Notes editor prefilled (you write the title).
(function(){
const D=document,V=()=>window.YapnrView||null,SS=()=>window.YapnrSource||null,NS=()=>window.YapnrNotes||null;
const store={get(k,d){try{let v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const unesc=s=>String(s).replace(/&(amp|lt|gt|quot|#39);/g,(m,k)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"})[k]);
function h(t,a,...k){let e=D.createElement(t);if(a)for(let [x,v] of Object.entries(a)){if(v==null||v===false)continue;if(x==='class')e.className=v;else if(x.startsWith('on'))e[x]=v;else e.setAttribute(x,v===true?'':v)}for(let c of k.flat(9))if(c!=null&&c!==false)e.append(c.nodeType?c:String(c));return e}
const G={session:null,busy:false,ctl:null,turn:null,ctx:[],turns:[],status:null,statusAt:0,model:store.get('yapnr-agent-model',null),ui:null,web:false,claimed:new Set()};
// Web toggle per conversation (default: the server's --agent-web): remembered per session id in this browser.
const webPref={get(s){let m=store.get('yapnr-agent-web',{});return s&&typeof m[s]==='boolean'?m[s]:null},set(s,v){if(!s)return;let m=store.get('yapnr-agent-web',{});delete m[s];m[s]=!!v;let k=Object.keys(m);for(let i=0;i<k.length-300;i++)delete m[k[i]];store.set('yapnr-agent-web',m)}};
const tms=v=>{if(v==null||v==='')return NaN;if(typeof v==='number')return v<1e12?v*1000:v;let n=+v;return isFinite(n)&&String(v).trim()!==''&&!/[-:T]/.test(v)?(n<1e12?n*1000:n):Date.parse(v)};
function ago(v){let t=tms(v);if(!isFinite(t))return '';let s=(Date.now()-t)/1000;if(s<45)return 'just now';if(s<3600)return Math.round(s/60)+' min ago';if(s<86400)return Math.round(s/3600)+' h ago';if(s<6*86400)return Math.round(s/86400)+' d ago';return new Date(t).toLocaleDateString(undefined,{month:'short',day:'numeric'})}

// ------------------------------------------------------------------ markdown-lite (escape first, then a few block/inline forms)
const CHIP=/\[\[(ref|net|pad|src|note):([^\]\n]+?)\]\]/g;
function chipHtml(k,v){let raw=unesc(v).trim(),S=SS(),label=S?.chipLabel?S.chipLabel(k,raw):raw,tip=S?.chipTip?S.chipTip(k,raw):raw;
 if(k==='note'){let n=NS()?.get?.(raw);label=raw;tip=n?raw+' · '+n.status+' '+n.kind+'\n'+n.title:'Open note '+raw}
 return `<button class="dk-chip k-${k}" data-k="${k}" data-v="${esc(raw)}" title="${esc(tip).replace(/\n/g,'&#10;')}">${esc(label)}</button>`}
// `name` in an answer that is exactly a known part, pad or (non-trivial) net name becomes a chip too.
function codeName(v){let x=SS()?.index?.();if(!x)return null;if(x.components?.[v])return 'ref';let i=v.indexOf('.');if(i>0&&x.components?.[v.slice(0,i)]?.pins?.[v.slice(i+1)])return 'pad';return x.nets?.[v]&&!/^(\d+|[A-Za-z])$/.test(v)?'net':null}
function shortUrl(u){try{let x=new URL(u),p=(x.pathname+x.search).replace(/\/$/,'');return x.host.replace(/^www\./,'')+(p.length>34?p.slice(0,16)+'…'+p.slice(-16):p)}catch(e){return u.length>50?u.slice(0,48)+'…':u}}
// u and t are already escaped; only http(s) URLs become links (new tab, no referrer)
const linkHtml=(u,t)=>`<a class="ask-link" href="${u}" target="_blank" rel="noopener noreferrer" title="${u}">${t||esc(shortUrl(unesc(u)))}</a>`;
const MDLINK=/\[([^\]\n]{1,300})\]\((https?:\/\/[^\s()<>]+(?:\([^\s()<>]*\)[^\s()<>]*)*)\)/g,BARE=/(^|[\s(>])(https?:\/\/[^\s<]+)/g;
function inline(s){ // s is already escaped
 return s.split(/(`[^`\n]+`)/).map((p,i)=>{
  if(i%2){let c=p.slice(1,-1),m=/^\[\[(ref|net|pad|src|note):([^\]\n]+?)\]\]$/.exec(c),k=m?null:codeName(unesc(c));return m?chipHtml(m[1],m[2]):k?chipHtml(k,c):`<code>${c}</code>`}
  let keep=[];const K=x=>'\u0000'+(keep.push(x)-1)+'\u0001';
  p=p.replace(CHIP,(m,k,v)=>K(chipHtml(k,v))).replace(MDLINK,(m,t,u)=>K(linkHtml(u,t)))
   .replace(BARE,(m,a,u)=>{let cut=/(?:&quot;|&#39;|&lt;|&gt;)/.exec(u),tail='';if(cut){tail=u.slice(cut.index);u=u.slice(0,cut.index)}let e=/[.,;:!?)\]*_]+$/.exec(u);if(e){tail=u.slice(e.index)+tail;u=u.slice(0,e.index)}return u.length>10?a+K(linkHtml(u))+tail:m});
  return p.replace(/\*\*([^*\n]+?)\*\*/g,'<b>$1</b>').replace(/(^|[^\w*])\*([^*\s][^*\n]*?)\*(?![\w*])/g,'$1<i>$2</i>').replace(/\u0000(\d+)\u0001/g,(m,n)=>keep[+n])}).join('')}
function md(src){
 const L=esc(src).split('\n'),out=[];let i=0;
 const isList=l=>/^\s*(?:[-*+•]|\d+[.)])\s+/.test(l),isTable=k=>/^\s*\|.*\|\s*$/.test(L[k]||'')&&/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(L[k+1]||'');
 const cells=l=>l.trim().replace(/^\||\|$/g,'').split('|').map(c=>inline(c.trim()));
 while(i<L.length){let l=L[i],m;
  if(/^\s*```/.test(l)){let buf=[];i++;while(i<L.length&&!/^\s*```/.test(L[i]))buf.push(L[i++]);i++;out.push(`<pre><code>${buf.join('\n')}</code></pre>`);continue}
  if(!l.trim()){i++;continue}
  if((m=/^\s*(#{1,6})\s+(.*)$/.exec(l))){out.push(`<h4>${inline(m[2])}</h4>`);i++;continue}
  if(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(l)){out.push('<hr>');i++;continue}
  if(isTable(i)){let head=cells(L[i]),rows=[];i+=2;while(i<L.length&&/^\s*\|.*\|?\s*$/.test(L[i])&&L[i].trim())rows.push(cells(L[i++]));
   out.push(`<div class="md-tw"><table><thead><tr>${head.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);continue}
  if(isList(l)){let ord=/^\s*\d/.test(l),items=[];
   while(i<L.length&&(isList(L[i])||(/^\s{2,}\S/.test(L[i])&&items.length))){let t=L[i];if(isList(t)){let ind=/^\s*/.exec(t)[0].length;items.push({ind,t:t.replace(/^\s*(?:[-*+•]|\d+[.)])\s+/,'')})}else items[items.length-1].t+='\n'+t.trim();i++}
   let base=Math.min(...items.map(x=>x.ind)),st=ord?parseInt(l.trim()):1;out.push(`<${ord?'ol':'ul'}${st>1?` start="${st}"`:''}>${items.map(x=>`<li${x.ind>base?' class="sub"':''}>${inline(x.t).replace(/\n/g,'<br>')}</li>`).join('')}</${ord?'ol':'ul'}>`);continue}
  if(/^&gt;\s?/.test(l)){let buf=[];while(i<L.length&&/^&gt;\s?/.test(L[i]))buf.push(L[i++].replace(/^&gt;\s?/,''));out.push(`<blockquote>${inline(buf.join('\n')).replace(/\n/g,'<br>')}</blockquote>`);continue}
  let buf=[];while(i<L.length&&L[i].trim()&&!/^\s*```/.test(L[i])&&!/^\s*#{1,6}\s/.test(L[i])&&!isList(L[i])&&!isTable(i))buf.push(L[i++]);
  out.push(`<p>${inline(buf.join('\n')).replace(/\n/g,'<br>')}</p>`)}
 return out.join('')}
// markdown links [title](https://…) in an answer: the web sources a saved note keeps
function mdSources(text){let out=[],seen=new Set();String(text||'').replace(/\[([^\]\n]{1,300})\]\((https?:\/\/[^\s()<>]+(?:\([^\s()<>]*\)[^\s()<>]*)*)\)/g,(m,t,u)=>{if(!seen.has(u)&&out.length<20){seen.add(u);out.push({url:u,title:t.slice(0,200)})}});return out}

// ------------------------------------------------------------------ context chips
const ctxKey=x=>x.kind==='component'?'c:'+x.ref:x.kind==='net'?'n:'+x.name:x.kind==='pad'?'p:'+x.ref+'.'+x.pad:x.kind==='region'?'r:'+(x.lane||'')+':'+(x.bbox||[]).map(v=>(+v).toFixed(2)).join(','):x.kind==='source'?'s:'+x.file+':'+x.line+'-'+(x.end||x.line):x.kind==='group'?'g:'+(x.id||x.label):x.kind==='lane'?'l:'+x.lane:x.kind==='event'?'e:'+x.id:JSON.stringify(x);
function ctxLabel(x){const S=SS(),ix=S?.index?.();
 if(x.kind==='component'){let c=ix?.components?.[x.ref];return x.ref+(c?.type?' · '+c.type:'')}
 if(x.kind==='net')return S?.netLabel?S.netLabel(x.name):x.name;
 if(x.kind==='pad'){let p=ix?.components?.[x.ref]?.pins?.[x.pad];return x.ref+'.'+x.pad+(p?.pin&&p.pin!==x.pad?' · '+p.pin:'')}
 if(x.kind==='region'){let b=x.bbox||[0,0,0,0];return `Region ${Math.abs(b[2]-b[0]).toFixed(1)}×${Math.abs(b[3]-b[1]).toFixed(1)} mm · ${(x.refs||[]).length} parts · ${(x.nets||[]).length} nets`}
 if(x.kind==='source')return String(x.file).split('/').pop()+':'+x.line+((x.end||x.line)>x.line?'–'+x.end:'');
 if(x.kind==='group')return (x.label||x.id||'Group')+' · '+(x.refs||[]).length+' parts';
 if(x.kind==='lane')return 'Lane '+x.lane;if(x.kind==='event')return 'Event '+(x.event_kind||'')+(x.lane?' · '+x.lane.split('/').pop():'');if(x.kind==='probe')return x.label||'Probe';return x.kind}
const ctxKind=x=>({component:'ref',net:'net',pad:'pad',source:'src',region:'region',group:'group',lane:'lane',event:'event',probe:'probe'})[x.kind]||'ref';
// one chip per context item / note target; onRemove adds the × button (Ask context, note editor)
function itemChip(x,onRemove){let k=ctxKind(x);return h('span',{class:'dk-chip k-'+k+' ctx',title:'Click to show · '+(x.kind==='region'?'bbox '+(x.bbox||[]).map(v=>(+v).toFixed(1)).join(', ')+' mm':ctxLabel(x))},
  h('button',{class:'ctx-l',type:'button',onclick:e=>{e.stopPropagation();showItem(x)}},ctxLabel(x)),onRemove?h('button',{class:'ctx-x',type:'button',title:'Remove','aria-label':'Remove',onclick:e=>{e.stopPropagation();onRemove()}},'×'):null)}
const ctxChip=(x,i,removable)=>itemChip(x,removable?()=>removeContext(i):null);
function showItem(x){const S=SS(),v=V();
 if(x.kind==='source')return S?.open?.(x.file,x.line,x.end||x.line);
 if(x.kind==='lane'||x.kind==='event'||x.kind==='probe'){if(x.lane)try{v?.selectLane?.(x.lane)}catch(e){}return S?.inspect?.(x,{focus:false})}
 let hl=x.kind==='component'?{refs:[x.ref]}:x.kind==='net'?{nets:[x.name]}:x.kind==='pad'?{pads:[x.ref+'.'+x.pad]}:{refs:x.refs||[],nets:x.nets||[],bbox:x.kind==='region'?x.bbox:undefined};
 try{v?.highlight?.({refs:[],nets:[],pads:[],...hl})}catch(e){}S?.inspect?.(x,{focus:false})}
function addContext(item){if(!item||!item.kind)return;let k=ctxKey(item);if(!G.ctx.some(x=>ctxKey(x)===k))G.ctx.push(JSON.parse(JSON.stringify(item)));renderCtx();}
function removeContext(i){if(i>=0&&i<G.ctx.length)G.ctx.splice(i,1);renderCtx()}
function renderCtx(){window.YapnrDock?.count?.(G.ctx.length);let u=G.ui;if(!u)return;u.ctx.replaceChildren(...G.ctx.map((x,i)=>ctxChip(x,i,true)));u.ctxRow.hidden=!G.ctx.length;
 if(G.ctx.length>1)u.ctx.append(h('button',{class:'dk-more ctx-clear',type:'button',onclick:()=>{G.ctx=[];renderCtx()}},'Clear'))}

// ------------------------------------------------------------------ status
async function status(force){
 if(!force&&G.status&&Date.now()-G.statusAt<20000)return G.status;
 try{let r=await fetch('/api/agent/status'),j=await r.json().catch(()=>null);if(!r.ok||!j)throw Error(j?.error||'HTTP '+r.status);G.status=j}
 catch(e){G.status={available:false,reason:'Assistant endpoint unreachable ('+e.message+').',models:[]}}
 G.statusAt=Date.now();if(!G.session&&!G.turns.length)G.web=!!G.status.web;renderStatus();return G.status}
// status.enabled false: this server runs no assistant (--agent off). The page then hides every Ask entry
// point (class ask-entry, see dock.css) and Shift+click explains instead of collecting context.
const absent=()=>G.status?.enabled===false;
function renderStatus(){let u=G.ui,s=G.status;if(!u||!s)return;D.documentElement.classList.toggle('ask-off',absent());
 let models=s.models?.length?s.models:['opus','sonnet'];if(!models.includes(G.model))G.model=models.includes(s.default)?s.default:models[0];
 u.model.replaceChildren(...models.map(m=>h('option',{value:m,selected:m===G.model||undefined},m)));
 let off=!s.available;u.ta.disabled=off;u.note.hidden=!off&&!s.busy;
 u.note.textContent=off?(s.reason||s.error||'The assistant is unavailable on this server: the Claude CLI backend is missing or disabled.')+' Inspect, Source and Notes still work.':s.busy?`Assistant busy (${s.max_concurrent||1} concurrent answer${(s.max_concurrent||1)>1?'s':''} max); your question may wait or be refused.`:'';
 u.note.className='ask-note'+(off?' off':'');u.ex.hidden=u.hint.hidden=off;ui()}
// status.web: true = the server lets turns use WebSearch/WebFetch (--agent-web on); false = off; absent = old server (toggle hidden)
const webAvail=()=>G.status?.web;
function renderWeb(){let u=G.ui;if(!u)return;let a=webAvail(),on=a!==false&&G.web;u.web.hidden=a==null;u.web.disabled=a===false||G.busy;u.web.classList.toggle('on',!!on);u.web.setAttribute('aria-pressed',on?'true':'false');
 u.web.title=a===false?'Web access is disabled on this server (--agent-web off).':on?'Web on for this conversation: the assistant may search the web and fetch public pages (never local, private or tailnet addresses). Every query and URL is shown. Click to turn off.':'Web off for this conversation: design files and notes only. Click to allow web search and fetch.'}

// ------------------------------------------------------------------ conversation
function ui(){let u=G.ui;if(!u)return;let off=G.status&&!G.status.available;u.send.hidden=G.busy;u.cancel.hidden=!G.busy;u.send.disabled=off||!u.ta.value.trim();u.model.disabled=G.busy||off;u.hist.disabled=G.busy||absent();u.newc.disabled=absent();
 u.newc.title=(G.session?'Session '+G.session+' · '+G.turns.length+' question'+(G.turns.length!==1?'s':'')+'\n':'')+'Start a new conversation (this one stays in History)';renderWeb()}
const TOOLVERB={Read:'Reading',Grep:'Searching',Glob:'Listing',LS:'Listing',WebFetch:'Fetching',WebSearch:'Searching the web for',Bash:'Running',Task:'Delegating',TodoWrite:'Planning'};
const NOTEVERB={add_note:'Recording a note:',list_notes:'Looking up notes:',get_note:'Reading note',comment_note:'Commenting on note',update_note:'Updating note'};
const noteTool=n=>(/^mcp__\w+?_notes__(\w+)$/.exec(n||'')||[])[1]||null;
function toolText(t){let n=t.name,sr=G.status?.src_root,d=String(t.detail??'').trim(),m=noteTool(n);if(sr&&d.startsWith(sr+'/'))d=d.slice(sr.length+1);d=d.replace(/^.*\/(?=[^/]+\.ato\b)/,'');
 if(n==='WebSearch'&&d)d='“'+d.replace(/^["“]|["”]$/g,'')+'”';if(n==='WebFetch'){let u=/https?:\/\/\S+/.exec(d);if(u)d=shortUrl(u[0])}
 return (m?NOTEVERB[m]||'Notes: '+m:TOOLVERB[n]||n||'Tool')+(d?' '+d.replace(/…$/,''):'')+'…'}
// full: the whole URL / query (the server clips detail to 240 characters); a long one is flagged, it can carry data out
function toolLine(s){let web=s.name==='WebSearch'||s.name==='WebFetch',m=noteTool(s.name),full=String(s.full||s.detail||''),u=s.name==='WebFetch'&&/https?:\/\/[^\s"'<>]+/.exec(full)?.[0],txt=toolText(s);
 let q=0;if(u)try{q=new URL(u).search.length}catch(e){}
 let long=web&&(s.name==='WebFetch'?q>120||full.length>300:full.length>200)?h('em',{class:'ask-long',title:`Long ${s.name==='WebFetch'?'URL':'query'}: ${full.length} characters (full text in this tooltip and the stored conversation)`},full.length+' chars'):null;
 let body=u?[txt.slice(0,txt.indexOf(' ')+1),h('a',{href:u,target:'_blank',rel:'noopener noreferrer',title:u},shortUrl(u)),'…',long]:[txt,long];
 return h('div',{class:'ask-tool'+(web?' web':'')+(m?' notes':''),title:(web?'Web · ':m?'Notes · ':'')+full},h('i',null,'›'),body)}
const atEnd=()=>{let l=G.ui?.log;return !l||l.scrollHeight-l.scrollTop-l.clientHeight<40};
function scroll(force){let log=G.ui?.log;if(log&&(force||G.stick))log.scrollTop=log.scrollHeight}
let raf=0;function paint(turn){if(raf)return;raf=requestAnimationFrame(()=>{raf=0;G.stick=atEnd();renderTurn(turn);scroll()})}
const answerText=t=>t.seg.filter(s=>s.type==='text').map(s=>s.text).join('').trim();
function renderTurn(t){
 let parts=[];for(let s of t.seg){if(s.type!=='text')parts.push(toolLine(s));else if(s.text.trim()){let el=h('div',{class:'ask-md'});el.innerHTML=md(s.text);parts.push(el)}}
 if(!t.end&&(!t.seg.length||t.seg[t.seg.length-1].type!=='text'))parts.push(h('div',{class:'ask-wait'},t.seg.length?'Working…':'Thinking…'));
 else if(!t.end){let el=parts[parts.length-1];while(el?.lastElementChild&&!/^(PRE|TABLE|BUTTON|CODE|BR|HR|A)$/.test(el.lastElementChild.tagName)&&!el.lastElementChild.classList.contains('md-tw'))el=el.lastElementChild;el?.append(h('span',{class:'ask-caret'}))}
 if(t.error)parts.push(h('div',{class:'ask-err'},t.error));
 if(t.cancelled)parts.push(h('div',{class:'ask-err ask-cx'},'Cancelled.'));
 t.el.classList.toggle('live',!t.end);t.body.replaceChildren(...parts);renderFoot(t)}
// a user note saved from this turn (Save as note -> Notes editor): provenance session + turn and the 'ask' tag
function savedNote(t){let N=NS(),sid=t.session||t.sid;if(!N?.list||!sid||!t.no)return null;return N.list().find(n=>n.author==='user'&&n.provenance?.session===sid&&n.provenance?.turn===t.no&&(n.tags||[]).includes('ask'))||null}
function renderFoot(t){if(!t.foot)return;if(!t.end){t.foot.replaceChildren();return}
 let secs=t.dur!=null?(t.dur/1000).toFixed(1)+' s':t.stored?null:((t.end-t.t0)/1000).toFixed(1)+' s',when=t.stored&&isFinite(tms(t.ts))?new Date(tms(t.ts)).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):null;
 let meta=[t.model,t.cost!=null?'$'+(+t.cost).toFixed(t.cost<0.1?4:2):null,secs,when,t.tools?t.tools+' tool call'+(t.tools>1?'s':''):null,t.web?'web':null,t.session?'session '+String(t.session).slice(0,8):null].filter(Boolean);
 let N=NS(),text=answerText(t),save=null,sv=savedNote(t);
 if(N&&N.available?.()!==false&&text&&!t.cancelled)save=sv?h('button',{class:'dk-lnk ask-save',type:'button',title:'Open '+sv.id+' in Notes',onclick:()=>N.open(sv.id)},'Saved · '+sv.id):
  h('button',{class:'dk-lnk ask-save',type:'button',title:'Save this answer as a note: opens the Notes editor with the answer, this question’s context as targets and its links as sources; you write the title and save',onclick:()=>saveTurn(t)},'Save as note');
 t.foot.replaceChildren(...[h('div',{class:'ask-meta',title:t.session||''},meta.join(' · ')),h('span',{class:'dk-sp'}),save].filter(Boolean))}
function renderNotes(t){if(!t.notesEl)return;let N=NS(),ids=t.noteIds||[];t.notesEl.hidden=!ids.length;if(!ids.length||!N){t.notesEl.replaceChildren();return}
 t.notesEl.replaceChildren(...ids.map(id=>N.card(id,{compact:true,undo:!t.stored,caption:(N.get(id)?.author||'agent')==='agent'?'Recorded by Claude':'Saved as note'})))}
function claim(t,id){if(!id||G.claimed.has(id)&&t.noteIds?.includes(id))return;for(let x of G.turns)if(x!==t&&x.noteIds?.includes(id))return;G.claimed.add(id);(t.noteIds||=[]).includes(id)||t.noteIds.push(id);renderNotes(t)}
// new agent notes that carry this conversation's session (the notes MCP server stamps provenance.session) join the latest turn
function claimNew(){let N=NS();if(!N)return;let t=[...G.turns].reverse().find(x=>x.known);if(!t||(t.end&&Date.now()-t.end>120000))return;let sid=t.sid||G.session;if(!sid)return;
 for(let n of N.list())if(n.author==='agent'&&!t.known.has(n.id)&&!G.claimed.has(n.id)&&n.provenance?.session===sid)claim(t,n.id)}
// a title from the answer's first sentence (markup, headings, code and link targets removed); the editor lets you rewrite it
function titleOf(text){let lines=String(text||'').replace(/```[\s\S]*?(```|$)/g,'\n').split('\n').filter(l=>!/^\s*(#|\||>|---)/.test(l));
 let clean=l=>l.replace(/\[\[(?:ref|net|pad|src|note):([^\]\n]+?)\]\]/g,'$1').replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,'$1').replace(/https?:\/\/\S+/g,'').replace(/[`*_]/g,'').replace(/^\s*(?:[-+•]|\d+[.)])\s+/,'').replace(/\s+/g,' ').trim();
 let line=lines.map(clean).find(x=>x.length>3)||'',m=/^(.{12,}?[.!?:])(?:\s|$)/.exec(line),t=(m?m[1].replace(/:$/,''):line).trim();
 if(t.length>120){let c=Math.max(t.lastIndexOf(', ',119),t.lastIndexOf('; ',119),t.lastIndexOf(' — ',119)),k=t.lastIndexOf(' ',117);t=c>=40?t.slice(0,c):t.slice(0,k>60?k:117)+'…'} // a whole clause, else a word
 return t||'Assistant answer'}
function saveTurn(t){let N=NS();if(!N?.newNote)return;let text=answerText(t);if(!text)return;let v=V(),call=f=>{try{return f?.()??null}catch(e){return null}};
 N.newNote({kind:'observation',title:titleOf(text),body:text.length>8000?text.slice(0,7990)+'\n\n…':text,targets:(t.ctx||[]).slice(0,40),tags:['ask'],sources:mdSources(text),
  provenance:{session:t.session||t.sid||G.session||undefined,turn:t.no||undefined,lane:t.lane??call(v?.lane)??undefined}})}
function turnShell(t){t.el=h('div',{class:'ask-a'});t.body=h('div');t.notesEl=h('div',{class:'ask-notes',hidden:true});t.foot=h('div',{class:'ask-foot'});t.el.append(t.body,t.notesEl,t.foot);return t.el}
function question(t){return h('div',{class:'ask-q'},h('div',{class:'ask-qt'},t.q),t.ctx?.length?h('div',{class:'ask-qc'},t.ctx.map((x,i)=>ctxChip(x,i,false))):null)}
async function send(text){
 text=String(text??G.ui?.ta.value??'').trim();if(!text||G.busy)return false;
 if(!G.status)await status();if(!G.status?.available){renderStatus();return false}
 closeHistory();
 const v=V(),call=f=>{try{return f?.()??null}catch(e){return null}};
 const ctx={lane:call(v?.lane),phase:call(v?.phase),view:call(v?.view),selection:G.ctx.map(x=>JSON.parse(JSON.stringify(x)))},web=!!(webAvail()&&G.web);
 const t={q:text,ctx:ctx.selection,seg:[],t0:Date.now(),end:0,model:G.model,tools:0,web,lane:ctx.lane,no:G.turns.length+1,known:new Set((NS()?.list?.()||[]).map(n=>n.id)),sid:G.session};G.turns.push(t);G.turn=t;
 const u=G.ui;u.intro.hidden=true;if(u.ta.value.trim()===text)u.ta.value='';autosize();
 u.log.append(question(t),turnShell(t));renderTurn(t);G.stick=true;scroll(true);
 G.busy=true;G.ctl=new AbortController();ui();NS()?.fast?.(true);
 try{
  const r=await fetch('/api/agent/chat',{method:'POST',headers:{'Content-Type':'application/json',Accept:'text/event-stream'},signal:G.ctl.signal,
   body:JSON.stringify({session:G.session||undefined,message:text,model:G.model,web,context:ctx})});
  if(!r.ok||!r.body){let j=await r.json().catch(()=>null);throw Error(j?.error||'HTTP '+r.status+(r.status===403?' (origin not allowed for the assistant)':''))}
  await sse(r.body,(ev,d)=>on(t,ev,d));
  if(!t.done&&!t.error&&!t.cancelled)t.error='The answer stream ended unexpectedly.';
 }catch(e){if(e.name==='AbortError'||t.cancelled)t.cancelled=true;else t.error=e.message||String(e)}
 finally{G.busy=false;G.ctl=null;G.turn=null;t.end=Date.now();t.session=G.session;cancelAnimationFrame(raf);raf=0;G.stick=atEnd();renderTurn(t);scroll();ui();status(true);
  let N=NS();if(N){N.fast(false);N.refresh().then(claimNew).catch(()=>{})}}
 return true}
function on(t,ev,d){
 if(ev==='session'){G.session=d.id||d.session||G.session;t.sid=G.session;if(d.turn)t.no=d.turn;if(webPref.get(G.session)==null)webPref.set(G.session,G.web);ui()}
 else if(ev==='delta'){let x=d.text??'';if(!x)return;let s=t.seg[t.seg.length-1];if(s?.type==='text')s.text+=x;else t.seg.push({type:'text',text:x});paint(t)}
 else if(ev==='tool'){t.seg.push({type:'tool',name:d.name,detail:d.detail,full:d.full});t.tools++;paint(t);let m=noteTool(d.name);if(m&&m!=='list_notes'&&m!=='get_note')setTimeout(()=>NS()?.refresh?.().catch(()=>{}),600)}
 else if(ev==='note'){let id=d.id||d.note?.id;if(id){if(d.note)NS()?.upsert?.(d.note);if(d.op==null||d.op==='create')claim(t,id);NS()?.refresh?.().catch(()=>{})}}
 else if(ev==='done'){t.done=true;t.cost=d.cost_usd;if(d.session)G.session=d.session;for(let id of d.notes_created||[])claim(t,id);if(!t.seg.some(s=>s.type==='text'&&s.text.trim())&&d.result)t.seg.push({type:'text',text:String(d.result)});paint(t)}
 else if(ev==='error'){t.error=d.error||d.message||'Assistant error';paint(t)}}
async function sse(body,cb){
 const rd=body.getReader(),dec=new TextDecoder();let buf='';
 const block=b=>{let ev='message',data=[];for(let l of b.split(/\r\n|\r|\n/)){if(!l||l[0]===':')continue;let k=l.indexOf(':'),f=k<0?l:l.slice(0,k),v=k<0?'':l.slice(k+1).replace(/^ /,'');if(f==='event')ev=v;else if(f==='data')data.push(v)}
  if(!data.length)return;let s=data.join('\n'),d;try{d=JSON.parse(s)}catch(e){d={text:s}}cb(ev,d)};
 for(;;){let {value,done}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});
  let m;while((m=/\r\n\r\n|\n\n|\r\r/.exec(buf))){let b=buf.slice(0,m.index);buf=buf.slice(m.index+m[0].length);block(b)}}
 buf+=dec.decode();if(buf.trim())block(buf)}
function cancel(){if(!G.busy)return;let s=G.session;if(G.turn)G.turn.cancelled=true;
 if(s)fetch('/api/agent/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session:s})}).catch(()=>{});
 G.ctl?.abort()}
function clearLog(){let u=G.ui;if(!u)return;for(let el of [...u.log.children])if(el!==u.intro)el.remove()}
function newConversation(){if(G.busy)cancel();G.session=null;G.turns=[];G.web=!!G.status?.web;let u=G.ui;if(!u)return;closeHistory();clearLog();u.intro.hidden=false;ui();u.ta.focus()}
function autosize(){let ta=G.ui?.ta;if(!ta)return;ta.style.height='auto';ta.style.height=Math.min(220,Math.max(58,ta.scrollHeight+2))+'px'}
function draft(text,append){let u=G.ui;if(!u)return;openAsk();u.ta.value=append&&u.ta.value.trim()?u.ta.value.replace(/\s*$/,' ')+text:text;autosize();ui();setTimeout(()=>{u.ta.focus();u.ta.setSelectionRange(u.ta.value.length,u.ta.value.length)},0)}

// ------------------------------------------------------------------ history (stored conversations)
async function jget(url){let r=await fetch(url,{cache:'no-store'}),j=await r.json().catch(()=>null);if(!r.ok||!j)throw Object.assign(Error(r.status===404?'not available on this server':j?.error||'HTTP '+r.status),{status:r.status});return j}
function closeHistory(){let u=G.ui;if(!u||u.histEl.hidden)return;u.histEl.hidden=true;u.log.hidden=false;u.hist.classList.remove('on');u.hist.setAttribute('aria-expanded','false')}
async function openHistory(){let u=G.ui;if(!u||G.busy)return;if(!u.histEl.hidden)return closeHistory();
 u.histEl.hidden=false;u.log.hidden=true;u.hist.classList.add('on');u.hist.setAttribute('aria-expanded','true');
 const head=(n)=>h('div',{class:'ask-hh'},h('b',null,'Conversations'+(n!=null?' · '+n:'')),h('span',{class:'dk-sp'}),h('button',{class:'dk-xs',type:'button',title:'Back to the conversation',onclick:closeHistory},'×'));
 u.histEl.replaceChildren(head(),h('div',{class:'dk-empty'},'Loading stored conversations…'));
 try{let j=await jget('/api/agent/conversations'),list=(Array.isArray(j)?j:j.conversations||[]).slice().sort((a,b)=>(tms(b.updated)||0)-(tms(a.updated)||0));
  if(u.histEl.hidden)return;
  u.histEl.replaceChildren(head(list.length),list.length?h('div',{class:'ask-hl'},list.map(c=>h('button',{type:'button',class:'ask-hc'+(c.session===G.session?' cur':''),title:(c.title||'')+'\nsession '+c.session,onclick:()=>loadConversation(c.session)},
    h('span',{class:'ask-hct'},c.title||'(untitled)'),h('span',{class:'ask-hcm'},[(c.turns||0)+' turn'+(c.turns===1?'':'s'),c.lane?'lane '+String(c.lane).split('/').slice(-2).join('/'):null,ago(c.updated||c.started),c.session===G.session?'current':null].filter(Boolean).join(' · ')))))
   :h('div',{class:'dk-empty'},h('b',null,'No stored conversations yet'),h('p',null,'Every question and answer is kept on the server with its context, tools and the notes it produced. Reopen one here to read it again or continue it.')))}
 catch(e){if(!u.histEl.hidden)u.histEl.replaceChildren(head(),h('div',{class:'dk-empty dk-err'},'Conversation history: '+e.message))}}
function storedTurn(r,sid,i){let tools=(r.tools||[]).map(x=>({type:'tool',name:x.name,detail:x.detail,full:x.full})),a=r.answer??r.text??r.result??'';
 return {q:r.message??r.user??r.question??'',ctx:r.selection||r.context?.selection||[],seg:[...tools,...(a?[{type:'text',text:String(a)}]:[])],t0:tms(r.ts)||0,end:tms(r.ts)||1,ts:r.ts,dur:r.duration_ms??null,model:r.model,cost:r.cost_usd,tools:tools.length,web:!!r.web,lane:r.lane,
  session:sid,sid,no:r.turn||i+1,noteIds:(r.notes_created||[]).slice(),stored:true,done:true,error:r.error||(r.status&&!['done','ok','success'].includes(r.status)&&!a?'('+r.status+')':null)}}
async function loadConversation(sid){let u=G.ui;if(!u||G.busy)return false;
 try{let j=await jget('/api/agent/conversations/'+encodeURIComponent(sid)),rows=Array.isArray(j)?j:j.turns||[];
  clearLog();u.intro.hidden=true;G.session=sid;G.turns=[];
  for(let [i,r] of rows.entries()){let t=storedTurn(r,sid,i);for(let id of t.noteIds)G.claimed.add(id);G.turns.push(t);u.log.append(question(t),turnShell(t));renderTurn(t);renderNotes(t)}
  let last=rows[rows.length-1],p=webPref.get(sid);G.web=p??(last&&typeof last.web==='boolean'?last.web:!!G.status?.web);
  u.log.append(h('div',{class:'ask-resumed'},'Reopened conversation · '+rows.length+' turn'+(rows.length===1?'':'s')+' · your next question continues it'));
  closeHistory();ui();G.stick=true;scroll(true);u.ta.focus();return true}
 catch(e){let m=h('div',{class:'ask-err'},'Cannot open conversation '+String(sid).slice(0,8)+': '+e.message);if(u.histEl.hidden){u.intro.hidden=true;u.log.append(m);scroll(true)}else u.histEl.append(m);return false}}

// ------------------------------------------------------------------ build
function build(){
 const K=window.YapnrDock,panel=K?.el('ask');if(!panel||G.ui)return;panel.innerHTML='';
 const ex=['What does this part do in the circuit, and why is it here?','Check the datasheet: is this capacitor bank adequate for the converter’s ripple current?','Record what we concluded as notes, with the sources you used.'];
 const u=G.ui={note:h('div',{class:'ask-note',hidden:true}),log:h('div',{class:'ask-log','aria-live':'polite'}),ctx:h('div',{class:'ask-ctx'}),histEl:h('div',{class:'ask-hist',hidden:true}),
  ta:h('textarea',{class:'ask-ta',rows:'2',placeholder:'Ask about the design, a part or a net…  (Enter sends · Shift+Enter new line)'}),
  model:h('select',{class:'ask-model',title:'Model'}),send:h('button',{class:'ask-send primary',type:'button'},'Send'),cancel:h('button',{class:'ask-cancel',type:'button',hidden:true},'Cancel'),
  web:h('button',{class:'ask-web on',type:'button','aria-pressed':'true',hidden:true},h('i',{class:'ask-webi'}),'Web'),
  hist:h('button',{class:'ask-histb',type:'button','aria-expanded':'false',title:'Stored conversations: reopen, read and continue'},'History'),
  newc:h('button',{class:'ask-new',type:'button'},'New')};
 u.intro=h('div',{class:'ask-intro'},h('b',null,'Ask Claude about this board'),
  h('p',null,'Answers come from the atopile sources, the netlist, your selection and existing notes; with Web on it can also search the web and fetch public pages (datasheets, app notes) and cites them as links.'),
  h('p',null,'It never edits design files, routes copper or drives KiCad. When you ask, it records conclusions, requirements, questions and proposals as notes in the Notes tab; only you accept, reject or apply them.'),
  u.hint=h('p',{class:'dk-muted'},'Add context with “Ask about this” in Inspect or Source. Chips in answers highlight the part, net or pad on the board.'),
  u.ex=h('div',{class:'ask-ex'},ex.map(q=>h('button',{type:'button',onclick:()=>{u.ta.value=q;autosize();ui();u.ta.focus()}},q))));
 u.log.append(u.intro);
 u.ctxRow=h('div',{class:'ask-ctxrow',hidden:true},h('span',{class:'ask-lbl'},'Context'),u.ctx);
 panel.append(h('div',{class:'ask'},u.note,u.log,u.histEl,h('div',{class:'ask-compose'},u.ctxRow,u.ta,h('div',{class:'ask-row'},u.model,u.web,u.hist,u.newc,h('span',{class:'dk-sp'}),u.cancel,u.send))));
 u.ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();send()}});
 u.ta.addEventListener('input',()=>{autosize();ui()});
 u.send.onclick=()=>send();u.cancel.onclick=cancel;u.newc.onclick=newConversation;u.model.onchange=()=>{G.model=u.model.value;store.set('yapnr-agent-model',G.model)};
 u.web.onclick=()=>{G.web=!G.web;webPref.set(G.session,G.web);renderWeb()};u.hist.onclick=openHistory;
 u.log.addEventListener('click',e=>{let b=e.target.closest('button.dk-chip');if(!b||!u.log.contains(b))return;let k=b.dataset.k,v=b.dataset.v,S=SS();
  if(k==='note')return NS()?.open?.(v);
  if(S?.act)return S.act(k,v,{focus:false});try{V()?.highlight?.({refs:k==='ref'?[v]:[],nets:k==='net'?[v]:[],pads:k==='pad'?[v]:[]})}catch(err){}});
 D.addEventListener('yapnr:dock',e=>{if(e.detail?.tab==='ask'&&e.detail.open){status();if(u.histEl.hidden)setTimeout(()=>u.ta.focus(),0)}});
 D.addEventListener('yapnr:notes',()=>{claimNew();for(let t of G.turns){if(t.noteIds?.length)renderNotes(t);if(t.end)renderFoot(t)}}); // inline cards, Undo, "Saved · N-…"
 renderCtx();ui();status();
 SS()?.ready?.().then(()=>renderCtx()).catch(()=>{}); // chip labels need the source index (types, net titles)
}
function openAsk(){let K=window.YapnrDock;if(!K)return;K.tab('ask');if(G.ui)setTimeout(()=>G.ui.ta.focus(),0)}
window.YapnrAgent={enabled:()=>!absent(),offText:'The assistant is off on this server (start the viewer with --agent on).',addContext,removeContext,context:()=>G.ctx.slice(),ask(text){openAsk();return send(text)},open:openAsk,cancel,newConversation,status:()=>G.status,markdown:md,
 chip:itemChip,label:ctxLabel,show:showItem,key:ctxKey,draft,session:()=>G.session,busy:()=>G.busy,web:()=>!!(webAvail()&&G.web),history:openHistory,load:loadConversation,sources:mdSources,titleOf};
if(window.YapnrDock)build();else D.addEventListener('DOMContentLoaded',()=>window.YapnrDock?build():console.warn('agent.js: window.YapnrDock missing (load source.js before agent.js)'));
})();
