"""pnr.shove: make-room transactions for blocked power/plane routes (PNR_SHOVE=1).

Imported only when ``PNR_SHOVE=1``; with the flag unset no engine path imports
this package and all outputs stay byte-identical.

* :mod:`pnr.shove.targets` - trunk-first power targets and root-inclusive bounds.
* :mod:`pnr.shove.geom` / :mod:`pnr.shove.qp` - pure-Python geometry and the
  dual-ascent (Hildreth) quadratic program.
* :mod:`pnr.shove.world` - native items as primitives, joints and movability.
* :mod:`pnr.shove.relax` - delta-relaxed wish plans from the production planner.
* :mod:`pnr.shove.ladder` - the make-room ladder (L1 copper, L2 parts, L4
  rip-reserve-restore) run by ``python -m pnr.shove``.
* :mod:`pnr.shove.gates` - the whole-transaction gate against the original board.
"""

import os


def enabled(env=None):
    """True when PNR_SHOVE=1 in ``env`` (default: this process)."""
    return (os.environ if env is None else env).get("PNR_SHOVE") == "1"
