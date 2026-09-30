"""LLM net labels: one tool-less `claude -p` call (per ~60k-char chunk) over
SourceService.dossier().

Output {schema, dossier_sha, source_sha, prompt_version, model, generated_at,
nets:{name:{label,summary}}, missing, dropped, cost_usd} is written atomically and regenerated only
when the dossier, PROMPT_VERSION or model changes; nets left missing by a failed chunk are retried
alone, with backoff (usable()/settled() are the cache checks the viewer server and SourceService
share). Entries are grounded in the dossier: labels <=6 words, summaries <=2 sentences, and an entry
naming a component ref absent from the dossier is dropped. Usage: python -m
yapnr.viewer.agent.net_llm OUT.json [--index FILE|URL] [--model sonnet] [--budget-usd 1] [--force]
[--dry-run] Each call is paid (on the operator's Claude account): the viewer runs it only with
--net-summaries on. Every call has a budget (--max-budget-usd, the viewer's --net-summary-budget-usd);
in the viewer the calls also count against the server's spend cap (spend.SpendMeter, shared with the
Ask agent), and a run the cap stops leaves the remaining nets missing.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

from yapnr.viewer.agent.service import child_env, clip

MODELS = ("sonnet", "opus", "haiku")
CHUNK = 60000
PROMPT_VERSION = 3  # 3: the prompt no longer names a particular board
RETRIES = 4
BACKOFF = 600
BUDGET = 1.0  # USD per call (--max-budget-usd)
CLAUDE = "claude"
REF = re.compile(r"\b([A-Z]{1,3})(\d{1,3})\b")
PREFIXES = {
    "C",
    "R",
    "L",
    "U",
    "Q",
    "D",
    "J",
    "SW",
    "USB",
    "TP",
    "F",
    "FB",
    "Y",
    "X",
    "LED",
    "BT",
    "K",
    "P",
    "IC",
    "CN",
    "T",
    "M",
    "MH",
}
SYSTEM = (
    "You label nets of a PCB netlist for an engineering viewer. You are given a mechanical "
    "dossier derived from the design source and netlist. Reply with one JSON object only."
)
ASK = (
    "Write a label and a summary for EVERY net in the dossier below (a PCB described "
    "in atopile).\n"
    "- label: at most 6 words, human meaningful (role plus voltage where stated), "
    'e.g. "5V LED supply rail", "USB-C CC1 line", "Buck switch node".\n'
    "- summary: at most 2 sentences: what the net carries and which modules/ICs/pins "
    "it joins.\n"
    "- Use only facts stated in the dossier. Never invent component references, "
    "values, voltages or currents; mention a component ref only if it appears in the "
    "dossier.\n"
    "- Do not mention the dossier itself, its truncation or pin counts.\n"
    '- Keys must be the exact net names given after "### net:" (case and punctuation '
    "preserved).\n"
    "Return ONLY this JSON, no prose and no code fences:\n"
    '{"nets":{"<net name>":{"label":"...","summary":"..."}}}\n'
    "Nets in this batch (%d): %s\n"
    "\n"
    "<dossier>\n"
    "%s\n"
    "</dossier>"
)


class IndexSource:
    """SourceService stand-in over a saved /api/source/index JSON (file path or URL)."""

    def __init__(self, where):
        self.where = where
        self._i = None

    def index(self):
        if self._i is None:
            w = str(self.where)
            if w.startswith(("http://", "https://")):
                with urlopen(w, timeout=60) as r:
                    self._i = json.load(r)
            else:
                self._i = json.loads(Path(w).read_text())
        return self._i


def net_text(name, n, comps):
    """Compact per-net dossier from an index net entry or a SourceService.dossier() entry (items may
    be dicts or strings)."""

    def s(x, keys):
        if isinstance(x, str):
            return x
        return " ".join(
            str(x[k]) for k in keys if isinstance(x, dict) and x.get(k) not in (None, "")
        )

    out = [
        f"### net: {name}",
        f"title: {n.get('title') or name} | kind: {n.get('kind') or '?'}"
        + (f" | voltage: {n['voltage']}" if n.get("voltage") else "")
        + (f" | class: {n['net_class']}" if n.get("net_class") else ""),
    ]
    if n.get("summary"):
        out.append("source: " + clip(n["summary"], 400))
    al = [
        a if isinstance(a, str) else a.get("path") or a.get("rel")
        for a in (n.get("aliases") or [])
        if isinstance(a, (str, dict))
    ]
    al = [a for a in al if a]
    if al:
        out.append("aliases: " + ", ".join(al[:10]) + (f" (+{len(al)-10})" if len(al) > 10 else ""))
    if n.get("comments"):
        out.append("comments: " + " | ".join(clip(c, 140) for c in n["comments"][:4]))
    for c in (n.get("currents") or [])[:3]:
        out.append(
            "current: "
            + (
                c
                if isinstance(c, str)
                else (
                    f"{c.get('rms_current_a')} A rms / {c.get('peak_current_a')} A peak ({c.get('scope')}, "
                    f"target {c.get('target')})"
                )
            )
        )
    st = [s(x, ("file", "line", "text")) for x in (n.get("statements") or [])]
    if st:
        out.append("statements: " + "; ".join(clip(x, 120) for x in st[:6]))
    pins = n.get("pins") or []
    more = (n.get("more_pins") or 0) + max(0, len(pins) - 16)

    def pin(p):
        if isinstance(p, str):
            return p
        c = comps.get(p.get("ref")) or {}
        return (
            f"{p.get('ref')}.{p.get('pad')}"
            + (f" {p['pin']}" if p.get("pin") else "")
            + (
                f" [{c.get('type')} {c.get('instance') or p.get('address') or ''}]".replace(
                    " ]", "]"
                )
                if c or p.get("address")
                else ""
            )
        )

    if pins:
        out.append(
            f'pins ({len(pins)+(n.get("more_pins") or 0)}): '
            + "; ".join(pin(p) for p in pins[:16])
            + (f"; +{more} more" if more else "")
        )
    return "\n".join(out)


def collect(svc):
    """(source_sha, [(net, text)], known_refs) from svc.dossier() when present, else from
    svc.index()."""
    idx = None
    if hasattr(svc, "index"):
        try:
            idx = svc.index()
        except Exception:
            idx = None
    comps = (idx or {}).get("components") or {}
    refs = set(comps)
    d = svc.dossier() if hasattr(svc, "dossier") else None
    if d is None:
        if not idx:
            raise ValueError("source service offers neither dossier() nor index()")
        d = dict(sha=idx.get("sha"), nets=idx.get("nets") or {})
    if isinstance(d, tuple) and len(d) == 2:
        d = dict(sha=d[0], nets=d[1])  # ato_index.dossier(): (sha, [net dicts])
    nets = d.get("nets") if isinstance(d, dict) else d
    if isinstance(nets, list):
        nets = {(x.get("name") if isinstance(x, dict) else None): x for x in nets}
    if not isinstance(nets, dict) or not nets:
        raise ValueError("dossier has no nets")
    entries = []
    for name in sorted(k for k in nets if isinstance(k, str) and k):
        v = nets[name]
        if isinstance(v, str):
            t = v if v.lstrip().startswith("### net:") else f"### net: {name}\n{v}"
        elif isinstance(v, dict) and ("pins" in v or "title" in v):
            t = net_text(name, v, comps)
        else:
            t = f"### net: {name}\n" + json.dumps(
                v, separators=(",", ":"), sort_keys=True, default=str
            )
        entries.append((name, t))
    return (d.get("sha") if isinstance(d, dict) else None) or (idx or {}).get("sha"), entries, refs


def chunks(entries, limit=CHUNK):
    out, cur, size = [], [], 0
    for e in entries:
        if cur and size + len(e[1]) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += len(e[1]) + 2
    return out + ([cur] if cur else [])


def extract_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t)
    except ValueError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in model output")
    return json.loads(t[a : b + 1])


def tidy(s):
    """Strip markdown markup only; identifier underscores (EN_UVLO, C_CC1, sink_enabled_n) are
    kept."""
    s = re.sub(r"[*`#]|\[\[|\]\]", "", s)
    for m in ("__", "_"):
        s = re.sub(
            r"(?<!\w)" + m + r"(\S(?:.*?\S)?)" + m + r"(?!\w)", r"\1", s
        )  # paired emphasis at word boundaries only
    return re.sub(r"\s+", " ", s).strip()


def usable(doc, mech_sha, model=None):
    """Labels in doc were made for this mechanical dossier (source_sha), this prompt and (when
    given) this model."""
    return (
        isinstance(doc, dict)
        and bool(mech_sha)
        and doc.get("source_sha") == mech_sha
        and doc.get("prompt_version") == PROMPT_VERSION
        and (model is None or doc.get("model") == model)
    )


def due(doc, now=None):
    """A doc with missing nets gets a retry of those nets only, at most RETRIES attempts,
    BACKOFF*2^n apart."""
    n = int(doc.get("attempts") or 1)
    return (
        bool(doc.get("missing"))
        and n < RETRIES
        and (now or time.time()) - float(doc.get("attempted_ts") or 0) >= BACKOFF * 2 ** (n - 1)
    )


def settled(doc, mech_sha, model):
    """Nothing to generate automatically: usable, and complete or not yet due for a retry of its
    missing nets."""
    return usable(doc, mech_sha, model) and not due(doc)


def validate(raw, names, allowed, prefixes):
    """-> (nets, dropped, missing). allowed: lower-cased tokens present anywhere in the dossier."""
    got = (raw or {}).get("nets") if isinstance(raw, dict) else None
    got = got if isinstance(got, dict) else {}
    nets, dropped = {}, {}
    for name in names:
        v = got.get(name)
        if v is None:
            continue
        if not (
            isinstance(v, dict)
            and isinstance(v.get("label"), str)
            and isinstance(v.get("summary"), str)
        ):
            dropped[name] = dict(reason="malformed")
            continue
        label = " ".join(tidy(v["label"]).split()[:6])
        label = clip(label, 60)
        summ = " ".join(re.split(r"(?<=[.!?])\s+", tidy(v["summary"]))[:2])
        summ = clip(summ, 320)
        if not label or not summ:
            dropped[name] = dict(reason="empty")
            continue
        bad = sorted(
            {
                m.group(0)
                for m in REF.finditer(label + " " + summ)
                if m.group(1) in prefixes and m.group(0).lower() not in allowed
            }
        )
        if bad:
            dropped[name] = dict(
                reason="refs not in dossier: " + ", ".join(bad), label=label, summary=summ
            )
            continue
        nets[name] = dict(label=label, summary=summ)
    return nets, dropped, [n for n in names if n not in nets and n not in dropped]


class CallError(RuntimeError):
    """A CLI call that failed after it started; cost is what the CLI reported (None: unknown)."""

    def __init__(self, message, cost=None):
        super().__init__(message)
        self.cost = cost


def call(prompt, model, claude_bin=CLAUDE, timeout=600, budget=BUDGET, cache=None):
    """One tool-less CLI call -> (result text, cost_usd). Raises CallError (with the reported
    cost, if any) or TimeoutError."""
    if model not in MODELS:
        raise ValueError("model must be one of " + ", ".join(MODELS))
    mcp = Path(cache or Path(__file__).parent / ".cache/agent")
    mcp.mkdir(parents=True, exist_ok=True)
    mcp = mcp / "mcp-empty.json"
    if not mcp.is_file():
        mcp.write_text('{"mcpServers":{}}')
    cmd = [
        str(claude_bin),
        "-p",
        prompt,
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp),
        "--setting-sources",
        "",
        "--restricted",
        "--permission-prompts",
        "none",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--model",
        model,
        "--effort",
        "low",
        "--max-budget-usd",
        f"{budget:g}",
        "--system-prompt",
        SYSTEM,
    ]
    p = subprocess.Popen(
        cmd,
        cwd=str(mcp.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env(),
        start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
            except OSError:
                pass
            time.sleep(0.5)
        p.communicate()
        raise TimeoutError(f"claude call exceeded {timeout}s")
    try:
        r = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise CallError(f"claude exited {p.returncode}: " + clip((err or out).strip(), 400))
    if not isinstance(r, dict):
        raise CallError(f"claude exited {p.returncode}: unexpected output")
    if r.get("is_error") or r.get("subtype") != "success":
        raise CallError(
            "claude error: " + clip(r.get("result") or r.get("subtype"), 400),
            cost=r.get("total_cost_usd"),
        )
    return r.get("result") or "", r.get("total_cost_usd")


def write_atomic(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def generate(
    source_service,
    out_path,
    model="sonnet",
    claude_bin=CLAUDE,
    force=False,
    chunk=CHUNK,
    timeout=600,
    runner=None,
    log=print,
    cache=None,
    budget=BUDGET,
    meter=None,
):
    """Label every net (or only the nets a previous run left missing, when due); returns the written
    or current document. budget: USD per call; meter: a spend.SpendMeter each call reserves its
    budget from and is charged to (the reported cost, else the whole budget). When the meter's cap
    cannot cover the next call, the remaining nets stay missing and the document says capped."""
    if model not in MODELS:
        raise ValueError("model must be one of " + ", ".join(MODELS))
    src_sha, entries, refs = collect(source_service)
    names = [n for n, _ in entries]
    sha = hashlib.sha256(
        json.dumps([PROMPT_VERSION, entries], separators=(",", ":")).encode()
    ).hexdigest()
    try:
        old = json.loads(Path(out_path).read_text())
    except (OSError, ValueError):
        old = None
    same = (
        not force
        and isinstance(old, dict)
        and old.get("dossier_sha") == sha
        and old.get("model") == model
        and old.get("prompt_version") == PROMPT_VERSION
    )
    if same and not due(old):
        log(
            (
                f'net_llm: {out_path} current ({len(old.get("nets") or {})} nets, {len(old.get("missing") or [])} '
                f"missing, dossier {sha[:12]})"
            )
        )
        return old
    retry = set(old["missing"]) if same else None
    if retry:
        entries = [e for e in entries if e[0] in retry]
        log(
            f'net_llm: retrying {len(entries)} missing net(s), attempt {int(old.get("attempts") or 1)+1}'
        )
    text = "\n\n".join(t for _, t in entries)
    allowed = {t.lower() for t in re.findall(r"[A-Za-z0-9_]+", text)}
    prefixes = {
        m.group(1) for r in refs if (m := REF.fullmatch(r))
    } or PREFIXES  # real designator prefixes when the index is known
    runner = runner or (
        lambda prompt: call(prompt, model, claude_bin, timeout, budget=budget, cache=cache)
    )
    nets, dropped, missing, cost, t0 = ({}, {}, [], 0.0, time.time())
    parts = chunks(entries, chunk)
    capped = False
    for i, part in enumerate(parts):
        if meter is not None and not meter.reserve(budget):
            capped = True
            left = [n for p in parts[i:] for n, _ in p]
            log(
                f"net_llm: the server's spend cap cannot cover another call (${budget:g});"
                f" {len(left)} net(s) left unlabelled"
            )
            missing += left
            break
        prompt = ASK % (len(part), ", ".join(n for n, _ in part), "\n\n".join(t for _, t in part))
        c = None  # the reported cost; None: unknown (the meter then charges the whole budget)
        try:
            out, c = runner(prompt)
            raw = extract_json(out)
        except (ValueError, RuntimeError, TimeoutError, OSError) as ex:
            if c is None:
                c = getattr(ex, "cost", None)
                if c is None and isinstance(ex, OSError) and not isinstance(ex, TimeoutError):
                    c = 0.0  # the CLI did not start
            log(f"net_llm: chunk {i+1}/{len(parts)} failed: {ex}")
            missing += [n for n, _ in part]
            continue
        finally:
            if meter is not None:
                meter.settle(budget, c)
            try:
                cost += float(c or 0)
            except (TypeError, ValueError):
                pass
        n, d, m = validate(raw, [n for n, _ in part], allowed, prefixes)
        nets.update(n)
        dropped.update(d)
        missing += m
    attempts = 1
    if retry:
        nets, dropped, attempts, cost = (
            {**old.get("nets", {}), **nets},
            {**old.get("dropped", {}), **dropped},
            int(old.get("attempts") or 1) + 1,
            cost + float(old.get("cost_usd") or 0),
        )
    doc = dict(
        schema="yapnr-net-llm-v1",
        dossier_sha=sha,
        source_sha=src_sha,
        prompt_version=PROMPT_VERSION,
        model=model,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        nets=nets,
        missing=missing,
        dropped=dropped,
        calls=len(parts),
        capped=capped,
        cost_usd=round(cost, 6),
        duration_s=round(time.time() - t0, 1),
        attempts=attempts,
        attempted_ts=round(time.time(), 1),
    )
    write_atomic(out_path, doc)
    log(
        (
            f"net_llm: {len(nets)}/{len(names)} nets labelled, {len(dropped)} dropped, {len(missing)} "
            f"missing, {len(parts)} call(s), ${cost:.4f} -> {out_path}"
        )
    )
    return doc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("out", type=Path)
    ap.add_argument(
        "--index",
        required=True,
        help="a saved /api/source/index JSON, or its URL on a running viewer",
    )
    ap.add_argument("--model", default="sonnet", choices=MODELS)
    ap.add_argument("--claude", default=CLAUDE)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--budget-usd", type=float, default=BUDGET, help="spend cap per CLI call")
    ap.add_argument(
        "--dry-run", action="store_true", help="print dossier size and chunking; no model call"
    )
    a = ap.parse_args()
    svc = IndexSource(a.index)
    if a.dry_run:
        sha, entries, refs = collect(svc)
        print(
            json.dumps(
                dict(
                    source_sha=sha,
                    nets=len(entries),
                    chars=sum(len(t) for _, t in entries),
                    chunks=[len(c) for c in chunks(entries)],
                    refs=len(refs),
                ),
                indent=1,
            )
        )
        sys.exit(0)
    generate(svc, a.out, a.model, a.claude, a.force, timeout=a.timeout, budget=a.budget_usd)
