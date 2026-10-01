"""Runs inside atopile's interpreter during ``yapnr atopile build``; stdlib only.

The runner puts this directory on ``PYTHONPATH``, so ``sitecustomize.py`` imports this module at
the start of **every** Python process of the build: ``ato`` itself, its multiprocessing
forkserver and the build workers forked from it (atopile 0.15.8 builds in forkserver workers, so
anything set up in the parent process alone never reaches the build). Nothing happens unless the
runner set ``YAPNR_ATO_HOOK=1``.

When enabled, the hook patches atopile 0.15.8 as its modules are imported (an import hook; no
file of the atopile environment is changed):

- **Loopback picker only.** ``ApiClient._cfg`` hands atopile a placeholder bearer token only when
  the effective components URL parses to the runner's picker: ``http``, a literal loopback
  address and the same port (``YAPNR_ATO_PICKER_URL``). Any other URL is refused, so a hosted
  service never sees a request from a yapnr build. The token never enters atopile's global
  sign-in state (``atopile.auth``), which its telemetry and agent clients read.
- **Empty queries.** ``fetch_parts_multiple([])`` returns ``[]`` without a request.
- **Parts from the project.** With ``YAPNR_ATO_LOCAL_PARTS=1``, a picked LCSC id whose atomic part
  is in the project's parts directory (the runner materializes them from the part cache) is
  attached from there instead of being downloaded from EasyEDA.
- **Offline.** With ``YAPNR_ATO_OFFLINE=1``, EasyEDA is never contacted; a pick that needs it
  fails with a message naming the missing part. Nor is a git repository: atopile installs a
  missing dependency at the start of every build, and a ``git`` one is cloned
  (``faebryk.libs.git.clone_repo``); offline, that fails naming the dependency, and the runner
  also limits git to local repositories (``GIT_ALLOW_PROTOCOL=file``). Registry dependencies
  fail on their own: the packages URL is the loopback picker, which serves none.
- **No GUI KiCad.** ``kicad-cli`` is the one in ``YAPNR_ATO_KICAD_CLI`` (the headless copy) or
  none; atopile's search of ``/Applications/KiCad`` and its ``pcbnew`` launcher are disabled.
  ``YAPNR_ATO_KICAD_FOOTPRINTS`` replaces the stock footprint directory it assumes.
- **No running KiCad.** atopile looks for KiCad's IPC sockets (a fixed ``/tmp/kicad``, not
  ``TMPDIR``) to reload an open board after every build and while ingesting parts; the hook
  hands it no sockets, so a build never talks to a KiCad instance on the machine.

Every patch checks that the attribute it replaces exists and fails loudly otherwise, so an
atopile version this hook was not written for cannot build silently unpatched.
"""

from __future__ import annotations

import importlib.machinery
import ipaddress
import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

ENABLE = "YAPNR_ATO_HOOK"
PICKER_URL = "YAPNR_ATO_PICKER_URL"
LOCAL_PARTS = "YAPNR_ATO_LOCAL_PARTS"
OFFLINE = "YAPNR_ATO_OFFLINE"
KICAD_CLI = "YAPNR_ATO_KICAD_CLI"
KICAD_FOOTPRINTS = "YAPNR_ATO_KICAD_FOOTPRINTS"
EVENT_LOG = "YAPNR_ATO_HOOK_LOG"

# The bearer token atopile sends to the loopback picker, which ignores it.
TOKEN = "yapnr-local-picker"


class HookError(RuntimeError):
    """This hook does not fit the atopile it was loaded into."""


_log_lock = threading.Lock()


def record(event: str, **fields) -> None:
    """Append one decision to the runner's hook log (a JSON line), if the runner asked for one."""
    path = os.environ.get(EVENT_LOG)
    if not path:
        return
    line = json.dumps({"event": event, "pid": os.getpid(), **fields}, sort_keys=True)
    with _log_lock, open(path, "a", encoding="utf-8") as out:
        out.write(line + "\n")


def _flag(name: str) -> bool:
    return os.environ.get(name, "") == "1"


def url_problem(url: str, allowed: str) -> "str | None":
    """Why ``url`` is not the runner's loopback picker ``allowed`` (None when it is)."""
    if not allowed:
        return "no local picker is running for this build"
    try:
        want, got = urlsplit(allowed), urlsplit(url or "")
        want_host = ipaddress.ip_address(want.hostname or "")
        want_port, got_port = want.port, got.port
    except ValueError as err:
        return f"unparsable URL ({err})"
    if want.scheme != "http" or not want_host.is_loopback or want_port is None:
        return f"the runner's picker URL {allowed!r} is not a loopback http URL with a port"
    try:
        got_host = ipaddress.ip_address(got.hostname or "")
    except ValueError:
        return f"{url!r} does not name a literal loopback address"
    if (
        got.scheme != "http"
        or got_host != want_host
        or got_port != want_port
        or got.path not in ("", "/")
        or got.query
        or got.fragment
        or "@" in got.netloc
        or "\\" in (url or "")
    ):
        return f"{url!r} is not the runner's picker {allowed!r}"
    return None


def _require(module, *names: str) -> None:
    for name in names:
        target = module
        for part in name.split("."):
            if not hasattr(target, part):
                raise HookError(
                    f"yapnr atopile hook: {module.__name__}.{name} is missing; this atopile is not"
                    " the version yapnr supports (docs/frontends/atopile.md)"
                )
            target = getattr(target, part)


# --- patches ----------------------------------------------------------------------------------


def patch_api(module) -> None:
    """faebryk.libs.picker.api.api: loopback-only token, empty queries answered locally."""
    _require(module, "ApiClient._cfg", "ApiClient.ApiConfig", "ApiClient.fetch_parts_multiple")
    client_class = module.ApiClient
    original_multiple = client_class.fetch_parts_multiple

    def fetch_parts_multiple(self, params):
        if not params:
            record("empty-query")
            return []
        return original_multiple(self, params)

    def _cfg(self):
        cached = self.__dict__.get("_yapnr_cfg")
        if cached is not None:
            return cached
        from atopile.config import config
        from atopile.errors import UserException

        allowed = os.environ.get(PICKER_URL, "")
        effective = config.project.services.components.url
        problem = url_problem(effective, allowed) or url_problem(self.ApiConfig.api_url, allowed)
        if problem:
            record("picker-refused", reason=problem)
            raise UserException(
                f"yapnr builds pick parts from their local picker only: {problem}.",
                title="Part picking refused",
            )
        cfg = self.ApiConfig(api_url=effective.rstrip("/"), api_key=TOKEN)
        self.__dict__["_yapnr_cfg"] = cfg
        return cfg

    client_class.fetch_parts_multiple = fetch_parts_multiple
    client_class._cfg = property(_cfg)


class LocalPart:
    """What ``download_easyeda_info`` returns for a part found in the project's parts directory."""

    def __init__(self, lcsc_id: str, atopart):
        self.lcsc_id = lcsc_id
        self.atopart = atopart


_local_parts: "dict[str, LocalPart | None]" = {}


def find_local_part(lcsc_id: str, parts_dir: Path, load) -> "LocalPart | None":
    """The atomic part in ``parts_dir`` picked as ``lcsc_id`` (``C<digits>``), else None.

    A part matches when its ``.ato`` declares ``supplier_partno="<lcsc_id>"``. With several
    matches the first directory in name order wins, and the choice is recorded.
    """
    key = f"{parts_dir}::{lcsc_id}"
    if key in _local_parts:
        return _local_parts[key]
    needle = f'supplier_partno="{lcsc_id}"'
    found = []
    if parts_dir.is_dir():
        for part_dir in sorted(p for p in parts_dir.iterdir() if p.is_dir()):
            ato = part_dir / f"{part_dir.name}.ato"
            try:
                text = ato.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if needle in text:
                found.append(part_dir)
    result = None
    if found:
        result = LocalPart(lcsc_id, load(found[0]))
        record("local-part", lcsc=lcsc_id, part=found[0].name, candidates=len(found))
    _local_parts[key] = result
    return result


def _offline_message(lcsc_id: str) -> str:
    return (
        f"yapnr builds are offline: {lcsc_id} is not in the project's parts. Add the part to the"
        " part cache and to the project's parts lock (docs/part-cache.md)."
    )


def patch_lcsc(module) -> None:
    """faebryk.libs.picker.lcsc: parts from the project first; no EasyEDA when offline."""
    _require(module, "download_easyeda_info", "get_raw", "LCSC_ServiceException")
    original_download = module.download_easyeda_info
    original_get_raw = module.get_raw

    def get_raw(lcsc_id):
        if _flag(OFFLINE):
            record("easyeda-refused", lcsc=lcsc_id)
            raise module.LCSC_ServiceException(lcsc_id, _offline_message(lcsc_id))
        return original_get_raw(lcsc_id)

    def download_easyeda_info(lcsc_id, get_model=True):
        if _flag(LOCAL_PARTS):
            from atopile.config import config
            from faebryk.libs.ato_part import AtoPart

            local = find_local_part(lcsc_id, Path(config.project.paths.parts), AtoPart.load)
            if local is not None:
                return local
        if _flag(OFFLINE):
            record("easyeda-refused", lcsc=lcsc_id)
            raise module.LCSC_ServiceException(lcsc_id, _offline_message(lcsc_id))
        return original_download(lcsc_id, get_model=get_model)

    module.get_raw = get_raw
    module.download_easyeda_info = download_easyeda_info


def patch_part_lifecycle(module) -> None:
    """faebryk.libs.part_lifecycle: a LocalPart is already in the library; register it only."""
    _require(module, "PartLifecycle.Library.ingest_part_from_easyeda")
    library_class = module.PartLifecycle.Library
    _require(library_class, "_insert_fp_lib")
    original = library_class.ingest_part_from_easyeda

    def ingest_part_from_easyeda(self, epart):
        if isinstance(epart, LocalPart):
            self._insert_fp_lib(epart.atopart.path.name)
            return epart.atopart
        return original(self, epart)

    library_class.ingest_part_from_easyeda = ingest_part_from_easyeda


def patch_kicadcli(module) -> None:
    """kicadcliwrapper.lib: the configured headless kicad-cli, never a search of the GUI app."""
    _require(module, "find_kicad_cli")

    def find_kicad_cli():
        path = os.environ.get(KICAD_CLI, "")
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return Path(path)
        raise FileNotFoundError(
            "kicad-cli: yapnr runs only the headless KiCad it discovered, and found none"
        )

    module.find_kicad_cli = find_kicad_cli


def _no_ipc_dir() -> Path:
    """A directory with no KiCad sockets in it (private to the build; never created)."""
    return Path(os.environ.get("TMPDIR") or "/nonexistent") / "yapnr-no-kicad-ipc"


def patch_kicad_paths(module) -> None:
    """faebryk.libs.kicad.paths: no pcbnew GUI; stock footprints; no KiCad IPC sockets."""
    _require(module, "find_pcbnew", "GLOBAL_FP_DIR_PATH", "get_ipc_socket_path")

    def find_pcbnew():
        raise FileNotFoundError("pcbnew: yapnr builds never start the KiCad GUI")

    module.find_pcbnew = find_pcbnew
    module.get_ipc_socket_path = _no_ipc_dir
    footprints = os.environ.get(KICAD_FOOTPRINTS)
    if footprints:
        module.GLOBAL_FP_DIR_PATH = Path(footprints)


def patch_kicad_ipc(module) -> None:
    """faebryk.libs.kicad.ipc: no socket of a running KiCad is ever opened."""
    _require(module, "_kicad_socket_files", "reload_pcb")

    def _kicad_socket_files():
        return []

    def reload_pcb(pcb_path, backup_path=None):
        record("kicad-reload-skipped")

    module._kicad_socket_files = _kicad_socket_files
    module.reload_pcb = reload_pcb


def patch_git(module) -> None:
    """faebryk.libs.git: no clone while offline (atopile installs missing git dependencies)."""
    _require(module, "clone_repo")
    original = module.clone_repo

    def clone_repo(repo_url, clone_target, depth=None, ref=None):
        if _flag(OFFLINE):
            record("git-refused", repo=str(repo_url))
            from atopile.errors import UserException

            raise UserException(
                f"yapnr builds are offline: the git dependency {repo_url} is not installed."
                " Install it into the project's .ato/modules before the build"
                " (docs/frontends/atopile.md).",
                title="Dependency fetch refused",
            )
        return original(repo_url, clone_target, depth=depth, ref=ref)

    module.clone_repo = clone_repo


PATCHES = {
    "faebryk.libs.picker.api.api": patch_api,
    "faebryk.libs.picker.lcsc": patch_lcsc,
    "faebryk.libs.part_lifecycle": patch_part_lifecycle,
    "kicadcliwrapper.lib": patch_kicadcli,
    "faebryk.libs.kicad.paths": patch_kicad_paths,
    "faebryk.libs.kicad.ipc": patch_kicad_ipc,
    "faebryk.libs.git": patch_git,
}


class _PatchingFinder:
    """A meta-path finder that runs a patch right after one of the PATCHES modules executes."""

    def find_spec(self, fullname, path=None, target=None):
        patch = PATCHES.get(fullname)
        if patch is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            raise HookError(f"yapnr atopile hook: cannot load {fullname} to patch it")
        original = spec.loader.exec_module

        def exec_module(module, _original=original, _patch=patch):
            _original(module)
            _patch(module)
            record("patched", module=fullname)

        spec.loader.exec_module = exec_module
        return spec

    def invalidate_caches(self):
        pass


def install() -> bool:
    """Install the import hook when the runner enabled it; returns whether it did."""
    if not _flag(ENABLE):
        return False
    already = [name for name in PATCHES if name in sys.modules]
    if already:
        raise HookError(f"yapnr atopile hook: loaded too late, {already} already imported")
    if not any(isinstance(f, _PatchingFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _PatchingFinder())
    return True
