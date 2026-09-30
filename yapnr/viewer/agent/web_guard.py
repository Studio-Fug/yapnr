"""WebFetch permission hook for the Ask agent (PreToolUse, `python web_guard.py`): stdlib only,
imports nothing from this folder.

It is the only thing that approves WebFetch. agent_service leaves WebFetch out of --allowedTools and
runs the CLI with
--permission-prompts none, so a fetch goes ahead only when this hook answers permissionDecision
  "allow" for a vetted public URL.
Everything else fails closed: a private/local/tailnet host gets "deny"; an exception while checking
gets "deny"; and a hook that cannot run at all (broken file, missing interpreter, timeout, non-zero
exit) gives the CLI no decision, which leaves the un-allowed WebFetch to --permission-prompts none:
denied. Only plain http(s) URLs of at most MAX_URL characters to public hosts pass: IP literals in
any notation must be global, local and tailnet suffixes and single-label names are refused, and
every address a name resolves to must be global."""

import ipaddress
import json
import re
import socket
import sys
import threading
from urllib.parse import urlsplit

MAX_URL = 2000
LOCAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".ts.net",
    ".internal",
    ".lan",
    ".home.arpa",
    ".arpa",
    ".intranet",
    ".corp",
    ".home",
    ".localdomain",
)
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ip(host):
    h = host.strip("[]").split("%")[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if re.fullmatch(
        r"(0x[0-9a-f]*|\d+)(\.(0x[0-9a-f]*|\d+)){0,3}", h
    ):  # legacy IPv4 forms (127.1, 0x7f.1, 2130706433) that URL parsers accept
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def public_ip(ip):
    if ip.version == 6:
        for emb in (ip.ipv4_mapped, ip.sixtofour, (ip.teredo or (None, None))[1]):
            if emb is not None:
                return public_ip(emb)
        if ip in NAT64:
            return public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if int(ip) >> 32 in (0, 0xFFFF0000):
            return False  # ::a.b.c.d (IPv4-compatible) and ::ffff:0:a.b.c.d (IPv4-translated) forms
    return ip.is_global and not ip.is_multicast


def resolve(host, port, timeout=4.0):
    box = {}

    def run():
        try:
            box["a"] = [x[4][0] for x in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]
        except (OSError, UnicodeError) as ex:
            box["e"] = ex

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("a")


def web_block_reason(url, resolver=resolve):
    """None when WebFetch may fetch url, else why not."""
    if not isinstance(url, str) or not url:
        return "invalid URL"
    if len(url) > MAX_URL:
        return f"URL longer than {MAX_URL} characters"
    if re.search(r"[\\\s\x00-\x1f\x7f]", url):
        return "URL contains backslashes, spaces or control characters"
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return "invalid URL"
    if u.scheme.lower() not in ("http", "https"):
        return "only http(s) URLs"
    if "@" in u.netloc:
        return "URLs with credentials are refused"
    host = (u.hostname or "").rstrip(".").lower()
    if not host:
        return "URL without a host"
    ip = _ip(host)
    if ip is None:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return f"{host} is not a valid host name"
        ip = _ip(host)
    if ip is not None:
        return None if public_ip(ip) else f"{host} is not a public address"
    if not re.fullmatch(r"[a-z0-9_-]+(\.[a-z0-9_-]+)+", host):
        return f"{host} is not a public host name"
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return f"{host} is a local, private or tailnet name"
    if resolver is None:
        return None
    addrs = resolver(host, port or (443 if u.scheme.lower() == "https" else 80))
    if not addrs:
        return f"{host} does not resolve"
    bad = [a for a in addrs if (lambda ip: ip is None or not public_ip(ip))(_ip(a))]
    return f"{host} resolves to a non-public address ({bad[0]})" if bad else None


def decision(why):
    d = dict(
        hookEventName="PreToolUse",
        permissionDecision="deny" if why else "allow",
        permissionDecisionReason=(
            f"Blocked by the yapnr viewer: {why}. Only public web pages can be fetched."
            if why
            else "Public host (checked by the yapnr viewer web guard)."
        ),
    )
    return dict(hookSpecificOutput=d)


def main(stdin=None, stdout=None, resolver=resolve):
    """Hook entry: reads the PreToolUse event, writes allow/deny for WebFetch (nothing for other
    tools), always exits 0."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        ev = json.loads(stdin.read() or "{}")
        if ev.get("tool_name") != "WebFetch":
            return 0
        why = web_block_reason((ev.get("tool_input") or {}).get("url"), resolver=resolver)
    except BaseException as ex:
        why = f"guard error ({type(ex).__name__})"
    stdout.write(json.dumps(decision(why)))
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
