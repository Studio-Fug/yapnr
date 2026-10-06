"""The experiment-lane phase/progress model, in one place.

A lane's raw telemetry (``kind``, ``status``, ``phase``, ``opens``, ``violations``,
``last_route``, ``frames``...) is accurate but was never meant to be read by a person: phase
strings come straight from the PnR engine ("initial-placement screening complete",
"native-phase", "transaction-via-cleanup"...) and are the "unintelligible status text" the
viewer used to show verbatim. This module is the single place that turns that telemetry into:

- :data:`PHASES`: the canonical, ordered pipeline every lane is placed on (generate, place,
  legalize, detail, route, gloss, check), independent of which campaign produced the lane.
- :func:`classify`: where on that pipeline a lane is right now, as a 0..1 fraction suitable for
  a progress bar, plus a coarse ``state`` (queued / running / done / failed).
- :func:`humanize`: a short, plain-language status string for that classification ("Routing: 412
  of 530 connections (78%)"), with the raw phase/kind kept available for a tooltip.

Nothing here does any I/O; every function takes a lane dict (as stored in
``Viewer.state["lanes"]``) and returns a plain value, so this is unit-testable without a running
viewer or a live directory. :mod:`yapnr.viewer.server` calls :func:`classify_lane` once per lane
per poll and stores the result back onto the lane dict (``progress``, ``status_text``) so the
front end never has to re-derive it.

Engines evolve faster than this file: a phase string this module has never seen before is never
an error. :func:`classify` falls back to keyword matching, and failing that, to the lane's
current phase bucket held over from its last update (or ``generate`` if it has none yet), so an
unrecognised event kind still renders a sensible, monotonic bar instead of resetting to zero or
raising.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------- the canonical pipeline
# Every lane's progress bar is divided into these stages, in this order, regardless of which
# campaign (a "ladder" placement sweep, a Monte Carlo halving round, a single full-iteration
# candidate...) produced it. A lane that never visits a stage (most campaigns stop at "place" or
# "check") simply has nothing to report for the stages after the one it stopped at -- the bar
# still reads left to right as "how far through the whole pipeline", not "how far through the
# stages this particular lane happens to use".
PHASES = (
    ("generate", "Generating"),
    ("place", "Placing"),
    ("legalize", "Legalizing"),
    ("detail", "Detailed placement"),
    ("route", "Routing"),
    ("gloss", "Glossing"),
    ("check", "Checking"),
)
PHASE_KEYS = tuple(key for key, _label in PHASES)
PHASE_LABELS = dict(PHASES)
PHASE_INDEX = {key: i for i, key in enumerate(PHASE_KEYS)}

# Keyword -> phase key, checked in order against the lowercased raw phase/kind string. Longer,
# more specific keywords are listed first so e.g. "global-placement" matches "place" the same as
# plain "placement" would, and "legalization"/"legal" both land on "legalize" ahead of the
# generic "placement" bucket a naive single-pass match might prefer. Collected from the engine's
# own `phase=` call sites (hardware/pnr/pnr/{trace,provenance,native_loop,gloss,...}.py) and real
# live-directory samples, not guessed; see docs/viewer.md for the full table and how to extend it.
_KEYWORDS = (
    ("legal", "legalize"),
    ("detail", "detail"),
    ("global-placement", "place"),
    ("initial-placement", "generate"),
    ("placement", "place"),
    ("place", "place"),
    ("signals", "generate"),
    ("finalist", "generate"),
    ("geometry-tree", "generate"),
    ("gloss", "gloss"),
    ("power-bank", "gloss"),
    ("transaction-via-cleanup", "gloss"),
    ("congestion", "check"),
    ("selection", "check"),
    ("drc", "check"),
    ("result", "check"),
    ("screening complete", "check"),
    ("native-refinement", "route"),
    ("native-phase", "route"),
    ("usb-pairs", "route"),
    ("parallel-single-track", "route"),
    ("negotiation", "route"),
    ("rip-up", "route"),
    ("commit", "route"),
    ("routed", "route"),
    ("routing", "route"),
    ("route", "route"),
)


def _phase_key_for(raw: Optional[str]) -> Optional[str]:
    """The canonical phase key for a raw phase/kind string, or ``None`` if nothing matches."""
    if not raw:
        return None
    text = raw.strip().lower()
    for keyword, key in _KEYWORDS:
        if keyword in text:
            return key
    return None


def _route_fraction(lane: dict) -> Optional[float]:
    """0..1 fraction through the route phase, or ``None`` when no fine-grained count is known.

    ``route_result`` events can carry ``data.progress = {"done": n, "total": n}`` (routed
    connections so far / total to route for this transaction); ``apply_event`` stores that whole
    payload as ``lane["last_route"]``. When it is present this is exact. When a lane is in the
    route phase without it (an engine path that has not wired progress reporting, or a campaign
    that routes as one atomic step) the caller uses a fixed placeholder instead of fabricating
    precision the telemetry does not have -- see ``classify``.
    """
    progress = (lane.get("last_route") or {}).get("progress")
    if not isinstance(progress, dict):
        return None
    done, total = progress.get("done"), progress.get("total")
    if not isinstance(done, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
        return None
    return max(0.0, min(1.0, done / total))


# Any phase that is known to be under way but has no finer within-phase signal (route without a
# done/total progress dict; every other phase, which never has one) reads as mid-bar rather than
# at either end: "in this phase" is known, "how far through it" is not.
_PHASE_FRACTION_UNKNOWN = 0.5


def classify(lane: dict, held_over: Optional[dict] = None) -> dict:
    """Where ``lane`` is on the canonical pipeline right now.

    Returns a dict: ``state`` (one of ``queued``/``running``/``done``/``failed``), ``phase_key``
    (a key from :data:`PHASE_KEYS`, or ``None`` for a lane that has not started), ``phase_label``
    (the human label for that key, or ``None``), ``phase_fraction`` (0..1 progress *within* the
    current phase; always 1.0 once a phase is behind the lane), ``fraction`` (0..1 overall, the
    quantity a progress bar should fill to), and ``raw_phase`` (the untouched telemetry string,
    for a tooltip).

    ``held_over`` is the previous call's result for this same lane (``classify_lane`` passes the
    lane's own last-stored ``progress``), used only so that an event with no phase information at
    all (a bare ``worker_config_applied``, say) keeps the lane's last known phase instead of
    reporting "generate" every time something unrelated updates it.
    """
    status = lane.get("status")
    raw_phase = lane.get("phase")
    kind = lane.get("kind")

    if status == "queued" or kind == "candidate_queued":
        return dict(
            state="queued",
            phase_key=None,
            phase_label=None,
            phase_fraction=0.0,
            fraction=0.0,
            raw_phase=raw_phase,
        )

    if status == "failed" or kind == "candidate_failed" or status == "rejected":
        # A failed/rejected lane keeps whatever fraction it had reached -- "it got this far, then
        # failed" is more informative than snapping the bar back to zero or forward to full.
        prior = classify({**lane, "status": None, "kind": None}, held_over)
        return dict(prior, state="failed", raw_phase=raw_phase)

    if status in ("complete", "accepted"):
        return dict(
            state="done",
            phase_key="check",
            phase_label=PHASE_LABELS["check"],
            phase_fraction=1.0,
            fraction=1.0,
            raw_phase=raw_phase,
        )

    # Running (or not started but not explicitly queued either): find the phase.
    phase_key = _phase_key_for(raw_phase) or _phase_key_for(kind)
    if phase_key is None and held_over and held_over.get("phase_key"):
        phase_key = held_over["phase_key"]  # an event this lane's phase bucket survives (fallback)
    if phase_key is None:
        # Never seen a classifiable event yet: at the very start of the pipeline, not nowhere.
        return dict(
            state="running",
            phase_key=None,
            phase_label=None,
            phase_fraction=0.0,
            fraction=0.0,
            raw_phase=raw_phase,
        )

    # A phase in progress, with no finer within-phase signal, reads as half full -- "started, not
    # finished" -- rather than either snapping to the end of the phase (1.0, overstating it) or
    # the start (0.0, ignoring that every earlier phase in the pipeline is implicitly behind it).
    # Only route (below) ever has anything finer than that to report.
    phase_fraction = _PHASE_FRACTION_UNKNOWN
    if phase_key == "route":
        fine = _route_fraction(lane)
        phase_fraction = fine if fine is not None else _PHASE_FRACTION_UNKNOWN
    index = PHASE_INDEX[phase_key]
    fraction = (index + phase_fraction) / len(PHASE_KEYS)
    return dict(
        state="running",
        phase_key=phase_key,
        phase_label=PHASE_LABELS[phase_key],
        phase_fraction=phase_fraction,
        fraction=max(0.0, min(1.0, fraction)),
        raw_phase=raw_phase,
    )


def _unconnected_count(lane: dict) -> Optional[int]:
    """How many connections are still unmade, for a status line -- the frame/route-result
    ``opens`` field when a board checkpoint or route result exists, else the field a screening-
    only ``candidate_complete``/``candidate_failed`` reports it under (no board ever extracted,
    so no ``opens``): ``data.missing_connections``, stored as ``lane["last_candidate"]``."""
    if isinstance(lane.get("opens"), (int, float)):
        return int(lane["opens"])
    extra = lane.get("last_candidate") or {}
    for key in ("missing_connections", "opens"):
        if isinstance(extra.get(key), (int, float)):
            return int(extra[key])
    return None


def _violation_count(lane: dict) -> Optional[int]:
    """Same idea as :func:`_unconnected_count` for DRC violations: the frame's ``violations``
    field, else whatever a screening-only candidate's own payload happens to carry under that
    name (most do not run DRC at all and have none, which is a real "no DRC yet", not a zero)."""
    if isinstance(lane.get("violations"), (int, float)):
        return int(lane["violations"])
    extra = lane.get("last_candidate") or {}
    if isinstance(extra.get("violations"), (int, float)):
        return int(extra["violations"])
    return None


def _route_progress_text(lane: dict) -> Optional[str]:
    progress = (lane.get("last_route") or {}).get("progress")
    if not isinstance(progress, dict):
        return None
    done, total = progress.get("done"), progress.get("total")
    if not isinstance(done, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
        return None
    pct = round(100 * done / total)
    return f"Routing: {int(done)} of {int(total)} connections ({pct}%)"


def humanize(lane: dict, classified: Optional[dict] = None) -> str:
    """A short, plain-language status string for ``lane`` (fits a lane-panel line; raw detail
    belongs in a tooltip, not here). ``classified`` is :func:`classify`'s result for this lane;
    computed if omitted."""
    classified = classified or classify(lane)
    state = classified["state"]
    opens, violations = _unconnected_count(lane), _violation_count(lane)

    if state == "queued":
        return "Queued"
    if state == "failed":
        bits = []
        if isinstance(opens, (int, float)) and opens:
            bits.append(f"{int(opens)} unconnected")
        if isinstance(violations, (int, float)) and violations:
            bits.append(f"{int(violations)} DRC error" + ("s" if violations != 1 else ""))
        return "Failed: " + ", ".join(bits) if bits else "Failed"
    if state == "done":
        bits = ["all connected"]
        if violations in (0, None):
            bits.append("DRC clean")
        else:
            bits.append(f"{int(violations)} DRC warning" + ("s" if violations != 1 else ""))
        started, ended = lane.get("started_at"), lane.get("time")
        if (
            isinstance(started, (int, float))
            and isinstance(ended, (int, float))
            and ended >= started
        ):
            bits.append(f"{ended - started:.1f} s")
        return "Done: " + ", ".join(bits)

    phase_key = classified["phase_key"]
    if phase_key is None:
        return "Waiting for placement"
    if phase_key == "route":
        text = _route_progress_text(lane)
        if text:
            return text
        return "Routing"
    if phase_key == "check" and (opens is not None or violations is not None):
        o = int(opens) if isinstance(opens, (int, float)) else "?"
        v = int(violations) if isinstance(violations, (int, float)) else "?"
        return f"Checking: {o} opens / {v} violations"
    if phase_key == "legalize":
        passes = len(
            [f for f in (lane.get("frames") or []) if "legal" in (f.get("name") or "").lower()]
        )
        return f"Legalizing: pass {passes}" if passes else "Legalizing"
    return PHASE_LABELS[phase_key]


def classify_lane(lane: dict) -> dict:
    """``classify`` + ``humanize`` together, carrying over the lane's own previous ``progress``
    (if any) as the held-over phase bucket. The one function :mod:`yapnr.viewer.server` calls per
    lane per poll; it does not mutate ``lane``."""
    classified = classify(lane, held_over=lane.get("progress"))
    return dict(classified, status_text=humanize(lane, classified))
