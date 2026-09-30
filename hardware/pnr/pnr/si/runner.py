"""Time-bounded ngspice runs: one subprocess per deck, libngspice through ctypes.

The library is the headless KiCad copy's ``libngspice.0.dylib`` (``PNR_NGSPICE_LIB``
overrides; nothing inside ``/Applications/KiCad`` is used). ngspice state is
process-global and a code-model crash kills the process, so every deck runs in a
fresh ``python -m pnr.si.runner <job.json>`` child under :func:`pnr.proc.run_status`
(hard wall-clock timeout, whole process group killed). The child lowers its own
priority (``PNR_SI_NICE``, default 10) and arms its own deadline (``alarm`` +
``RLIMIT_CPU`` a few seconds past the parent's timeout), so it dies on its own even
when the parent was killed first (no ``preexec_fn``: the parent calls this from
threads). It loads the library, ``analog.cm`` (KIBIS I-V tables use the XSPICE
``pwl`` model), runs the deck with a blocking ``run`` and writes the requested
vectors as raw float64.

Machine-wide cap: every simulation child (and every KIBIS ``kicad-cli`` export and
pcbnew board read, see :func:`slot`) holds one of ``PNR_SI_SLOTS`` (default 3) slot
locks under ``PNR_SI_SLOT_DIR`` (default ``~/.cache/pnr-si/slots``): ``flock`` locks,
released by the kernel if the holder dies, shared by every process and thread on the
machine, so parallel candidates (e.g. ``halving --native-parallel 2``) never run more
than ``PNR_SI_SLOTS`` SI children at once. Waiting longer than ``PNR_SI_SLOT_WAIT``
(default 900 s) is an error (fail closed), never a hang.

The child interpreter is ``PNR_SI_PYTHON`` or the caller's own (``sys.executable``); an
interpreter inside ``/Applications/KiCad`` is refused (call from the pnr runtime, not
from a pcbnew worker). The parent turns every failure mode into ``status: 'error'`` - never a hang, never
a pass: non-zero exit, timeout, missing/short/non-finite vectors, an ngspice error
line, a ControlledExit callback, or a caller ``check`` rejecting the waveforms
(driver never reaches its rail, flat receiver). An error is retried once in a new
process.

A lib path ending in ``.py`` is a fake backend (tests): a module with
``Backend(lib, codemodels, log)`` offering ``circ(lines)``, ``command(str)`` and
``vector(name) -> list[float]``.
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import json
import math
import os
import random
import re
import signal
import sys
import tempfile
import time
from array import array
from pathlib import Path

HEADLESS_SIM = Path.home() / "Applications/KiCad-headless.app/Contents/PlugIns/sim"
HEADLESS_FW = Path.home() / "Applications/KiCad-headless.app/Contents/Frameworks"
GUI_BUNDLE = "/Applications/KiCad/"
PNR_ROOT = Path(__file__).resolve().parents[2]
ERROR_RE = re.compile(
    r"(\berror\b|timestep too small|singular matrix|simulation\(s\) aborted|"
    r"no such vector|could not find|unknown (?:subckt|model)|fatal)",
    re.I,
)
BENIGN_RE = re.compile(r"(stderr Note:|^stdout\s*$|Error on line 0)", re.I)


DEADLINE_GRACE_S = 5.0  # child self-deadline beyond the parent's timeout


def default_timeout(env=None):
    return float((os.environ if env is None else env).get("PNR_SI_TIMEOUT", "90"))


def nice_level(env=None):
    try:
        return int((os.environ if env is None else env).get("PNR_SI_NICE", "10"))
    except ValueError:
        return 10


# ------------------------------------------------------------------ machine-wide slots


class SlotTimeout(RuntimeError):
    """No SI slot became free within ``PNR_SI_SLOT_WAIT`` seconds."""


def slot_count(env=None):
    env = os.environ if env is None else env
    try:
        return max(1, int(env.get("PNR_SI_SLOTS") or 3))
    except ValueError:
        return 3


def slot_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get("PNR_SI_SLOT_DIR") or (Path.home() / ".cache/pnr-si/slots"))


@contextlib.contextmanager
def slot(env=None, wait_s=None, what="sim"):
    """Hold one of the ``PNR_SI_SLOTS`` machine-wide SI slots for the ``with`` body.

    ``flock`` on ``<slot dir>/slot-<i>.lock`` (non-blocking polls with jitter): the
    kernel drops a dead holder's lock, so a crash never leaks a slot; separate
    ``open()`` calls conflict even inside one process, so threads are capped too.
    Yields the slot index; raises :class:`SlotTimeout` after ``wait_s`` (default
    ``PNR_SI_SLOT_WAIT``, 900 s).
    """
    env = os.environ if env is None else env
    n = slot_count(env)
    d = slot_dir(env)
    d.mkdir(parents=True, exist_ok=True)
    if wait_s is None:
        wait_s = float(env.get("PNR_SI_SLOT_WAIT") or 900)
    deadline = time.monotonic() + wait_s
    start = random.randrange(n)
    fd = index = None
    while fd is None:
        for k in range(n):
            i = (start + k) % n
            f = os.open(str(d / ("slot-%d.lock" % i)), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(f)
                continue
            fd, index = f, i
            break
        if fd is None:
            if time.monotonic() >= deadline:
                raise SlotTimeout(
                    "no free SI slot for %s within %.0f s (PNR_SI_SLOTS=%d, %s)"
                    % (what, wait_s, n, d)
                )
            time.sleep(0.05 + 0.1 * random.random())
    try:
        yield index
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# Exec shim for external tools (kicad-cli, the pcbnew reader): lower priority and
# arm SIGALRM, then exec. The alarm survives exec, so the tool dies on its own even
# if the parent (and its process-group kill) is gone. No preexec_fn involved.
_BOUND_SHIM = (
    "import os, signal, sys\n"
    "try:\n    os.nice(int(sys.argv[2]))\nexcept OSError:\n    pass\n"
    "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "signal.alarm(max(1, int(float(sys.argv[1]) + 0.999)))\n"
    "os.execv(sys.argv[3], sys.argv[3:])\n"
)


def bounded_cmd(cmd, deadline_s, env=None):
    """``cmd`` wrapped so it runs niced (``PNR_SI_NICE``) with its own ``deadline_s`` alarm."""
    return [sys.executable, "-c", _BOUND_SHIM, "%g" % deadline_s, str(nice_level(env))] + [
        str(c) for c in cmd
    ]


def _limit_self(deadline_s, nice):
    """Child side: lower priority, arm SIGALRM and a CPU-time limit (default actions kill)."""
    if nice:
        try:
            os.nice(int(nice))
        except OSError:
            pass
    if deadline_s:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(max(1, int(math.ceil(deadline_s))))
        try:
            import resource

            cpu = int(math.ceil(deadline_s)) + 1
            soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
            if hard == resource.RLIM_INFINITY or hard > cpu:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, hard))
        except (ImportError, ValueError, OSError):
            pass


def find_lib(env=None):
    """(lib path, codemodel dir) of the headless libngspice."""
    env = os.environ if env is None else env
    cands = (
        [env["PNR_NGSPICE_LIB"]]
        if env.get("PNR_NGSPICE_LIB")
        else [str(HEADLESS_SIM / "libngspice.0.dylib"), str(HEADLESS_FW / "libngspice.0.dylib")]
    )
    for lib in cands:
        if lib.endswith(".py") and os.path.exists(lib):
            return lib, ""
        if os.path.realpath(lib).startswith(GUI_BUNDLE):
            raise RuntimeError("refusing libngspice inside %s; use the headless copy" % GUI_BUNDLE)
        if os.path.exists(lib):
            cm = env.get("PNR_NGSPICE_CODEMODELS") or next(
                (
                    str(d)
                    for d in (Path(lib).parent / "ngspice", HEADLESS_SIM / "ngspice")
                    if (d / "analog.cm").exists()
                ),
                "",
            )
            return lib, cm
    raise FileNotFoundError("libngspice not found (set PNR_NGSPICE_LIB): %s" % cands)


# ------------------------------------------------------------------ child side


class _VectorInfo(ctypes.Structure):
    _fields_ = [
        ("v_name", ctypes.c_char_p),
        ("v_type", ctypes.c_int),
        ("v_flags", ctypes.c_short),
        ("v_realdata", ctypes.POINTER(ctypes.c_double)),
        ("v_compdata", ctypes.c_void_p),
        ("v_length", ctypes.c_int),
    ]


class NgSpiceBackend:
    """Minimal sharedspice.h binding (written from the header, not copied)."""

    def __init__(self, lib, codemodels, log):
        C = ctypes
        self.log = log
        SendChar = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_int, C.c_void_p)
        ControlledExit = C.CFUNCTYPE(C.c_int, C.c_int, C.c_bool, C.c_bool, C.c_int, C.c_void_p)
        BGThread = C.CFUNCTYPE(C.c_int, C.c_bool, C.c_int, C.c_void_p)
        self._cbs = (
            SendChar(lambda s, i, u: self.log.append(s.decode(errors="replace")) or 0),
            SendChar(lambda s, i, u: 0),
            ControlledExit(
                lambda st, unload, quit_, i, u: self.log.append("<<controlled exit %d>>" % st) or 0
            ),
            BGThread(lambda r, i, u: 0),
        )
        L = self.lib = C.CDLL(lib)
        L.ngSpice_Init.argtypes = [
            SendChar,
            SendChar,
            ControlledExit,
            C.c_void_p,
            C.c_void_p,
            BGThread,
            C.c_void_p,
        ]
        L.ngSpice_Command.argtypes = [C.c_char_p]
        L.ngSpice_Circ.argtypes = [C.POINTER(C.c_char_p)]
        L.ngGet_Vec_Info.argtypes = [C.c_char_p]
        L.ngGet_Vec_Info.restype = C.POINTER(_VectorInfo)
        # Never ngSpice_nospinit/ngSpice_nospiceinit before Init: SIGSEGV in this build.
        rc = L.ngSpice_Init(
            self._cbs[0], self._cbs[1], self._cbs[2], None, None, self._cbs[3], None
        )
        if rc != 0:
            raise RuntimeError("ngSpice_Init returned %s" % rc)
        for cm in codemodels:
            self.command("codemodel %s" % cm)

    def command(self, cmd):
        return self.lib.ngSpice_Command(cmd.encode())

    def circ(self, lines):
        arr = (ctypes.c_char_p * (len(lines) + 1))(*[x.encode() for x in lines], None)
        return self.lib.ngSpice_Circ(arr)

    def vector(self, name):
        p = self.lib.ngGet_Vec_Info(name.encode())
        if not p:
            raise KeyError(name)
        v = p.contents
        if not v.v_realdata or v.v_length <= 0:
            raise KeyError(name)
        return [v.v_realdata[i] for i in range(v.v_length)]


def _load_backend(lib, codemodels, log):
    if lib.endswith(".py"):
        import importlib.util

        spec = importlib.util.spec_from_file_location("pnr_si_fake_backend", lib)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.Backend(lib, codemodels, log)
    return NgSpiceBackend(lib, codemodels, log)


def child_main(job_path):
    """Run one deck; write ``<out>.json`` (header) and ``<out>.bin`` (float64 vectors)."""
    os.environ["LC_ALL"] = "C"
    job = json.loads(Path(job_path).read_text())
    _limit_self(job.get("deadline_s"), job.get("nice"))
    log = []
    t0 = time.monotonic()
    ng = _load_backend(job["lib"], job.get("codemodels", []), log)
    t_init = time.monotonic() - t0
    for c in ("set noaskquit", "set nomoremode"):
        ng.command(c)
    lines = Path(job["deck"]).read_text().splitlines()
    rc = ng.circ(lines)
    t1 = time.monotonic()
    rc_run = ng.command("run")
    t_run = time.monotonic() - t1
    names, lengths, data, missing = [], [], array("d"), []
    for name in ["time"] + list(job["vectors"]):
        try:
            v = ng.vector(name)
        except KeyError:
            missing.append(name)
            continue
        names.append(name)
        lengths.append(len(v))
        data.extend(v)
    out = Path(job["out"])
    with open(str(out) + ".bin", "wb") as f:
        data.tofile(f)
    Path(str(out) + ".json").write_text(
        json.dumps(
            dict(
                names=names,
                lengths=lengths,
                n=lengths[0] if lengths else 0,
                missing=missing,
                rc_circ=rc,
                rc_run=rc_run,
                init_s=round(t_init, 4),
                run_s=round(t_run, 4),
                log=log[-400:],
            )
        )
    )
    return 0


# ------------------------------------------------------------------ parent side


def _read(out):
    head = json.loads(Path(str(out) + ".json").read_text())
    data = array("d")
    raw = Path(str(out) + ".bin").read_bytes()
    if len(raw) % 8:
        raise ValueError("truncated vector file")
    data.frombytes(raw)
    vecs, at = {}, 0
    for name, n in zip(head["names"], head["lengths"]):
        vecs[name] = data[at : at + n]
        at += n
    if at != len(data):
        raise ValueError("vector file size does not match its header")
    return head, vecs


def _problems(head, vecs, nodes, tstop):
    probs = []
    for name in ["time"] + list(nodes):
        if name not in vecs:
            probs.append("missing vector %s" % name)
    if probs:
        return probs
    t = vecs["time"]
    ragged = sorted(n for n in vecs if len(vecs[n]) != len(t))
    if ragged:
        return ["vector length mismatch: %s" % ragged]
    if len(t) < 20:
        probs.append("only %d time points" % len(t))
    elif tstop and t[-1] < 0.98 * tstop:
        probs.append("stopped at %.4g s of %.4g s" % (t[-1], tstop))
    for name, v in vecs.items():
        if any(not math.isfinite(x) for x in v):
            probs.append("non-finite values in %s" % name)
    for line in head.get("log", []):
        if ERROR_RE.search(line) and not BENIGN_RE.search(line):
            probs.append("ngspice: " + line.strip()[:160])
            break
    return probs


def run_deck(
    deck_text,
    nodes,
    *,
    tstop=None,
    lib=None,
    timeout=None,
    retries=1,
    work_dir=None,
    check=None,
    env=None,
):
    """Simulate ``deck_text``; return dict(status, t, v, attempts, runtime_s, error, log_tail).

    ``nodes``: node names whose voltages are returned (``v[node]``). ``check(t, v)``
    may return a string to reject the waveforms (treated like a simulation error).
    """
    from pnr.proc import run_status

    env = os.environ if env is None else env
    if lib is None:
        lib, cmdir = find_lib(env)
    else:
        cmdir = "" if lib.endswith(".py") else find_lib(dict(env, PNR_NGSPICE_LIB=lib))[1]
    timeout = default_timeout(env) if timeout is None else timeout
    codemodels = [os.path.join(cmdir, "analog.cm")] if cmdir else []
    py = env.get("PNR_SI_PYTHON") or sys.executable
    if os.path.realpath(py).startswith(GUI_BUNDLE) or py.startswith(GUI_BUNDLE):
        # e.g. called from a pcbnew worker: never start simulation children from the GUI bundle
        return dict(
            status="error",
            error="SI simulation child would run %s; run from the pnr runtime or set "
            "PNR_SI_PYTHON" % py,
            attempts=[],
            runtime_s=0.0,
            lib=lib,
        )
    tmp_owner = None
    if work_dir is None:
        tmp_owner = tempfile.TemporaryDirectory(prefix="pnr-si-run-")
        work_dir = tmp_owner.name
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    deck = work / "deck.cir"
    deck.write_text(deck_text)
    child_env = dict(os.environ, PYTHONPATH=str(PNR_ROOT), LC_ALL="C", LANG="C")
    attempts = []
    t_all = time.monotonic()
    result = None
    try:
        for attempt in range(1 + max(0, retries)):
            out = work / ("out%d" % attempt)
            job = work / ("job%d.json" % attempt)
            job.write_text(
                json.dumps(
                    dict(
                        deck=str(deck),
                        lib=lib,
                        codemodels=codemodels,
                        vectors=list(nodes),
                        out=str(out),
                        deadline_s=timeout + DEADLINE_GRACE_S,
                        nice=nice_level(env),
                    )
                )
            )
            t_wait = time.monotonic()
            try:
                with slot(env, what="deck"):
                    waited = time.monotonic() - t_wait
                    t0 = time.monotonic()
                    with open(work / ("child%d.log" % attempt), "wb") as log:
                        code, timed_out = run_status(
                            [py, "-m", "pnr.si.runner", str(job)],
                            timeout=timeout,
                            env=child_env,
                            stdout=log,
                            stderr=log,
                        )
            except SlotTimeout as error:
                attempts.append(
                    dict(
                        attempt=attempt,
                        exit=None,
                        wall_s=0.0,
                        slot_wait_s=round(time.monotonic() - t_wait, 3),
                        error=str(error),
                    )
                )
                continue
            wall = time.monotonic() - t0
            rec = dict(
                attempt=attempt, exit=code, wall_s=round(wall, 3), slot_wait_s=round(waited, 3)
            )
            if timed_out:
                rec["error"] = "timeout after %.0f s" % timeout
            elif code in (-9, -signal.SIGXCPU, -signal.SIGALRM):
                rec["error"] = (
                    "runner killed by signal %d (not the parent timeout: out of memory or its own deadline?)"
                    % -code
                )
            elif code != 0:
                tail = (work / ("child%d.log" % attempt)).read_text(errors="replace")[-300:]
                rec["error"] = "runner exit %s: %s" % (
                    code,
                    tail.strip().splitlines()[-1] if tail.strip() else "",
                )
            else:
                try:
                    head, vecs = _read(out)
                    probs = _problems(head, vecs, nodes, tstop)
                    if not probs and check is not None:
                        why = check(vecs["time"], {n: vecs[n] for n in nodes})
                        if why:
                            probs.append(why)
                    rec.update(
                        run_s=head.get("run_s"), init_s=head.get("init_s"), points=head.get("n")
                    )
                    if probs:
                        rec["error"] = "; ".join(probs)
                        rec["log_tail"] = head.get("log", [])[-8:]
                    else:
                        result = dict(status="ok", t=vecs["time"], v={n: vecs[n] for n in nodes})
                except Exception as error:
                    rec["error"] = "unreadable result: %r" % (error,)
            attempts.append(rec)
            if result is not None:
                break
    finally:
        if tmp_owner is not None:
            tmp_owner.cleanup()
    base = dict(attempts=attempts, runtime_s=round(time.monotonic() - t_all, 3), lib=lib)
    if result is None:
        return dict(base, status="error", error=attempts[-1].get("error", "unknown"))
    result.update(base)
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m pnr.si.runner <job.json>")
    sys.exit(child_main(sys.argv[1]))
