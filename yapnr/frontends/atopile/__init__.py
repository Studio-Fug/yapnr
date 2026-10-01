"""The atopile toolchain, without Nix (docs/frontends/atopile.md).

- ``toolchain``: the hash-pinned atopile environment (``yapnr atopile setup``) and its discovery.
- ``runner``: ``yapnr atopile build``, a bounded, isolated, offline ``ato build``.
- ``picker``: the offline components-API service that ``ato`` picks parts from.
- ``hook``: a stdlib-only module that runs inside atopile's interpreter during a build (the
  loopback-only picker token, the empty-query guard, parts from the project, no GUI KiCad).

The part data itself lives in the part cache (``yapnr.partcache``), never in this repository.
"""

# The atopile release this frontend is written and tested against. The runner refuses any other
# version; a new one comes in through an A/B (docs/frontends/atopile.md, "Versions").
ATOPILE_VERSION = "0.15.8"
