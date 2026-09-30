"""Build the yapnr documentation site: the ``//docs:build`` target.

    bazel run //docs:build            # -> docs/site/html/index.html
    bazel run //docs:build -- OUT_DIR # somewhere else

The content is read live from the working tree (``$BUILD_WORKSPACE_DIRECTORY``,
set by ``bazel run``), as in Splanc, so the site always reflects the checkout.
The script assembles a staging tree that mirrors the repository layout, so
relative links between the Markdown files resolve the same way on GitHub and on
the site:

    <stage>/conf.py, _static/   from docs/_sphinx/ (plus logo and favicon)
    <stage>/README.md, ...      the top-level Markdown documents
    <stage>/LICENSE             so links to it resolve (served as a download)
    <stage>/docs/**/*.md        the documentation pages
    <stage>/_extra/branding/    copied verbatim into the output (README images)

``docs/index.md`` is the root document; the output root gets a small redirect to
it. The output is then made safe for GitHub Pages, including the per-PR
previews under ``pr-preview/pr-N/`` (see ``_make_jekyll_safe``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from sphinx.cmd.build import main as sphinx_main

# Top-level documents folded into the site as-is.
ROOT_DOCUMENTS = [
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "DEVELOPERS.md",
    "THIRD_PARTY.md",
    "WORKLOG.md",
    "LICENSE",
]

# Brand assets used by the theme (docs/_sphinx/conf.py names them).
STATIC_ASSETS = {
    "branding/yapnr-logo-256.png": "yapnr-logo-256.png",
    "branding/yapnr-logo-light.png": "yapnr-logo-light.png",
    "branding/favicon/yapnr-favicon.svg": "yapnr-favicon.svg",
}

_REDIRECT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>yapnr documentation</title>
<meta http-equiv="refresh" content="0; url=docs/index.html">
<link rel="canonical" href="docs/index.html">
</head>
<body><p><a href="docs/index.html">yapnr documentation</a></p></body>
</html>
"""


def _workspace_root() -> Path:
    env = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env:
        return Path(env)
    try:  # outside `bazel run`
        top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
        return Path(top)
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _stage(ws: Path, stage: Path) -> None:
    """Assemble the Sphinx source tree in ``stage``."""
    # 1. The Sphinx project itself (conf.py, _static/) at the stage root.
    shutil.copytree(ws / "docs" / "_sphinx", stage, dirs_exist_ok=True)

    # 2. Theme assets from branding/.
    for src, name in STATIC_ASSETS.items():
        _copy(ws / src, stage / "_static" / name)

    # 3. Top-level documents.
    for name in ROOT_DOCUMENTS:
        if (ws / name).exists():
            _copy(ws / name, stage / name)

    # 4. Documentation pages under docs/, skipping the Sphinx project, build
    #    outputs and the history import area.
    docs = ws / "docs"
    skip_top = {"_sphinx", "site", "_build", "__pycache__", "hardware"}
    for md in sorted(docs.rglob("*.md")):
        rel = md.relative_to(docs)
        if rel.parts and rel.parts[0] in skip_top:
            continue
        _copy(md, stage / "docs" / rel)

    # 5. Files copied verbatim into the output root (raw-HTML images in README).
    shutil.copytree(ws / "branding", stage / "_extra" / "branding", dirs_exist_ok=True)


# Sphinx emits ``_``-prefixed asset directories. GitHub Pages runs Jekyll, which
# drops every ``_``-prefixed path, and a ``.nojekyll`` file only helps at the
# site root, which a ``pr-preview/pr-N/`` subdirectory cannot set. So the output
# is made Jekyll-safe: rename those directories and rewrite the references.
# Order matters (``_sphinx_design_static`` contains ``_static``): the regexes
# use a leading-boundary lookbehind and are applied longest first.
# Copied from Splanc's docs/build_docs.py.
_UNDERSCORE_DIRS = [
    ("_sphinx_design_static", "sphinx_design_static"),
    ("_static", "static"),
    ("_images", "images"),
    ("_sources", "sources"),
    ("_downloads", "downloads"),
]


def _make_jekyll_safe(out_dir: Path) -> None:
    for old, new in _UNDERSCORE_DIRS:
        src = out_dir / old
        if src.is_dir():
            dst = out_dir / new
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
    subs = [
        (re.compile(r"(?<![\w])" + re.escape(old) + r"\b"), new) for old, new in _UNDERSCORE_DIRS
    ]
    for path in out_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".html", ".js", ".css"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        new_text = text
        for pattern, new in subs:
            new_text = pattern.sub(new, new_text)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")


def _prepare_output(out_dir: Path) -> None:
    """Empty ``out_dir`` if it holds a previous build; never touch anything else."""
    if out_dir.exists() and any(out_dir.iterdir()):
        if not (out_dir / ".nojekyll").is_file():
            raise SystemExit(f"refusing to overwrite {out_dir}: not a previous docs build")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ws = _workspace_root()
    out_dir = Path(argv[0]).resolve() if argv else ws / "docs" / "site" / "html"

    stage = Path(tempfile.mkdtemp(prefix="yapnr-docs-"))
    try:
        print(f"==> staging docs sources from {ws}", file=sys.stderr)
        _stage(ws, stage)
        _prepare_output(out_dir)
        print(f"==> running Sphinx -> {out_dir}", file=sys.stderr)
        # Warnings stay non-fatal until the docs content lands (PR7 adds -W).
        rc = sphinx_main(["-b", "html", "-j", "auto", "-q", str(stage), str(out_dir)])
        if rc != 0:
            print(f"Sphinx build failed (rc={rc})", file=sys.stderr)
            return rc
        (out_dir / "index.html").write_text(_REDIRECT, encoding="utf-8")
        _make_jekyll_safe(out_dir)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    print(f"\ndocs built: {out_dir / 'docs' / 'index.html'}", file=sys.stderr)
    print("preview with:  bazel run //docs:serve", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
