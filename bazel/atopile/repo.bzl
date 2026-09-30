"""Discovers the atopile environment and the headless KiCad for Bazel, the way KiCad is found.

The repository rule runs yapnr's own discovery (yapnr/frontends/atopile/toolchain.py and
kicad.py) with the host's python3 when the repository is fetched, and generates an
`atopile_toolchain` from the result. The discovery order is the runner's: $YAPNR_ATO_PYTHON,
~/.config/yapnr/config.toml, the image's /opt/atopile, then the `yapnr atopile setup`
environment of the current lock; the atopile version must equal the pin.

Nothing machine-specific is recorded in MODULE.bazel.lock: the extension passes no attributes,
and the paths live only in the generated repository, which Bazel refetches when one of the
environment variables below changes (`bazel fetch --force @yapnr_atopile//...` after
`yapnr atopile setup`).

When nothing is found, the toolchain says why, and building a `yapnr_atopile_build` target fails
with that reason; nothing else in the repository needs atopile.
"""

_ENV = [
    "HOME",
    "PATH",
    "XDG_CACHE_HOME",
    "YAPNR_ATO_PYTHON",
    "YAPNR_ATOPILE_HOME",
    "YAPNR_KICAD_CLI",
    "PNR_KICAD_CLI",
    "YAPNR_KICAD_FOOTPRINTS",
]

_DISCOVER = """
import json, sys
sys.path.insert(0, sys.argv[1])
report = {"available": False}
try:
    from yapnr.frontends.atopile import kicad, toolchain
    found = toolchain.discover()
    report.update(available=True, python=str(found.python), version=found.version,
                  source=found.source)
except Exception as err:
    report["reason"] = str(err)
try:
    cli = kicad.find_cli()
    report["kicad_cli"] = str(cli)
    report["kicad_footprints"] = str(kicad.find_footprints(cli))
except Exception as err:
    report["kicad_reason"] = str(err)
print(json.dumps(report))
"""

_BUILD = """\
load("@yapnr//bazel/atopile:toolchain.bzl", "atopile_toolchain")

package(default_visibility = ["//visibility:public"])

atopile_toolchain(
    name = "atopile",
    available = {available},
    kicad_cli = {kicad_cli},
    kicad_footprints = {kicad_footprints},
    python = {python},
    reason = {reason},
    source = {source},
    version = {version},
)

toolchain(
    name = "toolchain",
    toolchain = ":atopile",
    toolchain_type = "@yapnr//bazel/atopile:toolchain_type",
)
"""

def _atopile_configure_impl(repository_ctx):
    source_root = repository_ctx.path(Label("//:MODULE.bazel")).dirname
    python3 = repository_ctx.which("python3")
    report = {"available": False, "reason": "no python3 on PATH to run the discovery"}
    if python3:
        result = repository_ctx.execute(
            [python3, "-c", _DISCOVER, str(source_root)],
            timeout = 300,
            quiet = True,
        )
        if result.return_code == 0:
            report = json.decode(result.stdout.strip().splitlines()[-1])
        else:
            report = {"available": False, "reason": "discovery failed: " + result.stderr[-500:]}
    repository_ctx.file("BUILD.bazel", _BUILD.format(
        available = repr(bool(report.get("available"))),
        kicad_cli = repr(report.get("kicad_cli", "")),
        kicad_footprints = repr(report.get("kicad_footprints", "")),
        python = repr(report.get("python", "")),
        reason = repr(report.get("reason", "")),
        source = repr(report.get("source", "")),
        version = repr(report.get("version", "")),
    ))

atopile_configure = repository_rule(
    implementation = _atopile_configure_impl,
    environ = _ENV,
    local = True,
    configure = True,
    doc = "Generates @yapnr_atopile with the discovered atopile toolchain.",
)
