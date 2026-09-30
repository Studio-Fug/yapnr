import unittest
from pnr.native_loop import electrical_search_bounds as bounds
class WidePower(unittest.TestCase):
 def test_pairs_keep_complete_chain_bounds(self):
  self.assertEqual(bounds('pair',1,[2,2,3,3],[0,0,10,10]),[0,0,10,10])
 def test_later_power_attempts_can_reach_global_tree(self):
  self.assertEqual(bounds('power',3,[2,2,3,3],[0,0,10,10],True),[0,0,10,10])
 def test_first_attempts_disabled_signal_and_plane_remain_local(self):
  for mode,attempt,enabled in [('power',1,True),('power',2,True),('power',3,False),('signal',8,True),('plane',8,True)]:
   self.assertEqual(bounds(mode,attempt,[2,2,3,3],[0,0,10,10],enabled),[2,2,3,3])
 def test_inputs_are_immutable(self):
  a=[0,0,10,10];b=bounds('power',3,[],a,True);b[0]=99;self.assertEqual(a[0],0)
