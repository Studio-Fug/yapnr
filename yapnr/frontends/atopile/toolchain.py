"""The atopile environment: a hash-pinned uv virtualenv per platform, and how builds find it.

``yapnr atopile setup`` creates ``<root>/<lock-id>/`` (root: ``$YAPNR_ATOPILE_HOME``, else
``$XDG_CACHE_HOME/yapnr/atopile``, else ``~/.cache/yapnr/atopile``), where ``<lock-id>`` is the
first 12 hex digits of the sha256 of the platform's lock and ``pins.json``:

- ``python/``: the pinned CPython from python-build-standalone, installed by uv (the release is
  checked against ``pins.json``);
- ``venv/``: the virtualenv, installed from ``locks/requirements-<platform>.lock`` with
  ``--require-hashes --no-deps``, so every file is one the lock names;
- ``yapnr-atopile-env.json``: written last; an environment without it is incomplete and is
  rebuilt.

atopile 0.15.8 publishes no linux-aarch64 wheel. There the lock leaves atopile out and ``setup``
installs a wheel built from the sha256-pinned sdist by ``tools/atopile/build_wheel.sh``
(``--wheel``), with ``--no-deps``.

Discovery order for a build (``discover``), like KiCad's: ``$YAPNR_ATO_PYTHON``; ``[atopile]
python`` in ``~/.config/yapnr/config.toml``; the container image's ``/opt/atopile``; the setup
environment of the current lock. The atopile version must equal the pin.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from yapnr.frontends.atopile import ATOPILE_VERSION, proc

LOCK_DIR = Path(__file__).resolve().parent / "locks"
PINS_FILE = LOCK_DIR / "pins.json"
PLATFORMS = ("darwin-arm64", "darwin-x86_64", "linux-aarch64", "linux-x86_64")
# Platforms whose lock leaves atopile out: its wheel is built from the sdist (build_wheel.sh).
SELF_BUILT = ("linux-aarch64",)
ENV_PYTHON = "YAPNR_ATO_PYTHON"
ENV_HOME = "YAPNR_ATOPILE_HOME"
IMAGE_PYTHON = Path("/opt/atopile/bin/python")
MARKER = "yapnr-atopile-env.json"
SETUP_TIMEOUT = 3600.0
PROBE_TIMEOUT = 120.0


class ToolchainError(RuntimeError):
    pass


def pins() -> Dict[str, Any]:
    return json.loads(PINS_FILE.read_text(encoding="utf-8"))


def host_platform() -> str:
    machine = _platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "aarch64", "arm64": "arm64"}.get(machine, machine)
    if sys.platform == "darwin":
        return f"darwin-{'arm64' if machine in ('arm64', 'aarch64') else machine}"
    if sys.platform.startswith("linux"):
        return f"linux-{'aarch64' if machine in ('arm64', 'aarch64') else machine}"
    return f"{sys.platform}-{machine}"


def lock_path(platform: Optional[str] = None) -> Path:
    platform = platform or host_platform()
    if platform not in PLATFORMS:
        raise ToolchainError(f"no atopile lock for {platform} (supported: {', '.join(PLATFORMS)})")
    return LOCK_DIR / f"requirements-{platform}.lock"


def lock_id(platform: Optional[str] = None) -> str:
    digest = hashlib.sha256()
    digest.update(lock_path(platform).read_bytes())
    digest.update(PINS_FILE.read_bytes())
    return digest.hexdigest()[:12]


def default_root(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_HOME):
        return Path(env[ENV_HOME]).expanduser()
    cache = env.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(cache) / "yapnr" / "atopile"


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@dataclass
class Toolchain:
    """A usable atopile interpreter and where it came from."""

    python: Path
    version: str
    source: str

    def describe(self) -> Dict[str, Any]:
        return {"python": str(self.python), "atopile": self.version, "source": self.source}


def _probe_env(tmp_home: Path) -> Dict[str, str]:
    """A minimal environment for version probes (no user HOME, no network use)."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_home),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def atopile_version(python: Path, timeout: float = PROBE_TIMEOUT) -> str:
    """The installed atopile version, read from package metadata (atopile's CLI is not run)."""
    home = Path(_tmpdir())
    try:
        out = proc.output(
            [
                str(python),
                "-I",
                "-c",
                "import importlib.metadata as m; print(m.version('atopile'))",
            ],
            timeout=timeout,
            env=_probe_env(home),
        )
    except (OSError, subprocess.CalledProcessError) as err:
        raise ToolchainError(f"{python}: cannot read the atopile version ({err})") from err
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return out.strip()


def _tmpdir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="yapnr-ato-")


def _config_python() -> Optional[str]:
    path = Path(os.path.expanduser("~/.config/yapnr/config.toml"))
    if not path.is_file():
        return None
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return None
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = (doc.get("atopile") or {}).get("python")
    return str(value) if value else None


def candidates(
    environ: Optional[Mapping[str, str]] = None, root: Optional[Path] = None
) -> List[tuple]:
    """(source, python path) in discovery order; paths may not exist."""
    env = os.environ if environ is None else environ
    out = []
    if env.get(ENV_PYTHON):
        out.append((ENV_PYTHON, Path(env[ENV_PYTHON]).expanduser()))
    configured = _config_python()
    if configured:
        out.append(("~/.config/yapnr/config.toml [atopile] python", Path(configured).expanduser()))
    out.append(("image /opt/atopile", IMAGE_PYTHON))
    try:
        env_dir = (root or default_root(env)) / lock_id()
        if (env_dir / MARKER).is_file():
            out.append((f"yapnr atopile setup ({env_dir.name})", venv_python(env_dir / "venv")))
    except ToolchainError:
        pass
    return out


def discover(environ: Optional[Mapping[str, str]] = None, root: Optional[Path] = None) -> Toolchain:
    """The first configured atopile interpreter; its version must equal ATOPILE_VERSION.

    An explicitly configured interpreter (environment or config file) that is missing or has
    another version is an error, not a reason to fall through to the next candidate.
    """
    tried = []
    for source, python in candidates(environ, root):
        explicit = not source.startswith(("image", "yapnr atopile setup"))
        if not python.is_file():
            if explicit:
                raise ToolchainError(f"{source} names {python}, which does not exist")
            tried.append(f"{source}: {python} (missing)")
            continue
        version = atopile_version(python)
        if version != ATOPILE_VERSION:
            raise ToolchainError(
                f"{source}: {python} has atopile {version}; yapnr needs {ATOPILE_VERSION}"
            )
        return Toolchain(python=python, version=version, source=source)
    raise ToolchainError(
        "no atopile environment: run `yapnr atopile setup` (or set "
        f"{ENV_PYTHON}); looked at: " + "; ".join(tried or ["nothing configured"])
    )


# --- setup ------------------------------------------------------------------------------------


def find_uv(explicit: Optional[str] = None) -> Path:
    found = explicit or shutil.which("uv")
    if not found:
        raise ToolchainError(
            "uv not found: install it with pipx or into a private virtualenv"
            " (https://docs.astral.sh/uv/), or pass --uv"
        )
    return Path(found)


def uv_version(uv: Path) -> str:
    out = proc.output([str(uv), "--version"], timeout=PROBE_TIMEOUT)
    parts = out.split()
    return parts[1] if len(parts) > 1 else out.strip()


def _uv_env(env_dir: Path, cache_dir: Path) -> Dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(env_dir)),
        "UV_CACHE_DIR": str(cache_dir),
        "UV_PYTHON_INSTALL_DIR": str(env_dir / "python"),
        "UV_PYTHON_PREFERENCE": "only-managed",
        "UV_NO_CONFIG": "1",
        "UV_LINK_MODE": "copy",
        "UV_COMPILE_BYTECODE": "1",
        "UV_NO_PROGRESS": "1",
    }
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _run(cmd: List[str], env: Dict[str, str], log: Path, what: str) -> None:
    with open(log, "a", encoding="utf-8") as out:
        out.write(f"$ {' '.join(cmd)}\n")
    code, timed_out = proc.run(cmd, timeout=SETUP_TIMEOUT, env=env, log_path=str(log))
    if timed_out or code:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        reason = "timed out" if timed_out else f"exit {code}"
        raise ToolchainError(f"{what} failed ({reason}); last lines of {log}:\n" + "\n".join(tail))


def _lock(path: Path):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def setup(
    root: Optional[Path] = None,
    uv: Optional[str] = None,
    wheel: Optional[Path] = None,
    force: bool = False,
    any_uv: bool = False,
    platform: Optional[str] = None,
    log=print,
) -> Path:
    """Create (or reuse) the environment of the current lock; returns its directory."""
    platform = platform or host_platform()
    if platform != host_platform():
        raise ToolchainError(f"setup installs for this host ({host_platform()}) only")
    pin = pins()
    lock = lock_path(platform)
    root = Path(root) if root else default_root()
    env_dir = root / lock_id(platform)
    handle = _lock(root / ".setup.lock")
    try:
        marker = env_dir / MARKER
        if marker.is_file() and not force:
            python = venv_python(env_dir / "venv")
            if python.is_file() and atopile_version(python) == pin["atopile"]:
                log(f"atopile {pin['atopile']} is set up in {env_dir}")
                return env_dir
        if platform in SELF_BUILT and wheel is None:
            raise ToolchainError(
                f"atopile has no {platform} wheel: build one with tools/atopile/build_wheel.sh"
                " and pass it with --wheel"
            )
        uv_path = find_uv(uv)
        found_uv = uv_version(uv_path)
        if found_uv != pin["uv"] and not any_uv:
            raise ToolchainError(
                f"uv {found_uv} found, the lock is made with uv {pin['uv']} (pass --any-uv to use"
                " it anyway)"
            )
        if env_dir.exists():
            shutil.rmtree(env_dir)
        env_dir.mkdir(parents=True)
        setup_log = env_dir / "setup.log"
        env = _uv_env(env_dir, root / "uv-cache")
        log(f"installing CPython {pin['python']} (python-build-standalone {pin['pbs_release']})")
        _run(
            [str(uv_path), "python", "install", "--no-bin", pin["python"]],
            env,
            setup_log,
            "uv python install",
        )
        builds = sorted((env_dir / "python").glob(f"cpython-{pin['python']}-*/BUILD"))
        release = builds[0].read_text(encoding="utf-8").strip() if builds else "unknown"
        if release != pin["pbs_release"]:
            raise ToolchainError(
                f"uv installed python-build-standalone {release}, the pins say"
                f" {pin['pbs_release']} (update pins.json together with uv)"
            )
        venv = env_dir / "venv"
        _run(
            [str(uv_path), "venv", "--python", pin["python"], str(venv)], env, setup_log, "uv venv"
        )
        python = venv_python(venv)
        log(f"installing {lock.name} (hash-checked)")
        _run(
            [
                str(uv_path),
                "pip",
                "install",
                "--python",
                str(python),
                "--require-hashes",
                "--no-deps",
                "--requirements",
                str(lock),
            ],
            env,
            setup_log,
            "uv pip install",
        )
        if platform in SELF_BUILT:
            _check_wheel(Path(wheel), pin)
            _run(
                [str(uv_path), "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
                env,
                setup_log,
                "uv pip install (atopile wheel)",
            )
        version = atopile_version(python)
        if version != pin["atopile"]:
            raise ToolchainError(f"installed atopile {version}, expected {pin['atopile']}")
        marker.write_text(
            json.dumps(
                {
                    "platform": platform,
                    "lock": lock.name,
                    "lock_id": env_dir.name,
                    "atopile": version,
                    "python": pin["python"],
                    "pbs_release": release,
                    "uv": found_uv,
                    "self_built_wheel": wheel.name if wheel else None,
                    "created": _dt.datetime.now(_dt.timezone.utc)
                    .replace(microsecond=0)
                    .isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        log(f"atopile {version} is set up in {env_dir}")
        return env_dir
    finally:
        handle.close()


def _check_wheel(wheel: Path, pin: Dict[str, Any]) -> None:
    name = wheel.name
    if not (name.startswith(f"atopile-{pin['atopile']}-") and name.endswith(".whl")):
        raise ToolchainError(f"{wheel} is not an atopile {pin['atopile']} wheel")
    if not wheel.is_file():
        raise ToolchainError(f"{wheel} does not exist")


def installed_version(python: Path) -> Optional[str]:
    """The atopile version of the environment ``python`` belongs to, read from its files.

    Starts no process (``yapnr doctor`` must not): the ``atopile-<version>.dist-info`` folder of
    the virtualenv's site-packages names it.
    """
    venv = python.parent.parent
    for info in sorted(venv.glob("lib/python*/site-packages/atopile-*.dist-info")):
        return info.name[len("atopile-") : -len(".dist-info")]
    return None


def static_status(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The first configured or set-up environment and its version, without running anything."""
    report: Dict[str, Any] = {"pinned": ATOPILE_VERSION, "configured": False, "ok": False}
    for source, python in candidates(environ):
        if not python.is_file():
            if not source.startswith(("image", "yapnr atopile setup")):
                report.update(configured=True, source=source, python=str(python), atopile=None)
                return report
            continue
        version = installed_version(python)
        report.update(
            configured=True,
            source=source,
            python=str(python),
            atopile=version,
            ok=version == ATOPILE_VERSION,
        )
        return report
    return report


def status(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """What ``yapnr atopile info`` reports (runs the interpreter to read the version)."""
    report: Dict[str, Any] = {"pinned": ATOPILE_VERSION, "platform": host_platform()}
    try:
        report["lock_id"] = lock_id()
    except ToolchainError as err:
        report["lock_id"] = None
        report["error"] = str(err)
        return report
    try:
        report.update(discover(environ).describe())
        report["ok"] = True
    except ToolchainError as err:
        report["ok"] = False
        report["error"] = str(err)
    return report
