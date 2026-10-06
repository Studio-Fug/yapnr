"""Build the experiment browser's hierarchy from lane ids.

Every campaign this viewer has seen names its lanes with slash-separated, directory-like ids:
a ladder sweep writes ``ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0/initial-start-04`` (campaign
arm / board case / seed / placement candidate); a single run writes just ``controller``; the
candidate-search audit writes ``r03/search``. :func:`build_tree` turns the *set* of ids the live
directory currently has into a nested tree by splitting each id on ``/`` and walking a trie --
nothing here is specific to one campaign's shape, any depth and branching works, and a lane id
that is itself a prefix of other lane ids (``.../s0`` is both a lane *and* the parent of
``.../s0/initial-start-00``) becomes a node that is simultaneously a leaf and a group.

This module does no I/O and knows nothing about HTTP: :mod:`yapnr.viewer.server` calls
:func:`build_tree` once per poll (on the same lanes dict it already built for ``/api/state``) and
ships the result as ``state["tree"]``, so the front end never re-derives the hierarchy from
scratch. It is unit-tested directly against lane-id samples (including odd shapes) without a
running viewer.

One exception to "does no I/O": :func:`_annotate`'s children-derivation rule (a lane with no
terminal event of its own, every child already ended) also corrects that lane's own entry in
``lanes`` (``progress``/``status_text``) in place, not just this module's own aggregate counts --
a case/seed lane is both a tree node *and* a row the Experiments drawer shows on its own, and a
person reading that row must see "Done"/"Finished", the same thing the tree says about it, not
"Stalled" because :mod:`yapnr.viewer.progress` never learns anything about children by itself.
Still a pure function of its inputs (``lanes``), still no filesystem or network I/O; "I/O" here
means exactly that one qualifier, not a broader exemption.
"""

from __future__ import annotations

import re
from typing import Optional

from yapnr.viewer.progress import classify_lane, humanize

# Lanes under this suffix are the candidate-search audit's own bookkeeping (app.js's `r<iter>
# /search`), not a browsable experiment; excluded from the tree the same way the flat lane list
# already excluded them.
_AUDIT_SUFFIX = "/search"

_NATURAL_RUN = re.compile(r"(\d+)")


def _natural_key(segment: str):
    """Sort key splitting ``segment`` into text/number runs, so seed names sort s0, s1, s2, s10
    instead of the lexicographic s0, s1, s10, s2 (`.localeCompare` with `numeric:true` does the
    same thing in app.js; this is that, in Python, for the server-side tree)."""
    parts = _NATURAL_RUN.split(segment)
    return [int(p) if p.isdigit() else p for p in parts]


# The full set of states yapnr.viewer.progress.classify can return, listed once here so a new
# state only needs adding in this one place (plus the matching bucket in COUNT_STATES below).
# "finished" is the fallback for a lane with no terminal event of its own whose verdict cannot be
# derived either (see _NO_TERMINAL_EVENT_STATES/_annotate below and progress.classify's docstring).
STATES = ("running", "stalled", "failed", "rejected", "queued", "finished", "done")

# A lane in one of these states never received its own terminal event (``candidate_complete``,
# ``candidate_failed``, ``iteration_complete``, or a case lane's new ``case_complete``/
# ``case_failed`` from ``hardware/pnr/regression/run.py``) -- "finished" itself is already a
# fallback (progress.classify, idle past STALE_SECONDS with the run known over), and "queued"
# means it never ran at all. Only a lane in one of these states is eligible for the
# children-derivation rule below; a lane whose own terminal event already carries a verdict
# (done/failed/rejected) is never overridden by it.
_NO_TERMINAL_EVENT_STATES = frozenset({"running", "stalled", "queued", "finished"})

# Lower number wins when picking a group's `best_leaf` (the "select this group, show one board"
# representative): prefer whatever is still actively happening (running, then stalled) over a
# terminal outcome, and among terminal outcomes prefer the one most worth a user's attention
# (failed, then rejected, then queued, then done last). Deliberately *not* "failed first" -- a
# single failed leaf in an otherwise-healthy, still-running group must not steal the group's
# selection away from the work actually in progress.
_STATE_PRIORITY = {state: i for i, state in enumerate(STATES)}


def _new_node(node_id: str, name: str) -> dict:
    return dict(id=node_id, name=name, lane_id=None, children={}, order=[])


def build_tree(lanes: dict) -> dict:
    """The root node of the hierarchy built from ``lanes`` (a dict of lane id -> lane dict, as
    stored in ``Viewer.state["lanes"]``). Every node is a plain, JSON-serializable dict:

    - ``id``: this node's full path (``""`` for the root).
    - ``name``: this node's own path segment (``""`` for the root).
    - ``lane_id``: the lane id this node *is*, if any lane's id is exactly this path; ``None``
      for a pure group node.
    - ``children``: ``{segment: node}``, already in display order (see ``order``).
    - ``order``: ``children``'s keys, in the order a browser should render them (natural sort,
      with the standalone id if any renders first before its own children).
    - ``counts``: leaf-descendant counts by :mod:`yapnr.viewer.progress` state (one key per
      :data:`STATES` -- ``running``, ``stalled``, ``failed``, ``rejected``, ``queued``, ``done``)
      plus ``total``; a leaf counts itself.
    - ``fraction``: the aggregate progress bar fill for this node, 0..1 -- the mean of every
      descendant leaf's own fraction (a group with 3 lanes done and 1 lane 50% routed reads as
      0.875, not just "3 of 4 done").
    - ``best_leaf``: a representative descendant lane id for "select this group, show one board":
      the first leaf still actively running (or stalled) in display order, else the first leaf at
      all in :data:`STATES` priority order (failed, then rejected, then queued, then done last);
      ``None`` for an otherwise-empty group (should not happen for a node reachable from the root,
      but a defensive default costs nothing).
    """
    root = _new_node("", "")
    for lane_id in lanes:
        if lane_id.endswith(_AUDIT_SUFFIX):
            continue
        segments = [s for s in lane_id.split("/") if s]
        if not segments:
            continue
        node = root
        path = []
        for seg in segments:
            path.append(seg)
            if seg not in node["children"]:
                node["children"][seg] = _new_node("/".join(path), seg)
            node = node["children"][seg]
        node["lane_id"] = lane_id

    def finish(node: dict) -> dict:
        for child in node["children"].values():
            finish(child)
        node["order"] = sorted(node["children"], key=_natural_key)
        _annotate(node, lanes)
        return node

    return finish(root)


def _annotate(node: dict, lanes: dict) -> None:
    """Fill in ``counts``/``fraction``/``best_leaf`` for ``node`` from its already-annotated
    children plus its own lane (if it is also a leaf); post-order, so a node's aggregate always
    reflects its whole subtree.

    A node that is both a group and a lane (``.../s0`` is both the parent of ``.../s0/initial-
    start-00`` and a lane in its own right) folds its children in first: when its own lane has no
    terminal event of its own (:data:`_NO_TERMINAL_EVENT_STATES`) but every child leaf underneath
    it has already ended, its contribution to this node's counts is derived from theirs instead of
    read verbatim -- "done" if every child is done, "failed" if any is, else "finished" (no event
    of its own and no clear verdict from the children either) -- rather than reading "stalled" (or
    "running") forever once the engine that was supposed to report it has moved on. This is the
    ladder runner's real shape before ``hardware/pnr/regression/run.py`` learned to emit a case-
    lane terminal event: a case/seed lane's last event is ``source_round_start`` or
    ``worker_config_applied``, never anything :mod:`yapnr.viewer.progress` recognises as terminal,
    while its ``initial-start-NN`` candidate children each end in ``candidate_complete``/
    ``candidate_failed``. Purely structural: it needs no outside "is the run finished" signal (see
    ``progress.classify``'s ``finished`` flag for the complementary case, a lane with no children
    to derive anything from)."""
    counts = {state: 0 for state in STATES}
    counts["total"] = 0
    fraction_sum = 0.0
    best: Optional[str] = None
    best_rank = None

    def consider(lane_id: str, state: str, fraction: float):
        nonlocal fraction_sum, best, best_rank
        counts[state] = counts.get(state, 0) + 1
        counts["total"] += 1
        fraction_sum += fraction
        rank = _STATE_PRIORITY.get(state, 0)
        if best is None or rank < best_rank:
            best, best_rank = lane_id, rank

    child_counts = {state: 0 for state in STATES}
    child_counts["total"] = 0
    for seg in node["order"]:
        child = node["children"][seg]
        for state in STATES:
            counts[state] += child["counts"][state]
            child_counts[state] += child["counts"][state]
        counts["total"] += child["counts"]["total"]
        child_counts["total"] += child["counts"]["total"]
        fraction_sum += child["fraction"] * child["counts"]["total"]
        if child["best_leaf"] is not None:
            rank = _STATE_PRIORITY.get(_leaf_state(child), 0)
            if best is None or rank < best_rank:
                best, best_rank = child["best_leaf"], rank

    if node["lane_id"] is not None:
        lane = lanes.get(node["lane_id"], {})
        # Use the lane's own already-computed classification (yapnr.viewer.server annotates every
        # lane with `progress` before building the tree, each poll) when there is one, rather than
        # recomputing classify_lane here: recomputing would feed the lane's *current* `progress`
        # back into classify() as its own held-over state (apply_event/annotate already moved
        # `lane["progress"]` forward to "now" by the time build_tree runs), double-applying the
        # round/lap-compounding carry-over every poll. Tests that build lane dicts directly
        # (without a running server) fall back to computing it fresh.
        classified = lane.get("progress") or classify_lane(lane)
        state, fraction = classified["state"], classified["fraction"]
        children_all_ended = child_counts["total"] > 0 and not (
            child_counts["running"] or child_counts["stalled"] or child_counts["queued"]
        )
        if state in _NO_TERMINAL_EVENT_STATES and children_all_ended:
            if child_counts["failed"]:
                state, fraction = "failed", 1.0
            elif child_counts["done"] == child_counts["total"]:
                state, fraction = "done", 1.0
            else:
                state, fraction = "finished", 1.0
            # Correct this lane's own row too (see the module docstring's "one exception"): a
            # person looking at the Experiments drawer must see the same verdict the tree's
            # aggregate counts below are about to reflect, not whatever progress.classify made of
            # this lane's last raw event on its own.
            classified = dict(classified, state=state, fraction=fraction)
            if isinstance(lanes.get(node["lane_id"]), dict):
                lanes[node["lane_id"]]["progress"] = classified
                lanes[node["lane_id"]]["status_text"] = humanize(lane, classified)
        consider(node["lane_id"], state, fraction)

    node["counts"] = counts
    node["fraction"] = (fraction_sum / counts["total"]) if counts["total"] else 0.0
    node["best_leaf"] = best


def _leaf_state(node: dict) -> str:
    """The state of ``node["best_leaf"]`` purely from the counts already folded into ``node``
    (the highest-priority non-zero bucket present -- see :data:`STATES`/``_STATE_PRIORITY`` --
    else ``running``), used only to rank a child subtree's representative leaf against its
    siblings without re-walking it."""
    for state in STATES:
        if node["counts"][state]:
            return state
    return "running"
