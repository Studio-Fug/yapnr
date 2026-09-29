"""Routing outcomes of evaluated rounds fed back into later placement rounds.

Opt-in (``PNR_FEEDBACK=1`` plus ``--generations``/``--rounds`` > 0 on the
drivers); with the defaults no code path changes.

Before this package the only routing -> placement effect active in the
hierarchical flow was PNR_SHOVE make-room nudges (<= 0.5 mm, inside one
evaluation); ``feedback.json`` was written for every block trial and every
halving candidate and read by nothing. This package closes that loop for block
synthesis (:mod:`pnr.hier.synth_native`) and top-level Monte Carlo
(:mod:`pnr.mc.halving`):

* :mod:`.signals` reads one evaluated round (``feedback.json``, evaluation,
  evaluated poses) into a JSON-safe record: the failed cross-part connections
  (same-part failures are counted, never acted on), their mode and whether shove
  reported ``no_make_room``; plus the router key that decides which evaluations
  may be pooled. The key includes the evaluation code (a hash of every module an
  evaluation can run, shove only for the shove router): imports routed by other
  code are refused, or with ``--import-code-mismatch rebase`` re-evaluated
  unchanged under this code first and never ranked themselves, so parents and
  children are always compared under one router.
* :mod:`.table` pools the records of one scope (a block template, a halving
  run) into per-connection failure rates, the structural floor and lineage
  failure counts; rebuilt deterministically each generation.
* :mod:`.moves` turns a parent's failed connections into children: PULL moves
  one non-anchor end of a failed connection on a 0.25 mm lattice (<= 3 mm,
  lineage cap 4 mm from the generation-0 pose), every other part exactly where
  it was; RAND is the matched random-move control; ``pair_weights`` feed the
  population failure rates to :func:`pnr.place.placer.place` as a pad-pair
  attraction for fresh 'prior' samples.

Selection stays mechanical: children and parents compete in one pool under the
drivers' existing rank keys, parents come from a fixed halving schedule, seeds
from sha256 of the lineage.
"""
