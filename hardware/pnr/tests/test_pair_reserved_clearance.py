import unittest
# src15: no wx.App here - it registered KiCad's Python.app as a Foreground (Dock) app and the
# Oracle checks do not need it (the other native_electrical tests run without one).
import pcbnew as k
from test_native_electrical import board,pad
from pnr.native_electrical import Oracle

class PairReservedTest(unittest.TestCase):
 def setUp(self):
  self.b=board()
  for n in ('p','n','other'):pad(self.b,n,'1',n,(15,15),(.2,.2))
  self.r=dict(fab=dict(clearance_mm=.15))
  self.p=dict(p='p',n='n',gap_mm=.15)
 def test_nominal_mate_spacing_valid_but_generic_guard_preserved(self):
  o=Oracle(self.b,self.r);o.reserve_track('n',k.B_Cu,(3,3.35),(8,3.35),.2)
  self.assertFalse(o.clear('p',k.B_Cu,(5,3),(6,3),.2))
  self.assertTrue(o.clear('p',k.B_Cu,(5,3),(6,3),.2,pair=self.p))
  self.assertFalse(o.clear('p',k.B_Cu,(5,3.001),(6,3.001),.2,pair=self.p))
  self.assertFalse(o.clear('p',k.B_Cu,(5,3),(5,4),.2,pair=self.p))
 def test_unrelated_reserved_track_and_actual_pad_keep_guard(self):
  o=Oracle(self.b,self.r);o.reserve_track('other',k.B_Cu,(3,3.35),(8,3.35),.2)
  self.assertFalse(o.clear('p',k.B_Cu,(5,3),(6,3),.2,pair=self.p))
  pad(self.b,'PAD','1','n',(5,6.35),(.2,.2));o=Oracle(self.b,self.r)
  self.assertFalse(o.clear('p',k.F_Cu,(5,6),(6,6),.2,pair=self.p))
 def test_fork_reservations_do_not_leak(self):
  o=Oracle(self.b,self.r);fork=o.fork();fork.reserve_track('n',k.B_Cu,(3,3),(8,3),.2)
  self.assertFalse(fork.clear('p',k.B_Cu,(5,3),(6,3),.2,pair=self.p));self.assertTrue(o.clear('p',k.B_Cu,(5,3),(6,3),.2,pair=self.p))
if __name__=='__main__':unittest.main(verbosity=2)
