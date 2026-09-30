"""`yapnr_atopile_build`: one offline, isolated `ato build` as a Bazel action.

    load("@yapnr//bazel/atopile:defs.bzl", "yapnr_atopile_build")

    yapnr_atopile_build(
        name = "board",
        ato_yaml = "ato.yaml",
        srcs = glob(["elec/src/**", "yapnr-parts.lock.json"]),
        build = "default",
    )

The action runs `yapnr atopile build` (yapnr/frontends/atopile/runner.py) with the atopile
environment of the registered toolchain (bazel/atopile/repo.bzl): the project is copied into a
work directory, the parts of its lock come from the part cache, the loopback picker answers from
their catalog entries and any `catalogs`, and nothing is fetched from the network. The part cache
is `cache`, else `--action_env=YAPNR_PART_CACHE`, else the default local cache.

It is `local` and `no-remote`: it reads an environment and a cache that Bazel does not track. It
is not `requires-network`: a build that would need the network fails instead.

Outputs: `<name>.kicad_pcb` (the board), `<name>.bom.csv`, `<name>.result.json` (versions,
hashes, the UUID-normalized input id, the picker requests) and `<name>.out/` (everything).
"""

YapnrAtopileBuildInfo = provider(
    doc = "The outputs of an atopile build.",
    fields = {
        "pcb": "The .kicad_pcb.",
        "bom": "The BOM (CSV).",
        "result": "result.json.",
        "out": "The directory with every output.",
        "annotation_sources": "The .ato sources (for the PnR frontend).",
    },
)

_TOOLCHAIN = "//bazel/atopile:toolchain_type"

def _impl(ctx):
    toolchain = ctx.toolchains[_TOOLCHAIN].atopile
    project = ctx.file.ato_yaml.dirname
    out = ctx.actions.declare_directory(ctx.label.name + ".out")
    pcb = ctx.actions.declare_file(ctx.label.name + ".kicad_pcb")
    bom = ctx.actions.declare_file(ctx.label.name + ".bom.csv")
    result = ctx.actions.declare_file(ctx.label.name + ".result.json")

    args = [project or ".", "--out", out.path, "--build", ctx.attr.build]
    args += ["--timeout", str(ctx.attr.timeout_s)]
    for target in ctx.attr.targets:
        args += ["--target", target]
    for catalog in ctx.files.catalogs:
        args += ["--catalog", catalog.path]
    if ctx.attr.cache:
        args += ["--cache", ctx.attr.cache]
    if ctx.attr.outline_margin_mm:
        args += ["--outline-margin-mm", str(ctx.attr.outline_margin_mm)]

    env = dict(ctx.configuration.default_shell_env)
    if toolchain.available:
        env["YAPNR_ATO_PYTHON"] = toolchain.python
    if toolchain.kicad_cli:
        env["YAPNR_KICAD_CLI"] = toolchain.kicad_cli
    if toolchain.kicad_footprints:
        env["YAPNR_KICAD_FOOTPRINTS"] = toolchain.kicad_footprints

    command = "\n".join([
        "set -euo pipefail",
        'export HOME="${HOME:-$(mktemp -d)}"',
        '"{tool}" "$@" || {{ tail -n 60 "{out}/ato.log" >&2 || true; exit 1; }}'.format(
            tool = ctx.executable._tool.path,
            out = out.path,
        ),
        'cp "{out}/{build}.kicad_pcb" "{pcb}"'.format(out = out.path, build = ctx.attr.build, pcb = pcb.path),
        'cp "{out}/{build}.bom.csv" "{bom}"'.format(out = out.path, build = ctx.attr.build, bom = bom.path),
        'cp "{out}/result.json" "{result}"'.format(out = out.path, result = result.path),
    ])
    if not toolchain.available:
        # Single-quoted for the shell: the reason is text, never expanded.
        reason = "no atopile environment for yapnr_atopile_build: " + toolchain.reason
        command = "echo '{}' >&2; exit 1".format(reason.replace("'", "'\\''"))

    ctx.actions.run_shell(
        outputs = [out, pcb, bom, result],
        inputs = depset(ctx.files.srcs + [ctx.file.ato_yaml] + ctx.files.catalogs),
        tools = [ctx.executable._tool],
        command = command,
        arguments = args,
        env = env,
        mnemonic = "YapnrAtopileBuild",
        progress_message = "atopile build %s (%s)" % (ctx.label, ctx.attr.build),
        execution_requirements = {"local": "1", "no-remote": "1"},
    )
    return [
        DefaultInfo(files = depset([pcb, bom, result])),
        OutputGroupInfo(all_outputs = depset([out])),
        YapnrAtopileBuildInfo(
            pcb = pcb,
            bom = bom,
            result = result,
            out = out,
            annotation_sources = depset([f for f in ctx.files.srcs if f.extension == "ato"]),
        ),
    ]

yapnr_atopile_build = rule(
    implementation = _impl,
    attrs = {
        "ato_yaml": attr.label(allow_single_file = ["ato.yaml"], mandatory = True),
        "srcs": attr.label_list(allow_files = True, doc = "The project's sources and parts lock."),
        "build": attr.string(default = "default", doc = "The ato.yaml build."),
        "targets": attr.string_list(doc = "atopile targets (default: the runner's set)."),
        "catalogs": attr.label_list(allow_files = [".json"], doc = "Extra picker catalogs."),
        "cache": attr.string(doc = "Part cache (directory or URL); default $YAPNR_PART_CACHE."),
        "timeout_s": attr.int(default = 1800),
        "outline_margin_mm": attr.int(default = 0, doc = "Frame the board (mm); 0 = off."),
        "_tool": attr.label(
            default = "//yapnr/frontends/atopile:build_tool",
            executable = True,
            cfg = "exec",
        ),
    },
    toolchains = [_TOOLCHAIN],
    doc = "Builds an atopile project offline with `yapnr atopile build`.",
)
