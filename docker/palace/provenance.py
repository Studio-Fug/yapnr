#!/usr/bin/env python3
"""Build-time proof that the Palace task image has no ParMETIS in it (docker/palace/Dockerfile).

    provenance.py deps --tripwire URL [--report FILE]
    provenance.py binary --map PALACE.map --binary PALACE.bin [--report FILE]

``deps`` (after the dependencies are built) checks the superbuild: no ParMETIS project or source
directory, the ParMETIS download pointed at a URL that cannot work (the tripwire), Scotch at its
pinned commit; that the only ``*parmetis*`` files installed are Scotch's ``parmetis.h`` and
``libptscotchparmetisv3.a``; that only that library defines ParMETIS names and only METIS defines
``METIS_NodeND``; and that SuperLU_DIST still calls the ParMETIS API (its parallel ordering) while
MUMPS calls PT-Scotch and not ParMETIS.

``binary`` (after Palace links, before ``strip``) reads the linker's map of the Palace executable:
every archive it loads is one of the known dependency libraries or a system library, and every
archive member it pulls in that defines a ParMETIS name comes from libptscotchparmetisv3.a; the
unstripped executable defines ``ParMETIS_V3_NodeND`` (from Scotch) and no ``ParMETIS_V32_NodeND``.
Names are matched without regard to case, so Fortran-mangled names count too. Either step exits
1, saying why, when a check fails.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

PREFIX = Path("/opt/palace")
BUILD = Path("/src/build")
COMPAT = "libptscotchparmetisv3.a"
# The consumers' own functions named after the call they wrap (their code, not ParMETIS's):
# SuperLU_DIST's get_perm_c_parmetis builds the graph and calls ParMETIS_V3_NodeND
CONSUMER_NAMES = {"get_perm_c_parmetis": "libsuperlu_dist.a"}
PARMETIS = re.compile("parmetis", re.IGNORECASE)
# The static libraries Palace may link from /opt/palace (the superbuild's dependencies)
ALLOWED = {
    "libHYPRE.a",
    "libarpack.a",
    "libparpack.a",
    "libdmumps.a",
    "libmumps_common.a",
    "libpord.a",
    "libfmt.a",
    "libgs.a",
    "libmetis.a",
    "libmfem.a",
    "libnlohmann_json_schema_validator.a",
    "libscalapack.a",
    "libscn.a",
    "libsuperlu_dist.a",
    COMPAT,
    "libptesmumps.a",
    "libptscotch.a",
    "libptscotcherr.a",
    "libesmumps.a",
    "libscotch.a",
    "libscotcherr.a",
}
NM = re.compile(r"^(.+?\.a):([^:]+):\s*([0-9a-fA-F]*)\s+([A-Za-z?-])\s+(\S+)$")


class Report:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.failures: List[str] = []

    def say(self, text: str) -> None:
        print(text)
        self.lines.append(text)

    def check(self, ok: bool, text: str) -> None:
        self.say(("ok   " if ok else "FAIL ") + text)
        if not ok:
            self.failures.append(text)

    def finish(self, path: Optional[Path]) -> int:
        verdict = "FAIL: %d check(s)" % len(self.failures) if self.failures else "PASS"
        self.say("== %s" % verdict)
        if path:
            path.write_text("\n".join(self.lines) + "\n")
        return 1 if self.failures else 0


def nm_archive(archive: Path, defined_only: bool) -> List[Tuple[str, str, str]]:
    """(member, type, symbol) of an archive."""
    argv = ["nm", "-A"] + (["--defined-only"] if defined_only else []) + [str(archive)]
    done = subprocess.run(argv, capture_output=True, text=True)
    out = []
    for line in done.stdout.splitlines():
        m = NM.match(line)
        if m:
            out.append((m.group(2), m.group(4), m.group(5)))
    return out


def archives() -> List[Path]:
    return sorted(p for d in ("lib", "lib64") for p in (PREFIX / d).glob("*.a"))


def cache_value(key: str) -> str:
    for line in (BUILD / "CMakeCache.txt").read_text().splitlines():
        if line.startswith(key + ":"):
            return line.split("=", 1)[1]
    return ""


def deps(tripwire: str, report: Report) -> None:
    extern = sorted(p.name for p in (BUILD / "extern").iterdir())
    hits = [n for n in extern if PARMETIS.search(n)]
    report.check(
        not hits, "no ParMETIS source or build directory in the superbuild: %s" % (hits or "none")
    )
    configure = (Path("/src/provenance/superbuild-configure.log")).read_text()
    report.check(
        "PARMETIS_OPTIONS" not in configure, "the superbuild configures no ParMETIS project"
    )
    report.check("SCOTCH_OPTIONS" in configure, "the superbuild configures Scotch")
    targets = subprocess.run(
        ["cmake", "--build", str(BUILD), "--target", "help"], capture_output=True, text=True
    ).stdout
    hits = [t for t in targets.split() if PARMETIS.search(t)]
    report.check(not hits, "no build target named after ParMETIS: %s" % (hits or "none"))
    url = cache_value("EXTERN_PARMETIS_URL")
    report.check(
        url == tripwire,
        "the ParMETIS download URL is the tripwire %s (cache: %s)" % (tripwire, url),
    )
    pinned = cache_value("EXTERN_SCOTCH_GIT_TAG")
    head = subprocess.run(
        ["git", "-C", str(BUILD / "extern" / "scotch"), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    report.check(
        bool(pinned) and head == pinned,
        "Scotch is at the pinned commit %s (HEAD %s)" % (pinned, head),
    )

    named = sorted(str(p.relative_to(PREFIX)) for p in PREFIX.rglob("*") if PARMETIS.search(p.name))
    # CMake find modules named after ParMETIS (the consumers' own, removed from the image) hold
    # no ParMETIS code
    modules = [f for f in named if f.endswith(".cmake")]
    files = [f for f in named if not f.endswith(".cmake")]
    expected = ["include/parmetis.h", "lib/" + COMPAT]
    report.check(
        set(files) <= set(expected),
        "the only *parmetis* files installed are Scotch's %s: %s" % (", ".join(expected), files),
    )
    if modules:
        report.say("  CMake modules named after ParMETIS: %s" % ", ".join(modules))
    header = PREFIX / "include" / "parmetis.h"
    report.check(
        header.exists() and "Scotch" in header.read_text(errors="replace"),
        "include/parmetis.h is Scotch's (libScotchMeTiS) header",
    )

    definers: Dict[str, Set[str]] = defaultdict(set)
    undefined: Dict[str, Set[str]] = defaultdict(set)
    for archive in archives():
        for member, kind, symbol in nm_archive(archive, defined_only=False):
            if kind == "U":
                undefined[archive.name].add(symbol)
            elif PARMETIS.search(symbol) or symbol == "METIS_NodeND":
                definers[symbol].add(archive.name)
    report.say("installed static libraries: %s" % ", ".join(a.name for a in archives()))
    for symbol in sorted(definers):
        report.say("  %s defined in %s" % (symbol, ", ".join(sorted(definers[symbol]))))
    own = {s: a for s, a in definers.items() if a == {CONSUMER_NAMES.get(s)}}
    stray = {
        s: a for s, a in definers.items() if PARMETIS.search(s) and a != {COMPAT} and s not in own
    }
    report.check(
        not stray,
        "only %s defines ParMETIS names (and the consumers' own wrappers %s): %s"
        % (COMPAT, sorted(own) or "none", stray or "yes"),
    )
    report.check(
        definers.get("METIS_NodeND") == {"libmetis.a"},
        "METIS_NodeND is defined once, in libmetis.a: %s"
        % sorted(definers.get("METIS_NodeND", [])),
    )
    slu = undefined.get("libsuperlu_dist.a", set())
    report.check(
        "ParMETIS_V3_NodeND" in slu,
        "SuperLU_DIST calls ParMETIS_V3_NodeND (its parallel ordering, ColPerm = PARMETIS)",
    )
    for lib in ("libmumps_common.a", "libdmumps.a"):
        refs = sorted(s for s in undefined.get(lib, set()) if PARMETIS.search(s))
        report.check(not refs, "%s calls no ParMETIS name: %s" % (lib, refs or "none"))
    mumps = undefined.get("libdmumps.a", set()) | undefined.get("libmumps_common.a", set())
    pt = sorted(s for s in mumps if s.lower().startswith("scotchfdgraphordercompute"))
    report.check(bool(pt), "MUMPS calls PT-Scotch (parallel analysis): %s" % pt)
    config = PREFIX / "include" / "superlu_dist_config.h"
    report.check(
        config.exists() and re.search(r"#define\s+HAVE_PARMETIS", config.read_text()) is not None,
        "SuperLU_DIST is configured with the ParMETIS API (HAVE_PARMETIS: METIS and ParMETIS ColPerm)",
    )


def map_sections(text: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    """The archives the link loaded (LOAD lines) and the members it pulled from them."""
    loads = [os.path.normpath(m.group(1)) for m in re.finditer(r"^LOAD (\S+\.a)$", text, re.M)]
    start = text.find("Archive member included to satisfy reference by file (symbol)")
    end_marks = [
        text.find(h, start + 1)
        for h in (
            "\nAllocating common symbols",
            "\nDiscarded input sections",
            "\nMemory Configuration",
            "\nAs-needed library included",
        )
    ]
    end = min([e for e in end_marks if e > start] or [len(text)])
    pulled = [
        (os.path.normpath(m.group(1)), m.group(2))
        for m in re.finditer(r"^(/\S+?\.a)\(([^)\s]+)\)", text[start:end], re.M)
    ]
    return loads, pulled


def binary(map_path: Path, exe: Path, report: Report) -> None:
    text = map_path.read_text(errors="replace")
    report.check("main.cpp.o" in text, "the link map %s is Palace's link" % map_path)
    loads, pulled = map_sections(text)
    report.say(
        "archives loaded: %d; archive members pulled in: %d" % (len(set(loads)), len(pulled))
    )
    allowed_dirs = ("/opt/palace/lib", "/opt/palace/lib64", "/src/build/palace-build", "/usr/lib")
    for archive in sorted(set(loads)):
        name = os.path.basename(archive)
        if archive.startswith("/opt/palace/"):
            report.check(name in ALLOWED, "loaded %s (a known dependency library)" % archive)
        elif not os.path.isabs(archive):
            report.say("ok   loaded %s (a library of Palace's own build tree)" % archive)
        else:
            report.check(
                archive.startswith(allowed_dirs), "loaded %s (build tree or system)" % archive
            )
        if PARMETIS.search(name):
            report.check(
                name == COMPAT, "the only ParMETIS-named archive is %s: %s" % (COMPAT, name)
            )
    members: Dict[str, Set[str]] = defaultdict(set)
    for archive, member in pulled:
        members[archive].add(member)
    for archive in sorted(members):
        report.say("  %4d members from %s" % (len(members[archive]), archive))

    definers: Dict[str, Set[str]] = defaultdict(set)
    compat_names: Set[str] = set()
    for archive in sorted(members):
        for member, _kind, symbol in nm_archive(Path(archive), defined_only=True):
            if member not in members[archive]:
                continue
            if PARMETIS.search(symbol) or symbol == "METIS_NodeND":
                definers[symbol].add("%s(%s)" % (os.path.basename(archive), member))
                if os.path.basename(archive) == COMPAT:
                    compat_names.add(symbol)
    for symbol in sorted(definers):
        report.say("  %s <- %s" % (symbol, ", ".join(sorted(definers[symbol]))))
    own = {
        s
        for s, d in definers.items()
        if s in CONSUMER_NAMES and all(x.startswith(CONSUMER_NAMES[s] + "(") for x in d)
    }
    stray = {
        s: d
        for s, d in definers.items()
        if PARMETIS.search(s) and s not in own and any(not x.startswith(COMPAT) for x in d)
    }
    report.check(
        not stray,
        "every pulled member defining a ParMETIS name is from %s (or is a consumer's own "
        "wrapper: %s): %s" % (COMPAT, sorted(own) or "none", stray or "yes"),
    )
    metis = definers.get("METIS_NodeND", set())
    report.check(
        len(metis) == 1 and next(iter(metis)).startswith("libmetis.a("),
        "METIS_NodeND comes from libmetis.a only: %s" % sorted(metis),
    )

    done = subprocess.run(["nm", "--defined-only", str(exe)], capture_output=True, text=True)
    names = sorted({line.split()[-1] for line in done.stdout.splitlines() if PARMETIS.search(line)})
    report.say("ParMETIS-named symbols in the unstripped %s: %s" % (exe.name, names))
    report.check("ParMETIS_V3_NodeND" in names, "the executable defines ParMETIS_V3_NodeND")
    report.check(
        not [n for n in names if "parmetis_v32" in n.lower()],
        "the executable has no ParMETIS_V32_NodeND",
    )
    report.check(
        set(names) <= compat_names | own,
        "each of them is defined by a member of %s or is a consumer's own wrapper: %s"
        % (COMPAT, sorted(set(names) - compat_names - own) or "yes"),
    )


def main(argv: Optional[Iterable[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="step", required=True)
    d = sub.add_parser("deps")
    d.add_argument("--tripwire", required=True)
    d.add_argument("--report", type=Path)
    b = sub.add_parser("binary")
    b.add_argument("--map", type=Path, required=True)
    b.add_argument("--binary", type=Path, required=True)
    b.add_argument("--report", type=Path)
    args = p.parse_args(list(argv) if argv is not None else None)
    report = Report()
    if args.step == "deps":
        deps(args.tripwire, report)
    else:
        binary(args.map, args.binary, report)
    return report.finish(args.report)


if __name__ == "__main__":
    sys.exit(main())
