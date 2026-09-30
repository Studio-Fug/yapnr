"""``python -m yapnr.viewer`` serves the live viewer (the same as ``bazel run //:viewer``)."""

from yapnr.viewer.server import main

if __name__ == "__main__":
    raise SystemExit(main())
