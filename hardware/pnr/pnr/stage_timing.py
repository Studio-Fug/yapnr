"""Canonical PnR stage names plus a ``stage_start``/``stage_end`` emitter.

The ladder/route/hier/mc drivers already know where one real stage of work begins and ends (the
``run()`` phase wrapper in ``pnr.full_iteration``, the native-loop call, the initial-pool
screening loop); this module gives them one place to say so, in a fixed vocabulary the viewer's
timing panel aggregates against, rather than each driver inventing its own event shape.

``stage()`` is a context manager around one stage's span: it emits ``stage_start`` on entry and
``stage_end`` (with ``seconds``) on exit, success or failure, via ``pnr.live.emit`` -- itself a
no-op without ``PNR_LIVE_DIR``, so this adds no behavior and no overhead to an unprofiled,
non-live run (see ``hardware/pnr/tests/test_stage_timing.py``'s ``ByteIdenticalResultTests``:
``route_and_place``'s own result -- not just that no file gets written -- is identical with and
without ``PNR_LIVE_DIR`` set).

A driver's own phase name (``"placement"``, ``"coalesce"``, a native_loop phase label, ...) maps
onto the fixed list below through ``_ALIAS``; a name with no entry passes through unchanged and
the viewer buckets it under "other" rather than dropping it, so adding a new phase here is a
labeling nicety, never a correctness requirement.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Dict, Optional

from pnr.live import emit

# The viewer's fixed stage vocabulary, in pipeline order (yapnr/viewer/timing.py STAGE_ORDER must
# match this list; tests/unit/viewer/test_timing.py checks that directly).
STAGES = (
    "setup",
    "initial-pool-screening",
    "global-placement",
    "legalize",
    "detailed-placement",
    "fanout-escape",
    "route",
    "gloss",
    "plane-partition-pours",
    "drc-judge",
    "artifacts",
)

_ALIAS = {
    "placement": "setup",
    "assemble": "setup",
    "board-gen": "setup",
    # pnr.route.feedback's per-round placement search (deliberately excludes the initial-pool
    # screening call it wraps, which has its own child lanes under "initial-pool-screening" --
    # see the comment at its call site for why that time must not be double counted here).
    "source-round-place": "global-placement",
    "plane-access": "plane-partition-pours",
    "planes": "plane-partition-pours",
    "refill": "plane-partition-pours",
    # The native-loop call interleaves legalize, detailed placement, fanout/escape and routing
    # internally (pnr.native_loop); until it reports its own sub-stage boundaries this whole call
    # is attributed to "detailed-placement" rather than split arbitrarily. Documented gap.
    "native-loop": "detailed-placement",
    # PNR_ROUTE_COMPACT's rip-up-and-reroute step (pnr.place.route_compact): the same
    # kind of work as the initial route, just run again on a squeezed placement.
    "route-compact": "route",
    # PNR_HULL_NEST (pnr.place.hull.nest): legalized hull macros slid into each other's
    # notches, a detailed-placement move; its hierarchical rectangle fallback re-places and
    # knits one top seed again, routing work.
    "hull-nest": "detailed-placement",
    "hull-safety-net": "route",
    "coalesce": "route",
    "geometry-relax": "gloss",
    "pad-entry": "fanout-escape",
    "audit": "drc-judge",
    "feedback": "artifacts",
    # hardware/pnr/regression/run.py's own per-case stage names (the ladder-cell path: no
    # native-loop, no KiCad-subprocess full_iteration.py -- see its `run()` closure). "place-route"
    # itself is deliberately never wrapped there: it shells out to route_case.py/hier_case.py/
    # mc_case.py, which call pnr.route.feedback.route_and_place *in that subprocess*, already
    # emitting its own "source-round-place"/"route" spans; an outer span here would overlap them
    # and double count. "generate"/"writeback" build the initial board (board-gen/placement's
    # ladder-path equivalent); "drc"/"via-scan"/"checks" are the cold kicad-cli judge, same bucket
    # as "audit"; "gloss-measure" is the optional --gloss-measure A/B run, same bucket as "gloss".
    "generate": "setup",
    "writeback": "setup",
    "drc": "drc-judge",
    "via-scan": "drc-judge",
    "checks": "drc-judge",
    "gloss-measure": "gloss",
    # gloss_stage()'s cold kicad-cli DRC before and after the gloss pass (its keep/restore gate).
    "gloss-drc-before": "drc-judge",
    "gloss-drc-after": "drc-judge",
    # Once-per-task bootstrap before the per-case loop even starts (main(), ahead of its own
    # `run()` closure): hashing and freezing every source file, the optional native maze-kernel
    # compile, and the python/kicad version probes. The live timing-bounds check measured this as
    # the dominant share of a cold GCP task's otherwise-unattributed time (~16s of a ~26s task,
    # almost all of it "freeze-source") -- real setup work, same bucket as "generate"/"placement".
    "freeze-source": "setup",
    "maze-kernel-build": "setup",
    "version-check": "setup",
    # regression/route_case.py (the flat ladder driver, inside run.py's unwrapped "place-route"
    # subprocess): its imports and rule compilation before route_and_place. Its writes after
    # route_and_place go under "artifacts" directly.
    "driver-setup": "setup",
}


def canonical(name: str) -> str:
    """``name``'s canonical stage, or ``name`` itself when it has no entry in ``_ALIAS``."""
    return _ALIAS.get(name, name)


class CaseTimer:
    """Accumulates seconds per canonical stage for one case, for a single ``timing_summary``
    event at candidate-complete time (one less thing for the viewer to re-derive from spans)."""

    def __init__(self):
        self._seconds: Dict[str, float] = {}

    def add(self, canon: str, seconds: float) -> None:
        self._seconds[canon] = self._seconds.get(canon, 0.0) + seconds

    def summary(self) -> Dict[str, float]:
        return dict(self._seconds)

    def emit_summary(self, **extra) -> None:
        emit("timing_summary", data=dict(stage_seconds=self.summary(), **extra))


@contextmanager
def stage(name: str, timer: Optional[CaseTimer] = None, **extra):
    """Emit ``stage_start``/``stage_end`` around the wrapped span; ``stage_end`` always fires,
    including on an exception, so a failed stage still reports its real duration."""
    canon = canonical(name)
    emit("stage_start", data=dict(stage=canon, label=name, **extra))
    started = time.perf_counter()
    try:
        yield
    finally:
        seconds = time.perf_counter() - started
        emit("stage_end", data=dict(stage=canon, label=name, seconds=seconds, **extra))
        if timer is not None:
            timer.add(canon, seconds)
