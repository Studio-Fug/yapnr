"""Write manifest.json: sha256 and size of every file in the handoff bundle (except manifest.json).

usage: python3 hash_bundle.py [BUNDLE_DIR]   (default: this file's bundle)
"""

import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
files = {}
for p in sorted(root.rglob("*")):
    if p.is_file() and p.name != "manifest.json" and "__pycache__" not in p.parts:
        files[str(p.relative_to(root))] = dict(
            sha256=hashlib.sha256(p.read_bytes()).hexdigest(), bytes=p.stat().st_size
        )
(root / "manifest.json").write_text(
    json.dumps(dict(schema="radar60-handoff-manifest-v1", files=files), indent=2, sort_keys=True)
    + "\n"
)
print(len(files), "files")
