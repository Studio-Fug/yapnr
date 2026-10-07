"""Timing aggregation for the viewer's Timing panel (``GET /api/timing``).

Reads every event under a live root's ``events/`` directory directly (not the capped, in-memory
``Viewer.state["events"]``, which only keeps the last 300) and summarizes them per stage, per
lane and over time, scoped to the whole campaign or to one tree node (a lane-id prefix, exactly
what :mod:`yapnr.viewer.lane_tree` groups under).

Three event shapes feed this:

- ``stage_start``/``stage_end`` (``hardware/pnr/pnr/stage_timing.py``): *observed* pipeline-stage
  spans, real seconds, canonicalized against :data:`pnr.stage_timing.STAGES`.
- ``task_timing`` (``yapnr/exp/timing.py``, the GCP mirror): *observed* infrastructure spans
  (``queue-wait``, ``boot-fetch``, ``run``) from a Batch task's own status history. These are
  real seconds too, but they are never pipeline stages -- a task's ``run`` span covers the same
  wall time as the engine's own stage spans on that lane -- so they are aggregated separately
  (:data:`task_overhead... <aggregate>`'s ``task_overhead``), never folded into ``stages`` or
  ``total_seconds``.
- Everything else a campaign already emits (``candidate_start``, ``candidate_complete``,
  ``phase_start``, ``source_round_start``, ``geometry_result``): no stage boundary is recorded, so
  :func:`_estimate_spans` turns the event *timestamps* themselves into spans -- consecutive marker
  events on one lane bound a span, labeled by whichever marker opened it. Every span this path
  produces carries ``estimated=True``.

A lane with observed ``stage_start``/``stage_end`` spans for some stages still keeps estimated
spans for any *other* stage it has no observed events for (gloss, on every driver so far, never
emits stage_start/stage_end -- dropping its estimated span would silently erase 30-45% of a
typical campaign's time). Only a stage covered by an observed span on that same lane drops its
estimated counterpart (observed and estimated boundaries disagree about exactly when a stage
ends, and showing both would double count). The aggregate response carries ``mode``
("observed"/"estimated"/"mixed"/"empty") so the panel can say plainly which numbers, if any, are
derived rather than measured.

**Bounding spans to a lane's own event window.** An unclosed span -- an ``_estimate_spans``
marker with no following marker, or a ``stage_start`` whose ``stage_end`` never arrived (the
process crashed or was preempted mid-stage) -- must never be stretched past what the lane itself
actually reports. :func:`_lane_pipeline_spans` takes an explicit ``open_bound`` (computed once per
lane in :func:`aggregate`, see below) and every such span closes there, never at a later event's
raw timestamp and never at the live wall clock unless the lane is genuinely still running.

A lane's **terminal event** is not necessarily trustworthy as *when the work ended*, only as
*that it ended*: ``task_complete`` (:data:`MIRROR_TERMINAL_KINDS`) is synthesized by an external
mirror/catch-up process once it notices a GCP task finished (``yapnr.exp.live``'s synthesizer),
potentially long after the engine's own last real event -- a campaign that is actually done in
twenty minutes can sit with nothing happening until the next catch-up poll hours later, and that
whole gap must not be charged to whichever pipeline stage happened to be open when the engine
stopped. This is exactly what produced multi-hour "route"/"gloss" medians against lv2p2-rungs-all
and dp-ab2 (real fixtures, see ``tests/unit/viewer/fixtures/timing/``): one trailing marker
stretched across the mirror's detection lag. ``case_complete``/``case_failed``/
``candidate_complete``/``candidate_failed``/``iteration_complete`` are emitted in-process by the
engine itself and their timestamps are real -- ``open_bound`` only ever excludes
:data:`MIRROR_TERMINAL_KINDS` events, never these.

Concretely, per lane: ``last_real`` is the latest event time among the lane's events excluding any
:data:`MIRROR_TERMINAL_KINDS` event (falling back to the lane's actual last event if every event
is one); the lane is ``terminal`` if it has *any* event in :data:`TERMINAL_KINDS` (unchanged -- a
mirror-synthesized terminal event still means "done", just not "done right at this timestamp");
it is ``running`` iff not terminal and ``now - last_real <= STALE_SECONDS``; and ``open_bound`` is
``now`` while running, else ``last_real``. A span that comes out empty (its only content was the
trailing, now-unbounded marker) contributes nothing to that stage -- the time is real, but which
stage it belongs to is not known, so it surfaces as ``unattributed`` instead of a guess (see
below).

**Running.** A lane reads ``running`` unless: it has its own terminal event (:data:`TERMINAL_KINDS`
-- ``candidate_complete``/``candidate_failed``/``case_complete``/``case_failed``/
``iteration_complete``/``task_complete``); or a GCP task_timing ``run`` span closed by a genuinely
terminal Batch state (``data.terminal``, set by :mod:`yapnr.exp.timing` -- a span merely closed by
a *retry* after preemption must not read as the lane finishing, see its module docstring); or,
lacking either, its last event is more than :data:`yapnr.viewer.progress.STALE_SECONDS` behind
``now`` (a campaign that ended five minutes ago is not "still running" just because none of its
lanes happened to report a terminal event -- the same reasoning
:func:`yapnr.viewer.progress.classify` already applies lane-by-lane). ``now`` defaults to the wall
clock; a test wanting deterministic "still running" behaviour against synthetic timestamps passes
an explicit ``now`` close to them, exactly as ``tests/unit/viewer/test_progress.py`` does for
:func:`yapnr.viewer.progress.classify`.

``total_seconds`` sums stage time *across lanes that ran in parallel* -- it is a lane-seconds
total, not a wall-clock figure. ``wall_seconds`` is the actual wall-clock span of the scope (its
last real event minus its first: like a span, a finished lane's window ends at its last event
outside :data:`MIRROR_TERMINAL_KINDS`, and a lane with only mirror events is left out of the
range), extended to ``now`` only while some lane in scope is genuinely
``running`` (a live campaign's span keeps growing as it runs; a finished one's does not, no matter
when someone happens to load the panel). Concurrency counts only *leaf* lanes (a lane with no
child lane under it in the same scope) -- counting a parent candidate lane and the child lanes it
spawned (e.g. ``initial-start-NN`` screening candidates) together overstates how many things were
really running at once.

**Unattributed time and coverage.** Per unit (below), a ``coverage_wall`` figure minus the union of
its pipeline spans is that unit's ``unattributed`` time: wall-clock the lane genuinely spent *doing
something real*, with no stage event to say where. ``coverage_wall`` is, in order: the task
command's own measured wall (``data.wall_s`` on the mirror's ``task_complete`` -- the in-task
wrapper times the engine process; a measurement, unlike that event's timestamp, and it includes the
engine's work before its first live event), once the lane is finished; else the task's ``run``
task_timing span (Batch RUNNING-state transitions, GCP only; it also holds the wrapper's input
staging and result upload, which is infrastructure, already shown by the task_overhead ``run``
row); else ``now - start`` while running; else ``last_real - start``. Never the raw
first-to-last-*recorded*-event range: queue-wait/boot is already its own ``task_overhead`` row, and
a trailing mirror-synthesized terminal event's detection lag is not pipeline activity at all. A
lane with a known task wall is a *unit* that absorbs every descendant lane in scope
(``initial-start-NN`` screening children run inside the same task): their spans count toward its
attributed time and their windows are not counted again. Attributed time is the *union* of a unit's
pipeline spans (overlaps counted once), capped at ``coverage_wall``, so coverage never exceeds 1.
Aggregated in the same shape as a stage (:func:`_stat_block`) under the top-level ``unattributed``
key, and ``coverage`` (also top-level, and in the CLI table) is attributed total over
``coverage_wall`` total across the scope -- the fraction of a lane's real run time any stage
actually accounts for. A campaign relying on marker-estimated spans with a sparse marker set reads
a low coverage; one instrumented with ``stage_start``/``stage_end`` through its last real phase
reads close to 1.0.

Caching: one :class:`_RootCache` per live root, keyed off the ``events`` directory's mtime and the
set of filenames already parsed (the same incremental-scan shape as ``Viewer.ingest`` in
``server.py``); a repeat call against an unchanged directory re-parses nothing. Aggregation itself
(histograms, groups, timeline) is recomputed per call from the cached parsed events -- cheap even
at a few thousand events, so a 3000-event campaign answers in low milliseconds after the first
scan.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from yapnr.viewer.progress import STALE_SECONDS

try:
    from pnr.stage_timing import STAGES, canonical
except ImportError:  # pragma: no cover - the viewer always has pnr on its path (see runtime.py)
    STAGES = ()

    def canonical(name: str) -> str:
        return name


# A lane's own terminal event: it never reads "running" once one of these appears, regardless of
# staleness.
TERMINAL_KINDS = frozenset(
    {
        "candidate_complete",
        "candidate_failed",
        "case_complete",
        "case_failed",
        "iteration_complete",
        "task_complete",
    }
)

# The subset of TERMINAL_KINDS synthesized by an external mirror/catch-up process after the fact
# (``yapnr.exp.live``'s synthesizer) rather than emitted in-process by the engine when the work
# actually finished. Their own timestamp is when the mirror *noticed*, not when the lane's real
# work ended -- trustworthy for "is this lane done" (TERMINAL_KINDS, unchanged), not for "bound an
# open span's end" (see the module docstring's "Bounding spans" section). `case_complete`/
# `case_failed` are emitted by hardware/pnr/regression/run.py itself, in-process, and
# `candidate_complete`/`candidate_failed`/`iteration_complete` by the engine's own candidate loop
# -- all four real timestamps, excluded from this set on purpose.
MIRROR_TERMINAL_KINDS = frozenset({"task_complete"})

# GCP task-level spans (yapnr.exp.timing): infrastructure overhead, never a pipeline stage. Kept
# out of `stages`/`total_seconds` (see module docstring); aggregated on their own as
# `task_overhead`, in this order.
TASK_STAGES = ("queue-wait", "boot-fetch", "run")

# Markers the estimator reads off a campaign that never emitted stage_start/stage_end. Each is
# (event kind, function(event) -> label or None); a lane's markers are its own events of these
# kinds, in time order, each opening a span that runs to the next marker (or the lane's last
# event). ``None`` from the label function means "boundary only, no span of its own" (so
# ``candidate_complete`` closes the final span without starting a new, empty one).
#
# ``source_round_start`` fires right after a round's placement/legalize search is done, as the
# routing portion of that same round begins (``pnr/route/feedback.py``) -- the span it opens is
# routing, not placement; labeling it "global-placement" (the pre-fix behaviour) attributed an
# entire round's routing time to the wrong stage on every campaign that predates stage events.
#
# A ``candidate_start`` on an ``initial-start-NN`` child lane is the initial-pool screening loop
# trying one candidate, not the case lane's own setup -- see ``pnr/place/initial_pool.py``.
_FALLBACK_MARKERS = (
    (
        "candidate_start",
        lambda e: (
            "initial-pool-screening" if "initial-start" in (e.get("candidate") or "") else "setup"
        ),
    ),
    ("source_round_start", lambda e: "route"),
    ("phase_start", lambda e: e.get("data", {}).get("phase") or e.get("data", {}).get("label")),
    ("geometry_result", lambda e: e.get("data", {}).get("phase") or "gloss"),
    ("candidate_complete", lambda e: None),
)
_FALLBACK_KINDS = {kind for kind, _ in _FALLBACK_MARKERS}
_MARKER_LABEL = dict(_FALLBACK_MARKERS)


def _in_scope(candidate: str, scope: str) -> bool:
    if not scope:
        return True
    return candidate == scope or candidate.startswith(scope + "/")


class _RootCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.seen: set = set()
        self.dir_mtime: Optional[int] = None
        self.events: List[dict] = (
            []
        )  # sorted by time as files are discovered (filenames sort by time)

    def refresh(self, root: Path) -> List[dict]:
        event_dir = root / "events"
        with self.lock:
            try:
                stamp = event_dir.stat().st_mtime_ns
            except OSError:
                return list(self.events)
            if stamp == self.dir_mtime:
                return list(self.events)
            try:
                names = sorted(
                    n for n in os.listdir(event_dir) if n.endswith(".json") and n not in self.seen
                )
            except OSError:
                return list(self.events)
            for name in names:
                try:
                    event = json.loads((event_dir / name).read_text())
                except (OSError, ValueError):
                    continue
                self.events.append(event)
                self.seen.add(name)
            self.dir_mtime = stamp
            self.events.sort(key=lambda e: e.get("time", 0))
            return list(self.events)


_caches: Dict[str, _RootCache] = {}
_caches_lock = threading.Lock()


def _cache_for(root: Path) -> _RootCache:
    key = str(root)
    with _caches_lock:
        cache = _caches.get(key)
        if cache is None:
            cache = _caches[key] = _RootCache()
        return cache


def load_events(root: Path) -> List[dict]:
    """Every live event under ``root/events``, oldest first (incrementally cached per root)."""
    return _cache_for(Path(root)).refresh(Path(root))


# ------------------------------------------------------------------ span extraction


def _observed_spans(lane_events: Sequence[dict], open_bound: Optional[float] = None) -> List[dict]:
    """``stage_start``/``stage_end`` pairs -- real pipeline-stage spans -- plus, for any
    ``stage_start`` whose ``stage_end`` never arrived (the process crashed or was preempted
    mid-stage), one span closed at ``open_bound`` (the lane's last real event, or ``now`` while
    genuinely running; see the module docstring). The latter are marked ``estimated=True``: the
    start was observed, the end was not. ``task_timing`` (GCP infrastructure spans) is handled
    separately by :func:`_task_spans`."""
    spans = []
    open_by_label: Dict[str, List[dict]] = {}
    for e in lane_events:
        if e["kind"] == "stage_start":
            data = e.get("data", {})
            open_by_label.setdefault(data.get("label", data.get("stage")), []).append(e)
        elif e["kind"] == "stage_end":
            data = e.get("data", {})
            stage = data.get("stage")
            if stage is None:
                continue
            seconds = data.get("seconds")
            label = data.get("label", stage)
            start_event = None
            pending = open_by_label.get(label)
            if pending:
                start_event = pending.pop(0)
            start = start_event["time"] if start_event else (e["time"] - (seconds or 0))
            end = e["time"]
            if seconds is None:
                seconds = max(0.0, end - start)
            spans.append(
                dict(stage=canonical(stage), start=start, end=end, seconds=seconds, estimated=False)
            )
    if open_bound is not None:
        for pending in open_by_label.values():
            for start_event in pending:
                data = start_event.get("data", {})
                stage = canonical(data.get("stage") or data.get("label"))
                start = start_event["time"]
                if open_bound <= start:
                    continue
                spans.append(
                    dict(
                        stage=stage,
                        start=start,
                        end=open_bound,
                        seconds=open_bound - start,
                        estimated=True,
                    )
                )
    return spans


def _task_spans(lane_events: Sequence[dict]) -> List[dict]:
    """``task_timing`` events (``yapnr.exp.timing``'s GCP mirror) as infra spans: queue-wait,
    boot-fetch, run. Never a pipeline stage -- see the module docstring. Each carries ``terminal``
    (``data.terminal``, default ``False``): whether a ``run`` span closed because the task truly
    finished (SUCCEEDED/FAILED/CANCELLED) rather than because it was preempted and retried."""
    spans = []
    for e in lane_events:
        if e["kind"] != "task_timing":
            continue
        data = e.get("data", {})
        stage = data.get("stage")
        seconds = data.get("seconds")
        if stage is None or seconds is None:
            continue
        end = e["time"]
        start = end - seconds
        spans.append(
            dict(
                stage=stage,
                start=start,
                end=end,
                seconds=seconds,
                estimated=False,
                terminal=bool(data.get("terminal")),
            )
        )
    return spans


def _estimate_spans(lane_events: Sequence[dict], open_bound: Optional[float] = None) -> List[dict]:
    """Marker-to-marker spans; the trailing marker (no following marker on this lane) closes at
    ``open_bound`` rather than the lane's raw last event -- see the module docstring's "Bounding
    spans" section for why (a lane whose last real marker predates a delayed terminal event must
    not have that whole gap folded into the marker's stage)."""
    markers = []
    for e in lane_events:
        if e["kind"] not in _FALLBACK_KINDS:
            continue
        label = _MARKER_LABEL[e["kind"]](e)
        markers.append((e["time"], label))
    if not markers:
        return []
    last_time = open_bound if open_bound is not None else lane_events[-1]["time"]
    spans = []
    for i, (t, label) in enumerate(markers):
        if label is None:
            continue
        end = markers[i + 1][0] if i + 1 < len(markers) else last_time
        if end <= t:
            continue
        spans.append(
            dict(stage=canonical(label), start=t, end=end, seconds=end - t, estimated=True)
        )
    return spans


def _lane_pipeline_spans(
    lane_events: Sequence[dict], open_bound: Optional[float] = None
) -> List[dict]:
    """A lane's pipeline-stage spans: every observed span, plus an estimated span for any stage
    the lane has *no* observed span for (gloss, most often -- see the module docstring). Sorted
    by start time. ``open_bound`` closes any unclosed span (a dangling ``stage_start``, or the
    trailing fallback marker) -- see :func:`_observed_spans`/:func:`_estimate_spans`."""
    observed = _observed_spans(lane_events, open_bound)
    estimated_all = _estimate_spans(lane_events, open_bound)
    if not observed:
        return estimated_all
    covered = {span["stage"] for span in observed}
    estimated = [span for span in estimated_all if span["stage"] not in covered]
    return sorted(observed + estimated, key=lambda s: s["start"])


def _union_seconds(spans: Sequence[dict]) -> float:
    """Total seconds covered by ``spans`` with overlaps counted once."""
    total = 0.0
    cur_start = cur_end = None
    for span in sorted(spans, key=lambda s: s["start"]):
        if cur_end is None or span["start"] > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = span["start"], span["end"]
        else:
            cur_end = max(cur_end, span["end"])
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def _lane_terminal(lane_events: Sequence[dict], task_spans: Sequence[dict]) -> bool:
    if any(e["kind"] in TERMINAL_KINDS for e in lane_events):
        return True
    return any(span["stage"] == "run" and span.get("terminal") for span in task_spans)


# ------------------------------------------------------------------ aggregation


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = p * (len(sorted_values) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_values[lo]
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _histogram(values: Sequence[float], bins: int = 12) -> dict:
    positive = sorted(v for v in values if v > 0)
    if not positive:
        return dict(edges=[], counts=[])
    lo, hi = positive[0], positive[-1]
    if lo == hi:
        return dict(edges=[lo, hi], counts=[len(positive)])
    log_lo, log_hi = math.log10(lo), math.log10(hi)
    edges = [10 ** (log_lo + (log_hi - log_lo) * i / bins) for i in range(bins + 1)]
    counts = [0] * bins
    for v in positive:
        i = bisect.bisect_right(edges, v) - 1
        i = min(max(i, 0), bins - 1)
        counts[i] += 1
    return dict(edges=edges, counts=counts)


def _stat_block(durations: Sequence[float], total_for_share: Optional[float] = None) -> dict:
    durations_sorted = sorted(durations)
    total = sum(durations_sorted)
    block = dict(
        count=len(durations_sorted),
        total=total,
        mean=total / len(durations_sorted),
        median=_percentile(durations_sorted, 0.5),
        p90=_percentile(durations_sorted, 0.9),
        max=durations_sorted[-1],
        histogram=_histogram(durations_sorted),
    )
    if total_for_share is not None:
        block["share"] = (total / total_for_share) if total_for_share else 0.0
    return block


def stage_order(stages: Sequence[str]) -> List[str]:
    known = [s for s in STAGES if s in stages]
    other = sorted(s for s in stages if s not in STAGES)
    return known + other


def aggregate(root: Path, scope: str = "", now: Optional[float] = None) -> dict:
    """The full timing response for ``root``, restricted to lanes under tree path ``scope``.

    See the module docstring for ``mode``, the estimation fallback, and what ``running`` means.
    ``now`` (default: the wall clock) is the time "still running" is judged against; a caller that
    wants deterministic staleness behaviour against fixed/synthetic timestamps passes one
    explicitly. The response is JSON-serializable as is (the API route returns it as is).
    """
    scope = scope.strip("/")
    now = now if now is not None else time.time()
    events = load_events(root)
    by_lane: Dict[str, List[dict]] = {}
    for e in events:
        candidate = e.get("candidate")
        if not candidate or not _in_scope(candidate, scope):
            continue
        by_lane.setdefault(candidate, []).append(e)

    candidates = set(by_lane)
    # A lane with a child lane under it in this same scope is a parent; only leaf lanes count
    # toward concurrency (see module docstring) and the Gantt rows the panel draws per lane are
    # unaffected either way.
    has_children = {
        c
        for c in candidates
        if any(other.startswith(c + "/") for other in candidates if other != c)
    }

    lanes_out = []
    stage_durations: Dict[str, List[float]] = {}
    task_durations: Dict[str, List[float]] = {s: [] for s in TASK_STAGES}
    any_observed = False
    any_estimated = False
    total_seconds = 0.0

    for candidate, lane_events in sorted(by_lane.items()):
        lane_events.sort(key=lambda e: e.get("time", 0))
        task_spans = _task_spans(lane_events)
        terminal = _lane_terminal(lane_events, task_spans)
        start = lane_events[0]["time"]
        end = lane_events[-1]["time"]
        idle_seconds = now - end
        running = not terminal and idle_seconds <= STALE_SECONDS
        # The bound an unclosed span may extend to: the lane's own last *real* event (excluding
        # MIRROR_TERMINAL_KINDS, whose timestamp is detection time, not completion time -- see the
        # module docstring), or `now` while the lane is genuinely still running.
        real_events = [e for e in lane_events if e["kind"] not in MIRROR_TERMINAL_KINDS]
        last_real = real_events[-1]["time"] if real_events else end
        open_bound = now if running else last_real
        # A finished lane's own window ends at its last real event too: the Gantt bar, the
        # "slowest lanes" seconds, concurrency and the campaign's wall_seconds must not carry the
        # mirror's detection lag any more than a span may (lv2p2-rungs-all read a 13520s wall for
        # ~15 minutes of real activity, the rest being the catch-up poller's delay).
        if not running:
            end = last_real
        spans = _lane_pipeline_spans(lane_events, open_bound)
        for span in spans:
            any_observed = any_observed or not span["estimated"]
            any_estimated = any_estimated or span["estimated"]
            stage_durations.setdefault(span["stage"], []).append(span["seconds"])
            total_seconds += span["seconds"]
        for span in task_spans:
            if span["stage"] in task_durations:
                task_durations[span["stage"]].append(span["seconds"])

        lane_wall = (end - start) if not running else max(end - start, now - start)
        # Unattributed/coverage measure against the task's own "run" span when one is known (the
        # GCP mirror's task_timing, real Batch status transitions) rather than the lane's full
        # first-to-last-event range: that range also spans queue-wait and boot (already their own
        # task_overhead stage, never meant to count as pipeline coverage) and any mirror-detection
        # lag past the lane's terminal event (the same artifact `open_bound` excludes above).
        # Lacking a run span -- the local backend, a campaign predating task_timing, or (seen
        # live: GCP Batch's statusEvents are not always still listable once a job has fully
        # finished, even moments later) a late mirror poll that simply missed catching it -- the
        # fallback is `last_real`, the exact same mirror-lag-excluding bound `open_bound` already
        # uses for spans, not the raw `lane_wall`: a dangling task_complete must not dilute
        # coverage any more than it is allowed to inflate a span.
        run_spans = [s for s in task_spans if s["stage"] == "run"]
        # The runner's own measured task wall time, carried on the mirror's task_complete
        # (``data.wall_s``): the task's real run span when Batch status events were not caught.
        # Unlike the event's timestamp it is not detection lag, and it includes work before the
        # task's first live event, which is exactly the gap coverage has to show.
        task_walls = [
            e["data"]["wall_s"]
            for e in lane_events
            if e["kind"] in MIRROR_TERMINAL_KINDS
            and isinstance((e.get("data") or {}).get("wall_s"), (int, float))
        ]
        if task_walls and not running:
            # The task command's own measured wall (the in-task wrapper times the engine
            # process): the most direct figure for what pipeline instrumentation can reach. The
            # wrapper's own input staging and result upload around it fall inside Batch's "run"
            # span but are infrastructure, already visible as the task_overhead "run" row.
            coverage_wall = float(task_walls[-1])
        elif run_spans:
            coverage_wall = max(
                0.0, max(s["end"] for s in run_spans) - min(s["start"] for s in run_spans)
            )
        elif running:
            coverage_wall = now - start
        else:
            coverage_wall = last_real - start
        lanes_out.append(
            dict(
                candidate=candidate,
                start=start,
                end=end,
                running=running,
                leaf=candidate not in has_children,
                seconds=lane_wall,
                spans=sorted(spans + task_spans, key=lambda s: s["start"]),
                pipeline_spans=spans,
                coverage_wall=coverage_wall,
                task_wall=bool(run_spans or task_walls),
                mirror_only=not real_events,
            )
        )

    # Unattributed/coverage per *task*: a lane whose own task wall is known (a run span, or the
    # runner's wall_s) absorbs every descendant lane under it in scope (initial-start-NN
    # screening children and the like run inside that same task), so their spans count toward
    # its attributed time and their windows are not counted a second time. A lane with no such
    # ancestor is its own unit. Attributed time is the *union* of the unit's pipeline spans
    # (overlapping/nested/parallel spans count once), capped at the unit's wall: coverage is a
    # fraction of real wall time, never above 1.
    task_lanes = {lane["candidate"] for lane in lanes_out if lane["task_wall"]}
    units: Dict[str, List[dict]] = {}
    for lane in lanes_out:
        parts = lane["candidate"].split("/")
        owner = lane["candidate"]
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            if ancestor in task_lanes:
                owner = ancestor
                break
        units.setdefault(owner, []).append(lane)
    by_candidate = {lane["candidate"]: lane for lane in lanes_out}
    unattributed_durations: List[float] = []
    lane_wall_total = 0.0
    attributed_total = 0.0
    for lane in lanes_out:
        lane["unattributed"] = 0.0
    for owner, members in sorted(units.items()):
        unit_wall = by_candidate[owner]["coverage_wall"]
        unit_spans = [span for member in members for span in member["pipeline_spans"]]
        attributed = min(_union_seconds(unit_spans), unit_wall)
        unattributed = unit_wall - attributed
        by_candidate[owner]["unattributed"] = unattributed
        unattributed_durations.append(unattributed)
        lane_wall_total += unit_wall
        attributed_total += attributed

    stages = {}
    for stage, durations in stage_durations.items():
        stages[stage] = _stat_block(durations, total_for_share=total_seconds)

    task_overhead = {}
    for stage in TASK_STAGES:
        if task_durations[stage]:
            task_overhead[stage] = _stat_block(task_durations[stage])

    # Per-group (parent tree path) stacked stage shares -- pipeline stages only, same reasoning
    # as `stages`/`total_seconds`.
    groups: Dict[str, Dict[str, float]] = {}
    for lane in lanes_out:
        group = lane["candidate"].rsplit("/", 1)[0] if "/" in lane["candidate"] else ""
        bucket = groups.setdefault(group, {})
        for span in lane["pipeline_spans"]:
            bucket[span["stage"]] = bucket.get(span["stage"], 0.0) + span["seconds"]
    group_breakdown = []
    for group, totals in sorted(groups.items()):
        group_total = sum(totals.values()) or 1.0
        group_breakdown.append(
            dict(
                group=group,
                total=sum(totals.values()),
                shares={stage: seconds / group_total for stage, seconds in totals.items()},
            )
        )

    # Concurrency over time: how many *leaf* lanes are "open" (started, not yet ended) at evenly
    # spaced points across the scope's wall-clock span.
    leaf_lanes = [lane for lane in lanes_out if lane["leaf"]]
    concurrency = []
    wall_seconds = 0.0
    if lanes_out:
        # A lane holding nothing but a mirror-synthesized terminal event (a task that never
        # emitted live events of its own) has no real timestamp at all: it says "done", not when.
        # It is left out of the campaign's wall-clock range whenever any lane has real events.
        windowed = [lane for lane in lanes_out if not lane["mirror_only"]] or lanes_out
        t0 = min(lane["start"] for lane in windowed)
        t1 = max(lane["end"] for lane in windowed)
        if any(lane["running"] for lane in lanes_out):
            # A still-running scope's wall-clock span keeps growing with the live clock, not just
            # whatever's been seen so far -- a finished scope's never reaches past its last event.
            t1 = max(t1, now)
        wall_seconds = max(0.0, t1 - t0)
        points = 60
        span = (t1 - t0) or 1.0
        for i in range(points + 1):
            t = t0 + span * i / points
            n = sum(1 for lane in leaf_lanes if lane["start"] <= t <= lane["end"])
            concurrency.append(dict(time=t, running=n))

    slowest = sorted(lanes_out, key=lambda lane: lane["seconds"], reverse=True)[:10]

    if any_observed and any_estimated:
        mode = "mixed"
    elif any_observed:
        mode = "observed"
    elif any_estimated:
        mode = "estimated"
    else:
        mode = "empty"

    # Unattributed: real lane wall-clock time no stage span claims (see the module docstring).
    # Aggregated the same shape as a stage; `coverage` is attributed/lane-wall across the scope.
    unattributed_stats = (
        _stat_block(unattributed_durations, total_for_share=lane_wall_total)
        if unattributed_durations
        else {}
    )
    coverage = (attributed_total / lane_wall_total) if lane_wall_total else 1.0

    return dict(
        schema="pnr-timing-v1",
        scope=scope,
        mode=mode,
        lane_count=len(lanes_out),
        running_count=sum(1 for lane in lanes_out if lane["running"]),
        total_seconds=total_seconds,  # summed across lanes that ran in parallel, NOT wall-clock
        wall_seconds=wall_seconds,  # the scope's real wall-clock span (last real event - first)
        stage_order=stage_order(list(stages)),
        stages=stages,
        unattributed=unattributed_stats,
        coverage=coverage,
        task_overhead=task_overhead,
        groups=group_breakdown,
        timeline=[
            dict(
                candidate=lane["candidate"],
                start=lane["start"],
                end=lane["end"],
                running=lane["running"],
                spans=lane["spans"],  # pipeline + task spans together, for the Gantt
                pipeline_spans=lane["pipeline_spans"],  # pipeline spans only (stage stats'/
                # groups' own source of truth; task spans are deliberately excluded, see docstring)
            )
            for lane in lanes_out
        ],
        concurrency=concurrency,
        slowest=[
            dict(candidate=lane["candidate"], seconds=lane["seconds"], running=lane["running"])
            for lane in slowest
        ],
    )
