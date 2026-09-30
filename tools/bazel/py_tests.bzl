"""One py_test per test file, generated from a glob.

Every `test_*.py` in a package that calls `yapnr_py_tests()` becomes its own
`py_test` target named after the file (`test_cli.py` -> `:test_cli`). A new test
file is wired to Bazel simply by being added to such a package. The repo check in
tests/unit/repo/test_wiring.py fails on test files that no BUILD file wires.
"""

load("@rules_python//python:defs.bzl", "py_test")

def yapnr_py_tests(
        deps = [],
        data = [],
        size = "small",
        tags = [],
        overrides = {},
        **kwargs):
    """Declares a py_test for every test_*.py file in the calling package.

    Args:
      deps: deps shared by every generated test.
      data: data shared by every generated test.
      size: default test size.
      tags: tags shared by every generated test.
      overrides: dict from test name (file name without `.py`) to a dict of
        extra attributes for that test only. `deps`, `data` and `tags` are
        appended to the shared lists; any other key replaces the default.
      **kwargs: passed to every py_test (e.g. `env`, `timeout`).
    """
    srcs = native.glob(["test_*.py"])
    if not srcs:
        fail("yapnr_py_tests() found no test_*.py files in this package")
    for name in overrides:
        if name + ".py" not in srcs:
            fail("yapnr_py_tests(): override for unknown test '%s'" % name)

    for src in srcs:
        name = src[:-len(".py")]
        extra = dict(overrides.get(name, {}))
        attrs = dict(kwargs)
        attrs.update({
            "data": data + extra.pop("data", []),
            "deps": deps + extra.pop("deps", []),
            "size": size,
            "tags": tags + extra.pop("tags", []),
        })
        attrs.update(extra)
        py_test(
            name = name,
            srcs = [src],
            main = src,
            **attrs
        )
