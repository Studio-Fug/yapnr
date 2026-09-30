"""Preview the built docs: the ``//docs:serve`` target.

    bazel run //docs:serve            # http://127.0.0.1:8000
    bazel run //docs:serve -- 9001    # another port

Serves ``docs/site/html`` (build it first with ``bazel run //docs:build``) on the
loopback interface only.
"""

from __future__ import annotations

import functools
import http.server
import os
import socketserver
import sys
from pathlib import Path
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[0]) if argv else 8000

    ws = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd()))
    root = ws / "docs" / "site" / "html"
    if not (root / "index.html").exists():
        print(f"No built docs at {root}. Run:  bazel run //docs:build", file=sys.stderr)
        return 1

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving {root} at http://127.0.0.1:{port}/  (Ctrl-C to stop)", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
