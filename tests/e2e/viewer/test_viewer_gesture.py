"""Touch drags on the board canvas stay smooth (static/app.js: gestureActive(), the deferred
render(), warmGestureCache() -- see docs/viewer.md "Rendering and performance").

A real headless Chrome with a phone profile and touch emulation, a real viewer server and a
synthetic dense board, driven like a finger: real touch events every 16ms, pauses of 200-400ms
mid-drag, several drags in a row, a live poll and a notes update that both ask for a render() while
a finger is down. Before the fix each of those was a hitch: every drag started by rebuilding the
gesture cache, any pause over 150ms fired a full render() mid-drag (which then made the next move
rebuild the cache again), and a live poll re-rendered mid-drag too. Checked here:

- no full render (paintFull) and no cache build while a finger is down;
- the drag starts from a cache built in idle time after the previous render;
- a render() asked for mid-gesture is not lost: it runs on release, and content changed
  mid-gesture (a layer toggle) reaches the cache afterwards;
- frame-time bounds at a modest CPU throttle (loose enough to stay stable on a loaded machine).

Skipped, not failed, when no Chrome/Chromium is on the machine. Run explicitly:
bazel test //tests/e2e/viewer:test_viewer_gesture
"""

import json
import shutil
import tempfile
import time
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
    viewer_dist_dir,
    wait_for,
)

N_VIAS = 3000
# Counters around the real functions (all page globals): full paints, cache builds and gesture
# frames, each tagged with whether a finger was down at the time (capture-phase listeners run
# before app.js's own handlers, so the release's own render() counts as after the gesture).
INSTRUMENT_JS = r"""
(()=>{const M=window.__m={paints:[],builds:[],frames:[],raf:[],down:false,ptr:0,gestures:[]};
 const now=()=>performance.now();
 const P=paintFull;paintFull=function(){let t=now();P();M.paints.push({t,d:now()-t,g:M.down})};
 const B=buildGestureCache;
 buildGestureCache=function(g){let t=now();B(g);M.builds.push({t,d:now()-t,g:M.down})};
 const F=fastFrame;fastFrame=function(){let t=now();F();M.frames.push({t,d:now()-t,g:M.down})};
 canvas.addEventListener('pointerdown',()=>{M.ptr++;M.down=true;M.gestures.push({t0:now()})},true);
 for(const ev of ['pointerup','pointercancel'])
  canvas.addEventListener(ev,()=>{
   M.ptr=Math.max(0,M.ptr-1);M.down=M.ptr>0;let g=M.gestures.at(-1);if(g)g.t1=now()},true);
 let last=null;
 const tick=ts=>{if(last!==null&&M.down)M.raf.push(ts-last);last=ts;requestAnimationFrame(tick)};
 requestAnimationFrame(tick);
 return true})()
"""


@unittest.skipUnless(chrome_binary() is not None, "no Chrome/Chromium found (see chrome_binary())")
class ViewerGestureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viewer-gesture-e2e-")
        root = Path(self.tmp.name) / "live"
        root.mkdir()
        seed_board_event(root, synthetic_via_board(n_vias=N_VIAS), candidate="mc0")
        self.viewer_proc, self.base_url = start_viewer(root, "--dist", str(viewer_dist_dir()))
        self.chrome_proc, self.devtools_url = start_chrome(Path(self.tmp.name) / "chrome-profile")
        self.page = new_chrome_page(self.devtools_url)
        self.page.call("Page.enable")
        self.page.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 412, "height": 915, "deviceScaleFactor": 2.625, "mobile": True},
        )
        self.page.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        self.page.call("Page.navigate", {"url": self.base_url + "/"})
        self.page.wait_event("Page.loadEventFired", timeout=15)
        wait_for(lambda: self.page.eval("!!(geo() && geo().vias.length)") or None, 20)
        self.page.eval(INSTRUMENT_JS)
        r = json.loads(self.page.eval("JSON.stringify(canvas.getBoundingClientRect())"))
        self.cx, self.cy = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2

    def tearDown(self):
        try:
            self.page.call("Emulation.setCPUThrottlingRate", {"rate": 1})
        except Exception:
            pass
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

    # ------------------------------------------------------------------ helpers
    def touch(self, kind, x=None, y=None):
        pts = [] if x is None else [{"x": x, "y": y, "id": 1}]
        self.page.call("Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})

    def set_zoom(self, factor):
        """Fit, zoom about the centre, full render; then wait for the idle cache build."""
        self.page.eval(
            "fit();{let w=canvas.clientWidth,h=canvas.clientHeight,wp=point(w/2,h/2),"
            f"s=view.scale*{factor};view={{scale:s,x:w/2-wp[0]*s,y:h/2+wp[1]*s}}}};render();true"
        )
        self.wait_cache_ready()

    def wait_cache_ready(self):
        wait_for(lambda: self.page.eval("!gestureCacheDirty&&!!gestureCache") or None, 10)

    def drag(self, dx, dy, ms, pauses=(), during=None):
        """One finger from the centre by (dx, dy) over `ms` of movement, a touchmove every 16ms,
        holding still (no events) for each (at_ms, for_ms) in `pauses`; `during(moved_ms)` runs
        between moves. Positions follow wall-clock time, like a finger would."""
        x0, y0 = self.cx, self.cy
        self.touch("touchStart", x0, y0)
        moved, last = 0.0, time.monotonic()
        while moved < ms:
            wait = last + 0.016 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            now = time.monotonic()
            step, last = (now - last) * 1000, now
            held = any(at <= moved < at + n for at, n in pauses)
            moved += step
            if during:
                during(moved)
            if held:
                continue
            f = min(moved, ms) / ms
            self.touch("touchMove", x0 + dx * f, y0 + dy * f)
        self.touch("touchEnd")

    def metrics(self):
        return json.loads(self.page.eval("JSON.stringify(window.__m)"))

    # ------------------------------------------------------------------ tests
    def test_realistic_drags_never_render_or_rebuild_mid_gesture(self):
        self.page.call("Emulation.setCPUThrottlingRate", {"rate": 2})
        polled = {"done": False, "notes": False}

        def during(moved):
            # a live poll that re-renders (revision reset: the next 1.2s poll renders) and a
            # notes update (yapnr:notes -> render()), both while the finger is down
            if not polled["done"]:
                self.page.eval("revision=-1;true")
                polled["done"] = True
            if moved > 900 and not polled["notes"]:
                self.page.eval("document.dispatchEvent(new CustomEvent('yapnr:notes'));true")
                polled["notes"] = True

        for zoom, (dx, dy) in [(1, (90, 60)), (4, (-120, 40)), (12, (60, -140)), (4, (100, 100))]:
            self.set_zoom(zoom)
            polled.update(done=False, notes=False)
            # ~2.4s with two holds: long enough for a 1.2s live poll to land mid-drag
            self.drag(dx, dy, 2400, pauses=((500, 300), (1500, 400)), during=during)
            time.sleep(0.3)
        self.page.call("Emulation.setCPUThrottlingRate", {"rate": 1})
        m = self.metrics()
        self.assertEqual(len(m["gestures"]), 4)
        self.assertEqual(
            [p for p in m["paints"] if p["g"]], [], "full render while a finger was down"
        )
        self.assertEqual(
            [b for b in m["builds"] if b["g"]], [], "cache rebuilt while a finger was down"
        )
        # deferred, not lost: every gesture ends with a full render after its release
        for g in m["gestures"]:
            self.assertTrue(
                any(g["t1"] <= p["t"] <= g["t1"] + 250 for p in m["paints"]), f"no render after {g}"
            )
        self.assertFalse(self.page.eval("renderDeferred"))
        frames = [f for f in m["frames"] if f["g"]]
        self.assertGreater(len(frames), 200)
        # each gesture's first frame is a blit of the idle-built cache, like every later one
        worst = max(f["d"] for f in frames)
        self.assertLess(worst, 33, f"slowest gesture frame {worst:.1f}ms")
        raf = sorted(m["raf"])
        p95 = raf[int(len(raf) * 0.95)]
        self.assertLess(p95, 34, f"rAF interval p95 during gestures {p95:.1f}ms")

    def test_render_mid_gesture_is_deferred_and_content_changes_reach_the_cache(self):
        self.set_zoom(4)
        key0 = self.page.eval("gestureCache.key")
        self.touch("touchStart", self.cx, self.cy)
        self.touch("touchMove", self.cx + 20, self.cy + 10)
        # a layer toggle mid-drag: render() is deferred, the cache untouched
        self.page.eval("layers.delete('B.Cu');render();true")
        self.assertTrue(self.page.eval("renderDeferred"))
        self.assertEqual(self.page.eval("gestureCache.key"), key0)
        self.touch("touchMove", self.cx + 30, self.cy + 15)
        self.touch("touchEnd")
        self.assertFalse(self.page.eval("renderDeferred"))
        self.wait_cache_ready()
        self.assertNotEqual(self.page.eval("gestureCache.key"), key0)
        self.assertEqual(self.page.eval("gestureCache.key===boardKey(geo())"), True)

    def test_rerender_of_same_board_and_view_keeps_the_cache(self):
        # A live poll hands over a fresh geometry object for the same board (same content hash):
        # no rebuild. Changing only the view does rebuild, but in idle time, not on the next drag.
        self.set_zoom(4)
        n0 = len(self.metrics()["builds"])
        self.page.eval("state=JSON.parse(JSON.stringify(state));render();true")
        self.assertFalse(self.page.eval("gestureCacheDirty"))
        time.sleep(0.5)
        self.assertEqual(len(self.metrics()["builds"]), n0)
        self.page.eval("view={...view,x:view.x+15};render();true")
        self.assertTrue(self.page.eval("gestureCacheDirty"))
        self.wait_cache_ready()
        self.assertEqual(len(self.metrics()["builds"]), n0 + 1)
        self.assertEqual(self.page.eval("gestureCache.x0===view.x"), True)

    def test_pointercancel_ends_the_gesture(self):
        self.set_zoom(4)
        self.touch("touchStart", self.cx, self.cy)
        self.touch("touchMove", self.cx + 20, self.cy)
        self.assertTrue(self.page.eval("gestureActive()"))
        self.touch("touchCancel")
        self.assertFalse(self.page.eval("gestureActive()"))
        self.page.eval("render();true")
        self.assertFalse(self.page.eval("renderDeferred"))

    def test_moves_paint_at_most_once_per_frame(self):
        # several pointermoves inside one frame queue a single gesture frame
        self.set_zoom(4)
        n = self.page.eval(
            "(()=>{let n0=__m.frames.length;"
            "drag={start:[10,10],last:[10,10],world:point(10,10),view:{...view}};"
            "for(let i=0;i<5;i++)canvas.dispatchEvent(new PointerEvent('pointermove',"
            "{pointerType:'mouse',clientX:20+i,clientY:20+i,pointerId:9}));"
            "return new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>{"
            "let n=__m.frames.length-n0;drag=null;r(n)})))})()"
        )
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
