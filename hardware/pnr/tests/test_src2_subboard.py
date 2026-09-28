"""Sub-board fidelity for hierarchical block evaluation (pnr.hier)."""
import copy,json,os,unittest
from unittest import mock
from pnr.constraints import BoardSpec,CompiledConstraints
from pnr.electrical import compile_policy,current_width,net_policy
from pnr.graph import BoardGraph,BoardOutline,Component,Net,Pad
from pnr.hier.blocks import Block,sub_board
from pnr.hier.native_block import with_apron
from pnr.hier.synth import instance_board

FAB=dict(outer_copper_oz=1,inner_copper_oz=1,delta_t_c=40,via_drill_mm=.3,via_diameter_mm=.6,min_via_plating_um=20,board_thickness_mm=1.6,copper_resistivity_ohm_mm=2.1e-5,via_barrel_loss_budget_w=.01,via_array_peak_drop_v=.01)
PARENT_SRC=dict(path='/parent/0-x.ato',line=7,sha256='abc')
LOCAL_SRC=dict(path='/sub/0-x.ato',line=7,sha256='abc')


def comp(ref,address,pads,pos=(5,5)):
    return Component(ref=ref,footprint='fp',pos=pos,rot=0,side='top',courtyard=(1,1),bbox=(1,1),pads=[Pad(n,net,off) for n,net,off in pads],address=address)


def board():
    g=BoardGraph(name='t',outline=BoardOutline(50,50))
    g.components=[comp('U1','board.blk.ic',[('1','rail',(0,.5)),('2','gnd',(0,-.5)),('3','dp',(.5,0)),('4','dn',(-.5,0)),('5','lp',(.5,.5)),('6','ln',(-.5,.5))]),
                  comp('C1','board.blk.cap',[('1','lp',(.4,.3)),('2','gnd',(-.4,-.3)),('3','ln',(0,.2))]),
                  comp('X9','board.conn',[('1','rail',(0,0)),('2','dp',(1,0)),('3','dn',(2,0)),('4','far',(3,0))],pos=(30,30))]
    pins={}
    for c in g.components:
        for p in c.pads:pins.setdefault(p.net,[]).append((c.ref,p.name))
    g.nets=[Net(n,i+1,v) for i,(n,v) in enumerate(sorted(pins.items()))]
    return g


def parent_rules():
    full=compile_policy(dict(fab={'track_width_mm':.2},net_classes=[dict(nets=['rail'],width_mm=.5)]),
                        [dict(scope='net',ref='X9',net='rail',rms_current_a=5,peak_current_a=5,source=PARENT_SRC),
                         dict(scope='net',ref='X9',net='far',rms_current_a=3,peak_current_a=3,source=dict(PARENT_SRC,line=8))],FAB)
    full.update(diff_pairs=[dict(name='usb',p='dp',n='dn',terminal_chain=[{'p':'X9.2','n':'X9.3'},{'p':'U1.3','n':'U1.4'}]),
                            dict(name='loc',p='lp',n='ln',terminal_chain=[{'p':'U1.5','n':'U1.6'},{'p':'C1.1','n':'C1.3'}])],
                length_match=[dict(name='a',nets=['lp','ln']),dict(name='b',nets=['lp','far'])],
                routed_pair_references=[dict(pair='loc',segments=[dict(reference_paths=[[[0,0],[1,1]]])])],
                current_intents=[dict(scope='terminal',ref='U1',net='gnd',pads=['2']),dict(scope='terminal',ref='X9',net='far',pads=['4'])])
    return full


BLOCK=Block(name='board.blk',refs=['C1','U1'],prefix='board.blk',external_nets=['dn','dp','rail'])


class SubBoardTest(unittest.TestCase):
    def cut(self):
        return sub_board(board(),CompiledConstraints(board=BoardSpec(50,50)),parent_rules(),BLOCK,10,8)

    def test_rules_keep_only_what_the_block_can_resolve(self):
        g,_,r=self.cut()
        self.assertEqual(sorted(r['electrical_nets']),['rail'])  # 'far' has no pins here
        self.assertEqual([d['name'] for d in r['diff_pairs']],['loc'])  # usb chain leaves the block
        self.assertEqual([m['name'] for m in r['length_match']],['a'])
        self.assertEqual(r['routed_pair_references'],[])  # parent-frame copper witnesses
        self.assertEqual([i['ref'] for i in r['current_intents']],['U1'])
        self.assertEqual(r['block_ports'],['dn','dp','rail'])
        self.assertNotIn('length_match',sub_board(board(),CompiledConstraints(board=BoardSpec(50,50)),dict(fab={}),BLOCK,10,8)[2])

    def test_subboard_policy_equals_parent_policy_on_present_nets(self):
        _,_,r=self.cut()
        # The block itself declares a smaller envelope on the shared rail.
        local=[dict(scope='net',ref='U1',net='rail',rms_current_a=2,peak_current_a=2,source=LOCAL_SRC)]
        with mock.patch.dict(os.environ,{'PNR_SUBBOARD':'1'}):
            sub=compile_policy(r,local,FAB)
        parent=parent_rules()['electrical_nets']['rail']
        got=sub['electrical_nets']['rail']
        self.assertEqual({k:v for k,v in got.items() if k!='sources'},{k:v for k,v in parent.items() if k!='sources'})
        self.assertEqual(got['sources'],[PARENT_SRC])  # same annotation line cited once
        self.assertAlmostEqual(got['inner_width_mm'],current_width(5,1,40,False))
        self.assertEqual(net_policy('rail',sub)['mode'],'power')
        self.assertEqual(sub['current_intents'],local)
        # A distinct local declaration still widens the envelope and is cited.
        more=[dict(local[0],rms_current_a=6,peak_current_a=6,source=dict(LOCAL_SRC,line=9))]
        with mock.patch.dict(os.environ,{'PNR_SUBBOARD':'1'}):
            wide=compile_policy(r,more,FAB)['electrical_nets']['rail']
        self.assertEqual((wide['rms_current_a'],len(wide['sources'])),(6,2))

    def test_full_board_policy_ignores_incoming_nets(self):
        _,_,r=self.cut()
        local=[dict(scope='net',ref='U1',net='rail',rms_current_a=2,peak_current_a=2,source=LOCAL_SRC)]
        with mock.patch.dict(os.environ,{},clear=False):
            os.environ.pop('PNR_SUBBOARD',None)
            full=compile_policy(r,local,FAB)
        self.assertEqual(full['electrical_nets']['rail']['rms_current_a'],2)
        self.assertEqual(full['electrical_nets']['rail']['sources'],[LOCAL_SRC])

    def test_apron_keeps_block_rectangle_and_grows_outline(self):
        g,_,_=self.cut()
        for c in g.components:c.pos=(1.5,2.5)
        before=g.to_json()
        a=with_apron(g,.5)
        self.assertEqual(g.to_json(),before)
        self.assertEqual((a.outline.width,a.outline.height),(11,9))
        self.assertEqual({c.pos for c in a.components},{(2.0,3.0)})
        self.assertEqual([p.offset for c in a.components for p in c.pads],[p.offset for c in g.components for p in c.pads])
        self.assertEqual(with_apron(g,0).to_json(),before)
        with self.assertRaises(ValueError):with_apron(g,-.1)

    def test_evaluate_writes_apron_board_and_matching_constraint_outline(self):
        import subprocess,tempfile,yaml
        from pathlib import Path
        import pnr.hier.native_block as nb
        g,_,r=self.cut()
        for c in g.components:c.pos=(1.5,2.5)
        calls=[]
        def run(cmd,**kw):
            calls.append(cmd);return subprocess.CompletedProcess(cmd,1)
        with tempfile.TemporaryDirectory() as d:
            inputs=Path(d)/'in';inputs.mkdir()
            for n in ('source.kicad_pro','fp-lib-table'):(inputs/n).write_text('x')
            doc={'board':{'outline':{'w':10.0,'h':8.0},'layers':4}}
            with mock.patch.object(nb.subprocess,'run',run),mock.patch.dict(os.environ,{'PNR_SUBBOARD_APRON_MM':'0.15'}):
                rec=nb.evaluate(Path(d)/'round',inputs,doc,g,r,1,1,Path(d))
                rec2=nb.evaluate(Path(d)/'round2',inputs,doc,g,r,1,1,Path(d),apron=0)
            placed=BoardGraph.from_json((Path(d)/'round/placed.json').read_text())
            con=yaml.safe_load((Path(d)/'round/constraints.yaml').read_text())
            self.assertEqual((rec['apron_mm'],rec['status'],rec2['apron_mm']),(.15,'failed',0))
            self.assertEqual((placed.outline.width,placed.outline.height),(10.3,8.3))
            self.assertEqual({c.pos for c in placed.components},{(1.65,2.65)})
            self.assertEqual(con['board'],{'outline':{'w':10.3,'h':8.3},'layers':4})
            self.assertEqual(doc['board']['outline'],{'w':10.0,'h':8.0})  # caller's doc untouched
            self.assertEqual((Path(d)/'round2/placed.json').read_text(),g.to_json())  # apron 0 == previous behaviour
            self.assertEqual(sorted(json.loads((Path(d)/'round/rules.json').read_text())['electrical_nets']),['rail'])
        self.assertEqual(len(calls),4)

    def test_default_apron_is_edge_clearance_so_copper_stays_in_block(self):
        import subprocess,tempfile
        from pathlib import Path
        import pnr.hier.native_block as nb
        g,_,r=self.cut()
        for c in g.components:c.pos=(1.5,2.5)
        calls=[]
        def run(cmd,**kw):
            calls.append(cmd);return subprocess.CompletedProcess(cmd,1)
        with tempfile.TemporaryDirectory() as d,mock.patch.object(nb.subprocess,'run',run),mock.patch.dict(os.environ):
            os.environ.pop('PNR_SUBBOARD_APRON_MM',None)
            d=Path(d);inputs=d/'in';inputs.mkdir()
            for n in ('source.kicad_pro','fp-lib-table'):(inputs/n).write_text('x')
            doc={'board':{'outline':{'w':10.0,'h':8.0}}}
            for fab,edge in (({},.2),({'edge_clearance_mm':.3},.3)):
                rules=dict(r,fab=dict(r['fab'],**fab))
                rec=nb.evaluate(d/'round',inputs,doc,g,rules,1,1,d)
                placed=BoardGraph.from_json((d/'round/placed.json').read_text())
                self.assertEqual(rec['apron_mm'],edge)
                # Copper kept `edge` inside Edge.Cuts is exactly the block rectangle.
                (x,y),=({c.pos for c in placed.components})
                self.assertAlmostEqual(x-edge,1.5);self.assertAlmostEqual(y-edge,2.5)
                self.assertAlmostEqual(placed.outline.width-2*edge,10);self.assertAlmostEqual(placed.outline.height-2*edge,8)
            self.assertEqual(len(calls),4)
            # A wider apron would put routable area outside the block: refused before any work.
            (d/'round/marker').write_text('keep')
            for kw,env in ((dict(apron=.25),{}),({},{'PNR_SUBBOARD_APRON_MM':'0.5'})):
                with mock.patch.dict(os.environ,env),self.assertRaisesRegex(ValueError,'exceeds the 0.2 mm edge clearance'):
                    nb.evaluate(d/'round',inputs,doc,g,r,1,1,d,**kw)
            self.assertEqual((d/'round/marker').read_text(),'keep')
            self.assertEqual(len(calls),4)

    def test_instance_board_mirrors_pads_when_side_changes(self):
        src=board();before=src.to_json()
        layout={'ic':[2,3,90,'top'],'cap':[6,4,0,'bottom']}
        g,_,_=instance_board(src,CompiledConstraints(board=BoardSpec(50,50)),parent_rules(),BLOCK,layout,10,8)
        by={c.ref:c for c in g.components}
        self.assertEqual((by['U1'].pos,by['U1'].rot,by['U1'].side),((2.0,3.0),90,'top'))
        self.assertEqual([p.offset for p in by['U1'].pads],[p.offset for p in src.component('U1').pads])
        self.assertEqual(by['C1'].side,'bottom')
        self.assertEqual([p.offset for p in by['C1'].pads],[(.4,-.3),(-.4,.3),(0,-.2)])
        self.assertEqual(src.to_json(),before)  # the parent graph is never mutated


if __name__=='__main__':
    unittest.main()
