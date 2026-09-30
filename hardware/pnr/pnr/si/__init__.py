"""Signal-integrity v1: design-time ``@pnr-si`` check + post-route SI report (``PNR_SI=1``).

Scope (user decisions 2026-09-29): a requirement binds a driver pin, its series
termination and a board connector pin to a profile from ``hardware/pnr/si_models``
(external cable + receiver, corners, limits). The design-time check simulates the
ideal (zero-length) board and fails rules compilation unless a ``@pnr-si-waiver``
covers the failure; the post-route report simulates the routed copper of every
candidate and classifies each failure as ``layout`` (the ideal board passes),
``design`` (it fails too) or ``error`` (simulation/model/extraction problem, fail
closed). Only side fields are produced (:func:`pnr.si.report.side_fields`); the
6-value objective vector is untouched. With ``PNR_SI=1``, ``si_layout_failures`` is a
rank key right after the legality terms (``pnr.mc.halving._rank_key``,
``pnr.hier.synth_native.rank_key``); design failures and errors never rank.

Every SI child process (ngspice deck, KIBIS kicad-cli export, pcbnew read) is
time-bounded twice (parent kill + its own alarm), niced, and holds one of the
machine-wide ``PNR_SI_SLOTS`` slots (:func:`pnr.si.runner.slot`).

Modules: :mod:`annotations` (parse/resolve), :mod:`models` (manifests, vendor cache,
KIBIS), :mod:`physics` (stackup L/C), :mod:`extract` (routed copper + placement
estimate), :mod:`deck` (ngspice netlist), :mod:`runner` (time-bounded libngspice
subprocess), :mod:`metrics`, :mod:`report` (orchestration + ``pnr-si-report-v1``).

This package module stays stdlib-only and Python 3.9 compatible: ``pnr.si.extract``
runs under KiCad's interpreter as a pcbnew worker.
"""

import os

ENV = "PNR_SI"
SCHEMA = "pnr-si-report-v1"


def enabled(env=None):
    """``PNR_SI=1`` turns on the compile-time check and the post-route report."""
    return ((os.environ if env is None else env).get(ENV) or "").strip() == "1"
