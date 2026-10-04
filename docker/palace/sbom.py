#!/usr/bin/env python3
"""SPDX 2.3 software bill of materials of the Palace task image (docker/palace), in two steps.

    sbom.py build --out PARTIAL.json        # build stage: Palace, every superbuild dependency
    sbom.py image --partial PARTIAL.json --out SBOM.spdx.json   # task image: + apt and pip
    sbom.py compare-syft SBOM.spdx.json SYFT.spdx.json          # Cloud Build: syft's view

``build`` reads the superbuild tree (/src/build): every dependency it fetched (a source directory
under extern/), with its git remote and commit or its download URL (CMakeCache.txt), the MUMPS
release the MUMPS CMake wrapper downloads, the licence files found, and an SPDX licence id from
the table below. ``image`` adds the Debian packages installed in the image (dpkg) and the Python
packages of the venv. Both fail when any component is ParMETIS, which the image must not contain
(its licence forbids redistribution); ``compare-syft`` fails when syft's independent scan of the
pushed image finds it, and prints how the two package lists differ.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

BUILD = Path("/src/build")
PALACE_SRC = Path("/src/palace")
META = Path("/src/meta")
FORBIDDEN = re.compile("parmetis", re.IGNORECASE)
# The declared licence of each superbuild dependency (its source directory under extern/): SPDX
# ids, from each project's licence file.
LICENSES = {
    "palace": "Apache-2.0",
    "arpack-ng": "BSD-3-Clause",
    "eigen": "MPL-2.0",
    "fmt": "MIT",
    "gslib": "MIT",
    "hypre": "Apache-2.0 OR MIT",
    "json": "MIT",
    "json-schema-validator": "MIT",
    "libCEED": "BSD-2-Clause",
    "libxsmm": "BSD-3-Clause",
    "metis": "Apache-2.0",
    "mfem": "BSD-3-Clause",
    "mumps": "MIT",  # the scivision CMake wrapper; MUMPS itself is mumps-upstream below
    "mumps-upstream": "CECILL-C",
    "scalapack": "BSD-3-Clause",
    "scn": "Apache-2.0",
    "scotch": "CECILL-C",
    "superlu_dist": "BSD-3-Clause-LBNL",
}
# Source directories fetched by URL (no .git): their CMake cache variable
URL_KEYS = {
    "eigen": "EIGEN",
    "fmt": "FMT",
    "json": "JSON",
    "json-schema-validator": "JSON_SCHEMA_VALIDATOR",
    "scn": "SCN",
}
LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|Copyright)", re.IGNORECASE)


def run(argv: List[str], cwd: Optional[Path] = None) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def cache_values(path: Path) -> Dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_]+):[A-Z]+=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def spdx_id(name: str) -> str:
    return "SPDXRef-" + re.sub(r"[^A-Za-z0-9.-]", "-", name)


def package(name: str, version: str, location: str, license_id: str, **extra) -> Dict:
    item = {
        "name": name,
        "SPDXID": spdx_id(name),
        "versionInfo": version or "NOASSERTION",
        "downloadLocation": location or "NOASSERTION",
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": license_id or "NOASSERTION",
        "copyrightText": "NOASSERTION",
        "filesAnalyzed": False,
    }
    item.update(extra)
    return item


def git_package(name: str, src: Path, license_id: str, comment: str = "") -> Dict:
    remote = run(["git", "-C", str(src), "config", "--get", "remote.origin.url"])
    commit = run(["git", "-C", str(src), "rev-parse", "HEAD"])
    files = sorted(p.name for p in src.iterdir() if p.is_file() and LICENSE_FILE.match(p.name))
    extra = {"comment": (comment + " " if comment else "") + "licence files: " + ", ".join(files)}
    return package(name, commit, "git+%s@%s" % (remote, commit), license_id, **extra)


def build_components() -> List[Dict]:
    cache = cache_values(BUILD / "CMakeCache.txt")
    patches = sorted((Path("/src/patches")).glob("*.diff"))
    sums = ", ".join(
        "%s sha256:%s" % (p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in patches
    )
    out = [
        git_package(
            "palace",
            PALACE_SRC,
            LICENSES["palace"],
            "%s, with the yapnr patches %s."
            % ((META / "palace.version").read_text().strip(), sums),
        )
    ]
    extern = BUILD / "extern"
    for src in sorted(p for p in extern.iterdir() if p.is_dir()):
        name = src.name
        if re.search(r"-(build|cmake|install|patches)$", name):  # mfem-patches: part of MFEM
            continue
        if (src / ".git").exists():
            out.append(git_package(name, src, LICENSES.get(name, "")))
        elif name in URL_KEYS:
            url = cache.get("EXTERN_%s_URL" % URL_KEYS[name], "")
            files = sorted(
                p.name for p in src.iterdir() if p.is_file() and LICENSE_FILE.match(p.name)
            )
            out.append(
                package(
                    name,
                    url.rsplit("/", 1)[-1],
                    url,
                    LICENSES.get(name, ""),
                    comment="licence files: " + ", ".join(files),
                )
            )
        else:  # CMakeFiles and the like: not a dependency's source
            print("sbom: %s is not a dependency's source directory" % name, file=sys.stderr)
            continue
        if not out[-1]["licenseDeclared"] or out[-1]["licenseDeclared"] == "NOASSERTION":
            print("sbom: no declared licence for %s" % name, file=sys.stderr)
    header = Path("/opt/palace/include/dmumps_c.h")
    if header.exists():
        m = re.search(r'#define MUMPS_VERSION "([^"]+)"', header.read_text())
        version = m.group(1) if m else ""
        out.append(
            package(
                "mumps-upstream",
                version,
                "https://mumps-solver.org/MUMPS_%s.tar.gz" % version,
                LICENSES["mumps-upstream"],
                comment="MUMPS itself, downloaded by the mumps CMake wrapper",
            )
        )
    return out


def image_components() -> List[Dict]:
    out = []
    rows = run(["dpkg-query", "-W", "-f", "${Package}\\t${Version}\\t${source:Package}\\n"])
    for row in rows.splitlines():
        name, version, source = row.split("\t")
        out.append(
            package(
                "deb-" + name,
                version,
                "NOASSERTION",
                "",
                sourceInfo="Ubuntu 24.04 package %s (source %s)" % (name, source),
            )
        )
    venv = Path("/opt/palace/venv/bin/python")
    if venv.exists():
        listing = run(
            [
                str(venv),
                "-c",
                "import importlib.metadata as m, json; print(json.dumps(sorted("
                "[d.metadata['Name'], d.version, d.metadata.get('License-Expression') or '']"
                " for d in m.distributions())))",
            ]
        )
        for name, version, expression in json.loads(listing):
            out.append(
                package(
                    "pypi-" + name,
                    version,
                    "https://pypi.org/project/%s/%s/" % (name, version),
                    expression,
                )
            )
    return out


def forbidden(components: List[Dict]) -> List[str]:
    keys = ("name", "versionInfo", "downloadLocation", "sourceInfo")
    return [c["name"] for c in components if any(FORBIDDEN.search(str(c.get(k, ""))) for k in keys)]


def document(components: List[Dict]) -> Dict:
    digest = hashlib.sha256(json.dumps(components, sort_keys=True).encode()).hexdigest()
    version = "palace"
    for path in (META / "palace.version", Path("/opt/palace/palace.version")):
        if path.exists():
            version = path.read_text().strip()
            break
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "palace-task-image",
        "documentNamespace": "https://github.com/Studio-Fug/yapnr/spdx/palace/%s/%s"
        % (version, digest[:16]),
        "creationInfo": {
            "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: yapnr docker/palace/sbom.py"],
        },
        "packages": components,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": c["SPDXID"],
            }
            for c in components
        ],
    }


def write(components: List[Dict], out: Path) -> int:
    bad = forbidden(components)
    out.write_text(json.dumps(document(components), indent=1) + "\n")
    print("sbom: %d components -> %s" % (len(components), out))
    if bad:
        print("sbom: FAIL: ParMETIS in the image: %s" % ", ".join(bad), file=sys.stderr)
        return 1
    return 0


def compare_syft(ours: Path, theirs: Path) -> int:
    mine = json.loads(ours.read_text())["packages"]
    syft = json.loads(theirs.read_text()).get("packages", [])
    names = sorted({p.get("name", "") for p in syft})
    bad = [n for n in names if FORBIDDEN.search(n)]
    bad += [
        p.get("name", "")
        for p in syft
        if any(
            FORBIDDEN.search(str(r.get("referenceLocator", ""))) for r in p.get("externalRefs", [])
        )
    ]
    deb_ours = {p["name"][4:] for p in mine if p["name"].startswith("deb-")}
    pip_ours = {
        p["name"][5:].lower().replace("_", "-") for p in mine if p["name"].startswith("pypi-")
    }
    deb_syft, pip_syft, other = set(), set(), []
    for p in syft:
        refs = " ".join(str(r.get("referenceLocator", "")) for r in p.get("externalRefs", []))
        if "pkg:deb/" in refs:
            deb_syft.add(p["name"])
        elif "pkg:pypi/" in refs:
            pip_syft.add(p["name"].lower().replace("_", "-"))
        else:
            other.append("%s %s" % (p.get("name"), p.get("versionInfo", "")))
    print(
        "syft: %d packages (%d deb, %d pypi, %d other)"
        % (len(syft), len(deb_syft), len(pip_syft), len(other))
    )
    print("syft: deb only in syft: %s" % (sorted(deb_syft - deb_ours) or "none"))
    print("syft: deb only in ours: %s" % (sorted(deb_ours - deb_syft) or "none"))
    print("syft: pypi only in syft: %s" % (sorted(pip_syft - pip_ours) or "none"))
    print("syft: pypi only in ours: %s" % (sorted(pip_ours - pip_syft) or "none"))
    print("syft: other (binaries syft recognised): %s" % (sorted(other) or "none"))
    if bad:
        print(
            "syft: FAIL: ParMETIS in the image: %s" % ", ".join(sorted(set(bad))), file=sys.stderr
        )
        return 1
    print("syft: no ParMETIS component")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="step", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    i = sub.add_parser("image")
    i.add_argument("--partial", type=Path, required=True)
    i.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare-syft")
    c.add_argument("ours", type=Path)
    c.add_argument("syft", type=Path)
    args = p.parse_args(argv)
    if args.step == "build":
        return write(build_components(), args.out)
    if args.step == "image":
        partial = json.loads(args.partial.read_text())["packages"]
        return write(partial + image_components(), args.out)
    return compare_syft(args.ours, args.syft)


if __name__ == "__main__":
    sys.exit(main())
