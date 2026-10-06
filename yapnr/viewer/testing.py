"""Helpers for the viewer's tests: child environments, fake executables and viewer processes.

The viewer itself never imports this module. It lives in the package because the repository's
test folders hold only ``test_*.py`` files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional, Sequence, Tuple

from yapnr.viewer import runtime

# tests/fixtures/viewer next to the tests (runfiles or a checkout).
FIXTURE_DIR = Path("tests/fixtures/viewer")


def fixture(*parts: str) -> Path:
    """A path under tests/fixtures/viewer, from Bazel runfiles or the checkout."""
    roots = []
    srcdir = os.environ.get("TEST_SRCDIR")
    if srcdir:
        roots.append(Path(srcdir) / os.environ.get("TEST_WORKSPACE", "_main"))
    roots.append(Path(__file__).parent.parent.parent)
    for root in roots:
        p = root / FIXTURE_DIR.joinpath(*parts)
        if p.exists():
            return p.absolute()
    raise FileNotFoundError(f"test fixture {FIXTURE_DIR.joinpath(*parts)} not found")


def fixture_copy(*parts: str) -> Path:
    """A private copy of a fixture (regular files, symlinks followed), removed at exit.

    Bazel runfiles are symlinks, and the atopile source browser skips symlinked files on
    purpose (they could point out of the source folder), so tests that read sources use a copy.
    """
    import atexit
    import shutil
    import tempfile

    src = fixture(*parts)
    tmp = Path(tempfile.mkdtemp(prefix="viewer-fixture-"))
    atexit.register(shutil.rmtree, tmp, True)
    dest = tmp / src.name
    shutil.copytree(src, dest, symlinks=False)
    return dest


def viewer_dist_dir() -> Path:
    """What to pass as ``--dist``: the assembled ``//yapnr/viewer:dist`` if a test declared it in
    `data` (elkjs/three.js included, from Bazel runfiles), else the bare `static/` source
    directory (works too -- the schematic layout and 3D view just report their library missing,
    same as docs/viewer.md describes for a plain checkout). Works under `bazel test` and under a
    plain `python3 -m unittest` from the checkout, like fixture() above."""
    roots = []
    srcdir = os.environ.get("TEST_SRCDIR")
    if srcdir:
        roots.append(Path(srcdir) / os.environ.get("TEST_WORKSPACE", "_main"))
    roots.append(Path(__file__).parent.parent.parent)
    for root in roots:
        p = root / "yapnr/viewer/dist"
        if p.is_dir():
            return p.absolute()
    for root in roots:
        p = root / "yapnr/viewer/static"
        if p.is_dir():
            return p.absolute()
    raise FileNotFoundError("neither yapnr/viewer/dist nor yapnr/viewer/static found")


def child_env(**extra: str) -> dict:
    """Environment for a child Python of the test: this interpreter's import path, no machine
    config (``YAPNR_USER_CONFIG`` empty) and no KiCad or claude found by accident."""
    env = runtime.hermetic_env(threads=False)
    env["YAPNR_USER_CONFIG"] = ""
    for name in ("YAPNR_KICAD_CLI", "PNR_KICAD_CLI", "YAPNR_KICAD_PYTHON"):
        env.pop(name, None)
    env.update(extra)
    return env


def module_argv(module: str, *args: str) -> list:
    return [sys.executable, "-m", module, *args]


def write_fake(path: os.PathLike, script: str, python: Optional[str] = None) -> Path:
    """An executable ``path`` that runs the Python ``script`` (saved as ``<path>.py``).

    A ``#!/bin/sh`` wrapper instead of a ``#!<python>`` shebang: the interpreter paths of Bazel
    runfiles are longer than the kernel's shebang limit on Linux.
    """
    path = Path(path)
    body = path.with_name(path.name + ".py")
    body.write_text(script)
    path.write_text(
        '#!/bin/sh\nexec %s %s "$@"\n'
        % (shlex.quote(python or sys.executable), shlex.quote(str(body)))
    )
    path.chmod(0o755)
    return path


def start_viewer(
    root: os.PathLike, *args: str, env: Optional[dict] = None, stderr=subprocess.PIPE
) -> Tuple[subprocess.Popen, str]:
    """Start ``python -m yapnr.viewer`` on an ephemeral loopback port; (process, base URL)."""
    proc = subprocess.Popen(
        module_argv(
            "yapnr.viewer", "--root", str(root), "--port", "0", "--listen", "127.0.0.1", *args
        ),
        env=env or child_env(),
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
    )
    line = proc.stdout.readline().strip()
    if not line.startswith("yapnr viewer: "):
        proc.kill()
        err = proc.stderr.read() if proc.stderr else ""
        proc.wait(10)
        raise RuntimeError(f"viewer did not start: {line!r} {err[-2000:]}")
    return proc, line.split(": ", 1)[1]


def stop(proc: subprocess.Popen, timeout: float = 10) -> int:
    """Terminate a started viewer and close its pipes; its exit code."""
    if proc.poll() is None:
        proc.terminate()
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait(timeout)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()
    return code


def wait_for(fn, timeout: float = 20, step: float = 0.05):
    """fn()'s first truthy result within timeout seconds (AssertionError otherwise)."""
    end = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(step)


def argv_value(argv: Sequence[str], flag: str) -> str:
    return argv[list(argv).index(flag) + 1]


# ---------------------------------------------------------------------- synthetic dense geometry
# tests/e2e/viewer's rendering-performance tests need a board with thousands of vias (ground
# stitching, the reported-laggy case) but no real board or KiCad export: this hand-builds viewer
# geometry (the same shape yapnr/viewer/server.py's from_graph()/extract() produce: width, height,
# parts, tracks, vias, zones) directly, with vias and tracks on a regular, fully predictable grid so
# a test can compute exactly where a given via/track/pad is without re-deriving it from the output.


def synthetic_via_board(n_vias: int = 3000, width: float = 120.0, height: float = 90.0) -> dict:
    """Deterministic geometry: a regular grid of ``n_vias`` ground-stitching-style vias (net
    mostly GND, a few other nets sprinkled in), one track per adjacent pair in each row (a
    comparable count to the vias, as on a real stitched board), a GND zone covering the board, and
    a handful of two-pad parts off to one side (unrelated to the via grid, for pad/component hit
    tests). ``via_at()`` below locates a specific grid element.
    """
    import math

    cols = max(2, math.ceil(math.sqrt(n_vias * width / height)))
    rows = max(2, math.ceil(n_vias / cols))
    dx, dy = (width - 4) / (cols - 1), (height - 4) / (rows - 1)
    vias = []
    for j in range(rows):
        for i in range(cols):
            if len(vias) >= n_vias:
                break
            net = "GND" if (i + j) % 5 else f"SIG{(i + j) % 7}"
            vias.append(
                {"net": net, "xy": [round(2 + i * dx, 4), round(2 + j * dy, 4)], "diameter": 0.4}
            )
    tracks = []
    for j in range(rows):
        for i in range(cols - 1):
            a, b = j * cols + i, j * cols + i + 1
            if b >= len(vias):
                continue
            tracks.append([vias[a]["net"], "F.Cu", vias[a]["xy"], vias[b]["xy"], 0.15])
    parts = []
    for n in range(8):
        x, y = width - 14 + (n % 4) * 3, 4 + (n // 4) * 3
        parts.append(
            dict(
                ref=f"C{n + 1}",
                xy=[x, y],
                pads=[
                    dict(
                        number="1",
                        net="3V3",
                        xy=[x - 0.5, y],
                        size=[0.6, 0.6],
                        angle=0,
                        shape="rect",
                        layers=["F.Cu"],
                    ),
                    dict(
                        number="2",
                        net="GND",
                        xy=[x + 0.5, y],
                        size=[0.6, 0.6],
                        angle=0,
                        shape="rect",
                        layers=["F.Cu"],
                    ),
                ],
            )
        )
    zone = dict(
        layer="F.Cu",
        net="GND",
        paths=[[[1.0, 1.0], [width - 1.0, 1.0], [width - 1.0, height - 1.0], [1.0, height - 1.0]]],
    )
    return dict(
        frame="mm-y-up",
        width=width,
        height=height,
        parts=parts,
        tracks=tracks,
        vias=vias,
        zones=[zone],
        cols=cols,
        rows=rows,
    )


def via_at(geo: dict, i: int, j: int) -> dict:
    return geo["vias"][j * geo["cols"] + i]


def seed_board_event(root: os.PathLike, geo: dict, candidate: str = "mc0") -> str:
    """Write ``geo`` straight into <root>/geometry/<sha>.json and drop one event referencing it
    (kind ``signal_start``, matching a real one's shape) into <root>/events/ -- the running
    viewer's ingest loop picks it up exactly as it would a real extracted board, because the
    geometry file already exists under its sha256 (server.py's geometry() skips extraction
    whenever the cache file is already there). Returns the sha256. ``geo`` is trimmed of the
    ``cols``/``rows`` bookkeeping fields synthetic_via_board() adds before writing.
    """
    root = Path(root)
    body = {k: v for k, v in geo.items() if k not in ("cols", "rows")}
    payload = json.dumps(body).encode()
    sha = hashlib.sha256(payload).hexdigest()
    (root / "geometry").mkdir(parents=True, exist_ok=True)
    (root / "geometry" / f"{sha}.json").write_bytes(payload)
    (root / "events").mkdir(parents=True, exist_ok=True)
    eid = f"{time.time_ns()}-seed"
    event = {
        "schema": "pnr-live-event-v1",
        "id": eid,
        "time": time.time(),
        "kind": "signal_start",
        "candidate": candidate,
        "iteration": None,
        "data": {"phase": "signals"},
        "board": "synthetic.kicad_pcb",
        "board_sha256": sha,
    }
    (root / "events" / f"{eid}.json").write_text(json.dumps(event))
    return sha


def seed_lane_events(root: os.PathLike, events: Sequence[dict]) -> None:
    """Drop each of ``events`` into <root>/events/ as its own file (schema ``pnr-live-event-v1``,
    matching :func:`yapnr.viewer.server.ingest`'s expectations), for tests of the experiment tree
    and progress bars that need several lanes at various phases/statuses without any board
    geometry at all. Each dict needs at least ``candidate`` and ``kind``; ``id``/``time`` default
    to a fresh, increasing value per call so ingestion order matches list order, and every other
    field (``data``, ``iteration``, ...) is used as given. Files are written one per millisecond
    apart in id ordering (the real id format, ``<time_ns>-<hex>``) so two events for the same
    candidate replay in the order this function was called with them.
    """
    root = Path(root)
    (root / "events").mkdir(parents=True, exist_ok=True)
    for i, event in enumerate(events):
        eid = event.get("id") or f"{time.time_ns() + i}-seed{i:04d}"
        body = {
            "schema": "pnr-live-event-v1",
            "time": time.time(),
            "iteration": None,
            "data": {},
            **event,
            "id": eid,  # always matches the filename, even if `events[i]` set its own "id"
        }
        (root / "events" / f"{eid}.json").write_text(json.dumps(body))


# ---------------------------------------------------------------------- headless Chrome / CDP
# A minimal, stdlib-only Chrome DevTools Protocol client: tests/e2e/viewer drives a real headless
# Chrome (touch emulation, Input.dispatchTouchEvent, screenshots) and there is no CDP/WebSocket
# client in the pypi lock, so this does the WebSocket opening handshake and frame (de)masking by
# hand. Good enough for a test driver, nothing more: text frames only, one in-flight call per id,
# a background thread demuxing unsolicited events from call replies.

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def chrome_binary() -> Optional[Path]:
    """The headless Chrome/Chromium to drive, or None (YAPNR_CHROME, else PATH, else the usual
    install locations). Tests skip (never fail) when this is None: Chrome is a local convenience
    for tests/e2e/viewer, not something CI is guaranteed to have."""
    import shutil

    env = os.environ.get("YAPNR_CHROME")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for p in CHROME_PATHS:
        if Path(p).exists():
            return Path(p)
    return None


class CDPError(RuntimeError):
    pass


class _WS:
    """One ws:// connection: the RFC 6455 client handshake, then masked text frames."""

    def __init__(self, url: str, timeout: float = 20):
        import socket

        assert url.startswith("ws://"), url
        rest = url[len("ws://") :]
        host_port, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = host_port.partition(":")
        port = int(port or 80)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("socket closed during the WebSocket handshake")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise CDPError("WebSocket handshake failed: " + head.decode(errors="replace"))
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if accept.encode() not in head:
            raise CDPError("bad Sec-WebSocket-Accept")
        self._buf = bytearray(rest)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n))
            if not chunk:
                raise CDPError("websocket closed")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send_text(self, text: str):
        payload = text.encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < (1 << 16):
            header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        self.sock.sendall(header + mask + masked)

    def recv_text(self) -> str:
        parts = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode, masked, ln = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            if ln == 126:
                (ln,) = struct.unpack("!H", self._recv_exact(2))
            elif ln == 127:
                (ln,) = struct.unpack("!Q", self._recv_exact(8))
            mask = self._recv_exact(4) if masked else None
            data = self._recv_exact(ln)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:  # ping -> pong (same payload)
                n = len(data)
                pmask = os.urandom(4)
                self.sock.sendall(
                    struct.pack("!BB", 0x8A, 0x80 | n)
                    + pmask
                    + bytes(b ^ pmask[i % 4] for i, b in enumerate(data))
                )
                continue
            if opcode == 0x8:
                raise CDPError("websocket closed by peer")
            parts.append(data)
            if fin:
                break
        return b"".join(parts).decode()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """One CDP target (the browser endpoint, or one page's own): call() sends {id,method,params}
    and blocks for the matching {id,result|error}; wait_event() scans unsolicited {method,params}
    notifications a background reader thread appends as they arrive."""

    def __init__(self, ws_url: str, timeout: float = 20):
        self.ws = _WS(ws_url, timeout=timeout)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._events = []
        self._ev_lock = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        try:
            while not self._stop:
                obj = json.loads(self.ws.recv_text())
                if "id" in obj:
                    with self._lock:
                        box = self._pending.pop(obj["id"], None)
                    if box is not None:
                        box[1] = obj
                        box[0].set()
                else:
                    with self._ev_lock:
                        self._events.append(obj)
        except Exception as e:  # the socket died or close() ran: wake up anyone still waiting
            with self._lock:
                for box in self._pending.values():
                    box[1] = {"error": {"message": str(e)}}
                    box[0].set()

    def call(self, method: str, params=None, session_id=None, timeout=None):
        with self._lock:
            self._id += 1
            mid = self._id
            done = threading.Event()
            box = [done, None]
            self._pending[mid] = box
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send_text(json.dumps(msg))
        if not done.wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise CDPError(f"timed out waiting for {method}")
        resp = box[1]
        if "error" in resp:
            raise CDPError(f"{method}: {resp['error']}")
        return resp.get("result", {})

    def wait_event(self, method: str, predicate=None, timeout: float = 10):
        end = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < end:
            with self._ev_lock:
                new, seen = self._events[seen:], len(self._events)
            for e in new:
                if e.get("method") == method and (
                    predicate is None or predicate(e.get("params", {}))
                ):
                    return e.get("params", {})
            time.sleep(0.02)
        raise CDPError(f"timed out waiting for event {method}")

    def eval(self, expression: str, timeout: float = 20):
        """Runtime.evaluate, awaited, returned by value (a JS error becomes a CDPError)."""
        r = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if r.get("exceptionDetails"):
            raise CDPError(r["exceptionDetails"].get("text") or str(r["exceptionDetails"]))
        return r.get("result", {}).get("value")

    def close(self):
        self._stop = True
        self.ws.close()


def http_json(url: str, timeout: float = 10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def start_chrome(
    user_data_dir: os.PathLike, extra_args: Sequence[str] = ()
) -> Tuple[subprocess.Popen, str]:
    """A headless Chrome with an isolated profile; (process, its http://127.0.0.1:<port>/json base
    URL). Finds its own free port (``--remote-debugging-port=0``, parsed off stderr) rather than
    one this caller picked, so it never collides with another viewer or test on the machine."""
    binary = chrome_binary()
    if binary is None:
        raise RuntimeError("no Chrome/Chromium found (see chrome_binary())")
    proc = subprocess.Popen(
        [
            str(binary),
            "--headless=new",
            "--remote-debugging-port=0",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-sync",
            "--disable-background-networking",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={user_data_dir}",
            *extra_args,
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    end = time.monotonic() + 15
    port = None
    buf = ""
    while time.monotonic() < end:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"chrome exited early (code {proc.returncode}): {buf[-2000:]}")
            continue
        buf += line
        m = re.search(r"DevTools listening on ws://127\.0\.0\.1:(\d+)/", line)
        if m:
            port = int(m.group(1))
            break
    if port is None:
        proc.kill()
        raise RuntimeError(f"chrome never printed its DevTools port: {buf[-2000:]}")
    return proc, f"http://127.0.0.1:{port}"


def new_chrome_page(base_url: str, timeout: float = 20) -> CDP:
    """A CDP session on the about:blank page/tab a start_chrome() browser already opened (its own
    WebSocket, simpler than multiplexing everything through the browser-level socket). start_chrome
    always launches with that one URL, so /json/list already has it -- no need for /json/new
    (a POST-only endpoint in current Chrome; GET answers 405)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        targets = http_json(f"{base_url}/json/list", timeout=timeout)
        pages = [t for t in targets if t.get("type") == "page"]
        if pages:
            return CDP(pages[0]["webSocketDebuggerUrl"], timeout=timeout)
        time.sleep(0.1)
    raise CDPError("chrome never exposed a page target on /json/list")
