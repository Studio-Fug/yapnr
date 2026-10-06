"""Real headless Chrome against a real viewer server (CDP; see yapnr.viewer.testing):

- pinch-zoom changes the board/schematic view scale, anchored at the pinch centre
- a two-finger drag pans without changing scale
- the page itself never scrolls or pinch-zooms (only the canvas/SVG view does)
- double-tap zooms
- the side panels (Experiments, Exploration) close/reopen/collapse and persist per browser, with
  a working Reset, and default to closed (board maximized) at phone width
- no horizontal page scroll at 360px width
- an annotation (design note) recorded in one experiment lane does not appear in another lane
  with the same refdes (the cross-experiment leak fix)

Skipped, not failed, when no Chrome/Chromium is on the machine (chrome_binary() is None) -- this
exercises real browser behaviour as a convenience, it is not something every CI runner is
guaranteed to have. Run explicitly: bazel test //tests/e2e/viewer:test_touch_and_panels
(manual, like its siblings; see tests/e2e/viewer/BUILD.bazel). A screenshot of the open and the
fully-closed layout, desktop and phone, is saved under --test_env=YAPNR_SCREEN_DIR if set.
"""

import base64
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from yapnr.viewer.testing import (
    CDP,
    chrome_binary,
    new_chrome_page,
    start_chrome,
    start_viewer,
    stop,
    viewer_dist_dir,
    wait_for,
)

SCREEN_DIR = os.environ.get("YAPNR_SCREEN_DIR")


def _save_shot(page: CDP, name: str):
    if not SCREEN_DIR:
        return
    Path(SCREEN_DIR).mkdir(parents=True, exist_ok=True)
    data = page.call("Page.captureScreenshot", {"format": "png"})["data"]
    (Path(SCREEN_DIR) / f"{name}.png").write_bytes(base64.b64decode(data))


def _alpha(css_color: str) -> float:
    """The alpha channel of a computed `rgb(...)`/`rgba(...)` colour, as 0..1. A colour with no
    alpha component (`rgb(r, g, b)`, the computed form for a fully opaque fill) is 1.0. Used to
    check a drawer/scrim is actually opaque rather than merely not the literal string
    "transparent" -- a translucent fill would pass that naive check while still being hard to
    read against whatever is underneath."""
    parts = css_color.strip().removeprefix("rgba(").removeprefix("rgb(").rstrip(")").split(",")
    return float(parts[3]) if len(parts) > 3 else 1.0


@unittest.skipUnless(chrome_binary() is not None, "no Chrome/Chromium found (see chrome_binary())")
class TouchAndPanelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viewer-touch-e2e-")
        root = Path(self.tmp.name) / "live"
        root.mkdir()
        self.viewer_proc, self.base_url = start_viewer(root, "--dist", str(viewer_dist_dir()))
        self.profile_dir = Path(self.tmp.name) / "chrome-profile"
        self.chrome_proc, self.devtools_url = start_chrome(self.profile_dir)
        self.page = new_chrome_page(self.devtools_url)
        self.page.call("Page.enable")

    def tearDown(self):
        self.page.close()
        self.chrome_proc.terminate()
        try:
            self.chrome_proc.wait(10)
        except Exception:
            self.chrome_proc.kill()
            self.chrome_proc.wait(10)
        stop(self.viewer_proc)
        # Chrome's profile directory can still have a helper process flushing a lock file right
        # after terminate(); never fail the test over a leftover temp directory.
        try:
            self.tmp.cleanup()
        except OSError:
            shutil.rmtree(self.tmp.name, ignore_errors=True)

    def goto(self, path="/", width=None, height=None, mobile=False):
        if width:
            self.page.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height or width,
                    "deviceScaleFactor": 2 if mobile else 1,
                    "mobile": mobile,
                },
            )
        else:
            self.page.call("Emulation.clearDeviceMetricsOverride")
        self.page.call("Page.navigate", {"url": self.base_url + path})
        self.page.wait_event("Page.loadEventFired", timeout=15)
        wait_for(lambda: self.page.eval("typeof window.YapnrPanels==='object'") or None, 10)

    def rect(self, selector):
        return json.loads(
            self.page.eval(
                f"JSON.stringify(document.querySelector({json.dumps(selector)}).getBoundingClientRect())"
            )
        )

    def touch(self, points, kind):
        self.page.call("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})

    def _panel_bg(self, element_id):
        return self.page.eval(
            f"getComputedStyle(document.getElementById('{element_id}')).backgroundColor"
        )

    def pinch(self, cx, cy, d_from, d_to, steps=8, axis="x"):
        """Two fingers straddling (cx, cy) along `axis`, distance d_from -> d_to."""
        import time

        def pts(d):
            if axis == "x":
                return [{"x": cx - d, "y": cy, "id": 1}, {"x": cx + d, "y": cy, "id": 2}]
            return [{"x": cx, "y": cy - d, "id": 1}, {"x": cx, "y": cy + d, "id": 2}]

        self.touch(pts(d_from), "touchStart")
        time.sleep(0.03)
        last = pts(d_from)
        for i in range(1, steps + 1):
            d = d_from + (d_to - d_from) * i / steps
            last = pts(d)
            self.touch(last, "touchMove")
            time.sleep(0.03)
        self.touch(last, "touchEnd")
        time.sleep(0.08)

    def tap(self, x, y):
        import time

        p = {"x": x, "y": y, "id": 1}
        self.touch([p], "touchStart")
        time.sleep(0.02)
        self.touch([p], "touchEnd")
        time.sleep(0.03)

    # ------------------------------------------------------------------ touch: board canvas
    def test_pinch_zoom_anchors_on_centre_and_page_does_not_move(self):
        self.goto(width=412, height=915, mobile=True)
        self.page.eval("view={scale:8,x:40,y:500};render();1")
        r = self.rect("#board")
        cx, cy = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2
        self.pinch(cx, cy, 20, 90)
        scale = self.page.eval("view.scale")
        self.assertAlmostEqual(scale, 36, delta=0.5)  # 8 * (90/20)
        # the world point under the (unmoved) pinch centre is still under it on screen
        local = self.page.eval(f"JSON.stringify(screen(point({cx - r['left']},{cy - r['top']})))")
        sx, sy = json.loads(local)
        self.assertAlmostEqual(sx, r["width"] / 2, delta=1.5)
        self.assertAlmostEqual(sy, r["height"] / 2, delta=1.5)
        # the browser page itself must not have scrolled or pinch-zoomed
        page_state = json.loads(
            self.page.eval(
                "JSON.stringify({sx:window.scrollX,sy:window.scrollY,"
                "vv:window.visualViewport?window.visualViewport.scale:1,"
                "overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth})"
            )
        )
        self.assertEqual(page_state, {"sx": 0, "sy": 0, "vv": 1, "overflow": False})

    def test_two_finger_pan_does_not_change_scale(self):
        self.goto(width=412, height=915, mobile=True)
        self.page.eval("view={scale:8,x:40,y:500};render();1")
        r = self.rect("#board")
        cx, cy = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2
        import time

        p1 = {"x": cx - 50, "y": cy, "id": 1}
        p2 = {"x": cx + 50, "y": cy, "id": 2}
        self.touch([p1, p2], "touchStart")
        time.sleep(0.03)
        for dx in range(0, 60, 10):
            p1 = {"x": cx - 50 + dx, "y": cy + dx, "id": 1}
            p2 = {"x": cx + 50 + dx, "y": cy + dx, "id": 2}
            self.touch([p1, p2], "touchMove")
            time.sleep(0.02)
        self.touch([p1, p2], "touchEnd")
        time.sleep(0.08)
        view = json.loads(self.page.eval("JSON.stringify({scale:view.scale,x:view.x,y:view.y})"))
        self.assertEqual(view["scale"], 8)
        self.assertAlmostEqual(view["x"], 90, delta=1)
        self.assertAlmostEqual(view["y"], 550, delta=1)

    def test_double_tap_zooms_then_back_out(self):
        self.goto(width=412, height=915, mobile=True)
        self.page.eval("view={scale:8,x:40,y:500};render();1")
        r = self.rect("#board")
        cx, cy = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2
        self.tap(cx, cy)
        self.tap(cx, cy)
        self.assertAlmostEqual(self.page.eval("view.scale"), 20, delta=0.5)
        self.tap(cx, cy)
        self.tap(cx, cy)
        self.assertAlmostEqual(self.page.eval("view.scale"), 8, delta=0.5)

    def test_schematic_svg_pinch_zoom(self):
        self.goto(width=900, height=800)
        self.page.eval(
            "document.getElementById('viewswitch').querySelector('[data-v=split]').click();1"
        )
        wait_for(lambda: (self.rect("#sch-svg")["width"] > 50) or None, 5)
        self.page.eval("sch.v={x:0,y:0,k:4};schApplyView();1")
        r = self.rect("#sch-svg")
        cx, cy = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2
        self.pinch(cx, cy, 20, 70)
        self.assertAlmostEqual(self.page.eval("sch.v.k"), 14, delta=0.5)  # 4 * (70/20)

    # ------------------------------------------------------------------ dockable panels
    def test_panels_close_reopen_collapse_and_persist(self):
        self.goto(width=1600, height=900)
        self.assertTrue(self.page.eval("YapnrPanels.lanesOpen()"))
        self.assertTrue(self.page.eval("YapnrPanels.exploreOpen()"))
        self.assertEqual(
            self.page.eval(
                "getComputedStyle(document.documentElement).getPropertyValue('--lanes-w')"
            ),
            "245px",
        )

        # a panel can collapse to a 34px rail instead of closing outright (mirrors the Inspect
        # dock's own collapse), keeping its close/expand affordance reachable
        self.page.eval("YapnrPanels.setLanesRail(true);1")
        self.assertEqual(
            self.page.eval(
                "getComputedStyle(document.documentElement).getPropertyValue('--lanes-w')"
            ),
            "34px",
        )
        self.assertTrue(
            self.page.eval("document.getElementById('lanes-panel').classList.contains('sp-rail')")
        )
        self.assertTrue(self.page.eval("YapnrPanels.lanesOpen()"))  # rail is not closed
        self.page.eval("YapnrPanels.setLanesRail(false);1")
        self.assertEqual(
            self.page.eval(
                "getComputedStyle(document.documentElement).getPropertyValue('--lanes-w')"
            ),
            "245px",
        )

        # close via the real toolbar button (not just the JS API), reopen via the same button
        br = self.rect("#panel-bar button")
        self.page.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": br["left"] + 5,
                "y": br["top"] + 5,
                "button": "left",
                "clickCount": 1,
            },
        )
        self.page.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": br["left"] + 5,
                "y": br["top"] + 5,
                "button": "left",
                "clickCount": 1,
            },
        )
        wait_for(lambda: (self.page.eval("YapnrPanels.lanesOpen()") is False) or None, 3)
        self.assertEqual(
            self.page.eval(
                "getComputedStyle(document.documentElement).getPropertyValue('--lanes-w')"
            ),
            "0px",
        )
        self.assertTrue(
            self.page.eval("document.getElementById('lanes-panel').hasAttribute('inert')")
        )

        self.page.eval("YapnrPanels.setExplore(false);1")
        self.assertEqual(
            self.page.eval(
                "getComputedStyle(document.documentElement).getPropertyValue('--explore-w')"
            ),
            "0px",
        )
        self.page.eval("window.YapnrDock.close();1")  # the fourth, pre-existing dock column too
        self.assertFalse(self.page.eval("YapnrDock.isOpen()"))
        # with every side panel (lanes, exploration, dock) closed the board gets effectively the
        # whole window (the dock keeps its own pre-existing 34px collapsed rail on a wide screen)
        self.assertFalse(
            self.page.eval(
                "document.documentElement.scrollWidth>document.documentElement.clientWidth"
            )
        )
        _save_shot(self.page, "desktop-panels-closed")  # every panel closed, board maximized

        # persists across a reload
        self.goto("/", width=1600, height=900)
        self.assertFalse(self.page.eval("YapnrPanels.lanesOpen()"))
        self.assertFalse(self.page.eval("YapnrPanels.exploreOpen()"))

        # Reset layout restores the defaults and clears both storage keys
        rb = self.rect("#panel-bar button.reset")
        self.page.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": rb["left"] + 5,
                "y": rb["top"] + 5,
                "button": "left",
                "clickCount": 1,
            },
        )
        self.page.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": rb["left"] + 5,
                "y": rb["top"] + 5,
                "button": "left",
                "clickCount": 1,
            },
        )
        self.page.wait_event("Page.loadEventFired", timeout=10)
        wait_for(lambda: (self.page.eval("typeof window.YapnrPanels==='object'")) or None, 10)
        self.assertIsNone(self.page.eval("localStorage.getItem('yapnr-panels-v1')"))
        self.assertIsNone(self.page.eval("localStorage.getItem('yapnr-dock')"))
        self.assertTrue(self.page.eval("YapnrPanels.lanesOpen()"))

    def test_phone_default_is_board_maximized_and_panels_overlay(self):
        self.goto(width=412, height=915, mobile=True)
        self.assertFalse(self.page.eval("YapnrPanels.lanesOpen()"))
        self.assertFalse(self.page.eval("YapnrPanels.exploreOpen()"))
        self.assertEqual(
            self.page.eval(
                "getComputedStyle(document.querySelector('main')).gridTemplateColumns.split(' ').length"
            ),
            1,
        )
        _save_shot(self.page, "phone-board-maximized")
        # opening it is an overlay drawer (fixed, over the board), not a grid column
        self.page.eval("YapnrPanels.setLanes(true);1")
        self.assertEqual(
            self.page.eval("getComputedStyle(document.getElementById('lanes-panel')).position"),
            "fixed",
        )
        # the drawer paints a solid background -- it must never let the board/toolbar underneath
        # show through (the transparent-drawer bug). Checked by alpha channel, not just against
        # the literal strings "transparent"/rgba(0,0,0,0): a translucent fill (e.g. 50% alpha)
        # would pass a naive string check while still being hard to read against the board.
        self.assertGreaterEqual(_alpha(self._panel_bg("lanes-panel")), 0.99)
        # the exploration drawer is the other one the owner reported as transparent -- same check.
        # Checked and closed again right away: with lanes docked left and exploration docked right
        # each at min(320px,88vw), the two together can cover the full 412px phone width, which
        # would leave no sliver of scrim to click in the check below.
        self.page.eval("YapnrPanels.setExplore(true);1")
        self.assertGreaterEqual(_alpha(self._panel_bg("explore-panel")), 0.99)
        self.page.eval("YapnrPanels.setExplore(false);1")
        # a scrim sits behind the open drawer, dimming the board and giving a tap-outside target;
        # it must itself be visible (not accidentally transparent) while a drawer is open
        self.assertFalse(self.page.eval("document.getElementById('sp-scrim').hidden"))
        self.assertGreater(
            _alpha(
                self.page.eval(
                    "getComputedStyle(document.getElementById('sp-scrim')).backgroundColor"
                )
            ),
            0.0,
        )
        # tapping the scrim (not the drawer itself, which is docked left and narrower than the
        # viewport) closes it
        scrim_rect = self.rect("#sp-scrim")
        tap_x, tap_y = scrim_rect["x"] + scrim_rect["width"] - 5, scrim_rect["y"] + 5
        self.page.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": tap_x, "y": tap_y, "button": "left", "clickCount": 1},
        )
        self.page.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": tap_x, "y": tap_y, "button": "left", "clickCount": 1},
        )
        wait_for(lambda: (self.page.eval("YapnrPanels.lanesOpen()") is False) or None, 3)
        self.assertTrue(self.page.eval("document.getElementById('sp-scrim').hidden"))
        # reopen lanes (closed by the scrim tap above) for the focus/Esc checks below
        self.page.eval("YapnrPanels.setLanes(true);1")
        # opening the drawer from the toolbar moves focus into it
        wait_for(
            lambda: (
                self.page.eval(
                    "document.getElementById('lanes-panel').contains(document.activeElement)"
                )
            )
            or None,
            3,
        )
        # Esc closes it even though focus never left the drawer's own close button
        self.page.call(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
        )
        wait_for(lambda: (self.page.eval("YapnrPanels.lanesOpen()") is False) or None, 3)
        _save_shot(self.page, "phone-panel-open")

    def test_phone_drawer_escape_closes_without_focus_inside(self):
        self.goto(width=412, height=915, mobile=True)
        self.page.eval("YapnrPanels.setExplore(true);1")
        self.assertTrue(self.page.eval("YapnrPanels.exploreOpen()"))
        # move focus somewhere else entirely (the brand heading has none normally; give it one)
        self.page.eval(
            "document.getElementById('brand').tabIndex=-1;document.getElementById('brand').focus();1"
        )
        self.page.call(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
        )
        wait_for(lambda: (self.page.eval("YapnrPanels.exploreOpen()") is False) or None, 3)

    def test_panel_side_can_dock_left_or_right(self):
        self.goto(width=412, height=915, mobile=True)
        self.assertEqual(self.page.eval("YapnrPanels.exploreSide()"), "right")
        self.page.eval("YapnrPanels.setExplore(true);1")
        r = self.rect("#explore-panel")
        self.assertGreater(r["left"], 50)  # docked at the right edge
        self.page.eval("YapnrPanels.setExploreSide('left');1")
        # the drawer's slide is a transitioned transform (style.css); give it a moment to settle
        # before measuring, rather than reading the rect mid-transition
        wait_for(lambda: (self.rect("#explore-panel")["left"] < 10) or None, 3)
        r = self.rect("#explore-panel")
        self.assertLess(r["left"], 10)  # now docked at the left edge instead
        self.page.eval("YapnrPanels.setExploreSide('right');1")  # restore the default

    def test_laptop_width_keeps_lanes_and_exploration_as_columns(self):
        # a laptop (not a phone) should not lose its columns to the overlay-drawer behaviour
        self.goto(width=1280, height=800)
        self.assertTrue(self.page.eval("YapnrPanels.lanesOpen()"))
        self.assertTrue(self.page.eval("YapnrPanels.exploreOpen()"))
        self.assertEqual(
            self.page.eval("getComputedStyle(document.getElementById('lanes-panel')).position"),
            "relative",
        )
        self.assertGreaterEqual(
            self.page.eval(
                "getComputedStyle(document.querySelector('main')).gridTemplateColumns.split(' ').length"
            ),
            3,
        )

    def test_narrow_header_and_toolbar_controls_stay_reachable(self):
        # at phone width the header-right chips and the board toolbar's buttons must not be
        # clipped out of reach by the page's own overflow:hidden -- they get their own
        # (non-page) horizontal scroll instead
        self.goto(width=412, height=915, mobile=True)
        self.page.eval(
            "document.getElementById('health').textContent="
            "'\\u25cf LOCAL LIVE \\u00b7 123456 events (a long status string)';1"
        )
        self.page.eval("document.querySelector('.header-right').scrollLeft=99999;1")
        about = self.rect("#about")
        self.assertLessEqual(about["right"], self.page.eval("window.innerWidth") + 1)
        self.page.eval("document.querySelector('.workspace>.toolbar').scrollLeft=99999;1")
        fit = self.rect("#fit")
        self.assertLessEqual(fit["right"], self.page.eval("window.innerWidth") + 1)
        self.assertGreaterEqual(fit["left"], 0)
        # none of this adds up to page-level horizontal scroll
        self.assertLessEqual(
            self.page.eval("document.documentElement.scrollWidth"),
            self.page.eval("window.innerWidth"),
        )

    def test_ios_gesture_events_are_prevented(self):
        # Safari's non-standard gesture* events (pinch zoom of the whole page) must be
        # suppressed as a backstop alongside touch-action:none
        self.goto(width=412, height=915, mobile=True)
        prevented = self.page.eval(
            "(()=>{let e=new Event('gesturestart',{cancelable:true});"
            "document.dispatchEvent(e);return e.defaultPrevented})()"
        )
        self.assertTrue(prevented)

    def test_no_horizontal_scroll_at_360(self):
        self.goto(width=360, height=740, mobile=True)
        self.assertLessEqual(
            self.page.eval("document.documentElement.scrollWidth"),
            self.page.eval("window.innerWidth"),
        )
        self.page.eval("YapnrPanels.setLanes(true);YapnrPanels.setExplore(true);1")
        self.assertLessEqual(
            self.page.eval("document.documentElement.scrollWidth"),
            self.page.eval("window.innerWidth"),
        )

    def test_desktop_and_phone_screenshots(self):
        if not SCREEN_DIR:
            self.skipTest("set --test_env=YAPNR_SCREEN_DIR to save screenshots")
        self.goto(width=1600, height=900)
        _save_shot(self.page, "desktop-panels-open")
        self.goto(width=412, height=915, mobile=True)
        _save_shot(self.page, "phone-default")

    # ------------------------------------------------------------------ annotation scoping
    def test_annotation_does_not_leak_across_experiments(self):
        self.goto(width=1200, height=800)
        created = self.page.eval(
            """
            (async()=>{
             let a = await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},
               body: JSON.stringify({title:'C1 issue in lane A', targets:[{kind:'component',ref:'C1'}],
                 provenance:{lane:'laneA'}})}).then(r=>r.json());
             let b = await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},
               body: JSON.stringify({title:'C1 issue in lane B', targets:[{kind:'component',ref:'C1'}],
                 provenance:{lane:'laneB'}})}).then(r=>r.json());
             return JSON.stringify({a:a.note.scope, b:b.note.scope});
            })()
            """
        )
        scopes = json.loads(created)
        self.assertEqual(scopes["a"], {"kind": "lane", "lane": "laneA"})
        self.assertEqual(scopes["b"], {"kind": "lane", "lane": "laneB"})
        wait_for(
            lambda: (
                self.page.eval(
                    "(async()=>{await window.YapnrNotes.refresh();return window.YapnrNotes.list().length})()"
                )
                >= 2
            )
            or None,
            5,
        )

        ids_a = json.loads(
            self.page.eval("JSON.stringify(window.YapnrNotes.badgesFor('laneA').flatMap(b=>b.ids))")
        )
        ids_b = json.loads(
            self.page.eval("JSON.stringify(window.YapnrNotes.badgesFor('laneB').flatMap(b=>b.ids))")
        )
        ids_c = json.loads(
            self.page.eval("JSON.stringify(window.YapnrNotes.badgesFor('laneC').flatMap(b=>b.ids))")
        )
        self.assertEqual(ids_a, ["N-0001"])  # only its own lane's note about C1
        self.assertEqual(ids_b, ["N-0002"])  # the other lane's note never leaks in
        self.assertEqual(ids_c, [])  # a third, unrelated lane sees neither

    def test_global_note_shows_in_every_lane_and_unscoped_in_neither(self):
        self.goto(width=1200, height=800)
        self.page.eval(
            """
            (async()=>{
             await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},
               body: JSON.stringify({title:'Always relevant', targets:[{kind:'component',ref:'C5'}],
                 global:true})});
             await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},
               body: JSON.stringify({title:'No lane recorded', targets:[{kind:'component',ref:'C6'}]})});
             await window.YapnrNotes.refresh();
            })()
            """
        )
        for lane in ("laneA", "laneZ"):
            ids = json.loads(
                self.page.eval(
                    f"JSON.stringify(window.YapnrNotes.badgesFor({lane!r}).flatMap(b=>b.ids))"
                )
            )
            self.assertIn("N-0001", ids)
            self.assertNotIn("N-0002", ids)  # unscoped: never drawn on any board

    def test_note_created_through_the_editor_stays_on_its_own_lane(self):
        # the other scoping tests create notes directly through the API with provenance.lane
        # already set; this one drives the real editor UI (window.YapnrNotes.newNote + the Save
        # button, exactly what a click on "+ note" does) while "viewing" lane A (app.js's laneId,
        # which is all the editor ever reads -- YapnrView.lane() returns it verbatim), then
        # switches to lane B the same way a lane click does (select() starts by reassigning
        # laneId) and checks the note never shows there.
        self.goto(width=1200, height=800)
        self.page.eval(
            "laneId='laneA';window.YapnrNotes.newNote("
            "{title:'via the editor',targets:[{kind:'component',ref:'C9'}]});1"
        )
        # Note: checked by count, not by querySelector's own truthiness -- Runtime.evaluate's
        # returnByValue serializes a DOM Element as `{}` (CDP can't structured-clone a node),
        # which is falsy in Python, so `page.eval(...) and True` would never see the element.
        wait_for(
            lambda: (self.page.eval("document.querySelectorAll('.nt-save').length") > 0) or None, 3
        )
        self.page.eval("document.querySelector('.nt-save').click();1")
        wait_for(lambda: (self.page.eval("window.YapnrNotes.list().length") >= 1) or None, 5)
        note = json.loads(self.page.eval("JSON.stringify(window.YapnrNotes.list()[0])"))
        self.assertEqual(note["scope"], {"kind": "lane", "lane": "laneA"})

        ids_a = json.loads(
            self.page.eval("JSON.stringify(window.YapnrNotes.badgesFor('laneA').flatMap(b=>b.ids))")
        )
        self.assertIn(note["id"], ids_a)

        # switch lanes the same way a click on a lane button does (select(id) starts with
        # laneId=id): the note made while viewing A must not follow onto B
        self.page.eval("laneId='laneB';1")
        ids_b = json.loads(
            self.page.eval("JSON.stringify(window.YapnrNotes.badgesFor('laneB').flatMap(b=>b.ids))")
        )
        self.assertNotIn(note["id"], ids_b)


if __name__ == "__main__":
    unittest.main()
