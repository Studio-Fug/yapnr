#!/usr/bin/env python3
"""Derive yapnr's version and container image tags from git.

The release tag is the only version source (docs/releases.md): there is no
version in MODULE.bazel or in the sources. Release tags are ``vX.Y.Z`` and
release candidates ``vX.Y.Z-rc.N``; every other build is a development build
that sorts after the release it descends from:

====================================  ============================  =========================
HEAD                                  PEP 440 (wheel, ``--version``)  image tags (OCI)
====================================  ============================  =========================
at ``vX.Y.Z``                         ``X.Y.Z``                     ``X.Y.Z``, ``X.Y``, ...
at ``vX.Y.Z-rc.N``                    ``X.Y.ZrcN``                  ``X.Y.Z-rc.N``
N commits after ``vX.Y.Z``            ``X.Y.(Z+1).devN+g<sha>``     ``edge``, ``sha-<sha>``
N commits after ``vX.Y.Z-rc.K``       ``X.Y.Zrc(K+1).devN+g<sha>``  ``edge``, ``sha-<sha>``
no release tag, N commits in total    ``0.0.0.devN+g<sha>``         ``edge``, ``sha-<sha>``
====================================  ============================  =========================

``<sha>`` is the first seven hex digits of the commit. A working tree with
uncommitted changes adds ``.dirty`` to the local version and is never a release.

Image tags depend on the ref being built as well as on the version:

- ``refs/tags/v...``: the release tags above. ``X.Y`` (and ``X`` from 1.0) move
  only when the tag is the highest stable release of that series, and
  ``latest`` only when it is the highest stable release overall, so a
  backport such as 0.2.5 never takes ``latest`` from 0.3.0. Release
  candidates get their own tag only. No major tag is published while the
  major version is 0.
- ``refs/heads/main``: ``edge`` and ``sha-<sha>``, even when HEAD happens to
  carry a release tag (the release workflow publishes release tags).
- anything else (pull requests, branches): none; such builds are not pushed.

Usage::

    tools/release/version.py                         # JSON on stdout
    tools/release/version.py --field pep440          # one value
    tools/release/version.py --ref "$GITHUB_REF" --github-output "$GITHUB_OUTPUT"
    tools/release/version.py --expect-tag v0.1.0     # fail unless HEAD is exactly v0.1.0

Stdlib-only and Python 3.9 compatible: CI runs it with the runner's python3.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, NamedTuple, Optional, Sequence

TAG_PATTERN = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-rc\.(?P<rc>[1-9]\d*))?$"
)
# `git describe --match` takes a glob, which also matches tags such as
# v0.2.0-beta.1 or v1.0.0-rc1; from_git excludes every tag that TAG_PATTERN
# rejects, so such a tag never becomes the base of a version.
DESCRIBE_GLOB = "v[0-9]*.[0-9]*.[0-9]*"
MAIN_REF = "refs/heads/main"
TAG_REF_PREFIX = "refs/tags/"
SHORT_SHA_LENGTH = 7


class Release(NamedTuple):
    """A release tag: ``vMAJOR.MINOR.PATCH`` or ``vMAJOR.MINOR.PATCH-rc.N``."""

    major: int
    minor: int
    patch: int
    rc: Optional[int] = None

    @property
    def is_rc(self) -> bool:
        return self.rc is not None

    @property
    def semver(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if self.rc is None else f"{core}-rc.{self.rc}"

    @property
    def tag(self) -> str:
        return "v" + self.semver

    @property
    def pep440(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if self.rc is None else f"{core}rc{self.rc}"

    @property
    def key(self):
        """Sort key in SemVer order: X.Y.Z-rc.N sorts below X.Y.Z."""
        return (self.major, self.minor, self.patch, self.rc is None, self.rc or 0)


def parse_tag(name: str) -> Optional[Release]:
    """Parse a release tag name; None for anything that is not a release tag."""
    match = TAG_PATTERN.match(name.strip())
    if not match:
        return None
    rc = match.group("rc")
    return Release(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        int(rc) if rc else None,
    )


def release_tags(names: Iterable[str]) -> List[Release]:
    """The release tags among ``names``, in ascending SemVer order."""
    found = {parse_tag(name) for name in names}
    return sorted((r for r in found if r is not None), key=lambda r: r.key)


@dataclass
class VersionInfo:
    """Everything the build and release workflows need to know about one commit."""

    version: str  # SemVer for a release tag, else the PEP 440 development version
    pep440: str  # the wheel version and what `yapnr --version` prints
    tag: str  # the release tag at HEAD, or ""
    release: bool  # HEAD is exactly a release tag (and the tree is clean)
    prerelease: bool  # a release candidate or a development build
    latest: bool  # images get the `latest` tag
    previous_stable: str  # the highest stable tag below this version, or ""
    base_tag: str  # the release tag this build descends from, or ""
    sha: str
    short_sha: str
    oci_tags: List[str] = field(default_factory=list)

    def outputs(self):
        """The fields as strings, as GitHub step outputs want them."""
        result = {}
        for key, value in asdict(self).items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, list):
                value = ",".join(value)
            result[key] = value
        return result


def _previous_stable(below: Optional[Release], tags: Sequence[Release]) -> str:
    candidates = [r for r in tags if not r.is_rc and (below is None or r.key < below.key)]
    return candidates[-1].tag if candidates else ""


def _release_image_tags(current: Release, tags: Sequence[Release]) -> List[str]:
    if current.is_rc:
        return [current.semver]
    stable = [r for r in tags if not r.is_rc] + [current]
    result = [current.semver]
    if max(r.key for r in stable if r[:2] == current[:2]) == current.key:
        result.append(f"{current.major}.{current.minor}")
    if current.major >= 1 and max(r.key for r in stable if r.major == current.major) == current.key:
        result.append(str(current.major))
    if max(r.key for r in stable) == current.key:
        result.append("latest")
    return result


def compute(
    *,
    sha: str,
    ref: str = "",
    head_tags: Sequence[str] = (),
    base_tag: str = "",
    distance: int = 0,
    commit_count: int = 0,
    dirty: bool = False,
    all_tags: Sequence[str] = (),
) -> VersionInfo:
    """Compute the version of one commit from facts gathered from git.

    Args:
      sha: the full commit hash of HEAD.
      ref: the ref being built (``GITHUB_REF``), for example ``refs/tags/v0.1.0``.
      head_tags: tag names that point at HEAD.
      base_tag: the nearest release tag reachable from HEAD (``git describe``), or "".
      distance: commits from ``base_tag`` to HEAD.
      commit_count: commits reachable from HEAD (used when there is no release tag).
      dirty: the working tree has uncommitted changes.
      all_tags: every tag name in the repository.
    """
    short = sha[:SHORT_SHA_LENGTH]
    tags = release_tags(list(all_tags) + list(head_tags) + ([base_tag] if base_tag else []))
    at_head = release_tags(head_tags)

    current: Optional[Release] = None
    if at_head and not dirty:
        wanted = parse_tag(ref[len(TAG_REF_PREFIX) :]) if ref.startswith(TAG_REF_PREFIX) else None
        # Several tags on one commit: the final release goes on the commit of its
        # last release candidate. Prefer the tag being built, else the highest.
        current = wanted if wanted in at_head else at_head[-1]

    if current is not None:
        version, pep440 = current.semver, current.pep440
        base, distance = current, 0
    else:
        if at_head:  # a dirty tree at a release tag builds after that release
            base, distance = at_head[-1], 0
        elif base_tag:
            base = parse_tag(base_tag)
            if base is None:
                raise ValueError(
                    f"not a release tag: {base_tag!r} (expected vX.Y.Z or vX.Y.Z-rc.N)"
                )
        else:
            base = None
        if base is None:
            public = f"0.0.0.dev{commit_count}"
        elif base.is_rc:
            public = f"{base.major}.{base.minor}.{base.patch}rc{base.rc + 1}.dev{distance}"
        else:
            public = f"{base.major}.{base.minor}.{base.patch + 1}.dev{distance}"
        pep440 = f"{public}+g{short}" + (".dirty" if dirty else "")
        version = pep440

    if ref == MAIN_REF:
        oci_tags = ["edge", f"sha-{short}"]
    elif (
        ref.startswith(TAG_REF_PREFIX)
        and current is not None
        and ref == TAG_REF_PREFIX + current.tag
    ):
        oci_tags = _release_image_tags(current, tags)
    else:
        oci_tags = []

    return VersionInfo(
        version=version,
        pep440=pep440,
        tag=current.tag if current else "",
        release=current is not None,
        prerelease=current is None or current.is_rc,
        latest="latest" in oci_tags,
        previous_stable=_previous_stable(current, tags) if current else "",
        base_tag=base.tag if base else "",
        sha=sha,
        short_sha=short,
        oci_tags=oci_tags,
    )


# --- git ----------------------------------------------------------------------


def _git(repo: str, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip() if result.returncode == 0 else ""


def from_git(repo: str = ".", ref: str = "") -> VersionInfo:
    """Gather the facts for ``compute`` from the git checkout at ``repo``."""
    sha = _git(repo, "rev-parse", "HEAD")
    head_tags = _git(repo, "tag", "--points-at", "HEAD").split()
    all_tags = _git(repo, "tag", "--list", "v*").split()
    # The nearest release tag: tags that match the glob but are not release tags
    # (v0.2.0-beta.1, v1.0.0-rc1) are skipped, so describe falls back to the
    # nearest one that is.
    excludes = []
    for name in all_tags:
        if parse_tag(name) is None:
            excludes += ["--exclude", name]
    base_tag = _git(
        repo,
        "describe",
        "--tags",
        "--abbrev=0",
        "--match",
        DESCRIBE_GLOB,
        *excludes,
        "HEAD",
        check=False,
    )
    if base_tag:
        # Several tags on the base commit (an RC and its final release): take the highest.
        on_base = release_tags(_git(repo, "tag", "--points-at", base_tag).split())
        base_tag = on_base[-1].tag if on_base else base_tag
    distance = int(_git(repo, "rev-list", "--count", f"{base_tag}..HEAD")) if base_tag else 0
    return compute(
        sha=sha,
        ref=ref,
        head_tags=head_tags,
        base_tag=base_tag,
        distance=distance,
        commit_count=int(_git(repo, "rev-list", "--count", "HEAD")),
        dirty=bool(_git(repo, "status", "--porcelain", "--untracked-files=no")),
        all_tags=all_tags,
    )


# --- command line ---------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=".", help="git checkout (default: cwd)")
    parser.add_argument(
        "--ref",
        default=os.environ.get("GITHUB_REF", ""),
        help="the ref being built, e.g. refs/heads/main (default: $GITHUB_REF)",
    )
    parser.add_argument("--field", help="print only this field")
    parser.add_argument(
        "--github-output", metavar="PATH", help="append the fields as step outputs to PATH"
    )
    parser.add_argument(
        "--expect-tag", metavar="TAG", help="fail unless HEAD is exactly the release tag TAG"
    )
    args = parser.parse_args(argv)

    info = from_git(args.repo, args.ref)
    outputs = info.outputs()

    if args.expect_tag is not None:
        expected = parse_tag(args.expect_tag)
        if expected is None:
            print(
                f"{args.expect_tag!r} is not a release tag (vX.Y.Z or vX.Y.Z-rc.N)", file=sys.stderr
            )
            return 1
        if info.tag != expected.tag:
            print(
                f"HEAD is not exactly {expected.tag} (clean tree): computed {info.version}",
                file=sys.stderr,
            )
            return 1

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    if args.field:
        if args.field not in outputs:
            parser.error(f"unknown field {args.field!r}; known: {', '.join(outputs)}")
        print(outputs[args.field])
    else:
        print(json.dumps(asdict(info), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
