"""Bounded native validation; a stale report can never certify a new attempt.

Opt-in content-addressed cache for the cold CLI: set PNR_DRC_CACHE_DIR (unset or
empty keeps the original behaviour). The key hashes the exact bytes of every input
kicad-cli reads for this report (board, .kicad_pro, .kicad_dru, project and global
fp-lib-tables with nested tables, the library footprints the board references),
the kicad-cli build (`version --format about` plus install stat), the CLI arguments,
the board file name (the report's "source") and the KICAD* environment. A hit
rewrites the report bytes and log a real run wrote; <cache>/stats.json counts
hits/misses. Inputs that change while kicad-cli runs are never stored.
"""

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

_ARGS = ("pcb", "drc", "{board}", "--format", "json", "--output", "{report}")


def _profile_name(env):
    """The fab profile of the caller's environment (subprocess env wins)."""
    from .fab_profile import active_name

    return active_name(env)


def _run_cli(cli, board, report, *, timeout=45, retries=1, env=None):
    report = Path(report)
    command = [str(cli)] + [arg.format(board=board, report=report) for arg in _ARGS]
    log = report.with_suffix(".log")
    with log.open("w") as stream:
        for attempt in range(retries + 1):
            if report.exists():
                report.unlink()
            try:
                subprocess.run(
                    command,
                    env=env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=timeout,
                )
                result = json.loads(report.read_text())
                _check(result)
                return result
            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                ValueError,
                OSError,
            ) as exc:
                if report.exists():
                    report.unlink()
                stream.write("\nNative DRC attempt %d failed: %s\n" % (attempt + 1, exc))
                stream.flush()
                if attempt == retries:
                    raise


def _check(result):
    if not isinstance(result.get("violations"), list) or not isinstance(
        result.get("unconnected_items"), list
    ):
        raise ValueError("incomplete native DRC report")


def _run_cold(cli, board, report, *, timeout=45, retries=1, env=None):
    root = _cache_root(env)
    if root is None:
        return _run_cli(cli, board, report, timeout=timeout, retries=retries, env=env)
    report = Path(report)
    key = None
    try:
        key = _cache_key(cli, board, root, env)
        if key is not None:
            hit = _cache_hit(root, key, report)
            if hit is not None:
                return hit
            _count(root, misses=1)
    except Exception as exc:
        _count(root, errors=1, last_error=repr(exc))
        key = None
    started = time.perf_counter()
    result = _run_cli(cli, board, report, timeout=timeout, retries=retries, env=env)
    if key is not None:
        try:
            _cache_store(root, key, cli, board, report, result, time.perf_counter() - started, env)
        except Exception as exc:
            _count(root, errors=1, last_error=repr(exc))
    return result


def run_drc(cli, board, report, *, timeout=45, retries=1, env=None, service=None, final=False):
    """Use the cold CLI by default; opt into a qualified private warm session.

    Pass final=True for final acceptance, even when PNR_DRC_SERVICE is set.
    Changed rules, unsupported reports, nonzero findings and host failures use
    the original cold implementation above. A warm host never refills zones.

    Every board is checked under the selected fab profile: its generated
    ``<board>.kicad_dru`` is (re)written here, so boards copied without it
    (phase captures, trial folders) are still checked for the PTH/NPTH/filled-via
    rules. Legacy removes only a stale generated file (none existed before).
    """
    from .fab_profile import write_dru

    if Path(board).is_file():
        write_dru(board, name=_profile_name(env))
    from .drc_warm.client import run_drc as evaluate

    return evaluate(
        cli, board, report, timeout=timeout, retries=retries, env=env, service=service, final=final
    )


# Content-addressed cache (PNR_DRC_CACHE_DIR). Everything below is best effort:
# any cache failure is counted in stats.json and falls back to the real CLI run.
_SCHEMA = "pnr-native-drc-cache-v1"
_VERSIONS = {}
_ROW = re.compile(
    r'\(lib\s+\(name\s+"((?:[^"\\]|\\.)*)"\)\s*\(type\s+"((?:[^"\\]|\\.)*)"\)\s*\(uri\s+"((?:[^"\\]|\\.)*)"\)',
    re.S,
)
_FOOTPRINT = re.compile(rb'\(footprint\s+"((?:[^"\\]|\\.)*)"')
_VAR = re.compile(r"\$\{([^}]*)\}")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    try:
        return Path(path).read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None


def _digest(path):
    data = _read(path)
    return None if data is None else _sha(data)


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cache_root(env):
    value = (os.environ if env is None else env).get("PNR_DRC_CACHE_DIR")
    return Path(value).expanduser().absolute() if value else None


def _count(root, **delta):
    """Update <root>/stats.json under an exclusive lock; never raises."""
    try:
        import fcntl

        root.mkdir(parents=True, exist_ok=True)
        with open(root / "stats.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                stats = json.loads((root / "stats.json").read_text())
            except (FileNotFoundError, ValueError):
                stats = {}
            for name, value in delta.items():
                stats[name] = (
                    stats.get(name, 0) + value if isinstance(value, (int, float)) else value
                )
            stats.update(schema=_SCHEMA, updated=time.time())
            _atomic(root / "stats.json", json.dumps(stats, indent=2, sort_keys=True).encode())
    except Exception:
        pass


def _install(cli):
    """kicad-cli path plus stat of the binary, pcbnew plugin and bundle Info.plist."""
    exe = Path(shutil.which(str(cli)) or str(cli)).resolve()
    record = []
    for path in (
        exe,
        exe.parent.parent / "PlugIns" / "_pcbnew.kiface",
        exe.parent.parent / "Info.plist",
    ):
        try:
            stat = path.stat()
            record.append([str(path), stat.st_size, stat.st_mtime_ns])
        except OSError:
            record.append([str(path), None, None])
    return exe, record


def _version(cli, install, root, env):
    """`kicad-cli version --format about`, memoised per install stat in-process and on disk."""
    tag = _sha(json.dumps(install).encode())
    if tag not in _VERSIONS:
        memo = root / "cli" / (tag + ".txt")
        data = _read(memo)
        if not data:
            data = subprocess.run(
                [str(cli), "version", "--format", "about"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=30,
            ).stdout.strip()
            if not data:
                raise ValueError("kicad-cli reported no version")
            _atomic(memo, data)
        _VERSIONS[tag] = data.decode("utf-8", "replace")
    return _VERSIONS[tag]


def _config_dirs(version, env):
    """KiCad user settings folders (KICAD_CONFIG_HOME or platform default)/<major.minor>."""
    m = re.search(r"Version:\s*(\d+)\.(\d+)", version) or re.search(r"(\d+)\.(\d+)", version)
    release = "%s.%s" % (m[1], m[2]) if m else ""
    home = Path(env.get("HOME") or Path.home())
    if env.get("KICAD_CONFIG_HOME"):
        bases = [Path(env["KICAD_CONFIG_HOME"])]
    else:
        bases = [
            home / "Library" / "Preferences" / "kicad",
            Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "kicad",
        ]
    return [base / release for base in bases]


def _cache_key(cli, board, root, env):
    env = os.environ if env is None else env
    board = Path(board).absolute()
    data = _read(board)
    if data is None:
        return None
    exe, install = _install(cli)
    version = _version(cli, install, root, env)
    configs = _config_dirs(version, env)
    defined = {}
    for folder in reversed(configs):
        try:
            defined.update(
                json.loads(_read(folder / "kicad_common.json") or b"{}")
                .get("environment", {})
                .get("vars")
                or {}
            )
        except (ValueError, AttributeError, TypeError):
            pass
    variables = dict(defined)
    variables.update(env)
    variables["KIPRJMOD"] = str(board.parent)
    stock = str(exe.parent.parent / "SharedSupport" / "footprints")

    def expand(text):
        return _VAR.sub(
            lambda m: variables.get(m[1])
            or (stock if re.fullmatch(r"KICAD\d*_FOOTPRINT_DIR", m[1]) else m[0]),
            text,
        )

    # Tables are labelled by role (project table / global path / nested URI as written), not by
    # the board folder, so identical inputs in different run folders share one entry.
    tables = []
    rows = {}

    def table(label, path, depth):
        raw = _read(path)
        tables.append([label, None if raw is None else _sha(raw)])
        if raw is None or depth > 8:
            return
        for name, kind, uri in _ROW.findall(raw.decode("utf-8", "replace")):
            if kind == "Table":
                table("table:" + uri, Path(expand(uri)), depth + 1)
            else:
                rows.setdefault(name, (kind, expand(uri)))

    table("project", board.parent / "fp-lib-table", 0)
    for folder in configs:
        table(str(folder / "fp-lib-table"), folder / "fp-lib-table", 0)
    footprints = {}
    for raw in sorted(set(_FOOTPRINT.findall(data))):
        fpid = raw.decode("utf-8", "replace")
        lib, name = fpid.split(":", 1) if ":" in fpid else ("", fpid)
        row = rows.get(lib)
        if row is None:
            footprints[fpid] = None
            continue
        path = Path(row[1])
        footprints[fpid] = [
            row[0],
            _digest(path / (name + ".kicad_mod") if path.is_dir() else path),
        ]
    manifest = dict(
        schema=_SCHEMA,
        version=version,
        install=install,
        args=list(_ARGS),
        source=board.name,
        board=_sha(data),
        project={
            suffix: _digest(board.with_suffix(suffix)) for suffix in (".kicad_pro", ".kicad_dru")
        },
        tables=tables,
        footprints=footprints,
        defined=defined,
        environment={name: value for name, value in env.items() if name.startswith("KICAD")},
    )
    return _sha(json.dumps(manifest, sort_keys=True).encode())


def _entry(root, key):
    return root / "entries" / key[:2] / key


def _cache_hit(root, key, report):
    entry = _entry(root, key)
    if not (entry / "meta.json").exists():
        return None
    try:
        meta = json.loads((entry / "meta.json").read_text())
        data = gzip.decompress((entry / "report.json.gz").read_bytes())
        if meta.get("key") != key or _sha(data) != meta.get("report_sha256"):
            raise ValueError("corrupt native DRC cache entry")
        result = json.loads(data)
        _check(result)
        log = (entry / "report.log").read_bytes()
        original = str(meta.get("report") or "")
        if not original:
            raise ValueError("native DRC cache entry has no report path")
    except Exception as exc:
        shutil.rmtree(entry, ignore_errors=True)
        _count(root, corrupt=1, last_error=repr(exc))
        return None
    # Same files as a real run: the log (report path rewritten) then the report bytes.
    log = (
        log.replace(original.encode(), str(report).encode())
        + ("\nNative DRC cache hit %s\n" % key).encode()
    )
    report.with_suffix(".log").write_bytes(log)
    report.unlink(missing_ok=True)
    _atomic(report, data)
    _count(root, hits=1, saved_seconds=float(meta.get("seconds") or 0))
    return result


def _cache_store(root, key, cli, board, report, result, seconds, env):
    data = report.read_bytes()
    # Never store a report whose inputs changed during the run or that is not the validated one.
    if _cache_key(cli, board, root, env) != key or json.loads(data) != result:
        _count(root, unstored=1)
        return
    entry = _entry(root, key)
    if entry.exists():
        return
    staging = entry.with_name(".%s.%s.tmp" % (key, uuid.uuid4().hex))
    staging.mkdir(parents=True)
    try:
        (staging / "report.json.gz").write_bytes(gzip.compress(data, mtime=0))
        (staging / "report.log").write_bytes(_read(report.with_suffix(".log")) or b"")
        (staging / "meta.json").write_text(
            json.dumps(
                dict(
                    schema=_SCHEMA,
                    key=key,
                    report=str(report),
                    board=str(Path(board).absolute()),
                    report_sha256=_sha(data),
                    seconds=seconds,
                    created=time.time(),
                ),
                indent=2,
            )
        )
        try:
            os.rename(staging, entry)
        except OSError:
            if not entry.exists():
                raise
            return
        _count(root, stores=1)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
