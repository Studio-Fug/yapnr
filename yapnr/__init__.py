"""yapnr: yet another place and route, for printed circuit boards.

The engine is being migrated here from the Splanc repository; see
docs/migration-plan.md. Until then this package only carries the command-line
entry point (``yapnr --version``, ``yapnr doctor``).
"""

__all__ = ["__version__"]

# The version of an installed yapnr (the wheel, or the container image) is
# stamped into the wheel's metadata from the release tag (docs/releases.md;
# tools/release/version.py). A source tree that is not installed, such as
# `bazel run //:yapnr`, has no version of its own.
UNINSTALLED_VERSION = "0.0.0.dev0"


def _installed_version() -> str:
    from importlib import metadata

    try:
        return metadata.version("yapnr")
    except metadata.PackageNotFoundError:
        return UNINSTALLED_VERSION


__version__ = _installed_version()
