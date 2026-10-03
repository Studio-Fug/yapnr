"""The backends of ``yapnr exp``: ``local``, ``gcp-batch`` and ``slurm``, behind one interface."""

from __future__ import annotations

from yapnr.exp.backends.base import Backend, Stores, SubmitError, done_markers, submissions
from yapnr.exp.backends.gcp_batch import GcpBatch
from yapnr.exp.backends.local import Local
from yapnr.exp.backends.slurm import Slurm

BACKENDS = {backend.name: backend for backend in (Local(), GcpBatch(), Slurm())}

__all__ = ["BACKENDS", "Backend", "Stores", "SubmitError", "done_markers", "get", "submissions"]


def get(name: str) -> Backend:
    try:
        return BACKENDS[name]
    except KeyError:
        raise ValueError(
            "no backend %r (one of %s)" % (name, ", ".join(sorted(BACKENDS)))
        ) from None
