"""Real headless Chrome against a real viewer server (CDP; see yapnr.viewer.testing), for the
Timing dock tab (yapnr/viewer/static/timing.js, GET /api/timing):

- the tab renders a per-stage table, a Gantt timeline and a group breakdown against a seeded
  campaign that has no stage events at all (the estimated-from-timestamps fallback: real
  campaigns today)
- against a seeded campaign with real stage_start/stage_end events, the mode reads "observed"
- it works at phone width with no horizontal page scroll
- clicking a slowest-lane row selects that lane (window.YapnrView.selectLane)

Skipped, not failed, when no Chrome/Chromium is on the machine (mirrors test_touch_and_panels.py).
A screenshot of the open panel, desktop and phone, is saved under --test_env=YAPNR_SCREEN_DIR.
Run explicitly: bazel test //tests/e2e/viewer:test_timing_panel
"""

import base64
import os
import tempfile
import time
import unittest
from pathlib import Path

from yapnr.viewer.testing import (
    CDP,
    chrome_binary,
    new_chrome_page,
    seed_lane_events,
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


def _estimated_events(base_t: float):
    """One seed-level lane plus one screening child, the real event shapes a ladder campaign
    emits today (no stage_start/stage_end): forces timing.js into the "estimated" fallback."""
    return [
        {
            "candidate": "ladder/01-cell/s0/initial-start-00",
            "kind": "candidate_start",
            "time": base_t,
            "data": {"phase": "initial-placement signals"},
        },
        {
            "candidate": "ladder/01-cell/s0/initial-start-00",
            "kind": "candidate_complete",
            "time": base_t + 2,
            "data": {"phase": "initial-placement screening complete"},
        },
        {
            "candidate": "ladder/01-cell/s0",
            "kind": "source_round_start",
            "time": base_t + 2.5,
            "data": {"phase": "source P/R round 1"},
        },
        {
            "candidate": "ladder/01-cell/s0",
            "kind": "phase_start",
            "time": base_t + 4,
            "data": {"phase": "gloss"},
        },
        {
            "candidate": "ladder/01-cell/s0",
            "kind": "geometry_result",
            "time": base_t + 9,
            "data": {"phase": "gloss"},
        },
        {
            "candidate": "ladder/01-cell/s0",
            "kind": "candidate_complete",
            "time": base_t + 9.5,
            "data": {},
        },
    ]


def _observed_events(base_t: float):
    """A campaign built on this branch: real stage_start/stage_end pairs."""
    events = []
    t = base_t
    for stage, seconds in (("setup", 1.2), ("route", 3.4)):
        events.append(
            dict(candidate="ladder/02-cell/s0", kind="stage_start", time=t, data=dict(stage=stage))
        )
        t += seconds
        events.append(
            dict(
                candidate="ladder/02-cell/s0",
                kind="stage_end",
                time=t,
                data=dict(stage=stage, seconds=seconds),
            )
        )
    events.append(dict(candidate="ladder/02-cell/s0", kind="candidate_complete", time=t, data={}))
    return events


@unittest.skipUnless(chrome_binary() is not None, "no Chrome/Chromium found (see chrome_binary())")
class TimingPanelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viewer-timing-e2e-")
        self.root = Path(self.tmp.name) / "live"
        self.root.mkdir()
        now = time.time() - 3600
        seed_lane_events(self.root, [*_estimated_events(now), *_observed_events(now + 100)])
        self.viewer_proc, self.base_url = start_viewer(self.root, "--dist", str(viewer_dist_dir()))
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
        try:
            self.tmp.cleanup()
        except OSError:
            import shutil

            shutil.rmtree(self.tmp.name, ignore_errors=True)

    def goto(self, width=None, height=None, mobile=False):
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
        self.page.call("Page.navigate", {"url": self.base_url + "/"})
        self.page.wait_event("Page.loadEventFired", timeout=15)
        wait_for(lambda: self.page.eval("typeof window.YapnrDock==='object'") or None, 10)

    def open_timing_tab(self):
        self.page.eval("window.YapnrDock.tab('timing');1")
        wait_for(
            lambda: self.page.eval(
                "document.querySelector('.timing-panel .tm-table,.timing-panel .tm-empty')!==null"
            )
            or None,
            10,
        )

    def test_renders_estimated_and_observed_stages(self):
        # A short viewport (not the usual 900px): the whole point of the height assertion below
        # is a flex min-height collapse that only bites once the panel's content actually
        # overflows the dock's available height -- this fixture's 2 lanes are not much content,
        # so a tall viewport would not reproduce it.
        self.goto(width=1440, height=280)
        # app.js auto-selects some lane for the main PCB view the moment data loads
        # (updateControls()) -- not a deliberate choice for the Timing panel's own scope, so this
        # checks the whole-campaign view: clear it the same way test_touch_and_panels.py seeds a
        # lane directly, rather than through the (single-lane-only) selectLane() API.
        self.page.eval("laneId='';1")
        self.open_timing_tab()
        mode_text = self.page.eval("document.querySelector('.timing-panel .tm-mode').textContent")
        self.assertIn("Mixed", mode_text)  # one lane estimated, one lane observed
        stage_text = self.page.eval("document.querySelector('.timing-panel .tm-table').textContent")
        self.assertIn("setup", stage_text)
        self.assertIn("route", stage_text)
        # .dk-panel (every dock tab) is a column flexbox; .tm-table-wrap's own overflow-x:auto
        # gives it an automatic flex min-height of 0 by spec, so a tall panel (lots of groups,
        # like a real multi-case campaign) could squash it to a real, textContent-bearing table
        # that is nonetheless rendered at zero height -- invisible despite "passing" a textContent
        # assertion. This is the actual regression check for that (dock.css's `.timing-panel>*`).
        self.assertGreater(
            self.page.eval(
                "document.querySelector('.timing-panel .tm-table-wrap').getBoundingClientRect().height"
            ),
            0,
        )
        self.assertTrue(
            self.page.eval("document.querySelectorAll('.timing-panel .tm-gantt-seg').length>0")
        )
        _save_shot(self.page, "timing-desktop")

    def test_slowest_row_click_selects_lane(self):
        self.goto(width=1440, height=900)
        self.open_timing_tab()
        wait_for(
            lambda: self.page.eval("document.querySelectorAll('.tm-slowest-row').length>0") or None,
            10,
        )
        self.page.eval("document.querySelector('.tm-slowest-row').click();1")
        wait_for(lambda: self.page.eval("window.YapnrView.lane()") or None, 10)
        lane = self.page.eval("window.YapnrView.lane()")
        self.assertTrue(lane.startswith("ladder/"))

    def test_scope_follows_the_full_selected_lane_path_not_just_its_top_segment(self):
        # Pre-fix, currentScope() took only the selected lane's first path segment ("ladder"),
        # so selecting any one lane scoped the panel to the *whole* campaign regardless -- never
        # down to just that lane.
        self.goto(width=1440, height=900)
        self.page.eval("window.YapnrView.selectLane('ladder/02-cell/s0');1")
        self.open_timing_tab()
        wait_for(
            lambda: self.page.eval("document.querySelector('.tm-scope').textContent").startswith(
                "Scope:"
            )
            or None,
            10,
        )
        scope_text = self.page.eval("document.querySelector('.tm-scope').textContent")
        self.assertIn("ladder/02-cell/s0", scope_text)
        mode_text = self.page.eval("document.querySelector('.timing-panel .tm-mode').textContent")
        self.assertIn("1 lane(s)", mode_text)  # scoped down, not the whole 2-lane campaign

    def test_phone_width_no_horizontal_page_scroll(self):
        self.goto(width=360, height=740, mobile=True)
        wait_for(lambda: self.page.eval("typeof window.YapnrPanels==='object'") or None, 10)
        self.page.eval("window.YapnrPanels.setLanes(false);window.YapnrPanels.setExplore(false);1")
        self.page.eval("window.YapnrDock.tab('timing');1")
        wait_for(
            lambda: self.page.eval("document.querySelector('.timing-panel')!==null") or None, 10
        )
        time.sleep(
            0.3
        )  # let the drawers' CSS close transition (dock.css, 180ms) finish before the shot
        self.assertLessEqual(
            self.page.eval("document.documentElement.scrollWidth"),
            self.page.eval("window.innerWidth"),
        )
        _save_shot(self.page, "timing-phone")


if __name__ == "__main__":
    unittest.main()
