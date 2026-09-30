"""Design-source frontends: the Source tab, the Ask context and the notes resolver.

atopile is the one frontend today (``atopile.py``; it moves to ``yapnr.frontends.atopile`` in PR3e).
Its entry module comes from the project's ``ato.yaml``: ``builds.<build>.entry`` for the configured
build target, else every build's entry (the one that covers the netlist best wins). Without
atopile sources, or without a netlist to join them to, there is no source index: the Source tab
says why (``reason``, also in ``/api/about``), the schematic draws generic symbols, notes resolve
against the netlist alone and 3D model paths are used as the board has them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class Sources:
    service: object = None  # SourceService or None
    src: Optional[Path] = None  # atopile source folder
    parts: Optional[Path] = None  # part libraries (symbols, 3D models)
    entry: Optional[Tuple[str, str]] = None  # (file relative to src, module)
    reason: Optional[str] = None  # why there is no source index

    @property
    def entry_label(self) -> Optional[str]:
        return f"{self.entry[0]}:{self.entry[1]}" if self.entry else None


def read_builds(ato_yaml: Path) -> dict:
    """{build name: {entry: ...}} from an ato.yaml (PyYAML when available, else a line parser)."""
    text = ato_yaml.read_text(errors="replace")
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError as ex:
            raise ValueError(f"{ato_yaml}: {ex}") from ex
        builds = doc.get("builds") if isinstance(doc, dict) else None
        return {str(k): v for k, v in (builds or {}).items() if isinstance(v, dict)}
    builds: dict = {}
    current = None
    in_builds = False
    for line in text.splitlines():
        if re.match(r"^builds:\s*$", line):
            in_builds = True
            continue
        if in_builds and re.match(r"^\S", line):
            in_builds = False
        if not in_builds:
            continue
        m = re.match(r"^  (\w[\w.-]*):\s*$", line)
        if m:
            current = builds.setdefault(m[1], {})
            continue
        m = re.match(r"^\s{4,}entry:\s*[\"']?([^\"'\s]+)[\"']?\s*$", line)
        if m and current is not None:
            current["entry"] = m[1]
    return builds


def resolve_entry(project: Path, build: Optional[str]) -> Tuple[Path, str]:
    """(entry file, module) of an atopile build target (ValueError when there is none)."""
    y = project / "ato.yaml"
    if not y.is_file():
        raise ValueError(f"no ato.yaml in {project}")
    builds = read_builds(y)
    if build not in builds:
        known = ", ".join(sorted(builds)) or "none"
        raise ValueError(f"{y}: no build target {build!r} (targets: {known})")
    entry = str(builds[build].get("entry") or "")
    m = re.fullmatch(r"([^:]+\.ato):(\w+)", entry)
    if not m:
        raise ValueError(f"{y}: build {build!r} has no entry of the form file.ato:Module")
    return (project / m[1]).absolute(), m[2]


def open_sources(cfg, llm_model: Optional[str] = None) -> Sources:
    """The atopile source index for a viewer config (a Sources whose service may be None)."""
    from yapnr.viewer.sources.service import SourceService

    out = Sources()
    src = Path(cfg.atopile_src).absolute() if cfg.atopile_src else None
    entry = None
    try:
        if cfg.atopile_root and cfg.atopile_build:
            entry_file, module = resolve_entry(Path(cfg.atopile_root), cfg.atopile_build)
            src = src or entry_file.parent
            if not entry_file.is_relative_to(src):
                raise ValueError(f"the entry {entry_file} is not under the source folder {src}")
            entry = (entry_file.relative_to(src).as_posix(), module)
        elif cfg.atopile_root and not src:
            src = Path(cfg.atopile_root).absolute()
    except ValueError as ex:
        out.reason = str(ex)
        return out
    out.src, out.entry = src, entry
    if src and not src.is_dir():
        out.reason = f"atopile source folder {src} does not exist"
        out.src = None
        return out
    out.parts = src / "parts" if src and (src / "parts").is_dir() else None
    if src is None:
        out.reason = "no atopile sources configured (--atopile-root/--atopile-build)"
        return out
    if not cfg.graph or not Path(cfg.graph).is_file():
        out.reason = "the atopile sources need a netlist (--graph) to join to"
        return out
    service = SourceService(
        src,
        cfg.graph,
        rules=cfg.rules,
        cache_dir=cfg.cache_dir("source"),
        entry=entry,
        llm_model=llm_model or cfg.net_summary_model,
    )
    if service.configured():
        out.service = service
    else:
        out.reason = "the atopile sources or the netlist are missing"
    return out
