"""Cached atopile source index for /api/source/index and /api/source/file.

The index is content addressed: the key hashes every .ato file, graph.json,
rules.json, the entry and this code. Inputs are re-stat'ed at most every 2 s;
a changed stat triggers a content hash, and only a changed hash rebuilds (or
loads <cache_dir>/source-index-<key>.json). Optional LLM net summaries are
attached only when net_llm.usable(): made for the current mechanical dossier (source_sha), the
current net_llm.PROMPT_VERSION and the configured model.
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import ato_index
import net_llm

CODE = ("ato_index.py", "source_service.py")
# hashed once, as imported: a running viewer must never file an index built by its (older) in-memory code under the key of newer files on disk
CODE_SHA = {n: hashlib.sha256((Path(__file__).parent / n).read_bytes()).digest() for n in CODE}
MAX_FILE = 2 << 20


class SourceNotFound(ValueError):
    pass


class SourceService:
    def __init__(
        self, src_dir, graph, rules=None, cache_dir=None, llm_cache=None, entry=None, llm_model=None
    ):
        self.src = Path(src_dir).resolve()
        self.graph = Path(graph)
        self.rules = Path(rules) if rules else self.graph.parent / "rules.json"
        self.cache = Path(cache_dir) if cache_dir else None
        self.llm_path = (
            Path(llm_cache)
            if llm_cache
            else (self.cache / "source-llm.json" if self.cache else None)
        )
        self.entry = tuple(entry.split(":", 1)) if isinstance(entry, str) else entry
        self.llm_model = llm_model  # None: any model
        self.code = Path(__file__).parent
        self.lock = threading.RLock()
        self._checked, self._stat, self._key = 0.0, None, None
        self._mech = self._view = self._bytes = self._dossier = None
        self._llm_stamp, self.error = None, None

    @classmethod
    def from_defaults(cls, runtime=None, graph=None, **kw):
        """Viewer defaults: SPLANC_ATO_SRC / SPLANC_GRAPH, else the frozen runtime's design (<runtime>/../splanc_dev/elec/src,
        default src11.frozen) and <hier>/inputs10b/graph.json."""
        hier = Path(__file__).resolve().parents[1]
        rt = Path(runtime) if runtime else hier / "src11.frozen/hardware/pnr"
        src = os.environ.get("SPLANC_ATO_SRC") or rt.parent / "splanc_dev/elec/src"
        return cls(
            src, graph or os.environ.get("SPLANC_GRAPH") or hier / "inputs10b/graph.json", **kw
        )

    def configured(self):
        return self.src.is_dir() and self.graph.is_file()

    def describe(self):
        return dict(
            src=str(self.src),
            graph=str(self.graph),
            rules=str(self.rules) if self.rules.is_file() else None,
            cache=str(self.cache) if self.cache else None,
            llm_cache=str(self.llm_path) if self.llm_path else None,
            configured=self.configured(),
            key=self._key,
            error=self.error,
        )

    # ------------------------------------------------------------ inputs
    def _inputs(self):
        out = (
            [("src/" + rel, p) for rel, p in ato_index.ato_files(self.src)]
            if self.src.is_dir()
            else []
        )
        return out + [("graph", self.graph), ("rules", self.rules)]

    @staticmethod
    def _st(p):
        try:
            s = os.stat(p)
            return s.st_size, s.st_mtime_ns
        except OSError:
            return None

    def _stats(self):
        return tuple((k, self._st(p)) for k, p in self._inputs()) + (
            ("llm", self._st(self.llm_path) if self.llm_path else None),
        )

    def _content_key(self):
        h = hashlib.sha256(json.dumps(self.entry).encode())
        for n in CODE:
            h.update(("code/" + n).encode() + b"\0" + CODE_SHA[n])
        for k, p in self._inputs():
            try:
                h.update(k.encode() + b"\0" + hashlib.sha256(Path(p).read_bytes()).digest())
            except OSError:
                h.update(k.encode() + b"\0-")
        return h.hexdigest()

    # ------------------------------------------------------------ index
    def _build(self, key):
        f = self.cache / f"source-index-{key[:32]}.json" if self.cache else None
        if f and f.is_file():
            try:
                ix = json.loads(f.read_text())
                if ix.get("sha") == key:
                    return ix
            except (OSError, ValueError):
                pass
        ix = ato_index.build_index(
            self.src, self.graph, self.rules if self.rules.is_file() else None, entry=self.entry
        )
        ix["sha"] = key
        ix["dossier_sha"] = ato_index.dossier(ix)[0]
        if f:
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(ix, separators=(",", ":"), ensure_ascii=False))
                os.replace(tmp, f)
            except OSError:
                pass
        return ix

    def index(self):
        with self.lock:
            now = time.monotonic()
            if self._view is not None and now - self._checked < 2:
                return self._view
            self._checked = now
            st = self._stats()
            if self._view is not None and st == self._stat:
                return self._view
            key = self._content_key()
            if key != self._key or self._mech is None:
                try:
                    mech = self._build(key)
                except Exception as e:  # a half-saved source edit: keep serving the last good index
                    if self._mech is None:
                        raise
                    self.error, self._stat = f"{type(e).__name__}: {e}", st
                    return self._view
                self._mech, self._key, self._dossier, self._llm_stamp, self.error = (
                    mech,
                    key,
                    None,
                    None,
                    None,
                )
                self._view, self._bytes = self._mech, None
            self._stat = st
            stamp = st[-1][1]
            if stamp != self._llm_stamp:
                self._llm_stamp = stamp
                self._attach(self._read_llm(self.llm_path) if stamp and self.llm_path else None)
            return self._view

    def index_bytes(self):
        with self.lock:
            ix = self.index()
            if self._bytes is None:
                self._bytes = json.dumps(ix, separators=(",", ":"), ensure_ascii=False).encode()
            return self._bytes

    def net(self, name):
        return self.index()["nets"].get(name)

    def component(self, ref):
        return self.index()["components"].get(ref)

    def at(self, file, line):
        """{refs, nets} whose source lines include file:line."""
        return self.index()["lines"].get(file, {}).get(str(line), dict(refs=[], nets=[]))

    # ------------------------------------------------------------ LLM summaries
    def dossier(self):
        """(sha, [per-net compact dossier]) - the only facts an LLM summary may be generated from."""
        with self.lock:
            self.index()
            if self._dossier is None:
                self._dossier = ato_index.dossier(self._mech)
            return self._dossier

    @staticmethod
    def _read_llm(path):
        try:
            data = json.loads(Path(path).read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _attach(self, data):
        """Copy-on-write view of the mechanical index with LLM summaries; the mechanical dicts are never mutated."""
        mech = self._mech
        self._bytes = None
        if not data:
            self._view = mech
            return 0
        meta = dict(
            model=data.get("model"),
            generated_at=data.get("generated_at"),
            dossier_sha=data.get("dossier_sha"),
            source_sha=data.get("source_sha"),
            prompt_version=data.get("prompt_version"),
        )
        if not net_llm.usable(data, mech["dossier_sha"], self.llm_model):
            self._view = dict(mech, llm=dict(meta, stale=True, nets=0))
            return 0
        nets = dict(mech["nets"])
        for name, v in (data.get("nets") or {}).items():
            if name in nets and isinstance(v, dict) and (v.get("label") or v.get("summary")):
                nets[name] = dict(
                    nets[name],
                    llm=dict(
                        label=str(v.get("label") or "")[:120],
                        summary=str(v.get("summary") or "")[:1200],
                        model=v.get("model") or meta["model"],
                        generated_at=v.get("generated_at") or meta["generated_at"],
                    ),
                )
        k = sum(1 for n in nets if nets[n] is not mech["nets"][n])
        self._view = dict(mech, nets=nets, llm=dict(meta, stale=False, nets=k))
        return k

    def merge_llm(self, path=None):
        """Attach summaries from an LLM cache JSON {source_sha, prompt_version, model, generated_at, nets:{name:{label, summary}}}.
        source_sha must equal dossier()[0], prompt_version net_llm.PROMPT_VERSION and model the configured llm_model.
        Returns the number of nets attached (0 when stale). The path is remembered and re-read when it changes.
        """
        with self.lock:
            if path:
                self.llm_path = Path(path)
            self._checked, self._stat, self._llm_stamp = 0.0, None, object()
            return (self.index().get("llm") or {}).get("nets", 0)

    # ------------------------------------------------------------ files
    def file(self, rel):
        """{path, text, sha, lines} for one .ato file under the source dir; ValueError for anything else."""
        if (
            not isinstance(rel, str)
            or not rel
            or len(rel) > 512
            or "\0" in rel
            or "\\" in rel
            or ":" in rel
            or rel.startswith(("/", "~"))
            or not rel.endswith(".ato")
        ):
            raise ValueError("path not allowed")
        parts = rel.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            raise ValueError("path not allowed")
        q = self.src
        for p in parts:
            q = q / p
            if q.is_symlink():
                raise ValueError("path not allowed")
        try:
            r = q.resolve(strict=True)
        except OSError:
            raise SourceNotFound("no such source file") from None
        if not r.is_relative_to(self.src) or not r.is_file():
            raise ValueError("path not allowed")
        raw = r.read_bytes()
        if len(raw) > MAX_FILE:
            raise ValueError("file too large")
        text = raw.decode("utf-8", "replace")
        return dict(
            path=rel, text=text, sha=hashlib.sha256(raw).hexdigest(), lines=len(text.splitlines())
        )
