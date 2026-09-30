"""The served files: the assembled dist carries the pinned third-party files byte for byte, every
static file the pages load, and static paths are contained lexically (Bazel runfiles are
symlinks). Both ways of starting the server from a checkout reach its entry point."""

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from yapnr.viewer import server
from yapnr.viewer.testing import child_env, module_argv

# sha256 of the files served unmodified from the pinned npm tarballs (MODULE.bazel):
# elkjs 0.9.3 lib/elk.bundled.js and three.js 0.186.1; and of the Apache-2.0 text
# (third_party/licenses, as published at apache.org) for elkjs's web-worker shim.
PINNED = {
    "elk.bundled.js": "b0745abd7f23cd91690a1587e377edbe19fd7233c783300290936720546216d4",
    "third_party/elkjs/Apache-2.0.txt": (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    ),
    "vendor/three/LICENSE": "8b378ebe60e2fe500158cb0ac71cb5e8b7d92953c2abcc63a0eb90499653b5bc",
    "vendor/three/build/three.core.js": (
        "9edde002b066a9a05676a6127f67735b62baf399bdea529f2f7e31657da769e6"
    ),
    "vendor/three/build/three.module.js": (
        "9052042d676cb0fdc1ddfefe193053f34b7ac0513a616fdac4535d49987812ea"
    ),
    "vendor/three/examples/jsm/controls/OrbitControls.js": (
        "3d79d07ecb686b4e5d93232eedab255331c1beef711e13164eaa1f68655a5f2b"
    ),
    "vendor/three/examples/jsm/loaders/GLTFLoader.js": (
        "131c0f78c01d19368ae495caa65b3adaa10487810a36a05bb5901b769a35ac16"
    ),
    "vendor/three/examples/jsm/utils/BufferGeometryUtils.js": (
        "9fb63427ce6641fa14fd0baff9cc4d1b5f9c3d85fd084bf2e90e803c44ec1797"
    ),
    "vendor/three/examples/jsm/utils/SkeletonUtils.js": (
        "b1632a703206c3d830de9fcbe515696770d04b71a15ee6b50afa6d2c3298c86f"
    ),
}


class DistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dist, cls.missing = server.find_dist()

    def test_assembled_dist_is_found_and_complete(self):
        self.assertEqual(self.dist.name, "dist", "run under Bazel: //yapnr/viewer:dist")
        self.assertIsNone(self.missing)
        for rel, sha in PINNED.items():
            f = self.dist / rel
            self.assertEqual(hashlib.sha256(f.read_bytes()).hexdigest(), sha, rel)
        # the licenses are served next to the files
        self.assertIn(
            "Eclipse Public License", (self.dist / "third_party/elkjs/LICENSE.md").read_text()
        )
        self.assertIn("MIT", (self.dist / "vendor/three/LICENSE").read_text())
        # elkjs's Apache-2.0 part: the notice is in the file, the license text next to it
        self.assertIn("Apache License, Version 2.0", (self.dist / "elk.bundled.js").read_text())
        self.assertIn(
            "Apache License\n                           Version 2.0",
            (self.dist / "third_party/elkjs/Apache-2.0.txt").read_text(),
        )

    def test_pages_load_only_served_files(self):
        index = (self.dist / "index.html").read_text()
        refs = re.findall(r'(?:src|href)="([^":]+)"', index)
        imports = json.loads(re.search(r'type="importmap">(.*?)</script>', index).group(1))
        refs += list(imports["imports"].values())
        refs += re.findall(r"import\('\./([^']+)'\)", (self.dist / "viewer3d.js").read_text())
        refs.append("elk.bundled.js")  # schematic.js loads it on first use
        for ref in refs:
            self.assertTrue((self.dist / ref.removeprefix("./")).is_file(), ref)
        # no third-party file is fetched from anywhere else at run time
        for f in self.dist.glob("*.js"):
            if f.name != "elk.bundled.js":
                self.assertNotRegex(f.read_text(), r"https?://(?:cdn|unpkg|esm\.sh)", f.name)

    def test_static_paths_are_contained_lexically(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            (dist / "sub").mkdir(parents=True)
            target = Path(tmp) / "elsewhere.js"
            target.write_text("x")
            (dist / "index.html").write_text("<html></html>")
            (dist / "sub/linked.js").symlink_to(target)  # like a runfiles symlink
            (Path(tmp) / "secret.txt").write_text("secret")
            self.assertEqual(server.static_path(dist, "/"), dist / "index.html")
            self.assertEqual(server.static_path(dist, "/sub/linked.js"), dist / "sub/linked.js")
            for bad in (
                "/../secret.txt",
                "/sub/../../secret.txt",
                "//secret.txt",
                "/sub/",
                "/./index.html",
                "/a\\b",
                "/missing.js",
            ):
                self.assertIsNone(server.static_path(dist, bad), bad)


class EntryPointTest(unittest.TestCase):
    def test_module_and_script_print_usage(self):
        # `python -m yapnr.viewer` and `python yapnr/viewer/server.py` (the old habit) both run main
        for argv in (
            module_argv("yapnr.viewer", "--help"),
            [sys.executable, str(Path(server.__file__)), "--help"],
        ):
            done = subprocess.run(
                argv, env=child_env(), capture_output=True, text=True, timeout=120, check=False
            )
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertIn("usage: yapnr-viewer", done.stdout, argv)


if __name__ == "__main__":
    unittest.main()
