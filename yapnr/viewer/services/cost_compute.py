from pathlib import Path
import sys,json,yaml,time,hashlib,os
runtime=Path(os.environ['PNR_COST_RUNTIME']).resolve();sys.path.insert(0,str(runtime))
from pnr.graph import BoardGraph
from pnr.constraints import compile_constraints
from pnr.place.cost_inspect import Objective
request=json.loads(Path(sys.argv[1]).read_text());context=request['context'];start=time.monotonic()
for p,h in context['hashes'].items():assert hashlib.sha256(Path(p).read_bytes()).hexdigest()==h,'Context changed'
g=BoardGraph.from_json(Path(request['graph']).read_text());raw=yaml.safe_load(Path(context['constraints']).read_text());holes={v['name'] for v in raw.get('mounting_hole',[])};g.components=[c for c in g.components if c.ref not in holes]
for net in g.nets:net.pins=[p for p in net.pins if p[0] not in holes]
cc=compile_constraints(raw,g.refs,{c.address:c.ref for c in g.components if c.address},{f'{c.address}:{p.name}':p.net for c in g.components for p in c.pads if c.address})
if context.get('annotation_sources'):
 from pnr.capacitor_intent import annotations,resolve,placement_constraints
 cc=placement_constraints(cc,resolve(annotations(context['annotation_sources']),g.components))
m=Objective(g,cc,parameters=context['parameters']);report=m.report();report.update(context=context,board_sha256=request['board_sha256'],event_id=request['event_id'],model_sha256=hashlib.sha256((runtime/'pnr/place/cost_inspect.py').read_bytes()).hexdigest())
if request.get('ref'):
 ref=request['ref'];report['component']=report['components'][ref];report['field']=m.field(ref,1.5)
report['seconds']=time.monotonic()-start
p=Path(sys.argv[2]);temp=p.with_suffix('.tmp');temp.write_text(json.dumps(report,separators=(',',':')));temp.replace(p)
