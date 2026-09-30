"""Viewer configuration: flags, an optional TOML file and values derived from the root.

Precedence, per setting: a command-line flag, then the ``--config`` file (TOML; relative paths
are relative to the file), then a default derived from the live telemetry directory (``--root``).
Nothing defaults to a machine path. Machine-level tool paths (KiCad, the ``claude`` CLI) come from
flags, ``YAPNR_*`` environment variables or the user's machine config
(``$XDG_CONFIG_HOME/yapnr/config.toml``, default ``~/.config/yapnr/config.toml``; only its
``[kicad] cli``, ``[kicad] python`` and ``[agent] claude`` keys are read), never from the viewer
config file.

Relative command-line paths are relative to the directory ``bazel run`` was started from
(``BUILD_WORKING_DIRECTORY``), else to the current directory.

The paid features are off unless enabled explicitly: the Ask agent (``--agent on``, also on a
loopback listener), its web tools (``--agent-web on``) and the AI net labels
(``--net-summaries on``). See the viewer README for their cost and exposure.

Example file (every key is optional)::

    schema = "yapnr-viewer-v1"
    root = "runs/example/live"
    [server]
    listen = ["127.0.0.1"]
    port = 8766
    allow_origin = []            # e.g. ["http://viewer.example.com:8766"]
    allow_host = []
    title = "yapnr"
    source_url = "https://github.com/Studio-Fug/yapnr"
    [paths]
    experiment = ".."            # default: the root's parent (trial run dirs, shared locks)
    notes = "../notes"
    preferences = "../viewer-preferences.json"
    capture_root = ".."          # recorded cost captures must lie under it
    cache = "."                  # schematic/, source/, viewer3d/, agent/ caches
    dist = ""                    # default: the packaged static files
    [design]
    graph = ""                   # netlist graph.json (schematic view, notes resolver)
    rules = ""                   # default: rules.json next to the graph
    constraints = ""
    [design.atopile]
    root = ""                    # directory with ato.yaml
    build = ""                   # ato.yaml build target whose entry is the top module
    src = ""                     # default: the entry file's directory
    parts = ""                   # default: <src>/parts
    [engine]
    runtime = ""                 # directory containing `pnr` for subprocess services
    cost_contexts = ""
    [viewer3d]
    enabled = true
    timeout_s = 240
    max_mb = 64
    [agent]
    enabled = false
    model = "opus"
    turn_budget_usd = 2.0
    total_budget_usd = 20.0
    web = false
    cwd = ""
    read_dirs = []
    docs = []
    description = ""
    [net_summaries]
    enabled = false
    model = "sonnet"
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SCHEMA = "yapnr-viewer-v1"
SOURCE_URL = "https://github.com/Studio-Fug/yapnr"
AGENT_MODELS = ("opus", "sonnet")
SUMMARY_MODELS = ("sonnet", "opus", "haiku")
USER_CONFIG_ENV = "YAPNR_USER_CONFIG"


class ConfigError(ValueError):
    """An invalid configuration; the message names the setting."""


@dataclass
class ViewerConfig:
    root: Path
    listen: List[str] = field(default_factory=lambda: ["127.0.0.1"])
    port: int = 8766
    allow_origin: List[str] = field(default_factory=list)
    allow_host: List[str] = field(default_factory=list)
    title: str = "yapnr"
    source_url: str = SOURCE_URL
    experiment: Optional[Path] = None
    notes: Optional[Path] = None
    preferences: Optional[Path] = None
    capture_root: Optional[Path] = None
    cache: Optional[Path] = None
    dist: Optional[Path] = None
    graph: Optional[Path] = None
    rules: Optional[Path] = None
    constraints: Optional[Path] = None
    atopile_root: Optional[Path] = None
    atopile_build: Optional[str] = None
    atopile_src: Optional[Path] = None
    parts: Optional[Path] = None
    engine_runtime: Optional[Path] = None
    cost_contexts: Optional[Path] = None
    kicad_cli: Optional[Path] = None
    kicad_python: Optional[Path] = None
    machine_kicad_cli: Optional[Path] = None
    machine_kicad_python: Optional[Path] = None
    viewer3d: bool = True
    viewer3d_timeout: float = 240.0
    viewer3d_max_mb: float = 64.0
    agent: bool = False
    claude: Optional[str] = None
    agent_model: str = "opus"
    agent_budget_usd: float = 2.0
    agent_total_usd: float = 20.0
    agent_web: bool = False
    agent_cwd: Optional[Path] = None
    agent_read_dirs: List[Path] = field(default_factory=list)
    agent_docs: List[Path] = field(default_factory=list)
    agent_description: str = ""
    net_summaries: bool = False
    net_summary_model: str = "sonnet"

    def cache_dir(self, name: str) -> Path:
        return Path(self.cache) / name


# (flag, dest, TOML path, kind, help). kind: path, paths, str, strs, int, float, onoff.
_OPTIONS = (
    ("--listen", "listen", "server.listen", "strs", "address to listen on (repeatable)"),
    ("--port", "port", "server.port", "int", "TCP port (0: any free port)"),
    (
        "--allow-origin",
        "allow_origin",
        "server.allow_origin",
        "strs",
        "extra browser origin allowed to POST (repeatable), e.g. http://viewer.example.com:8766",
    ),
    (
        "--allow-host",
        "allow_host",
        "server.allow_host",
        "strs",
        "extra Host header name accepted (DNS-rebinding guard; loopback, --listen and"
        " --allow-origin hosts are always accepted)",
    ),
    ("--title", "title", "server.title", "str", "title shown in the page header"),
    ("--source-url", "source_url", "server.source_url", "str", "source repository (About link)"),
    (
        "--experiment-dir",
        "experiment",
        "paths.experiment",
        "path",
        "experiment folder: trial run directories must lie under it; shared locks and"
        " restart-status.json (default: the root's parent)",
    ),
    (
        "--notes-dir",
        "notes",
        "paths.notes",
        "path",
        "design notes store (default: <experiment>/notes)",
    ),
    (
        "--preferences",
        "preferences",
        "paths.preferences",
        "path",
        "saved control preferences (default: <experiment>/viewer-preferences.json)",
    ),
    (
        "--capture-root",
        "capture_root",
        "paths.capture_root",
        "path",
        "recorded placement-cost captures must lie under it (default: <experiment>)",
    ),
    ("--cache-dir", "cache", "paths.cache", "path", "viewer caches (default: the root)"),
    ("--dist", "dist", "paths.dist", "path", "assembled static files (default: packaged)"),
    ("--graph", "graph", "design.graph", "path", "netlist graph.json (schematic view, notes)"),
    ("--rules", "rules", "design.rules", "path", "rules.json (default: next to the graph)"),
    ("--constraints", "constraints", "design.constraints", "path", "placement constraints YAML"),
    (
        "--atopile-root",
        "atopile_root",
        "design.atopile.root",
        "path",
        "atopile project (directory with ato.yaml) for the Source tab",
    ),
    (
        "--atopile-build",
        "atopile_build",
        "design.atopile.build",
        "str",
        "ato.yaml build target whose entry is the top module",
    ),
    (
        "--atopile-src",
        "atopile_src",
        "design.atopile.src",
        "path",
        "atopile source folder (default: the entry file's folder)",
    ),
    (
        "--parts",
        "parts",
        "design.atopile.parts",
        "path",
        "part libraries: .kicad_sym symbols and 3D models (default: <atopile src>/parts)",
    ),
    (
        "--engine-runtime",
        "engine_runtime",
        "engine.runtime",
        "path",
        "directory containing the `pnr` package that the cost and schematic services run"
        " (default: the imported engine)",
    ),
    ("--cost-contexts", "cost_contexts", "engine.cost_contexts", "path", "cost contexts JSON"),
    (
        "--kicad-cli",
        "kicad_cli",
        None,
        "path",
        "headless kicad-cli (default: $YAPNR_KICAD_CLI, the machine config, then discovery;"
        " the GUI application's copy is refused)",
    ),
    (
        "--kicad-python",
        "kicad_python",
        None,
        "path",
        "KiCad's Python, which imports pcbnew (default: $YAPNR_KICAD_PYTHON, the machine config,"
        " then discovery)",
    ),
    ("--viewer3d", "viewer3d", "viewer3d.enabled", "onoff", "3D view (default on)"),
    (
        "--viewer3d-timeout",
        "viewer3d_timeout",
        "viewer3d.timeout_s",
        "float",
        "seconds per 3D export",
    ),
    ("--viewer3d-max-mb", "viewer3d_max_mb", "viewer3d.max_mb", "float", "largest GLB kept"),
    (
        "--agent",
        "agent",
        "agent.enabled",
        "onoff",
        "Ask agent: paid Claude CLI turns on the operator's account (default off, also on"
        " loopback)",
    ),
    ("--claude", "claude", None, "str", "claude CLI (default: `claude` on PATH)"),
    ("--agent-model", "agent_model", "agent.model", "str", "default Ask model: opus or sonnet"),
    (
        "--agent-budget-usd",
        "agent_budget_usd",
        "agent.turn_budget_usd",
        "float",
        "spend cap per Ask turn",
    ),
    (
        "--agent-total-usd",
        "agent_total_usd",
        "agent.total_budget_usd",
        "float",
        "spend cap for this server process (all turns); 0 disables the cap",
    ),
    (
        "--agent-web",
        "agent_web",
        "agent.web",
        "onoff",
        "Ask turns may use WebSearch/WebFetch on public hosts (default off)",
    ),
    ("--agent-cwd", "agent_cwd", "agent.cwd", "path", "Ask agent working directory"),
    (
        "--agent-read-dir",
        "agent_read_dirs",
        "agent.read_dirs",
        "paths",
        "folder the Ask agent may read (repeatable; default: the atopile sources and the"
        " experiment folder)",
    ),
    (
        "--agent-doc",
        "agent_docs",
        "agent.docs",
        "paths",
        "document offered to the Ask agent as context (repeatable)",
    ),
    (
        "--agent-description",
        "agent_description",
        "agent.description",
        "str",
        "one sentence about the design, for the Ask agent's prompt",
    ),
    (
        "--net-summaries",
        "net_summaries",
        "net_summaries.enabled",
        "onoff",
        "AI net labels: one paid CLI call per changed netlist dossier (default off)",
    ),
    (
        "--net-summary-model",
        "net_summary_model",
        "net_summaries.model",
        "str",
        "model for the AI net labels: sonnet, opus or haiku",
    ),
)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="yapnr-viewer",
        description="Serve the yapnr live viewer for one live telemetry directory.",
    )
    ap.add_argument("root_arg", nargs="?", type=str, metavar="ROOT", help="live telemetry dir")
    ap.add_argument("--root", dest="root", help="live telemetry dir (same as ROOT)")
    ap.add_argument("--config", help="viewer config file (TOML, schema yapnr-viewer-v1)")
    for flag, dest, _, kind, text in _OPTIONS:
        if kind in ("strs", "paths"):
            ap.add_argument(flag, dest=dest, action="append", help=text)
        elif kind == "onoff":
            ap.add_argument(flag, dest=dest, choices=("on", "off"), help=text)
        else:
            ap.add_argument(flag, dest=dest, type={"int": int, "float": float}.get(kind), help=text)
    return ap


def _lookup(doc: Dict[str, Any], dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _load_toml(path: Path) -> Dict[str, Any]:
    try:
        import tomllib
    except ImportError as ex:  # Python < 3.11
        raise ConfigError(f"reading {path} needs Python 3.11 or newer (tomllib)") from ex
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as ex:
        raise ConfigError(f"cannot read the config file {path}: {ex}") from ex


def user_config_path(environ: Optional[Dict[str, str]] = None) -> Optional[Path]:
    """The machine config file, or None when disabled (``YAPNR_USER_CONFIG=""``)."""
    env = os.environ if environ is None else environ
    if USER_CONFIG_ENV in env:
        value = env[USER_CONFIG_ENV]
        return Path(value).expanduser() if value else None
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or "~", ".config")
    return Path(base).expanduser() / "yapnr" / "config.toml"


def _base_dir(environ: Dict[str, str]) -> Path:
    return Path(environ.get("BUILD_WORKING_DIRECTORY") or os.getcwd())


def _known_keys() -> set:
    keys = {"schema", "root"}
    for _, _, dotted, _, _ in _OPTIONS:
        if dotted:
            keys.add(dotted)
    return keys


def _flatten(doc: Dict[str, Any], prefix: str = "") -> List[str]:
    out = []
    for key, value in doc.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out += _flatten(value, name + ".")
        else:
            out.append(name)
    return out


def _convert(kind: str, value: Any, base: Path, where: str) -> Any:
    def path(v: Any) -> Optional[Path]:
        if v in (None, ""):
            return None
        if not isinstance(v, (str, os.PathLike)):
            raise ConfigError(f"{where}: expected a path, got {v!r}")
        p = Path(v).expanduser()
        # Lexically normalized ("a/../b" -> "b"); symlinks are kept as given.
        return Path(os.path.normpath(p if p.is_absolute() else base / p))

    if value is None:
        return None
    if kind == "path":
        return path(value)
    if kind in ("paths", "strs"):
        items = value if isinstance(value, list) else [value]
        if kind == "paths":
            return [p for p in (path(v) for v in items) if p is not None]
        return [str(v) for v in items]
    if kind == "onoff":
        if isinstance(value, bool):
            return value
        if value in ("on", "off"):
            return value == "on"
        raise ConfigError(f"{where}: expected on/off or a boolean, got {value!r}")
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        return value
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        return float(value)
    if value == "":
        return None
    return str(value)


def load(argv: Optional[Sequence[str]] = None, environ: Optional[Dict[str, str]] = None):
    """Parse flags and files into a ViewerConfig (ConfigError or SystemExit on bad input)."""
    env = dict(os.environ if environ is None else environ)
    args = build_parser().parse_args(argv)
    cwd = _base_dir(env)
    doc: Dict[str, Any] = {}
    doc_base = cwd
    if args.config:
        cfg_path = Path(args.config).expanduser()
        cfg_path = cfg_path if cfg_path.is_absolute() else cwd / cfg_path
        doc = _load_toml(cfg_path)
        doc_base = cfg_path.parent
        if doc.get("schema", SCHEMA) != SCHEMA:
            raise ConfigError(f"{cfg_path}: schema must be {SCHEMA!r}, got {doc.get('schema')!r}")
        unknown = sorted(set(_flatten(doc)) - _known_keys())
        if unknown:
            raise ConfigError(f"{cfg_path}: unknown settings {', '.join(unknown)}")
    machine: Dict[str, Any] = {}
    user_cfg = user_config_path(env)
    if user_cfg and user_cfg.is_file():
        machine = _load_toml(user_cfg)
    values: Dict[str, Any] = {}
    for flag, dest, dotted, kind, _ in _OPTIONS:
        given = getattr(args, dest)
        if given is not None:
            values[dest] = _convert(kind, given, cwd, flag)
        elif dotted and _lookup(doc, dotted) is not None:
            values[dest] = _convert(kind, _lookup(doc, dotted), doc_base, dotted)
    root_given = args.root or args.root_arg
    if args.root and args.root_arg and args.root != args.root_arg:
        raise ConfigError("give the root once (ROOT or --root)")
    if root_given:
        root = _convert("path", root_given, cwd, "--root")
    elif doc.get("root"):
        root = _convert("path", doc["root"], doc_base, "root")
    else:
        raise ConfigError("no live telemetry directory: pass ROOT, --root or `root` in --config")
    machine_dir = user_cfg.parent if user_cfg else cwd
    for dest, dotted in (
        ("machine_kicad_cli", "kicad.cli"),
        ("machine_kicad_python", "kicad.python"),
    ):
        values[dest] = _convert("path", _lookup(machine, dotted), machine_dir, dotted)
    if "claude" not in values and _lookup(machine, "agent.claude"):
        values["claude"] = _convert("str", _lookup(machine, "agent.claude"), machine_dir, "claude")
    return finish(ViewerConfig(root=root.absolute(), **values))


def finish(cfg: ViewerConfig) -> ViewerConfig:
    """Fill the defaults derived from the root and check value ranges."""
    cfg.root = Path(cfg.root)
    cfg.experiment = Path(cfg.experiment or cfg.root.parent)
    cfg.notes = Path(cfg.notes or cfg.experiment / "notes")
    cfg.preferences = Path(cfg.preferences or cfg.experiment / "viewer-preferences.json")
    cfg.capture_root = Path(cfg.capture_root or cfg.experiment)
    cfg.cache = Path(cfg.cache or cfg.root)
    cfg.agent_cwd = Path(cfg.agent_cwd or cfg.experiment)
    if cfg.graph and not cfg.rules:
        cfg.rules = Path(cfg.graph).parent / "rules.json"
    if not cfg.listen:
        cfg.listen = ["127.0.0.1"]
    if not 0 <= int(cfg.port) <= 65535:
        raise ConfigError(f"--port {cfg.port} is out of range")
    if cfg.agent_model not in AGENT_MODELS:
        raise ConfigError(f"agent model must be one of {', '.join(AGENT_MODELS)}")
    if cfg.net_summary_model not in SUMMARY_MODELS:
        raise ConfigError(f"net summary model must be one of {', '.join(SUMMARY_MODELS)}")
    for name in ("agent_budget_usd", "agent_total_usd", "viewer3d_timeout", "viewer3d_max_mb"):
        if getattr(cfg, name) < 0:
            raise ConfigError(f"{name} must not be negative")
    for origin in cfg.allow_origin:
        if not re.fullmatch(r"https?://[^/\s]+", origin):
            raise ConfigError(f"--allow-origin {origin!r}: expected scheme://host[:port]")
    return cfg
