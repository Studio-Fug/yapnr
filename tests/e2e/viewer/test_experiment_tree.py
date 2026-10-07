"""Real headless Chrome against a real viewer server (CDP; see yapnr.viewer.testing), exercising
the hierarchical experiment browser (yapnr/viewer/static/lane-tree.js, built from
yapnr/viewer/lane_tree.py + progress.py): the tree renders a real campaign's hierarchy, groups
show aggregate counts/progress, expand/collapse persists across a reload, filtering by name and
by state works, progress bar widths match the phase/progress model on this fixture, plain-
language status text appears, keyboard navigation moves and selects, selecting a leaf still drives
the board the same way the old flat lane list did, and the tree works at phone width (it lives in
the Experiments drawer there, same as the rest of #lanes-panel). Also: a row's DOM node survives a
live update (renderBody() updates it in place rather than rebuilding it), and consequently a
user's scroll position in both the row list (.tr-body) and the surrounding Experiments panel/
drawer (#lanes-panel) is never reset by a live update, on desktop or phone -- the real campaign
directory keeps streaming new events into ``events/`` throughout, exercising the same poll ->
updateControls() -> YapnrTree.update() path a live viewer uses, not a synthetic call into the
module.

Skipped, not failed, when no Chrome/Chromium is on the machine, like its siblings (see
test_touch_and_panels.py). Run explicitly: bazel test //tests/e2e/viewer:test_experiment_tree.
A screenshot of a large campaign, desktop and phone, is saved under --test_env=YAPNR_SCREEN_DIR.
"""

import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from yapnr.viewer.testing import (
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

# A small but representative campaign: two cases under one arm (done+failed seed, and a
# queued+routing seed), a bare single-segment lane, and one candidate-search audit lane that must
# never appear in the tree at all. Shapes (candidate ids, kinds, data fields) match real ones
# gathered from live "ladder" directories -- see docs/viewer.md and yapnr/viewer/progress.py.
EVENTS = [
    dict(
        candidate="controller", kind="candidate_start", data=dict(phase="initial-placement signals")
    ),
    dict(
        candidate="ladder/keep-on/case-a/s0/initial-start-00",
        kind="candidate_complete",
        data=dict(phase="result", opens=0, violations=0),
    ),
    dict(
        candidate="ladder/keep-on/case-a/s0/initial-start-01",
        kind="candidate_failed",
        data=dict(phase="legalization", opens=2, violations=1),
    ),
    dict(candidate="ladder/keep-on/case-a/s1/initial-start-00", kind="candidate_queued", data={}),
    dict(
        candidate="ladder/keep-on/case-a/s1/initial-start-01",
        kind="route_result",
        data=dict(phase="routed", progress=dict(done=7, total=10)),
    ),
    dict(candidate="r01/search", kind="batch_alternatives", data={}),
]


@unittest.skipUnless(chrome_binary() is not None, "no Chrome/Chromium found (see chrome_binary())")
class ExperimentTreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viewer-tree-e2e-")
        self.root = Path(self.tmp.name) / "live"
        self.root.mkdir()
        seed_lane_events(self.root, EVENTS)
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
        self.tmp.cleanup()

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
        wait_for(lambda: self.page.eval("typeof window.YapnrTree==='object'") or None, 10)
        wait_for(lambda: (self.page.eval("YapnrTree.rows().length") > 0) or None, 10)

    def _save_shot(self, name):
        if not SCREEN_DIR:
            return
        Path(SCREEN_DIR).mkdir(parents=True, exist_ok=True)
        data = self.page.call("Page.captureScreenshot", {"format": "png"})["data"]
        (Path(SCREEN_DIR) / f"{name}.png").write_bytes(base64.b64decode(data))

    def row_texts(self):
        return json.loads(self.page.eval("JSON.stringify(YapnrTree.rows().map(r=>r.node.name))"))

    def stream_events(self, events, polls=3):
        """Append `events` to the live directory's events/ dir (same files a real engine writes)
        and give the running viewer time to pick them up: its background ingest loop scans
        events/ every 0.25s and bumps `revision`, and the page's own poll() (app.js) re-fetches
        every 1.2s and, on a revision bump, calls updateControls() -> YapnrTree.update() --
        exactly the path a live campaign drives lane-tree.js through. `polls` default (3) covers
        at least one full page poll interval with margin."""
        seed_lane_events(self.root, events)
        time.sleep(0.4 + 1.3 * polls)

    def revision(self):
        return self.page.eval("state&&state.revision")

    # ------------------------------------------------------------------ hierarchy
    def test_tree_groups_lane_ids_into_a_directory_like_hierarchy(self):
        self.goto(width=1400, height=900)
        names = self.row_texts()
        # top level: controller (bare id), ladder (grouped), r01/search excluded entirely
        self.assertIn("controller", names)
        self.assertIn("ladder", names)
        self.assertNotIn("r01", names)
        self.assertNotIn("search", names)
        # expand everything so the whole hierarchy is present in the flat row list
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("s0" in self.row_texts()) or None, 5)
        names = self.row_texts()
        for expected in ("keep-on", "case-a", "s0", "s1", "initial-start-00", "initial-start-01"):
            self.assertIn(expected, names)

    def test_group_counts_reflect_its_leaves(self):
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        case_counts = json.loads(
            self.page.eval(
                "JSON.stringify(YapnrTree.rows().find(r=>r.node.name==='case-a').node.counts)"
            )
        )
        self.assertEqual(case_counts["total"], 4)
        self.assertEqual(case_counts["done"], 1)
        self.assertEqual(case_counts["failed"], 1)
        self.assertEqual(case_counts["queued"], 1)
        self.assertEqual(case_counts["running"], 1)  # the routing lane

    # ------------------------------------------------------------------ progress bars + status text
    def test_progress_bar_width_matches_the_route_fraction(self):
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-01" in self.row_texts()) or None, 5)
        frac = self.page.eval(
            "YapnrTree.rows().find(r=>r.lane&&r.lane.id.endsWith('s1/initial-start-01')).node.fraction"
        )
        # route is index 4 of 7 canonical phases; done=7/total=10 -> (4+0.7)/7
        self.assertAlmostEqual(frac, (4 + 0.7) / 7, places=3)

    def test_status_text_is_plain_language_not_raw_telemetry(self):
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-01" in self.row_texts()) or None, 5)
        texts = json.loads(
            self.page.eval(
                "JSON.stringify(YapnrTree.rows().filter(r=>r.lane).map(r=>r.lane.status_text))"
            )
        )
        self.assertIn("Queued", texts)
        self.assertIn("Routing: 7 of 10 connections (70%)", texts)
        self.assertIn("Failed: 2 unconnected, 1 DRC error", texts)
        self.assertTrue(any(t.startswith("Done: all connected") for t in texts))
        self.assertFalse(any("legalization" == t for t in texts))  # never the raw engine string

    # ------------------------------------------------------------------ expand/collapse + persistence
    def test_expand_collapse_and_persistence_across_reload(self):
        self.goto(width=1400, height=900)
        ladder_depth0 = "YapnrTree.rows().find(r=>r.node.name==='ladder').open"
        self.assertTrue(self.page.eval(ladder_depth0))  # depth-0 group: open by default
        before = len(self.row_texts())
        self.page.eval(
            "document.querySelector('.tr-row[aria-expanded]').querySelector('.tr-twirl').click();1"
        )
        after = len(self.row_texts())
        self.assertNotEqual(before, after)
        collapsed_to = after
        self.goto("/", width=1400, height=900)  # reload: the toggle above must persist
        self.assertEqual(len(self.row_texts()), collapsed_to)

    def test_expand_all_and_collapse_all(self):
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelectorAll('.tr-btns button')[1].click();1")  # collapse all
        wait_for(
            lambda: (len(self.row_texts()) <= 2) or None, 5
        )  # only the two top-level rows left
        self.page.eval("document.querySelectorAll('.tr-btns button')[0].click();1")  # expand all
        wait_for(lambda: ("initial-start-00" in self.row_texts()) or None, 5)

    # ------------------------------------------------------------------ filtering
    def test_filter_by_name(self):
        self.goto(width=1400, height=900)
        self.page.eval("YapnrTree.setFilterText('controller');1")
        self.assertEqual(self.row_texts(), ["controller"])
        self.page.eval("YapnrTree.setFilterText('');1")

    def test_filter_by_state_keeps_only_matching_leaves_and_their_ancestry(self):
        self.goto(width=1400, height=900)
        self.page.eval("YapnrTree.setFilterStates(['failed']);1")
        wait_for(lambda: ("initial-start-01" in self.row_texts()) or None, 5)
        names = self.row_texts()
        self.assertIn("case-a", names)  # ancestor of the match stays reachable
        self.assertNotIn("initial-start-00", names)  # the done leaf under the same case is hidden
        self.page.eval("YapnrTree.setFilterStates(['queued','running','done','failed']);1")

    # ------------------------------------------------------------------ keyboard navigation
    def test_keyboard_arrow_down_and_enter_selects_a_leaf(self):
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")  # expand all
        wait_for(lambda: ("initial-start-00" in self.row_texts()) or None, 5)
        target = next(
            i
            for i, r in enumerate(
                json.loads(self.page.eval("JSON.stringify(YapnrTree.rows().map(r=>r.node.name))"))
            )
            if r == "initial-start-00"
        )
        self.page.eval("document.getElementById('tr-row-0').focus();1")
        for _ in range(target):
            self.page.call(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": "ArrowDown", "code": "ArrowDown"},
            )
        self.page.call(
            "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter"}
        )
        wait_for(lambda: ("initial-start-00" in (self.page.eval("laneId") or "")) or None, 5)

    def test_keyboard_focus_survives_a_live_update(self):
        # A live poll re-renders the tree on every new event; focus must follow the same tree
        # node across that re-render instead of falling back to <body> (which would silently
        # break arrow-key navigation every time a live campaign streams in new events).
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-00" in self.row_texts()) or None, 5)
        self.page.eval("document.getElementById('tr-row-0').focus();1")
        focused_id_before = self.page.eval("YapnrTree.rows()[0].node.id")
        # Force the same re-render a live poll triggers on every new event, without waiting on a
        # real poll cycle.
        self.page.eval("YapnrTree.update();1")
        wait_for(
            lambda: (
                self.page.eval("document.activeElement&&document.activeElement.id") or ""
            ).startswith("tr-row")
            or None,
            5,
        )
        focused_id_after = self.page.eval(
            "YapnrTree.rows()[Number((document.activeElement.id||'tr-row--1').slice(7))].node.id"
        )
        self.assertEqual(focused_id_after, focused_id_before)

    # ------------------------------------------------------------------ row identity across updates
    def test_row_dom_node_identity_persists_across_a_live_update(self):
        # renderBody() must update an existing node's row element in place (reconcileRows() in
        # lane-tree.js), never tear it down and build a new one, or focus/hover/scroll tied to
        # that element is silently lost on every single live update.
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-00" in self.row_texts()) or None, 5)
        node_id = self.page.eval("YapnrTree.rows()[0].node.id")
        same_node = self.page.eval(
            f"""(function(){{
                let before = YapnrTree.elementFor({node_id!r});
                YapnrTree.update();
                let after = YapnrTree.elementFor({node_id!r});
                return !!before && before === after;
            }})()"""
        )
        self.assertTrue(same_node, "the row's DOM element was recreated instead of reused")

    def test_keyed_rows_keep_dom_order_through_filters_and_new_lanes(self):
        # Keyed in-place updates must keep the DOM order identical to rows() when rows are
        # filtered away, come back, or appear from a live stream, with no stray/duplicate nodes
        # and with a persisting row keeping its element throughout.
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-01" in self.row_texts()) or None, 5)
        in_order = (
            "(function(){let b=document.querySelector('.tr-body'),rs=YapnrTree.rows();"
            "return b.children.length===rs.length&&b.childNodes.length===rs.length&&"
            "rs.every((r,i)=>b.children[i]===YapnrTree.elementFor(r.node.id)&&"
            "b.children[i].id==='tr-row-'+i)})()"
        )
        root_id = self.page.eval("YapnrTree.rows()[0].node.id")
        self.assertTrue(self.page.eval(in_order))
        # focus a row below others that the filter removes: it must stay focused, same element
        self.page.eval(
            "window._f=[...document.querySelectorAll('.tr-row')].find(e=>"
            "e.querySelector('.tr-label').textContent==='initial-start-01');"
            "window._f.focus({preventScroll:true});1"
        )
        self.page.eval("YapnrTree.setFilterText('initial-start-01');1")
        self.assertTrue(self.page.eval(in_order))
        self.assertTrue(self.page.eval("document.activeElement===window._f"))
        self.page.eval("YapnrTree.setFilterText('no-such-lane-anywhere');1")
        self.page.eval("YapnrTree.setFilterText('');1")
        self.assertTrue(self.page.eval(in_order))
        # (an empty filter result shows placeholder text, so rows are rebuilt after it)
        self.page.eval(f"window._rootEl=YapnrTree.elementFor({root_id!r});1")
        self.stream_events(
            [
                dict(
                    candidate=f"ladder/keep-on/case-a/s0/aa-new-{i}",
                    kind="candidate_queued",
                    data={},
                )
                for i in range(2)
            ]
        )
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("aa-new-1" in self.row_texts()) or None, 5)
        self.assertTrue(self.page.eval(in_order))
        self.assertTrue(self.page.eval(f"YapnrTree.elementFor({root_id!r})===window._rootEl"))

    # ------------------------------------------------------------------ scroll position across live updates
    def test_tree_body_scroll_position_survives_live_updates(self):
        # The reported bug: every update to the experiment progress (a real engine streaming new
        # events into the live directory, picked up by the viewer's ingest loop and the page's
        # own poll(), same as production) used to reset .tr-body's scroll position back to
        # whatever row last had focus. A real campaign keeps streaming throughout this test.
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-01" in self.row_texts()) or None, 5)
        # Force a scrollbar regardless of how many rows this fixture happens to have.
        self.page.eval("document.querySelector('.tr-body').style.maxHeight='40px';1")
        # Select the very first row (expected to focus+scroll it into view -- that part of
        # focus-driven scrolling is legitimate and is not what this test is about) and let that
        # settle.
        self.page.eval("document.getElementById('tr-row-0').click();1")
        time.sleep(0.3)
        # Now the user scrolls away on their own, as far as possible from that focused top row,
        # to look at other rows further down -- a buggy refocus-on-every-update would snap the
        # view straight back up to row 0.
        self.page.eval("let b=document.querySelector('.tr-body');b.scrollTop=b.scrollHeight;1")
        scroll_before = self.page.eval("document.querySelector('.tr-body').scrollTop")
        self.assertGreater(scroll_before, 0)
        focus_before = self.page.eval("document.activeElement&&document.activeElement.id")
        revision_before = self.revision()
        self.stream_events(
            [
                dict(
                    candidate=f"ladder/keep-on/case-a/s0/live-{i}",
                    kind="candidate_queued",
                    data={},
                )
                for i in range(3)
            ]
        )
        self.assertGreater(self.revision(), revision_before)  # the stream really was picked up
        self.assertEqual(
            self.page.eval("document.querySelector('.tr-body').scrollTop"), scroll_before
        )
        self.assertEqual(
            self.page.eval("document.activeElement&&document.activeElement.id"), focus_before
        )

    def test_lanes_panel_scroll_survives_live_updates_on_phone(self):
        # Same bug, the other scroll container: on phone width the Experiments drawer
        # (#lanes-panel) is itself `overflow:auto` around the toolbar + tree, and is the one a
        # thumb actually scrolls there.
        self.goto(width=412, height=915, mobile=True)
        self.page.eval("YapnrPanels.setLanes(true);1")
        wait_for(lambda: (self.page.eval("YapnrTree.rows().length") > 0) or None, 5)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-01" in self.row_texts()) or None, 5)
        panel = "document.getElementById('lanes-panel')"
        self.page.eval("document.getElementById('tr-row-0').click();1")
        time.sleep(0.3)
        self.page.eval(f"{panel}.style.maxHeight='140px';1")
        self.page.eval(f"{panel}.scrollTop={panel}.scrollHeight;1")
        scroll_before = self.page.eval(f"{panel}.scrollTop")
        self.assertGreater(scroll_before, 0)
        revision_before = self.revision()
        self.stream_events(
            [
                dict(
                    candidate="ladder/keep-on/case-a/s0/live-phone",
                    kind="candidate_queued",
                    data={},
                )
            ]
        )
        self.assertGreater(self.revision(), revision_before)
        self.assertEqual(self.page.eval(f"{panel}.scrollTop"), scroll_before)

    # ------------------------------------------------------------------ board selection still works
    def test_selecting_a_leaf_drives_the_board_like_before(self):
        self.goto(width=1400, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-00" in self.row_texts()) or None, 5)
        self.page.eval(
            "YapnrTree.rows().find(r=>r.lane&&r.lane.id.endsWith('s0/initial-start-00'))"
            ".node;document.querySelectorAll('.tr-row').forEach(el=>{"
            "if(el.querySelector('.tr-label')&&el.querySelector('.tr-label').title.endsWith("
            "'s0/initial-start-00'))el.click()});1"
        )
        wait_for(lambda: ((self.page.eval("laneId") or "").endswith("initial-start-00")) or None, 5)
        self.assertTrue(self.page.eval("laneId").endswith("initial-start-00"))

    def test_selecting_a_group_shows_its_best_leaf(self):
        self.goto(width=1400, height=900)
        self.page.eval(
            "document.querySelectorAll('.tr-row').forEach(el=>{"
            "if(el.querySelector('.tr-label')&&el.querySelector('.tr-label').title==='ladder/keep-on/case-a')"
            "el.click()})"
        )
        wait_for(lambda: (self.page.eval("laneId")) or None, 5)
        # the best leaf is whatever's still actively running (s1/initial-start-01, mid route) --
        # a single failed sibling elsewhere in the group must not steal the selection away from
        # the work actually in progress.
        self.assertTrue(self.page.eval("laneId").endswith("s1/initial-start-01"))

    # ------------------------------------------------------------------ phone width
    def test_phone_width_tree_lives_in_the_experiments_drawer(self):
        self.goto(width=412, height=915, mobile=True)
        self.assertFalse(self.page.eval("YapnrPanels.lanesOpen()"))
        self.page.eval("YapnrPanels.setLanes(true);1")
        wait_for(lambda: (self.page.eval("YapnrTree.rows().length") > 0) or None, 5)
        self.assertEqual(
            self.page.eval("getComputedStyle(document.getElementById('lanes-panel')).position"),
            "fixed",
        )
        self.assertLessEqual(
            self.page.eval("document.documentElement.scrollWidth"),
            self.page.eval("window.innerWidth"),
        )
        self._save_shot("phone-experiment-tree")

    def test_desktop_screenshot(self):
        if not SCREEN_DIR:
            self.skipTest("set --test_env=YAPNR_SCREEN_DIR to save screenshots")
        self.goto(width=1600, height=900)
        self.page.eval("document.querySelector('.tr-btns button').click();1")
        wait_for(lambda: ("initial-start-00" in self.row_texts()) or None, 5)
        self._save_shot("desktop-experiment-tree")


if __name__ == "__main__":
    unittest.main()
