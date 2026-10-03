"""Bazel's own (native) cc_binary, for the optional native kernels.

rules_cc is not a dependency of this module (MODULE.bazel), so these targets use the cc rules
built into Bazel 7, as the router's kernel does (hardware/pnr/BUILD.bazel). Buildifier's
`-lint=fix` would rewrite a direct `cc_binary` call into a load from @rules_cc, which this
module cannot resolve; the rule is therefore looked up here.
"""

def native_shared_library(name, **kwargs):
    """A shared library (cc_binary with linkshared = True) built with the native cc rules.

    Args:
      name: target name, the library's file name (e.g. "libfoo.so").
      **kwargs: passed to cc_binary (srcs, copts, linkopts, tags, ...).
    """
    getattr(native, "cc_binary")(
        name = name,
        linkshared = True,
        **kwargs
    )
