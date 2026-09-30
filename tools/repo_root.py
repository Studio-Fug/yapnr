"""Locate the source checkout from a Bazel test, a `bazel run` or a plain run.

Repository checks (privacy scan, test wiring) look at the whole working tree,
which a Bazel test does not declare as inputs. They resolve the real checkout
by following the runfiles symlink of //:MODULE.bazel (a declared data file) and
refuse to run against a runfiles or execroot copy, which would pass vacuously.
Such tests are tagged `external` so Bazel never serves a cached result.
"""

from __future__ import annotations

import os
from typing import List, Optional

_MARKERS = ("MODULE.bazel", os.path.join("tools", "privacy_scan.py"))
_BAZEL_TREES = ("/execroot/", ".runfiles/", "/sandbox/", "/bazel-out/")


def is_checkout(path: str) -> bool:
    probe = path.replace(os.sep, "/").rstrip("/") + "/"
    if any(part in probe for part in _BAZEL_TREES):
        return False
    return all(os.path.isfile(os.path.join(path, marker)) for marker in _MARKERS)


def _candidates() -> List[str]:
    found: List[str] = []
    for var in ("YAPNR_WORKSPACE", "BUILD_WORKSPACE_DIRECTORY"):
        value = os.environ.get(var)
        if value:
            found.append(value)
    srcdir = os.environ.get("TEST_SRCDIR") or os.environ.get("RUNFILES_DIR")
    if srcdir:
        workspace = os.environ.get("TEST_WORKSPACE", "_main")
        module = os.path.join(srcdir, workspace, "MODULE.bazel")
        found.append(os.path.dirname(os.path.realpath(module)))
    found.append(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    return found


def workspace_root() -> str:
    """Return the checkout root, or raise RuntimeError if it cannot be found."""
    tried: List[Optional[str]] = []
    for candidate in _candidates():
        if is_checkout(candidate):
            return candidate
        tried.append(candidate)
    raise RuntimeError(
        "cannot locate the yapnr checkout (set YAPNR_WORKSPACE); tried "
        + ", ".join(repr(t) for t in tried)
    )
