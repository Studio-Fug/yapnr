"""Bazel's own (native) cc_binary, for the optional native kernels.

rules_cc is not a dependency of this module (MODULE.bazel), so these targets use the cc rules
built into Bazel 7 and 8, as the router's kernel does (hardware/pnr/BUILD.bazel, which
buildifier does not lint). Buildifier's `-lint=fix` would rewrite a direct `cc_binary` call in
a linted BUILD file into a load from @rules_cc, which this module cannot resolve; the rule is
therefore looked up here. Where Bazel no longer has it (Bazel 9 moved the cc rules to rules_cc)
the target is an empty filegroup, so the package and everything that depends on the library (a
py_library's data, the wheel) still load and build, and the loader falls back to numpy; adding
rules_cc as a bazel_dep is the fix then.
"""

def native_shared_library(name, **kwargs):
    """A shared library (cc_binary with linkshared = True) built with the native cc rules.

    Args:
      name: target name, the library's file name (e.g. "libfoo.so").
      **kwargs: passed to cc_binary (srcs, copts, linkopts, tags, ...).
    """
    cc_binary = getattr(native, "cc_binary", None)
    if cc_binary == None:
        native.filegroup(name = name, srcs = [], tags = kwargs.get("tags", []))
        return
    cc_binary(
        name = name,
        linkshared = True,
        **kwargs
    )
