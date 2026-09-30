"""Explicit-interface live PnR telemetry, immutable pins and annotated snapshots."""
import argparse,copy,json,os,re,subprocess,threading,time,uuid,sys,hashlib,functools,math,select,signal,socket
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
ap=argparse.ArgumentParser();ap.add_argument('root',type=Path);ap.add_argument('--port',type=int,default=8766);ap.add_argument('--listen',action='append');ap.add_argument('--allow-origin',action='append',default=[]);ap.add_argument('--repo',type=Path,help='Repository for runtime imports and persistent preferences (isolated viewer deployment)');ap.add_argument('--cost-runtime',type=Path,help='Frozen PnR package parent for cost replay; defaults to repository hardware/pnr');ap.add_argument('--cost-contexts',type=Path,help='Optional explicit retrospective contexts JSON')
for flag,text in (('--schematic-graph','Board netlist graph.json for the schematic view'),('--schematic-rules','rules.json with current envelopes (default: next to the graph)'),('--schematic-constraints','Placement constraints YAML (hard groups)'),('--schematic-parts','atopile parts folder holding the .kicad_sym symbols'),('--schematic-runtime','Frozen pnr runtime with pnr.power_topology and pnr.hier (default: --cost-runtime)'),('--schematic-hier','Experiment output root that trial run directories must lie under (default: parent of root)'),('--schematic-cache','Schematic payload cache directory (default: <root>/schematic)'),('--source-dir','atopile src folder for Inspect/Source/Ask (default: <cost-runtime>/../splanc_dev/elec/src)'),('--source-cache','Source index and AI net-summary cache (default: <root>/source)')):ap.add_argument(flag,type=Path,help=text)
ap.add_argument('--agent',choices=('auto','on','off'),default='auto',help='Ask tab: headless Claude CLI turns with read-only tools. auto (default): on only when every --listen address is loopback; tailnet/LAN listeners need an explicit --agent on');ap.add_argument('--agent-read-dir',action='append',type=Path,help='Folder the assistant may read (repeatable; replaces the default: atopile src, the hier folder and the transfer folder holding HANDOFF-PROGRESS.md)');ap.add_argument('--agent-total-usd',type=float,default=20.0,help='Assistant spend cap for this server process (all turns); 0 disables the cap');ap.add_argument('--allow-host',action='append',default=[],help='Extra Host header name accepted (DNS-rebinding guard; loopback names, --listen addresses and --allow-origin hosts are always accepted)');ap.add_argument('--agent-model',choices=('opus','sonnet'),default='opus',help='Default Ask model');ap.add_argument('--agent-budget-usd',type=float,default=2.0,help='Per-turn CLI spend cap');ap.add_argument('--agent-claude',type=Path,default=Path('claude'),help='Claude CLI binary');ap.add_argument('--net-summaries',choices=('auto','on','off'),default='auto',help='AI net labels (one CLI call per changed dossier, about $0.20): on generates them in a background thread when the cache for the current dossier/prompt/model is missing; auto = on only while the assistant is enabled');ap.add_argument('--net-summary-model',choices=('sonnet','opus','haiku'),default='sonnet',help='Model for the AI net labels')
ap.add_argument('--viewer3d',choices=('on','off'),default='on',help='3D view: headless kicad-cli GLB export of the selected board (one at a time, cached by placement)');ap.add_argument('--viewer3d-cli',type=Path,help='Headless kicad-cli for GLB export (default: $PNR_KICAD_CLI, else ~/Applications/KiCad-headless.app; the /Applications/KiCad GUI copy is refused)');ap.add_argument('--viewer3d-parts',type=Path,help='atopile parts folder holding the 3D models (default: <source-dir>/parts)');ap.add_argument('--viewer3d-cache',type=Path,help='GLB cache (default: <root>/viewer3d)');ap.add_argument('--viewer3d-timeout',type=float,default=240.0,help='Seconds per 3D export');ap.add_argument('--viewer3d-max-mb',type=float,default=64.0,help='Largest compacted GLB kept')
ap.add_argument('--notes-dir',type=Path,help='Design notes store (notes.jsonl, notes.json, design-notes.md, conversations/) shared by viewers and the Ask agent (default: <root>/../notes)');ap.add_argument('--agent-web',choices=('on','off'),default='on',help='Ask turns may use WebSearch/WebFetch (public hosts only; loopback, private, link-local, CGNAT/tailnet and *.ts.net/*.local are refused); the UI toggles it per conversation')
a=ap.parse_args();root=a.root.resolve();root.mkdir(parents=True,exist_ok=True)
repo=(a.repo or Path(__file__).resolve().parents[3]).resolve();assets=Path(__file__).parent/'dist';lock=threading.RLock();state=dict(schema='pnr-live-state-v1',run=str(root.parent),revision=0,lanes={},events=[],search={},errors=[]);seen=set();cache=None
class Recent(dict):
 """Geometry cache holding the newest 48 boards: a full replay (thousands of boards) no longer keeps
 every one in memory. Lanes keep their own references; /api/geometry re-reads evicted files from disk."""
 def __setitem__(self,k,v):
  self.pop(k,None);super().__setitem__(k,v)
  while len(self)>48:
   try:del self[next(iter(self))]
   except (KeyError,StopIteration,RuntimeError):break
cache=Recent()
sys.path.insert(0,str(repo/'hardware/pnr'))
from pnr.runtime_controls import read as read_controls,write as write_controls,LIMITS
from settings import seed as seed_settings,save as save_settings
from event_schema import phase_frame
from cost_service import CostService
cost_service=CostService(root,repo,Path(__file__).parent,runtime=a.cost_runtime,contexts=a.cost_contexts)
from schematic_service import SchematicService
def schematic_defaults():
 runtimes=[p for p in (a.schematic_runtime,a.cost_runtime,repo/'hardware/pnr') if p and (p/'pnr/power_topology.py').is_file() and (p/'pnr/hier/blocks.py').is_file()]
 runtime=runtimes[0].resolve() if runtimes else None;design=runtime.parent/'splanc_dev' if runtime else None
 graphs=[p for p in (a.schematic_graph,root.parent/'inputs10b/graph.json',runtime and runtime.parents[2]/'inputs10b/graph.json') if p and Path(p).is_file()]
 pick=lambda given,fallback:given or (fallback if fallback and Path(fallback).exists() else None)
 return dict(graph=graphs[0] if graphs else None,rules=a.schematic_rules,constraints=pick(a.schematic_constraints,design and design/'mini-constraints.yaml'),parts=pick(a.schematic_parts,design and design/'elec/src/parts'),runtime=runtime,hier=a.schematic_hier or root.parent,cache_dir=a.schematic_cache or root/'schematic')
schematic_service=SchematicService(root,assets=Path(__file__).parent,**schematic_defaults())
# Trial source paths stay out of /api/state; the schematic route alone reads them.
lane_sources={}
# Native board paths / final objectives per lane, also outside /api/state: the Ask context reads them.
lane_meta={}
from source_service import SourceService,SourceNotFound
def source_defaults():
 d=schematic_defaults();rt=d['runtime']
 src=a.source_dir or next((p.resolve() for p in (a.cost_runtime and a.cost_runtime/'../splanc_dev/elec/src',rt and rt.parent/'splanc_dev/elec/src') if p and p.is_dir()),None)
 return src,d['graph'],d['rules']
source_dir,source_graph,source_rules=source_defaults();source_cache=a.source_cache or root/'source'
source_service=SourceService(source_dir,source_graph,rules=source_rules,cache_dir=source_cache,llm_model=a.net_summary_model) if source_dir and source_graph else None
if source_service and not source_service.configured():source_service=None
def lane_brief(lane_id):
 """Compact lane facts for the Ask context: objective, opens, run dir, native board path."""
 with lock:
  l=state['lanes'].get(lane_id)
  if l is None:return dict(error='unknown lane')
  out={k:l[k] for k in ('status','kind','iteration','phase','opens','violations','board_sha256') if l.get(k) is not None}
  if l.get('target'):out['routing_target']=l['target']
  if l.get('last_route'):out['last_route']={k:l['last_route'][k] for k in ('accepted','opens','phase','stage','status','scope') if k in l['last_route']}
  frames=l.get('frames') or [];out['checkpoints']=len(frames)
  if frames:out['last_checkpoint']={k:frames[-1].get(k) for k in ('name','opens','violations')}
  meta=dict(lane_meta.get(lane_id) or {});source=lane_sources.get(lane_id)
 o=meta.get('objective')
 if isinstance(o,list) and len(o)==6:o=dict(zip(('violations','blocked','reference','subwidth','unqualified_pairs','unconnected'),o))
 if o is not None:out.update(objective=o,objective_scope=meta.get('score_scope'),qualified=meta.get('qualified'))
 out.update(native_board=meta.get('board'),working_board=source)
 try:run=schematic_service.run_dir(lane_id,source)
 except (OSError,ValueError):run=None
 out['run_dir']=str(run) if run else None
 return out
from viewer3d_service import Viewer3DService
v3_parts=a.viewer3d_parts or (source_dir/'parts' if source_dir and (source_dir/'parts').is_dir() else schematic_defaults()['parts'])
# One export at a time per viewer, and across the viewers of one hier folder (flock); requests never wait for it.
viewer3d=Viewer3DService(a.viewer3d_cache or root/'viewer3d',cli=a.viewer3d_cli,parts=v3_parts,timeout=a.viewer3d_timeout,max_bytes=int(a.viewer3d_max_mb*(1<<20)),lock_path=root.parent/'viewer3d-export.lock') if a.viewer3d=='on' else None
if viewer3d and viewer3d.disabled:print('3D view: '+viewer3d.disabled,file=sys.stderr,flush=True)
from agent_service import AgentService,agent_mode,is_loopback
from notes_store import NotesStore,NoteNotFound,NoteConflict
# Design notes: always on (also with --agent off); the Ask agent writes them only through its per-turn MCP server (notes_mcp.py).
notes_dir=(a.notes_dir or root.parent/'notes').resolve()
try:notes=NotesStore(notes_dir,resolver=source_service)
except Exception as ex:notes=None;print(f'notes store unavailable ({notes_dir}): {type(ex).__name__}: {ex}',file=sys.stderr,flush=True)  # the viewer still starts
listen_hosts=a.listen or ['127.0.0.1'];exposed=[h for h in listen_hosts if not is_loopback(h)]
# Paid turns that can read the transfer folder: never enabled implicitly on a tailnet/LAN listener (the Origin check stops browsers, not forged headers).
agent_on,agent_off_reason=agent_mode(a.agent,listen_hosts)
if a.agent=='on' and exposed:print(f"WARNING: --agent on with non-loopback listeners {exposed}: any client that reaches them (and sends an allowlisted Origin header) can run paid assistant turns",file=sys.stderr,flush=True)
own_names=[*listen_hosts,*a.allow_host,*(urlparse(o).hostname or '' for o in a.allow_origin)]  # WebFetch deny rules (with this machine's addresses)
agent_service=AgentService(repo,claude_bin=a.agent_claude,default_model=a.agent_model,max_budget_usd=a.agent_budget_usd,source=source_service,state_fn=lane_brief,event_fn=lambda i:event_brief(i),cache_dir=root/'agent',src_root=source_dir,
                           add_dirs=a.agent_read_dir,max_total_usd=a.agent_total_usd or None,notes=notes,web=a.agent_web=='on',viewer_port=lambda:a.port,local_names=own_names) if agent_on else None
summaries_on=a.net_summaries=='on' or (a.net_summaries=='auto' and agent_service is not None)
net_summary_status=dict(mode=a.net_summaries,model=a.net_summary_model,state='pending' if summaries_on and source_service else 'off')
if source_service and not summaries_on:net_summary_status['reason']='--net-summaries off' if a.net_summaries=='off' else 'assistant disabled (--net-summaries auto follows --agent); pass --net-summaries on to generate anyway'
def net_summaries():
 """Background: label nets once per mechanical dossier, prompt version and model (one CLI call); nets a failed chunk
 left missing are retried alone with backoff, never a full re-run per restart. Never blocks serving."""
 import fcntl,net_llm
 def current(f):
  try:doc=json.loads(f.read_text())
  except (OSError,ValueError):return False
  return net_llm.settled(doc,source_service.index()['dossier_sha'],a.net_summary_model)
 try:
  out=source_service.llm_path;out.parent.mkdir(parents=True,exist_ok=True)
  if not current(out):
   net_summary_status.update(state='waiting')
   with open(out.parent/'source-llm.lock','w') as lk:
    fcntl.flock(lk,fcntl.LOCK_EX)  # a second viewer on the same cache waits, then finds it current
    if not current(out):
     net_summary_status.update(state='generating');print(f'net summaries: generating with {a.net_summary_model} ->',out,file=sys.stderr,flush=True)
     net_llm.generate(source_service,out,model=a.net_summary_model,claude_bin=a.agent_claude,cache=root/'agent',log=lambda m:print(m,file=sys.stderr,flush=True))
  try:missing=len(json.loads(out.read_text()).get('missing') or [])
  except (OSError,ValueError,AttributeError):missing=None
  net_summary_status.update(state='current' if current(out) and not missing else 'incomplete' if current(out) else 'failed',missing=missing,nets=source_service.merge_llm())
 except Exception as ex:
  net_summary_status.update(state='failed',error=f'{type(ex).__name__}: {ex}'[:400]);print('net summaries failed:',net_summary_status['error'],file=sys.stderr,flush=True)
def event_brief(event_id):
 """One event from the live stream for the Ask context (the summarised data /api/state shows)."""
 with lock:e=next((x for x in reversed(state['events']) if x.get('id')==event_id),None)
 return copy.deepcopy(e) if e else None
def agent_status():
 s=agent_service.status() if agent_service else dict(available=False,reason=agent_off_reason,models=['opus','sonnet'],default=a.agent_model,busy=False,max_concurrent=0,web=False,notes=notes is not None)
 return dict(s,net_summaries=net_summary_status,source=bool(source_service))
sse_origins=lambda:[f'http://127.0.0.1:{a.port}',f'http://localhost:{a.port}',*a.allow_origin]
# DNS-rebinding guard: a page on attacker.example that re-resolves to this address still sends Host: attacker.example.
allowed_hosts={'127.0.0.1','localhost','::1',*(h.lower().strip('[]') for h in listen_hosts),*(h.lower() for h in a.allow_host)}
for o in a.allow_origin:
 h=(urlparse(o).hostname or '').lower()
 if h:allowed_hosts|={h,h.split('.')[0]}  # also the MagicDNS short name (host for host.<tailnet>)
def host_allowed(value):
 if value is None:return True  # HTTP/1.0 clients; browsers always send Host
 v=value.strip().lower();host=v[1:v.find(']')] if v.startswith('[') else v.rsplit(':',1)[0] if v.count(':')==1 else v
 return host in allowed_hosts
gzip_cache={}
def gzipped(raw):
 import gzip
 c=gzip_cache.get('v')  # one (raw, gz, etag) tuple, swapped atomically between request threads
 if c is None or c[0] is not raw:c=gzip_cache['v']=(raw,gzip.compress(raw,6),'"'+hashlib.sha256(raw).hexdigest()[:32]+'"')
 return c[1],c[2]
preferences=repo/'output/pnr-settings.json'
state['controls']=seed_settings(root/'control.json',preferences);state['active_controls']=None
ki='/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3'
extracting={}  # running KiCad extractor -> its temporary output; killed and removed when this server is stopped
def sweep_tmp(folder,age=600):
 """Remove temporary geometry files older than age s (left when a viewer was killed mid-extraction; extraction times out at 40 s)."""
 now=time.time()
 for t in folder.glob('*.tmp'):
  try:
   if now-t.stat().st_mtime>age:t.unlink()
  except OSError:pass
last_sweep=[0.0]
def geometry(event):
 sha=event['board_sha256']
 if sha not in cache:
  folder=root/'geometry';folder.mkdir(exist_ok=True);f=folder/(sha+'.json')
  if time.time()-last_sweep[0]>600:last_sweep[0]=time.time();sweep_tmp(folder)
  if not f.exists():
   tmp=folder/(sha+'.'+uuid.uuid4().hex+'.tmp')
   try:
    with (folder/(sha+'.log')).open('w') as log:
     proc=subprocess.Popen([ki,str(Path(__file__).parent/'extract.py'),event['board'],str(tmp)],env=dict(os.environ,PYTHONPATH=str(repo/'hardware/pnr')),stdout=log,stderr=subprocess.STDOUT);extracting[proc]=tmp
     try:code=proc.wait(timeout=40)
     except subprocess.TimeoutExpired:proc.kill();proc.wait();raise
     finally:extracting.pop(proc,None)
     if code:raise subprocess.CalledProcessError(code,'extract.py')
    tmp.replace(f)
   finally:tmp.unlink(missing_ok=True)
  # Another viewer on the same root may still be writing f (older viewers write it in place).
  for attempt in range(40):
   try:cache[sha]=json.loads(f.read_text());break
   except ValueError:
    if attempt==39:raise
    time.sleep(.25)
 return cache[sha]
def from_graph(g):
 import math
 parts=[]
 for c in g['components']:
  angle=math.radians(c['rot']);pads=[]
  for p in c['pads']:
   x,y=p['offset'];xy=[c['pos'][0]+x*math.cos(angle)-y*math.sin(angle),c['pos'][1]+x*math.sin(angle)+y*math.cos(angle)]
   pads.append(dict(number=p['name'],net=p['net'],xy=xy,size=p['size'],angle=c['rot'],shape='rect',layers=['F.Cu' if c['side']=='top' else 'B.Cu']))
  parts.append(dict(ref=c['ref'],xy=c['pos'],pads=pads))
 return dict(frame='mm-y-up',width=g['outline']['width'],height=g['outline']['height'],parts=parts,tracks=[],vias=[],zones=[])
def ingest():
 event_dir=root/'events';event_dir.mkdir(exist_ok=True);last_mtime=None;last_restart=None
 while True:
  restart_file=root.parent/'restart-status.json'
  if restart_file.exists():
   stamp_restart=restart_file.stat().st_mtime_ns
   if stamp_restart!=last_restart:
    with lock:state['restart_status']=json.loads(restart_file.read_text());state['revision']+=1
    last_restart=stamp_restart
  stamp=event_dir.stat().st_mtime_ns
  if stamp==last_mtime:
   time.sleep(.25);continue
  # Files are atomically renamed into this immutable event directory. A rename
  # during this scan changes mtime and is discovered on the next pass.
  last_mtime=stamp
  with os.scandir(event_dir) as entries:
   pending=sorted(entry.name for entry in entries if entry.name.endswith('.json') and entry.name not in seen)
  for name in pending:
   f=event_dir/name
   if f.name in seen:continue
   try:
    e=json.loads(f.read_text());frame=phase_frame(e) if e['kind']=='phase_complete' else None;geo=geometry(e) if 'board' in e else (from_graph(e['layout']) if 'layout' in e else None)
    layout_sha=None
    if e['kind']=='placement_cost_capture':
     layout_sha=hashlib.sha256(json.dumps(e['layout'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
     cache[layout_sha]=geo;write_json_atomic(root/'geometry'/(layout_sha+'.json'),geo)
     frame=dict(name=e['data']['phase']+' · recorded placement cost',kind='placement-cost',layout_sha256=layout_sha,event_id=e['id'],opens=None,violations=None,label_source='data.phase',metric_sources={},cost_capture=e['data']['cost_capture'])
    with lock:
     lane=state['lanes'].setdefault(e['candidate'],dict(id=e['candidate'],draft={},costs={},frames=[]));lane.update(event_id=e['id'],time=e['time'],iteration=e['iteration'],kind=e['kind'])
     if isinstance(e.get('source'),str):lane_sources[e['candidate']]=e['source']
     if isinstance(e.get('board'),str):lane_meta.setdefault(e['candidate'],{})['board']=e['board']
     if e['kind']=='candidate_complete':lane_meta.setdefault(e['candidate'],{}).update({k:e['data'].get(k) for k in ('objective','qualified','score_scope')})
     if e['data'].get('phase'):lane['phase']=e['data']['phase']
     if e['kind']=='controls_applied':state['active_controls']=e['data']
     if e['kind']=='worker_config_applied':lane['worker_config']=e['data']
     if frame:
      lane['phase']=frame['name'];lane['opens']=frame['opens'];lane['violations']=frame['violations']
      lane['phase_label_source']=frame['label_source'];lane['phase_metric_sources']=frame['metric_sources'];lane['phase_accepted']=frame.get('accepted')
     if geo:
      if lane.get('geometry') and e.get('board_sha256')!=lane.get('board_sha256'):lane['previous']=lane['geometry']
      lane['geometry']=geo;lane['board_sha256']=e.get('board_sha256');lane['layout_sha256']=layout_sha;lane['geometry_event_id']=e['id'];lane['draft']={}
     if frame:lane['frames'].append(frame)
     if e['kind']=='route_result':lane['opens']=e['data'].get('opens');lane['last_route']=e['data'];lane['copper_changed_at']=time.time() if e['data'].get('accepted') else lane.get('copper_changed_at',0)
     if e['kind']=='candidate_queued':lane['moves']=e['data'].get('moves',[]);lane['cost']=e['data'].get('cost')
     if e['kind']=='route_start':lane['target']=e['data']['target']
     if e['kind']=='signal_net_added':lane['draft'][e['data']['net']]=e['data']['tracks']
     if e['kind']=='signal_net_removed':lane['draft'].pop(e['data']['net'],None)
     if e['kind']=='placement_costs':lane['costs'][e['data']['ref']]=e['data']
     if e['kind']=='batch_alternatives':state['search'][str(e['iteration'])]=e['data']
     if e['kind'] in ('candidate_queued','candidate_start','candidate_complete','candidate_failed'):lane['status']=e['kind'].removeprefix('candidate_')
     if e['kind']=='iteration_complete':lane['status']='accepted' if e['data'].get('accepted') else 'rejected'
     summary={k:e[k] for k in ('id','time','kind','candidate','iteration')};summary['data']={k:v for k,v in e['data'].items() if k not in ('tracks','candidates','alternatives','probes','electrical_audit','pad_entry','final')};state['events'].append(summary);state['events']=state['events'][-300:];state['revision']+=1
    seen.add(f.name)
   except Exception as ex:
    with lock:state['errors'].append(dict(file=f.name,error=str(ex)));state['errors']=state['errors'][-10:];state['revision']+=1
    seen.add(f.name)
  time.sleep(.25)
def current(selected=None,full=True):
 with lock:
  if full:return copy.deepcopy(dict(state,server_time=time.time()))
  # Filter before copying: unselected board geometry never enters the copy.
  lanes={key:{k:v for k,v in lane.items() if key==selected or k not in ('geometry','previous','draft')} for key,lane in state['lanes'].items()}
  return copy.deepcopy(dict(state,lanes=lanes,server_time=time.time()))
response_cache={}
def state_response(query):
 with lock:
  selected=query.get('lane',[None])[0]
  selected=selected or next((k for k in state['lanes'] if not k.endswith('/search')),None)
  rev=state['revision']
  if query.get('since',[None])[0]==str(rev) and query.get('run',[None])[0]==state['run']:
   return json.dumps(dict(unchanged=True,revision=rev,run=state['run']),separators=(',',':')).encode()
  key=(rev,selected)
  if key not in response_cache:
   # Cache only the current revision; no retained historical geometry copies.
   for old in list(response_cache):
    if old[0]!=rev:del response_cache[old]
   response_cache[key]=json.dumps(current(selected,full=False),separators=(',',':')).encode()
  return response_cache[key]
def write_json_atomic(path,value):
 path.parent.mkdir(exist_ok=True)
 temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
 try:
  temporary.write_text(json.dumps(value,indent=2));temporary.replace(path)
 finally:
  temporary.unlink(missing_ok=True)
@functools.lru_cache(maxsize=16)
def pin_summary(pin):
 # Immutable pins can be large; repeated keystrokes need only compact identity
 # metadata. Bound this cache and load full geometry only for a final bundle.
 snapshot=json.loads((root/'pins'/(pin+'.json')).read_text())
 return dict(run=snapshot['run'],lanes={key:dict(board_sha256=lane.get('board_sha256'),frames=lane.get('frames',[])) for key,lane in snapshot['lanes'].items()})
def read_annotation(body,full=False):
 pin=body['pin_id']
 if not isinstance(pin,str) or not re.fullmatch('[a-f0-9]{32}',pin):raise ValueError('invalid pin')
 summary=pin_summary(pin);snapshot=json.loads((root/'pins'/(pin+'.json')).read_text()) if full else None
 selected=body.get('view',{});lane=summary['lanes'].get(selected.get('lane'));phase=selected.get('phase','live')
 if lane is None:raise ValueError('selected lane is absent from the immutable pin')
 if phase!='live':
  if not isinstance(phase,str) or not phase.isdigit() or int(phase)>=len(lane.get('frames',[])):raise ValueError('selected phase is absent from the immutable pin')
  frame=lane['frames'][int(phase)];sha=frame.get('board_sha256') or frame['layout_sha256']
  # A server restart may not have replayed this old frame yet. The exact
  # immutable geometry is already on disk; never substitute a live board.
  if full:snapshot['selected_geometry']=cache.get(sha) or json.loads((root/'geometry'/(sha+'.json')).read_text())
 rects=body.get('annotations',[])
 if not isinstance(rects,list) or len(rects)>500:raise ValueError('invalid rectangle list')
 for rect in rects:
  if len(rect['bounds'])!=4 or not all(type(v) in (int,float) and math.isfinite(v) and abs(v)<100000 for v in rect['bounds']):raise ValueError('invalid bounds')
 note=body.get('note','')
 if not isinstance(note,str):raise ValueError('invalid note')
 revision=body.get('draft_revision',0)
 if type(revision) is not int or revision<0:raise ValueError('invalid draft revision')
 return pin,snapshot,dict(pin_id=pin,run=summary['run'],annotations=rects,view=selected,note=note[:10000],draft_revision=revision)
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def misdirected(self):
  if host_allowed(self.headers.get('Host')):return False
  self.send({'error':'unexpected Host header (DNS-rebinding guard); add --allow-host NAME for another name of this server'},421);return True
 def send(self,obj,status=200):
  raw=obj if isinstance(obj,bytes) else json.dumps(obj,separators=(',',':')).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_GET(self):
  if self.misdirected():return
  path=self.path.split('?')[0]
  if path=='/api/component-cost':
   q=parse_qs(urlparse(self.path).query)
   try:return self.send(cost_service.request(q.get('event_id',[''])[0],q.get('ref',[None])[0]))
   except (ValueError,KeyError,IndexError,TypeError,FileNotFoundError) as ex:return self.send(dict(error=str(ex)),400)
  if path=='/api/schematic':
   q=parse_qs(urlparse(self.path).query);lane_id=q.get('lane',[''])[0];scope=q.get('scope',['auto'])[0]
   with lock:
    entry=state['lanes'].get(lane_id)
    if entry is None:return self.send({'error':'unknown lane'},404)
    refs=[p['ref'] for p in (entry.get('geometry') or {}).get('parts',[]) if isinstance(p.get('ref'),str)];source=lane_sources.get(lane_id)
   try:return self.send(schematic_service.request(lane_id,refs,source,'board' if scope=='board' else 'auto'))
   except (ValueError,KeyError,IndexError,TypeError,OSError) as ex:return self.send(dict(error=str(ex)),400)
  if path.startswith('/api/schematic/payload/'):
   try:raw=schematic_service.payload(path.rsplit('/',1)[-1])
   except ValueError as ex:return self.send({'error':str(ex)},400)
   except FileNotFoundError as ex:return self.send({'error':str(ex)},404)
   # Content-addressed by every build input: safe to cache in the browser.
   self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','private, max-age=86400, immutable');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  if path=='/api/source/index':
   if source_service is None:return self.send({'error':'no atopile source configured (--source-dir)'},503)
   try:raw=source_service.index_bytes()
   except Exception as ex:return self.send({'error':f'source index failed: {type(ex).__name__}: {ex}'[:400]},500)
   gz,etag=gzipped(raw)
   if self.headers.get('If-None-Match')==etag:
    self.send_response(304);self.send_header('ETag',etag);self.send_header('Cache-Control','no-cache');self.end_headers();return
   zipped='gzip' in (self.headers.get('Accept-Encoding') or '');body=gz if zipped else raw
   self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-cache');self.send_header('ETag',etag);self.send_header('Vary','Accept-Encoding')
   if zipped:self.send_header('Content-Encoding','gzip')
   self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if path=='/api/source/file':
   if source_service is None:return self.send({'error':'no atopile source configured (--source-dir)'},503)
   try:return self.send(source_service.file(parse_qs(urlparse(self.path).query).get('path',[''])[0]))
   except SourceNotFound as ex:return self.send({'error':str(ex)},404)
   except (ValueError,OSError) as ex:return self.send({'error':str(ex)},400)
  if path=='/api/notes' or path=='/api/notes/export':
   if notes is None:return self.send({'error':'the notes store is not available on this server'},404)
   q=parse_qs(urlparse(self.path).query)
   if path=='/api/notes':  # {rev, unchanged:true} when since == rev: the 4 s poll costs one stat
    try:since=int(q.get('since',[''])[0])
    except ValueError:since=None
    return self.send(notes.payload(since))
   fmt=q.get('format',['md'])[0]
   try:raw,ctype=notes.export_data(fmt)
   except ValueError as ex:return self.send({'error':str(ex)},400)
   self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-store');self.send_header('Content-Disposition',f'inline; filename="design-notes.{fmt}"');self.send_header('X-Content-Type-Options','nosniff')
   self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  if path=='/api/agent/conversations' or path.startswith('/api/agent/conversations/'):
   if agent_service is None:return self.send({'error':'the assistant is disabled on this server (--agent off)'},503)
   if path=='/api/agent/conversations':return self.send(agent_service.conversations())
   try:return self.send(agent_service.conversation(path[len('/api/agent/conversations/'):]))
   except ValueError as ex:return self.send({'error':str(ex)},400)
   except KeyError:return self.send({'error':'no such conversation'},404)
  if path=='/api/agent/status':
   return self.send(agent_status())
  # 3D: ?peek=1 reports without enqueueing ('idle'): the pane enqueues once its board has stayed put for a moment
  if path=='/api/3d' or path=='/api/3d/status':
   if viewer3d is None:return self.send(dict(status='unavailable',error='3D view disabled on this server (--viewer3d off)'))
   if path=='/api/3d/status':return self.send(viewer3d.status())
   q=parse_qs(urlparse(self.path).query);lane_id=q.get('lane',[''])[0];ph=q.get('phase',['live'])[0];sha=q.get('sha',[''])[0]
   if sha and not re.fullmatch('[a-f0-9]{64}',sha):return self.send({'error':'invalid sha'},400)
   with lock:
    l=state['lanes'].get(lane_id);boards=[(lane_meta.get(lane_id) or {}).get('board'),lane_sources.get(lane_id)]
    if not sha and l is not None:sha=l.get('board_sha256') if ph=='live' else l['frames'][int(ph)].get('board_sha256') if ph.isdigit() and int(ph)<len(l.get('frames') or []) else None
   if not sha and l is None:return self.send({'status':'unavailable','error':'unknown lane'},404)
   # sha from the browser (pinned/phase state) or the lane: the immutable boards/<sha> copy, else the lane's board if it still hashes to sha
   return self.send(viewer3d.request(sha or '',[root/'boards'/(sha+'.kicad_pcb') if sha else None,*boards],retry=q.get('retry',[''])[0]=='1',peek=q.get('peek',[''])[0]=='1'))
  if path.startswith('/api/3d/glb/'):
   if viewer3d is None:return self.send({'error':'3D view disabled'},404)
   try:raw,enc=viewer3d.glb(path.rsplit('/',1)[-1],'gzip' in (self.headers.get('Accept-Encoding') or ''))
   except ValueError as ex:return self.send({'error':str(ex)},400)
   except FileNotFoundError as ex:return self.send({'error':str(ex)},404)
   # content addressed (placement fingerprint + export version): immutable in the browser
   self.send_response(200);self.send_header('Content-Type','model/gltf-binary');self.send_header('Cache-Control','private, max-age=86400, immutable');self.send_header('Vary','Accept-Encoding')
   if enc:self.send_header('Content-Encoding',enc)
   self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  if path=='/api/controls':
   return self.send(dict(requested=read_controls(root/'control.json'),active=state.get('active_controls'),limits=LIMITS,max_total_workers=16))
  if path=='/api/state':
   return self.send(state_response(parse_qs(urlparse(self.path).query)))
  if path.startswith('/api/geometry/'):
   sha=path.rsplit('/',1)[-1]
   if not re.fullmatch('[a-f0-9]{64}',sha):return self.send({'error':'not found'},404)
   if sha not in cache:
    f=root/'geometry'/(sha+'.json')
    if not f.exists():return self.send({'error':'not found'},404)
    cache[sha]=json.loads(f.read_text())
   return self.send(cache[sha])
  for prefix,directory in (('/api/pins/','pins'),('/api/drafts/','drafts')):
   if path.startswith(prefix):
    name=path[len(prefix):]
    if not re.fullmatch('[a-f0-9]{32}',name):return self.send({'error':'invalid id'},400)
    f=root/directory/(name+'.json')
    return self.send(json.loads(f.read_text())) if f.exists() else self.send({'error':'not found'},404)
  if path.startswith('/api/snapshots/'):
   name=path.rsplit('/',1)[-1]
   if not re.fullmatch('[a-f0-9]{32}',name):return self.send({'error':'invalid id'},400)
   p=root/'snapshots'/(name+'.json')
   return self.send(json.loads(p.read_text())) if p.exists() else self.send({'error':'not found'},404)
  f=assets/('index.html' if path=='/' else path.lstrip('/'))
  if not f.resolve().is_relative_to(assets.resolve()) or not f.is_file():return self.send({'error':'not found'},404)
  # pinned third-party bundles (elk, vendor/three) may be cached; our own files never are
  raw=f.read_bytes();self.send_response(200);self.send_header('Cache-Control','private, max-age=86400' if f.name=='elk.bundled.js' or path.startswith('/vendor/') else 'no-store');self.send_header('Content-Type','text/html' if f.suffix=='.html' else 'application/javascript' if f.suffix in ('.js','.mjs') else 'text/css' if f.suffix=='.css' else 'text/plain; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_POST(self):
  if self.misdirected():return
  # The assistant and every notes write need an allowlisted Origin; a missing one is rejected too (other POSTs allow it).
  if self.path=='/api/agent/chat' and self.headers.get('Origin') not in sse_origins():return self.send({'error':'origin required for the assistant'},403)
  if (self.path=='/api/notes' or self.path.startswith('/api/notes/')) and self.headers.get('Origin') not in sse_origins():return self.send({'error':'origin required for notes'},403)
  if self.headers.get('Origin') not in [None,*sse_origins()]:return self.send({'error':'origin rejected'},403)
  size=int(self.headers.get('Content-Length',0))
  if size>1000000:return self.send({'error':'request too large'},413)
  try:
   body=json.loads(self.rfile.read(size) or '{}')
   if not isinstance(body,dict):return self.send({'error':'invalid body'},400)
   if self.path=='/api/agent/chat':return self.chat(body)
   if self.path=='/api/notes' or self.path.startswith('/api/notes/'):return self.notes_write(body)
   if self.path=='/api/agent/cancel':
    if agent_service is None:return self.send({'error':'assistant disabled'},503)
    return self.send(agent_service.cancel(body.get('session')))
   if self.path=='/api/controls':
    with lock:
     updated=save_settings(root/'control.json',preferences,body['values'],body.get('expected_revision'),body.get('apply_mode','boundary'));state['controls']=updated;state['revision']+=1
     folder=root/'control-history';folder.mkdir(exist_ok=True);(folder/(str(updated['revision'])+'.json')).write_text(json.dumps(updated,indent=2))
    return self.send(updated)
   if self.path=='/api/pin':
    key=uuid.uuid4().hex;snapshot=current();folder=root/'pins';folder.mkdir(exist_ok=True);(folder/(key+'.json')).write_text(json.dumps(snapshot));return self.send(dict(pin_id=key,state=snapshot))
   if self.path in ('/api/draft','/api/snapshot'):
    pin,snapshot,payload=read_annotation(body,full=self.path=='/api/snapshot')
    if self.path=='/api/draft':
     path=root/'drafts'/(pin+'.json')
     with lock:
      old=json.loads(path.read_text()) if path.exists() else {}
      if old.get('draft_revision',-1)>payload['draft_revision']:return self.send({'error':'newer draft already saved'},409)
      draft=dict(payload,schema='pnr-annotation-draft-v1',updated_at=time.time())
      write_json_atomic(path,draft)
     return self.send(dict(pin_id=pin,draft_revision=payload['draft_revision'],path=str(path)))
    key=uuid.uuid4().hex;bundle=dict(schema='pnr-annotated-snapshot-v1',id=key,created_at=time.time(),coordinate_frame='mm-y-up',state=snapshot,annotations=payload['annotations'],view=payload['view'],note=payload['note'])
    dest=root/'snapshots'/(key+'.json');write_json_atomic(dest,bundle)
    return self.send(dict(id=key,path=str(dest),url='/api/snapshots/'+key))
   self.send({'error':'not found'},404)
  except (ValueError,KeyError,IndexError,TypeError,AttributeError,FileNotFoundError) as ex:self.send({'error':str(ex)},400)
 def notes_write(self,body):
  """POST /api/notes (create), /api/notes/<id> ({fields}, {comment}, expect_rev), /api/notes/<id>/delete. Always a user actor:
  the assistant writes only through its MCP server, so nothing here can act as the agent (or set its provenance)."""
  if notes is None:return self.send({'error':'the notes store is not available on this server'},404)
  m=re.fullmatch(r'/api/notes(?:/(N-\d{4,6})(/delete)?)?',self.path)
  if not m:return self.send({'error':'not found'},404)
  actor=dict(kind='user',remote=self.client_address[0])
  try:
   if m[2]:return self.send(dict(ok=True,**notes.delete(m[1],actor)))
   n=notes.request(m[1],body,actor) if m[1] else notes.create(body,actor)
   return self.send(dict(ok=True,rev=notes.rev,note=n))
  except NoteConflict as ex:return self.send({'error':str(ex)},409)  # subclasses ValueError
  except PermissionError as ex:return self.send({'error':str(ex)},403)
  except NoteNotFound as ex:return self.send({'error':str(ex)},404)
  except ValueError as ex:return self.send({'error':str(ex)},400)
 def chat(self,body):
  """SSE: one CLI turn on this request thread (ThreadingHTTPServer: other requests keep flowing)."""
  if agent_service is None:return self.send({'error':'the assistant is disabled on this server (--agent off)'},503)
  agent_service._validate(body)  # ValueError -> 400 before the stream starts; chat() validates again
  self.send_response(200);self.send_header('Content-Type','text/event-stream; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Accel-Buffering','no');self.send_header('Connection','close');self.end_headers()
  self.close_connection=True
  def emit(event,data):
   try:self.wfile.write(f'event: {event}\ndata: {json.dumps(data,separators=(",",":"))}\n\n'.encode());self.wfile.flush();return True
   except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,OSError):return False
  sock=self.connection
  def gone():
   """The browser closed the stream: the socket reads as EOF (the request body was already consumed)."""
   try:
    if not select.select([sock],[],[],0)[0]:return False
    return sock.recv(1,socket.MSG_PEEK|socket.MSG_DONTWAIT)==b''
   except (BlockingIOError,InterruptedError):return False
   except (OSError,ValueError):return True
  agent_service.chat(body,emit,gone=gone)
class Server(ThreadingHTTPServer):request_queue_size=64  # default 5: a page load's parallel fetches were reset under heavy machine load
def stop(signum,frame):
 # Kill a running KiCad extractor and drop its temporary file (a SIGKILLed viewer leaves them to the next sweep).
 for proc,tmp in list(extracting.items()):
  try:proc.kill()
  except OSError:pass
  try:tmp.unlink(missing_ok=True)
  except OSError:pass
 if agent_service:agent_service.shutdown()
 if viewer3d:viewer3d.shutdown()  # kills a running export's process group (kicad-cli included)
 os._exit(128+signum)
for sig in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT):signal.signal(sig,stop)
(root/'geometry').mkdir(exist_ok=True);sweep_tmp(root/'geometry')
servers=[Server((host,a.port),Handler) for host in listen_hosts]
a.port=servers[0].server_address[1]  # --port 0: origin allowlist uses the bound port
threading.Thread(target=ingest,daemon=True).start()
for server in servers[:-1]:threading.Thread(target=server.serve_forever,daemon=True).start()
for server in servers:print(f'Live PnR: http://{server.server_address[0]}:{server.server_address[1]}',flush=True)
if net_summary_status['state']=='pending':threading.Thread(target=net_summaries,daemon=True).start()
if agent_service:threading.Thread(target=agent_service.web_status,daemon=True).start()  # WebFetch guard self-test off the first /api/agent/status request
print(f'notes: {notes_dir}' if notes else 'notes: off',file=sys.stderr,flush=True)
servers[-1].serve_forever()
