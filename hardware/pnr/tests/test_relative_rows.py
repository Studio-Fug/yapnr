import unittest,math
from pnr.constraints import compile_constraints,ConstraintError
from pnr.graph import BoardGraph,BoardOutline,Component
from pnr.place.rows import sample_constraints,violations
from pnr.place.geometry import resolve_fixed_poses,resolve_hard_rotations,keepout_rects
class RowTest(unittest.TestCase):
 def fixture(self):
  g=BoardGraph('rows',[Component(r,'',(10+i*7,10),0,'top',(3,2),(3,2)) for i,r in enumerate(['SW1','SW2','J1'])],[],BoardOutline(50,40))
  d=dict(board=dict(outline=dict(w=50,h=40)),row=[dict(name='buttons',members=['SW1','SW2'],gap_mm=1,edge='any',facing='south'),dict(name='usb',members=['J1'],gap_mm=.2,edge='any',facing='south')]);return g,d
 def placed(self,g,c):
  poses=resolve_fixed_poses(g,c);rots=resolve_hard_rotations(c);p=BoardGraph.from_json(g.to_json())
  for ref,pos in poses.items():p.component(ref).pos=pos;p.component(ref).rot=rots[ref]
  return p
 def test_rows_cover_all_edges_and_have_no_source_origins(self):
  g,d=self.fixture();c=compile_constraints(d,g.refs);self.assertEqual(c.locked_refs,());seen=set()
  for seed in range(4):
   trial=sample_constraints(g,c,seed);p=self.placed(g,trial);self.assertEqual(violations(p,c),[])
   self.assertAlmostEqual(math.dist(p.component('SW1').pos,p.component('SW2').pos),4)
   lock=next(x for x in trial.constraints if x.kind=='fixed' and 'SW1' in x.refs);seen.add(lock.params['row_trial']['edge'])
  self.assertEqual(seen,{'north','south','east','west'});self.assertEqual(c.locked_refs,())
 def test_individual_button_or_inward_usb_move_rejected(self):
  g,d=self.fixture();c=compile_constraints(d,g.refs);p=self.placed(g,sample_constraints(g,c,0));x=p.component('SW1');x.pos=(x.pos[0]+.5,x.pos[1]+.5)
  self.assertIn('SW1',violations(p,c));p=self.placed(g,sample_constraints(g,c,0));x=p.component('J1');x.rot=(x.rot+180)%360;self.assertIn('J1',violations(p,c))
 def test_native_import_replays_row_pose_and_rejects_overlap(self):
  g,d=self.fixture();c=compile_constraints(d,g.refs);a=sample_constraints(g,c,7);b=sample_constraints(g,c,7);self.assertEqual(resolve_fixed_poses(g,a),resolve_fixed_poses(g,b))
  with self.assertRaises(ConstraintError):compile_constraints(dict(d,fixed={'SW1':{'at':[10,10]}}),g.refs)
 def test_attached_keepout_follows_translation_and_rotation(self):
  g,d=self.fixture();d['keepout']=[dict(ref='J1',extent=dict(edge='north',depth_mm=3))];c=compile_constraints(d,g.refs);j=g.component('J1');j.pos=(20,20);j.rot=90
  k=keepout_rects(g,c,{})[0];self.assertAlmostEqual(k.cx,17.5);self.assertAlmostEqual(k.cy,20);self.assertAlmostEqual(k.w,3);self.assertAlmostEqual(k.h,3)
if __name__=='__main__':unittest.main()
