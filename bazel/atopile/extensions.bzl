"""The `atopile` module extension: `use_repo(atopile, "yapnr_atopile")` (see repo.bzl)."""

load(":repo.bzl", "atopile_configure")

def _atopile_impl(module_ctx):
    atopile_configure(name = "yapnr_atopile")
    return module_ctx.extension_metadata(reproducible = True)

atopile = module_extension(
    implementation = _atopile_impl,
    doc = "Discovers the atopile environment (`yapnr atopile setup`) as a toolchain.",
)
