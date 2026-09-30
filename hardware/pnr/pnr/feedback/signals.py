"""Read one evaluated round directory into placement feedback (JSON-safe).

A round directory is what :mod:`pnr.full_iteration` leaves behind: a block
instance dir (``<synth out>/<tid>/native/<tag>``) or a halving stage dir
(``cand/<id>/native``). Only these files are read:

* ``feedback.json``: ``targets`` (the connections still open after the final
  refill), ``native_opens`` and, with PNR_SHOVE=1, ``shove`` (its presence marks
  the router, its ``no_make_room`` events mark connections nudges could not
  open);
* ``evaluation.json``: the objective;
* ``evaluated-placed.json`` (else ``placed.json``): ref -> address, so
  connections are keyed by a caller key (block-local path for templates, ref at
  top level) and pool across layouts and instances;
* ``electrical/native-loop/progress.json`` and ``rules.json``: the native budget
  and fab profile, for the router key;
* ``electrical/coalesce.json``: the source tree that ran the evaluation, whose
  evaluation modules give the code part of the router key (:func:`observed_code`).

Per-part scores (``component_scores``, ``routing_failure_scores``) and static
blockers are copied only as diagnostics: the signal study found them dominated
by the block IC and moving their parts goes with more opens, so no move uses
them.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

POWER_MODES = ("power", "plane")


def enabled() -> bool:
    return os.environ.get("PNR_FEEDBACK") == "1"


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def split_endpoint(endpoint):
    """'REF.pad' -> (REF, pad); refs never contain '.', pad names may not either."""
    ref, _, pad = str(endpoint).partition(".")
    return ref, pad


def conn_id(a, b):
    """Canonical id of the connection between pads ``a`` and ``b`` ((key, pad) each).

    Endpoints only: a pad has one net, so the pad pair names the connection, and
    template instances (whose flat net names differ) share ids.
    """
    return "|".join(sorted("%s.%s" % tuple(e) for e in (a, b)))


def _ends(a, b):
    return sorted([list(a), list(b)], key=lambda e: "%s.%s" % tuple(e))


def read_round(round_dir, key=None):
    """Feedback record of one evaluated round, or ``{'missing': True, 'dir': ...}``.

    ``key(component dict) -> str`` names parts (default: the ref). Never raises
    on missing or unreadable files.
    """
    d = Path(round_dir)
    feedback = _load(d / "feedback.json")
    if not isinstance(feedback, dict) or not isinstance(feedback.get("targets"), list):
        return dict(missing=True, dir=str(d))
    comps = (_load(d / "evaluated-placed.json") or {}).get("components") or []
    pose_source = "evaluated" if comps else None
    if not comps:
        comps = (_load(d / "placed.json") or {}).get("components") or []
        pose_source = "placed" if comps else None
    keyf = key or (lambda c: c["ref"])
    key_of = {}
    for c in comps:
        try:
            key_of[c["ref"]] = keyf(c)
        except (KeyError, TypeError):
            continue

    def k(ref):
        return key_of.get(ref, ref)

    no_room = set()
    for e in (feedback.get("shove") or {}).get("events") or []:
        if e.get("status") == "no_make_room":
            t = e.get("target") or {}
            no_room.add((t.get("net"), frozenset((t.get("source"), t.get("target")))))
    conns, same = {}, []
    for t in feedback["targets"]:
        try:
            (ra, pa), (rb, pb) = split_endpoint(t["source"]), split_endpoint(t["target"])
        except (KeyError, TypeError):
            continue
        a, b = (k(ra), pa), (k(rb), pb)
        cid = conn_id(a, b)
        if ra == rb:
            same.append(cid)
            continue
        if cid in conns:
            continue
        ends = _ends(a, b)
        conns[cid] = dict(
            id=cid,
            net=t.get("net"),
            a=ends[0],
            b=ends[1],
            mode=t.get("mode"),
            distance=round(float(t.get("distance") or 0.0), 3),
            no_room=(t.get("net"), frozenset((t.get("source"), t.get("target")))) in no_room,
        )
    endpoints = {}
    for c in conns.values():
        for key_, _ in (c["a"], c["b"]):
            endpoints[key_] = endpoints.get(key_, 0) + 1
    evaluation = _load(d / "evaluation.json") or {}
    progress = _load(d / "electrical" / "native-loop" / "progress.json") or {}
    rules = _load(d / "rules.json") or {}
    scores = feedback.get("routing_failure_scores") or {}
    top = sorted(
        ((k(r), round(float(s), 3)) for r, s in scores.items() if isinstance(s, (int, float))),
        key=lambda x: (-x[1], x[0]),
    )[:5]
    return dict(
        dir=str(d),
        opens=feedback.get("native_opens"),
        objective=evaluation.get("objective"),
        router="shove" if "shove" in feedback else "plain",
        budget_seconds=(progress.get("budgets") or {}).get("seconds"),
        fab_profile=rules.get("fab_profile", "legacy") if rules else None,
        conns=[conns[c] for c in sorted(conns)],
        same_part=dict(count=len(same), ids=sorted(same)),
        endpoints={k_: endpoints[k_] for k_ in sorted(endpoints)},
        pose_source=pose_source,
        diag=dict(routing_failure_top=[list(x) for x in top]),
    )


def merge(fbs):
    """One layout's feedback over its template instances (a connection fails in
    the layout when it fails in any instance). Missing instance feedback makes the
    merged record missing."""
    fbs = list(fbs)
    if not fbs or any(f.get("missing") for f in fbs):
        return dict(missing=True, dirs=[f.get("dir") for f in fbs])
    if len(fbs) == 1:
        return fbs[0]
    conns = {}
    for f in fbs:
        for c in f["conns"]:
            if c["id"] not in conns:
                conns[c["id"]] = dict(c)
            else:
                conns[c["id"]]["no_room"] = conns[c["id"]]["no_room"] or c["no_room"]
    endpoints = {}
    for c in conns.values():
        for key_, _ in (c["a"], c["b"]):
            endpoints[key_] = endpoints.get(key_, 0) + 1
    routers = sorted({f.get("router") for f in fbs})
    opens = [f.get("opens") for f in fbs]
    return dict(
        dirs=[f.get("dir") for f in fbs],
        opens=sum(opens) if all(o is not None for o in opens) else None,
        router=routers[0] if len(routers) == 1 else "mixed",
        budget_seconds=fbs[0].get("budget_seconds"),
        fab_profile=fbs[0].get("fab_profile"),
        conns=[conns[c] for c in sorted(conns)],
        same_part=dict(
            count=sum(f["same_part"]["count"] for f in fbs),
            ids=sorted({i for f in fbs for i in f["same_part"]["ids"]}),
        ),
        endpoints={k_: endpoints[k_] for k_ in sorted(endpoints)},
        pose_source=fbs[0].get("pose_source"),
    )


# ------------------------------------------------------------------ router keys


def file_sha(path, n=8):
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:n]
    except OSError:
        return None


# ---- code identity of the evaluation (router) code
#
# Two runs with the same router name, budget and flags can still route
# differently when the evaluation code differs (e.g. src10.frozen's shove capped
# a moved power line at min(.5, .05 * len); src10b/src11 restore the 0.15 mm
# floor). The code key is a hash of every module an evaluation can execute: the
# static import closure of the evaluation entry points (function-local imports,
# ``-m pnr.x`` subprocess targets and package ``__main__`` included), stopping at
# driver-only modules that launch evaluations but never run inside one. The shove
# package is part of the key only for the shove router (every import of it
# outside the package is gated on PNR_SHOVE=1).
#
# Key schemes (a record's ``code_key_scheme``; a record without one is legacy):
#
# 1 (legacy): sha1 of each module's file bytes, keyed by its path relative to
#   ``hardware/pnr``. Any reformatting changes it.
# 2: sha1 of each module's canonical syntax tree (:func:`canonical_source`),
#   keyed by module name. Comments, layout, quoting, blank lines and import
#   order within a block of imports do not change it (black and isort output
#   keys like its input); any change to what the code does, docstrings
#   included, does.

PNR_ROOT = Path(__file__).resolve().parents[2]  # .../hardware/pnr of this tree
EVAL_ENTRIES = ("pnr.full_iteration", "pnr.hier.native_block", "pnr.hier.synth", "pnr.hier.blocks")
DRIVER_MODULES = ("pnr.feedback", "pnr.mc", "pnr.hier.synth_native", "pnr.hier.top")
_MODULE_RE = None

LEGACY_CODE_KEY_SCHEME = 1
CODE_KEY_SCHEME = 2  # the scheme this code stamps
CODE_KEY_SCHEMES = (LEGACY_CODE_KEY_SCHEME, CODE_KEY_SCHEME)


def code_key_scheme(record):
    """Key scheme of a record's (or an observed_code's) ``code``: its ``code_key_scheme``;
    a record without one was stamped before schemes existed (legacy)."""
    scheme = (record or {}).get("code_key_scheme")
    return LEGACY_CODE_KEY_SCHEME if scheme is None else scheme


def _router():
    return "shove" if os.environ.get("PNR_SHOVE") == "1" else "plain"


def _excluded(module, router):
    prefixes = DRIVER_MODULES + (("pnr.shove",) if router == "plain" else ())
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _parse(path):
    """Syntax tree of one source file (bytes, so its coding declaration applies), or None."""
    import ast

    try:
        return ast.parse(Path(path).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None


def eval_code_modules(pnr_root, router):
    """{module: source file} of the modules an evaluation under ``router`` can run."""
    import ast
    import re

    global _MODULE_RE
    _MODULE_RE = _MODULE_RE or re.compile(r"^pnr(\.[A-Za-z_]\w*)+$")
    root = Path(pnr_root)

    def path_of(module):
        p = root.joinpath(*module.split("."))
        if (p / "__init__.py").exists():
            return p / "__init__.py"
        return p.with_suffix(".py") if p.with_suffix(".py").exists() else None

    todo, seen = list(EVAL_ENTRIES), {}
    while todo:
        m = todo.pop()
        if m in seen or _excluded(m, router):
            continue
        f = path_of(m)
        if f is None:
            continue
        seen[m] = f
        parts = m.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]
        is_pkg = f.name == "__init__.py"
        if is_pkg:
            todo.append(m + ".__main__")  # ``python -m <package>``
        pkg = m if is_pkg else m.rpartition(".")[0]
        tree = _parse(f)
        if tree is None:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                todo += [a.name for a in n.names if a.name.startswith("pnr")]
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (n.level - 1)]
                    mod = ".".join(base + ([n.module] if n.module else []))
                else:
                    mod = n.module or ""
                if mod.startswith("pnr"):
                    todo.append(mod)
                    todo += [mod + "." + a.name for a in n.names]
            elif (
                isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and _MODULE_RE.match(n.value)
            ):
                todo.append(n.value)
    return {m: seen[m] for m in sorted(seen)}


def _import_run(stmts):
    """Canonical form of consecutive import statements (isort's unit of reordering).

    Each imported name becomes one entry; the run is sorted and deduplicated
    unless the order can matter: a star import, or one name bound to two
    different things (the later binding wins)."""
    import ast

    items, binds, ordered = [], {}, False
    for s in stmts:
        for a in s.names:
            if isinstance(s, ast.Import):
                item = ("import", a.name, a.asname or "")
                top = a.name.partition(".")[0]
                bind = (a.asname, a.name) if a.asname else (top, top)
            else:
                item = ("from", s.level or 0, s.module or "", a.name, a.asname or "")
                bind = (a.asname or a.name, item[1:4])
                ordered |= a.name == "*"
            ordered |= binds.setdefault(bind[0], bind[1]) != bind[1]
            items.append(item)
    return "Imports(%r)" % (items if ordered else sorted(set(items)),)


def canonical_source(tree):
    """Canonical text of a module's syntax tree for code key scheme 2.

    The tree has no comments or layout. On top of that: fields are named and
    empty ones left out (Python versions add fields, such as 3.12's
    ``type_params``), the ``u`` string prefix is dropped, standalone strings
    (docstrings) are compared stripped line by line and ``del (a, b)`` equals
    ``del a, b`` (black's own AST check allows exactly these three), and each
    block of consecutive imports is one sorted set of imported names (isort's
    reordering, splitting, merging and deduplication) unless its order can
    matter (:func:`_import_run`). Iterative: deep trees do not hit the
    recursion limit."""
    import ast

    out, todo = [], [(tree, False)]  # work items: literal text, or (value, standalone)
    while todo:
        item = todo.pop()
        if isinstance(item, str):
            out.append(item)
            continue
        node, standalone = item
        parts = []
        if isinstance(node, list):
            parts.append("[")
            i = 0
            while i < len(node):
                if isinstance(node[i], (ast.Import, ast.ImportFrom)):
                    j = i
                    while j < len(node) and isinstance(node[j], (ast.Import, ast.ImportFrom)):
                        j += 1
                    parts.append(_import_run(node[i:j]))
                    i = j
                else:
                    parts.append((node[i], False))
                    i += 1
                parts.append(",")
            parts.append("]")
        elif not isinstance(node, ast.AST):
            if standalone and isinstance(node, str):
                # black re-indents docstrings (any standalone string): compare them
                # as black's AST check does, each line stripped, blank ends dropped
                node = "\n".join(line.strip() for line in node.splitlines()).strip()
            out.append("%s:%r" % (type(node).__name__, node))
            continue
        else:
            parts.append(type(node).__name__ + "(")
            for field in sorted(node._fields):
                if isinstance(node, ast.Constant) and field == "kind":
                    continue  # the u'' prefix: black drops it, Python 3 ignores it
                value = getattr(node, field, None)
                if value is None or (isinstance(value, list) and not value):
                    continue  # absent, or a field one Python version has and another lacks
                if isinstance(node, ast.Delete) and field == "targets":
                    # ``del (a, b)`` is ``del a, b`` (black drops the parentheses)
                    value = [
                        e for t in value for e in (t.elts if isinstance(t, ast.Tuple) else [t])
                    ]
                # standalone: a Constant that is a whole expression statement, then its value
                flag = (
                    isinstance(node, ast.Expr)
                    and isinstance(value, ast.Constant)
                    or standalone
                    and field == "value"
                )
                parts += [field + "=", (value, flag), ","]
            parts.append(")")
        todo.extend(reversed(parts))
    return "".join(out)


def code_files(modules, pnr_root, scheme=CODE_KEY_SCHEME):
    """{key: digest} of ``modules`` ({module: file}) under code key ``scheme``.

    Scheme 1 hashes file bytes by path relative to ``pnr_root``, scheme 2
    :func:`canonical_source` by module name. A file that does not parse is
    hashed by its bytes (``bytes:`` prefix); an unreadable one is left out."""
    if scheme not in CODE_KEY_SCHEMES:
        raise ValueError("unknown code key scheme %r" % (scheme,))
    root, out = Path(pnr_root), {}
    for m, f in modules.items():
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if scheme == LEGACY_CODE_KEY_SCHEME:
            out[str(f.relative_to(root))] = hashlib.sha1(data).hexdigest()
            continue
        tree = _parse(f)
        if tree is None:
            out[m] = "bytes:" + hashlib.sha1(data).hexdigest()
        else:
            out[m] = hashlib.sha1(
                canonical_source(tree).encode("utf-8", "backslashreplace")
            ).hexdigest()
    return {k: out[k] for k in sorted(out)}


def eval_code_files(pnr_root, router, scheme=CODE_KEY_SCHEME):
    """{module (scheme 2) or relative path (scheme 1): digest} of the modules an
    evaluation under ``router`` can run."""
    return code_files(eval_code_modules(pnr_root, router), pnr_root, scheme)


def code_sha(files, scheme=CODE_KEY_SCHEME):
    """Code key of an eval_code_files map of ``scheme``."""
    if scheme == LEGACY_CODE_KEY_SCHEME:
        return hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()[:10]
    return hashlib.sha1(json.dumps([scheme, sorted(files.items())]).encode()).hexdigest()[:10]


def code_key(pnr_root=None, router=None, scheme=CODE_KEY_SCHEME):
    """Code key of the evaluation code of a tree (default: this tree, this process's router)."""
    return code_sha(eval_code_files(pnr_root or PNR_ROOT, router or _router(), scheme), scheme)


def code_stamp(pnr_root=None, router=None):
    """The fields a record stamps for the code that evaluates it: ``code`` and its
    ``code_key_scheme``. Stamp both, always together."""
    return dict(code=code_key(pnr_root, router), code_key_scheme=CODE_KEY_SCHEME)


class TreeCode:
    """The evaluation code of one tree under one router, keyed in any scheme on
    first use (:func:`check_code` compares an import in the import's scheme)."""

    def __init__(self, pnr_root=None, router=None):
        self.root = Path(pnr_root or PNR_ROOT)
        self.router = router or _router()
        self._modules = None
        self._files = {}

    def modules(self):
        if self._modules is None:
            self._modules = eval_code_modules(self.root, self.router)
        return self._modules

    def files(self, scheme=CODE_KEY_SCHEME):
        if scheme not in self._files:
            self._files[scheme] = code_files(self.modules(), self.root, scheme)
        return self._files[scheme]

    def key(self, scheme=CODE_KEY_SCHEME):
        return code_sha(self.files(scheme), scheme)


def _tree_code(root, router, cache):
    if cache is None:
        return TreeCode(root, router)
    if (str(root), router) not in cache:
        cache[str(root), router] = TreeCode(root, router)
    return cache[str(root), router]


def evaluation_tree(round_dir):
    """Source tree (the dir holding hardware/) an evaluated round was produced by, or None.

    Both drivers pass ``--annotation-source <tree>/hardware/splanc_dev/elec/src/...``
    (the tree of the running code, not of the inputs); native_loop snapshots it
    with its origin (``source-inputs/origins.json``), the coalesce phase records
    it for templates with protected intents. ``fp-lib-table`` and ``rules.json``
    are not used: they come from the inputs directory."""
    import re

    pattern = re.compile(r"^(/.*?)/hardware/splanc_dev/elec/src/[^/]*\.ato$")
    d = Path(round_dir) / "electrical"
    origins = _load(d / "native-loop" / "source-inputs" / "origins.json")
    if isinstance(origins, list):
        trees = {
            m.group(1)
            for o in origins
            if isinstance(o, dict)
            for m in [pattern.match(str(o.get("original") or ""))]
            if m
        }
        if len(trees) == 1:
            return Path(trees.pop())
    try:
        text = (d / "coalesce.json").read_text()
    except OSError:
        return None
    trees = set(re.findall(r'"(/[^"]*?)/hardware/splanc_dev/elec/src/[^"/]*\.ato"', text))
    return Path(trees.pop()) if len(trees) == 1 else None


def _started(round_dir):
    d = Path(round_dir)
    try:
        st = d.stat()
    except OSError:
        return None
    start = getattr(st, "st_birthtime", None)
    if start:
        return start
    try:
        return (d / "evaluation.json").stat().st_mtime
    except OSError:
        return None


def observed_code(round_dir, router, stamped=None, cache=None, scheme=None):
    """dict(code, code_key_scheme, tree, files, reason) of the code that evaluated ``round_dir``.

    A code key stamped into the record at evaluation time wins; ``scheme`` is the
    record's ``code_key_scheme`` (None: a legacy record). A stamp of an older
    scheme is re-keyed in the current one when the round's evaluation tree still
    hashes to it in its own scheme (that tree holds the code that ran, so a
    reformat of this run's tree since does not refuse the import); otherwise it
    keeps its scheme, and :func:`check_code` compares it in that scheme.

    Without a stamp the tree is read from the round (:func:`evaluation_tree`)
    and hashed now; if any of its evaluation modules is newer than the round's
    start the tree changed after the evaluation and the code is unknown
    (``code`` None). ``cache`` (a dict) reuses one tree's module hashes across
    rounds of one import."""
    if stamped:
        scheme = LEGACY_CODE_KEY_SCHEME if scheme is None else scheme
        if scheme != CODE_KEY_SCHEME and scheme in CODE_KEY_SCHEMES and round_dir is not None:
            tree = evaluation_tree(round_dir)
            root = tree / "hardware" / "pnr" if tree is not None else None
            if root is not None and (root / "pnr").is_dir():
                code = _tree_code(root, router, cache)
                if code.key(scheme) == stamped:
                    return dict(
                        code=code.key(),
                        code_key_scheme=CODE_KEY_SCHEME,
                        tree=str(tree),
                        files=code.files(),
                        reason="stamped (key scheme %s, re-keyed from its tree)" % scheme,
                    )
        return dict(code=stamped, code_key_scheme=scheme, tree=None, files=None, reason="stamped")
    unknown = dict(code=None, code_key_scheme=CODE_KEY_SCHEME, tree=None, files=None)
    tree = evaluation_tree(round_dir)
    if tree is None:
        return dict(unknown, reason="evaluation tree not recorded")
    root = tree / "hardware" / "pnr"
    if not (root / "pnr").is_dir():
        return dict(unknown, tree=str(tree), reason="evaluation tree %s is gone" % tree)
    code = _tree_code(root, router, cache)
    start = _started(round_dir)
    newer = []
    if start is not None:
        for f in code.modules().values():
            try:
                if f.stat().st_mtime > start + 1.0:
                    newer.append(str(f.relative_to(root)))
            except OSError:
                newer.append(str(f.relative_to(root)))
    if newer:
        return dict(
            unknown,
            tree=str(tree),
            files=code.files(),
            reason="%s changed after the evaluation (%s)" % (tree.name, ", ".join(newer[:4])),
        )
    return dict(
        code=code.key(),
        code_key_scheme=CODE_KEY_SCHEME,
        tree=str(tree),
        files=code.files(),
        reason="tree",
    )


def code_diff(files_a, files_b):
    """Modules whose content differs between two eval_code_files maps (of one scheme)."""
    if not files_a or not files_b:
        return []
    return sorted(k for k in set(files_a) | set(files_b) if files_a.get(k) != files_b.get(k))


def check_code(observed, current_code, current_files=None, policy="error"):
    """(errors, warnings, stale) for an import's code identity against this run's.

    ``current_code`` is this run's :class:`TreeCode`, keyed in the scheme of the
    observed code (:func:`code_key_scheme`; a legacy record is compared under
    the legacy scheme), or a key string with ``current_files`` its
    eval_code_files map, both of the observed code's scheme.

    ``policy`` 'error': a different or unknown code refuses the import; 'warn':
    imported as is, with a warning naming the differing modules; 'rebase': the
    import is kept only as a layout source and is re-evaluated under this code
    before any feedback generation (``stale`` True)."""
    code, scheme = observed.get("code"), code_key_scheme(observed)
    if code is None:
        why = "code unknown (%s)" % observed.get("reason")
    else:
        known = scheme in CODE_KEY_SCHEMES  # a key of an unknown scheme never matches
        if isinstance(current_code, TreeCode):
            current_files = current_code.files(scheme) if known else None
            current_code = current_code.key(scheme) if known else current_code.key()
        if known and code == current_code:
            return [], [], False
        diff = code_diff(observed.get("files"), current_files)
        why = "code %s != this run %s%s%s" % (
            observed["code"],
            current_code,
            (
                ""
                if scheme == CODE_KEY_SCHEME
                else " (code key scheme %s%s)"
                % (scheme, "" if scheme in CODE_KEY_SCHEMES else ", unknown to this code")
            ),
            (" (differs: %s)" % ", ".join(diff[:6])) if diff else "",
        )
    if policy == "error":
        return [why], [], False
    if policy == "warn":
        return [], [why], False
    return [], [why + ": re-evaluated under this code (rebase)"], True


def current_key(stage, budget_seconds, inputs=None):
    """Router key of evaluations this process will run (environment + CLI + evaluation code)."""
    router = _router()
    return dict(
        router=router,
        stage=stage,
        budget_seconds=float(budget_seconds) if budget_seconds is not None else None,
        power_first=os.environ.get("PNR_POWER_FIRST") == "1",
        fanout_reserve=os.environ.get("PNR_FANOUT_RESERVE") == "1",
        fab_profile=os.environ.get("PNR_FAB_PROFILE") or "jlc-pofv",
        inputs=file_sha(Path(inputs) / "source.kicad_pcb") if inputs else None,
        code=code_key(PNR_ROOT, router),
        code_key_scheme=CODE_KEY_SCHEME,
    )


def key_string(key):
    return "|".join(
        str(key.get(f))
        for f in (
            "router",
            "stage",
            "budget_seconds",
            "power_first",
            "fanout_reserve",
            "fab_profile",
            "inputs",
            "code",
        )
    )


def observed_key(fb, *, stage=None, power_first=None):
    """What a record's feedback says about the evaluation that produced it."""
    return dict(
        router=fb.get("router"),
        stage=stage,
        budget_seconds=(
            float(fb["budget_seconds"]) if fb.get("budget_seconds") is not None else None
        ),
        power_first=power_first,
        fab_profile=fb.get("fab_profile"),
    )


def check_import(observed, current, budget_policy="error"):
    """(errors, warnings) comparing an imported evaluation's key with this run's.

    Router, power-first and fab profile must match (failure statistics and flags
    depend on the router). A different native budget is an error unless
    ``budget_policy`` is 'warn'. Fields the import cannot show are warnings."""
    errors, warnings = [], []
    for field in ("router", "power_first", "fab_profile", "stage"):
        o, c = observed.get(field), current.get(field)
        if o is None or c is None:
            if field != "stage":
                warnings.append("%s unknown for the import" % field)
        elif o != c:
            errors.append("%s %r != this run %r" % (field, o, c))
    o, c = observed.get("budget_seconds"), current.get("budget_seconds")
    if o is None:
        warnings.append("native budget unknown for the import")
    elif c is not None and abs(o - c) > 1e-6:
        (errors if budget_policy == "error" else warnings).append(
            "budget %gs != this run %gs" % (o, c)
        )
    return errors, warnings
