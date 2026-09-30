"""The yapnr wheel's metadata, shared by //release:wheel and :wheel_for_test."""

load("@rules_python//python:packaging.bzl", "py_wheel")

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
        entry_points = {"console_scripts": ["yapnr = yapnr.cli:main"]},
        extra_distinfo_files = {Label("//:LICENSE"): "LICENSE"},
        homepage = "https://github.com/Studio-Fug/yapnr",
        license = "AGPL-3.0-or-later",
        project_urls = {
            "Documentation": "https://studio-fug.github.io/yapnr/",
            "Issues": "https://github.com/Studio-Fug/yapnr/issues",
            "Source": "https://github.com/Studio-Fug/yapnr",
        },
        python_requires = ">=3.11",
        python_tag = "py3",
        requires_file = Label("//:requirements-runtime.in"),
        summary = "Yet another place and route: PCB placement and routing for KiCad",
        version = version,
        deps = deps,
        **kwargs
    )
