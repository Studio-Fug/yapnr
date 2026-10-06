"""Timing aggregation for the viewer's Timing panel (``GET /api/timing``).

Reads every event under a live root's ``events/`` directory directly (not the capped, in-memory
``Viewer.state["events"]``, which only keeps the last 300) and summarizes them per stage, per
lane and over time, scoped to the whole campaign or to one tree node (a lane-id prefix, exactly
what :mod:`yapnr.viewer.lane_tree` groups under).

Two event shapes feed this:

- ``stage_start``/``stage_end`` (``hardware/pnr/pnr/stage_timing.py``) and ``task_timing``
  (``yapnr/exp/timing.py``, the GCP mirror): *observed* stage spans, real seconds.
- Everything else a campaign already emits (``candidate_start``, ``candidate_complete``,
  ``phase_start``, ``source_round_start``, ``geometry_result``): no stage boundary is recorded, so
  :func:`_estimate_spans` turns the event *timestamps* themselves into spans -- consecutive marker
  events on one lane bound a span, labeled by whichever marker opened it. Every span this path
  produces carries ``estimated=True`` and the aggregate response carries
  ``mode: "estimated"`` so the panel can say plainly that the numbers are derived, not measured.

A lane with observed stage events anywhere uses observed spans for its own stages and never mixes
in an estimated span for the same lane (observed and estimated boundaries disagree about *when* a
stage ends, and showing both would double count).

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
from pathlib import Path
from typing import Dict, List, Optional, Sequence

try:
    from pnr.stage_timing import STAGES, canonical
except ImportError:  # pragma: no cover - the viewer always has pnr on its path (see runtime.py)
    STAGES = ()

    def canonical(name: str) -> str:
        return name


# Markers the estimator reads off a campaign that never emitted stage_start/stage_end. Each is
# (event kind, function(event) -> label or None); a lane's markers are its own events of these
# kinds, in time order, each opening a span that runs to the next marker (or the lane's last
# event). ``None`` from the label function means "boundary only, no span of its own" (so
# ``candidate_complete`` closes the final span without starting a new, empty one).
_FALLBACK_MARKERS = (
    ("candidate_start", lambda e: "setup"),
    ("source_round_start", lambda e: "global-placement"),
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


def _observed_spans(lane_events: Sequence[dict]) -> List[dict]:
    spans = []
    open_by_label: Dict[str, List[dict]] = {}
    for e in lane_events:
        if e["kind"] == "stage_start":
            data = e.get("data", {})
            open_by_label.setdefault(data.get("label", data.get("stage")), []).append(e)
        elif e["kind"] in ("stage_end", "task_timing"):
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
    return spans


def _estimate_spans(lane_events: Sequence[dict]) -> List[dict]:
    markers = []
    for e in lane_events:
        if e["kind"] not in _FALLBACK_KINDS:
            continue
        label = _MARKER_LABEL[e["kind"]](e)
        markers.append((e["time"], label))
    if not markers:
        return []
    last_time = lane_events[-1]["time"]
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


def _lane_spans(lane_events: Sequence[dict]) -> List[dict]:
    observed = _observed_spans(lane_events)
    if observed:
        return observed
    return _estimate_spans(lane_events)


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


def stage_order(stages: Sequence[str]) -> List[str]:
    known = [s for s in STAGES if s in stages]
    other = sorted(s for s in stages if s not in STAGES)
    return known + other


def aggregate(root: Path, scope: str = "") -> dict:
    """The full timing response for ``root``, restricted to lanes under tree path ``scope``.

    See the module docstring for ``mode`` ("observed" | "estimated" | "mixed" | "empty") and the
    estimation fallback. The response is JSON-serializable as is (the API route returns it as
    is).
    """
    scope = scope.strip("/")
    events = load_events(root)
    by_lane: Dict[str, List[dict]] = {}
    for e in events:
        candidate = e.get("candidate")
        if not candidate or not _in_scope(candidate, scope):
            continue
        by_lane.setdefault(candidate, []).append(e)

    now = events[-1]["time"] if events else 0.0
    lanes_out = []
    stage_durations: Dict[str, List[float]] = {}
    any_observed = False
    any_estimated = False
    total_seconds = 0.0

    for candidate, lane_events in sorted(by_lane.items()):
        lane_events.sort(key=lambda e: e.get("time", 0))
        spans = _lane_spans(lane_events)
        observed = _observed_spans(lane_events)
        if observed:
            any_observed = True
        elif spans:
            any_estimated = True
        # A bare stage_end only closes one stage, not the lane: a plain ladder-cell campaign has
        # no engine-level completion event at all yet (unlike full_iteration's candidate_complete;
        # a ladder runner terminal event is tracked separately), so the only other real signal is
        # a GCP task's own "run" span closing (yapnr.exp.timing only ever reports that span once
        # it is actually closed -- the task is done, succeeded or failed).
        running = not any(
            e["kind"] == "candidate_complete"
            or (e["kind"] == "task_timing" and e.get("data", {}).get("stage") == "run")
            for e in lane_events
        )
        start = lane_events[0]["time"]
        end = lane_events[-1]["time"]
        for span in spans:
            stage_durations.setdefault(span["stage"], []).append(span["seconds"])
            total_seconds += span["seconds"]
        lanes_out.append(
            dict(
                candidate=candidate,
                start=start,
                end=end,
                running=running,
                seconds=(end - start) if not running else (now - start),
                spans=spans,
            )
        )

    stages = {}
    for stage, durations in stage_durations.items():
        durations_sorted = sorted(durations)
        stage_total = sum(durations_sorted)
        stages[stage] = dict(
            count=len(durations_sorted),
            total=stage_total,
            mean=stage_total / len(durations_sorted),
            median=_percentile(durations_sorted, 0.5),
            p90=_percentile(durations_sorted, 0.9),
            max=durations_sorted[-1],
            share=(stage_total / total_seconds) if total_seconds else 0.0,
            histogram=_histogram(durations_sorted),
        )

    # Per-group (parent tree path) stacked stage shares.
    groups: Dict[str, Dict[str, float]] = {}
    for lane in lanes_out:
        group = lane["candidate"].rsplit("/", 1)[0] if "/" in lane["candidate"] else ""
        bucket = groups.setdefault(group, {})
        for span in lane["spans"]:
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

    # Concurrency over time: sample how many lanes are "open" (started, not yet ended) at evenly
    # spaced points across the campaign's wall-clock span.
    concurrency = []
    if lanes_out:
        t0 = min(lane["start"] for lane in lanes_out)
        t1 = max(lane["end"] for lane in lanes_out)
        points = 60
        span = (t1 - t0) or 1.0
        for i in range(points + 1):
            t = t0 + span * i / points
            n = sum(1 for lane in lanes_out if lane["start"] <= t <= lane["end"])
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

    return dict(
        schema="pnr-timing-v1",
        scope=scope,
        mode=mode,
        lane_count=len(lanes_out),
        running_count=sum(1 for lane in lanes_out if lane["running"]),
        total_seconds=total_seconds,
        stage_order=stage_order(list(stages)),
        stages=stages,
        groups=group_breakdown,
        timeline=[
            dict(
                candidate=lane["candidate"],
                start=lane["start"],
                end=lane["end"],
                running=lane["running"],
                spans=lane["spans"],
            )
            for lane in lanes_out
        ],
        concurrency=concurrency,
        slowest=[
            dict(candidate=lane["candidate"], seconds=lane["seconds"], running=lane["running"])
            for lane in slowest
        ],
    )
