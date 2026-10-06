"""Correctness of the board canvas's rendering performance work (static/app.js: the spatial grid,
viewport culling, via level-of-detail and the gesture raster cache -- see docs/viewer.md
"Rendering and performance"), against a real headless Chrome and a real viewer server, on a
synthetic dense board (thousands of ground-stitching-style vias on a predictable grid; see
yapnr.viewer.testing.synthetic_via_board).

Two properties must hold exactly, not just approximately:

- boardHit() (hover/click/the cost panel) must return exactly what the old, unaccelerated linear
  scan over every track and via would have returned, at every query point -- the grid only narrows
  *which* candidates are checked, never *how*. bruteHit() below is that original scan, copied
  verbatim into the page and run alongside boardHit() for many points per test.
- The gesture fast path (fastFrame(): blit the offscreen raster cache, then the live dynamic
  overlay) must reproduce full-quality render() pixel-for-pixel at the view the cache was built
  from -- checked at three scales chosen to exercise each via level-of-detail tier (full ring+hole,
  plain dot, cell-aggregated dot; see paintBoard()'s comment in static/app.js).

Skipped, not failed, when no Chrome/Chromium is on the machine (chrome_binary() is None), same as
its siblings. Run explicitly: bazel test //tests/e2e/viewer:test_viewer_perf
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from yapnr.viewer.testing import (
    chrome_binary,
    new_chrome_page,
    seed_board_event,
    start_chrome,
    start_viewer,
    stop,
    synthetic_via_board,
    via_at,
    viewer_dist_dir,
    wait_for,
)

N_VIAS = 1200
BRUTE_HIT_JS = """
window.__bruteHit = function(q) {
  let g = geo(); if (!g) return null;
  let tol = 2 / view.scale, vis = p => p.layers.some(l => layers.has(l)), best = null;
  for (let part of g.parts || [])
    for (let p of part.pads || [])
      if (p.number && vis(p) && p.size?.every(Number.isFinite) && padContains(p, q, tol))
        return {kind: 'pad', ref: part.ref, pad: p.number, net: p.net};
  for (let part of g.parts || []) {
    if (part.pads?.length && !part.pads.some(vis)) continue;
    let b = partBounds(part);
    if (b && q[0] >= b[0] - .3 && q[0] <= b[2] + .3 && q[1] >= b[1] - .3 && q[1] <= b[3] + .3) {
      let area = (b[2] - b[0]) * (b[3] - b[1]);
      if (!best || area < best.area) best = {kind: 'component', ref: part.ref, area};
    }
  }
  if (best) return best;
  for (let t of g.tracks || []) {
    if (!t[0] || !layers.has(t[1])) continue;
    let a = t[2], b = t[3], dx = b[0] - a[0], dy = b[1] - a[1];
    let f = Math.max(0, Math.min(1, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)));
    if (Math.hypot(q[0] - a[0] - f * dx, q[1] - a[1] - f * dy) < tol + t[4] / 2)
      return {kind: 'track', net: t[0]};
  }
  for (let v of g.vias || [])
    if (v.net && Math.hypot(q[0] - v.xy[0], q[1] - v.xy[1]) < v.diameter / 2 + tol)
      return {kind: 'via', net: v.net};
  for (let l of ['F.Cu', 'B.Cu', 'In1.Cu', 'In2.Cu'])
    if (layers.has(l))
      for (let z of g.zones || [])
        if (z.layer === l && z.net && z.paths.filter(path => inPoly(q, path)).length % 2)
          return {kind: 'zone', net: z.net};
  return null;
};
"""


@unittest.skipUnless(chrome_binary() is not None, "no Chrome/Chromium found (see chrome_binary())")
class ViewerPerfCorrectnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viewer-perf-e2e-")
        root = Path(self.tmp.name) / "live"
        root.mkdir()
        self.geo = synthetic_via_board(n_vias=N_VIAS)
        seed_board_event(root, self.geo, candidate="mc0")
        self.viewer_proc, self.base_url = start_viewer(root, "--dist", str(viewer_dist_dir()))
        wait_for(lambda: self._state_has_geometry(), 20)
        self.profile_dir = Path(self.tmp.name) / "chrome-profile"
        self.chrome_proc, self.devtools_url = start_chrome(self.profile_dir)
        self.page = new_chrome_page(self.devtools_url)
        self.page.call("Page.enable")
        self.page.call("Page.navigate", {"url": self.base_url + "/"})
        self.page.wait_event("Page.loadEventFired", timeout=15)
        wait_for(lambda: self.page.eval("!!(geo() && geo().vias.length)") or None, 10)
        self.page.eval(BRUTE_HIT_JS)

    def tearDown(self):
        self.page.close()
        self.chrome_proc.terminate()
        try:
            self.chrome_proc.wait(10)
        except Exception:
            self.chrome_proc.kill()
            self.chrome_proc.wait(10)
        stop(self.viewer_proc)
        try:
            self.tmp.cleanup()
        except OSError:
            shutil.rmtree(self.tmp.name, ignore_errors=True)

    def _state_has_geometry(self):
        import urllib.request

        with urllib.request.urlopen(self.base_url + "/api/state?lane=mc0", timeout=5) as r:
            data = json.loads(r.read().decode())
        return bool(data.get("lanes", {}).get("mc0", {}).get("geometry"))

    def hit(self, x, y):
        return json.loads(self.page.eval(f"JSON.stringify(boardHit([{x},{y}]))"))

    def brute(self, x, y):
        return json.loads(self.page.eval(f"JSON.stringify(window.__bruteHit([{x},{y}]))"))

    def set_view(self, scale, x=200.0, y=200.0):
        self.page.eval(f"view={{scale:{scale},x:{x},y:{y}}};render();true")

    # ------------------------------------------------------------------ hit-test parity
    def test_hit_test_matches_brute_force_at_known_points(self):
        # The oracle is bruteHit() (the unmodified linear-scan algorithm, copied verbatim into the
        # page): these hand-picked points only need to land on a specific *kind* of feature (a
        # via's centre, for instance, is also a track endpoint -- tracks win there in both the old
        # and new code, by priority order, same as a brute-force scan would say), so the real
        # assertion is new-matches-brute, not a hardcoded expectation per point.
        self.set_view(25)
        g = self.geo
        points = []
        for i, j in [
            (0, 0),
            (5, 5),
            (g["cols"] // 2, g["rows"] // 2),
            (g["cols"] - 1, g["rows"] - 1),
        ]:
            if i < g["cols"] and j < g["rows"] and j * g["cols"] + i < len(g["vias"]):
                v = via_at(g, i, j)
                points.append(tuple(v["xy"]))
        t = g["tracks"][0]
        points.append(((t[2][0] + t[3][0]) / 2 + 0.01, (t[2][1] + t[3][1]) / 2))  # a track midpoint
        p = g["parts"][0]["pads"][0]
        points.append(tuple(p["xy"]))  # a pad centre
        points.append((g["width"] / 2, g["height"] - 0.5))  # inside the zone, nothing else there
        for x, y in points:
            h, b = self.hit(x, y), self.brute(x, y)
            self.assertEqual(
                (h or {}).get("kind"), (b or {}).get("kind"), f"at ({x},{y}): new={h} brute={b}"
            )
            self.assertEqual(
                (h or {}).get("net"), (b or {}).get("net"), f"at ({x},{y}): new={h} brute={b}"
            )
        # and a definite miss: just outside the zone's inset, past every via/track/pad too.
        h = self.hit(g["width"] - 0.1, 0.1)
        b = self.brute(g["width"] - 0.1, 0.1)
        self.assertEqual(h, b)

    def test_hit_test_matches_brute_force_on_a_grid_sweep(self):
        # A dense sweep (not just hand-picked points) at a zoom where vias collapse to the
        # dot/cell-aggregated LoD tiers -- the tier only changes what is *drawn*, never what
        # boardHit() (always on true geometry) returns, so this must still match exactly.
        self.set_view(2.5)
        g = self.geo
        mismatches = []
        for xi in range(0, 24):
            for yi in range(0, 18):
                x, y = xi * (g["width"] / 24), yi * (g["height"] / 18)
                h, b = self.hit(x, y), self.brute(x, y)
                if (h or {}).get("kind") != (b or {}).get("kind") or (h or {}).get("net") != (
                    b or {}
                ).get("net"):
                    mismatches.append((x, y, h, b))
        self.assertEqual(mismatches, [])

    # ------------------------------------------------------------------ gesture-cache pixel fidelity
    def test_fast_path_matches_full_render_at_each_lod_tier(self):
        # diameter 0.4mm * scale: 40 -> 16px (full ring+hole), 2.5 -> 1.0px (plain dot),
        # 0.9 -> 0.36px (cell-aggregated dot). Every tier must blit identically to a full render.
        for scale in (40, 2.5, 0.9):
            result = json.loads(
                self.page.eval(
                    f"""(()=>{{
                      view={{scale:{scale},x:250,y:250}};
                      gestureCacheDirty=true; render();
                      let a=ctx.getImageData(0,0,canvas.width,canvas.height).data;
                      fastFrame();
                      let b=ctx.getImageData(0,0,canvas.width,canvas.height).data;
                      let diff=0,max=0;
                      for(let i=0;i<a.length;i+=4){{
                        let d=Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])
                              +Math.abs(a[i+2]-b[i+2])+Math.abs(a[i+3]-b[i+3]);
                        if(d>8){{diff++;if(d>max)max=d}}
                      }}
                      return JSON.stringify({{diff,max,total:a.length/4}});
                    }})()"""
                )
            )
            frac = result["diff"] / result["total"]
            self.assertLess(frac, 0.005, f"scale={scale}: {result}")

    # ------------------------------------------------------------------ overlay hooks stay live
    def test_fast_path_keeps_net_highlight_live(self):
        # Regression guard: schPcbOverlay() (schematic.js) draws the net/focus highlight+dimming
        # as an app.js overlay hook (see renderOverlay()'s overlayHooks loop) specifically so it
        # survives a gesture's fastFrame(), not just a full render(). This failed before that fix:
        # fastFrame() only redrew app.js's own renderOverlay() content, so a highlight (and the
        # note badges, cost field) vanished on every pan/pinch/zoom frame and only reappeared once
        # the gesture settled and a full render() fired ~150ms later.
        self.set_view(20)
        v = via_at(self.geo, 1, 0)  # (i+j)%5 == 1 -> "GND" (see synthetic_via_board())
        self.assertEqual(v["net"], "GND")
        self.page.eval("window.YapnrView.highlight({nets:['GND']},{frame:false});true")

        def px():
            return json.loads(
                self.page.eval(
                    f"(()=>{{let [sx,sy]=screen([{v['xy'][0]},{v['xy'][1]}]);"
                    "let d=ctx.getImageData(Math.round(sx),Math.round(sy),1,1).data;"
                    "return JSON.stringify([d[0],d[1],d[2]]);})()"
                )
            )

        full = px()  # full render(): highlight pink, per schPcbOverlay/drawViewHl via HL color
        self.assertGreater(full[0], 200, f"expected highlight pink, got {full}")  # '#ff7ad9'-ish
        # Pan a touch -- a real gesture frame, not a settled one -- and check the same spot again.
        self.page.eval("view={...view,x:view.x+3,y:view.y+2};fastFrame();true")
        mid_gesture = px()
        self.assertGreater(
            mid_gesture[0], 200, f"highlight disappeared during a gesture frame: {mid_gesture}"
        )

    def test_fast_path_does_not_redraw_vias_per_frame(self):
        # A regression guard for the actual perf win, not just pixel fidelity: a full render()
        # never rebuilds the cache inline (it only marks it stale and schedules a build for idle
        # time, so the next gesture starts from a ready cache); every frame of a gesture then
        # reuses it unchanged.
        self.set_view(20)
        wait_for(lambda: self.page.eval("!gestureCacheDirty&&!!gestureCache") or None, 10)
        built = self.page.eval("gestureCache.x0+':'+gestureCache.scale0")
        self.page.eval("fastFrame();")
        self.page.eval("view={...view,x:view.x+5};fastFrame();")
        self.assertFalse(self.page.eval("gestureCacheDirty"))
        self.assertEqual(self.page.eval("gestureCache.x0+':'+gestureCache.scale0"), built)


if __name__ == "__main__":
    unittest.main()
