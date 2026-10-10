"""The yapnr wheel's metadata, shared by //release:wheel and :wheel_for_test."""

load("@rules_python//python:packaging.bzl", "py_wheel")

# The platform tag of the wheel built here: it carries a native library built for the host
# (//yapnr/rf:libyapnr_fdtd.so). Linux: glibc 2.34 is the newest symbol version the library
# needs (pthread in libc); macOS: built for 11.0 and later.
WHEEL_PLATFORM = select({
    "@bazel_tools//src/conditions:darwin_arm64": "macosx_11_0_arm64",
    "@bazel_tools//src/conditions:darwin_x86_64": "macosx_11_0_x86_64",
    "@bazel_tools//src/conditions:linux_aarch64": "manylinux_2_34_aarch64",
    "@bazel_tools//src/conditions:linux_x86_64": "manylinux_2_34_x86_64",
})

def yapnr_wheel(name, version, deps, **kwargs):
    """A py_wheel of the yapnr package with the project's metadata.

    Args:
      name: target name; `<name>.dist` puts the wheel, named with the stamped
        version, into `<name>_dist/`.
      version: the wheel version; "{BUILD_EMBED_LABEL}" for the stamped release wheel.
      deps: the py_package with the files to ship.
      **kwargs: passed to py_wheel (stamp, tags, testonly, ...).
    """
    py_wheel(
        name = name,
        classifiers = [
            "Development Status :: 3 - Alpha",
            "Environment :: Console",
            "Intended Audience :: Science/Research",
            "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)",
            "Operating System :: MacOS",
            "Operating System :: POSIX :: Linux",
            "Programming Language :: Python :: 3.11",
            "Topic :: Scientific/Engineering :: Electronic Design Automation (EDA)",
        ],
        distribution = "yapnr",
        entry_points = {"console_scripts": ["yapnr = yapnr.cli:main", "rr = rules_requirements.cli:main"]},
        extra_distinfo_files = {
            Label("//:LICENSE"): "LICENSE",
            Label("@rules_requirements_toolkit//:LICENSE"): "licenses/rules_requirements/LICENSE",
        },
        homepage = "https://github.com/Studio-Fug/yapnr",
        license = "AGPL-3.0-or-later",
        platform = WHEEL_PLATFORM,
        project_urls = {
            "Documentation": "https://studio-fug.github.io/yapnr/",
            "Issues": "https://github.com/Studio-Fug/yapnr/issues",
            "Source": "https://github.com/Studio-Fug/yapnr",
        },
        python_requires = ">=3.11",
        python_tag = "py3",
        requires_file = Label("//:requirements-runtime.in"),
        strip_path_prefixes = ["python"],
        summary = "Yet another place and route: PCB placement and routing for KiCad",
        version = version,
        deps = deps,
        **kwargs
    )
