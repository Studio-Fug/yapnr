"""The container image inputs agree with each other and with the Bazel build.

- docker/yapnr/runtime-{arm64,amd64}.lock pin exactly the versions (and, apart
  from the CPU build of torch on amd64, the hashes) of requirements.lock, so
  the image runs what `bazel test` runs (tools/image/update_runtime_locks.sh).
- requirements-runtime.in is a subset of requirements.in.
- docker/yapnr-kicad/TAG, the KiCad version in its Dockerfile and the base
  image the application Dockerfile builds on name the same KiCad release.
- The image's controller Python is the patch release of Bazel's hermetic 3.11.
"""

from __future__ import annotations

import os
import platform
import re
import unittest
from typing import Dict, List, Tuple

from tools.repo_root import workspace_root

ROOT = workspace_root()
LOCKS = {arch: f"docker/yapnr/runtime-{arch}.lock" for arch in ("arm64", "amd64")}
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")
HASH = re.compile(r"--hash=(sha256:[0-9a-f]{64})")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> Dict[str, Tuple[str, List[str]]]:
    """Map each pinned package to (version, sorted hashes) in a pip-style lock."""
    result: Dict[str, Tuple[str, List[str]]] = {}
    current = None
    for line in text.splitlines():
        match = PIN.match(line)
        if match:
            current = _normalize(match.group(1))
            result[current] = (match.group(2), [])
        elif line.startswith((" ", "\t")) and current is not None:
            result[current][1].extend(HASH.findall(line))
        elif line.strip() and not line.lstrip().startswith("#"):
            current = None
    return {name: (ver, sorted(hashes)) for name, (ver, hashes) in result.items()}


def _requirement_lines(rel: str) -> List[str]:
    lines = []
    for line in _read(rel).splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            lines.append(line)
    return lines


def _dockerfile_arg(rel: str, name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", _read(rel), re.MULTILINE)
    if not match:
        raise AssertionError(f"ARG {name}=... not found in {rel}")
    return match.group(1)


class ParseLockTest(unittest.TestCase):
    def test_parse(self):
        text = (
            "# header\n"
            "Foo_Bar==1.0 \\\n"
            "    --hash=sha256:" + "b" * 64 + " \\\n"
            "    --hash=sha256:" + "a" * 64 + "\n"
            "    # via torch\n"
            "torch==2.3.1+cpu \\\n"
            "    --hash=sha256:" + "c" * 64 + "\n"
        )
        self.assertEqual(
            parse_lock(text),
            {
                "foo-bar": ("1.0", ["sha256:" + "a" * 64, "sha256:" + "b" * 64]),
                "torch": ("2.3.1+cpu", ["sha256:" + "c" * 64]),
            },
        )


class RuntimeLockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bazel = parse_lock(_read("requirements.lock"))
        cls.locks = {arch: parse_lock(_read(rel)) for arch, rel in LOCKS.items()}

    def test_runtime_requirements_are_in_requirements_in(self):
        runtime = _requirement_lines("requirements-runtime.in")
        self.assertTrue(runtime)
        self.assertEqual(sorted(set(runtime) - set(_requirement_lines("requirements.in"))), [])

    def test_locks_cover_the_runtime_requirements(self):
        names = {
            _normalize(re.split(r"[<>=!~;\[ ]", line, 1)[0])
            for line in _requirement_lines("requirements-runtime.in")
        }
        for arch, lock in self.locks.items():
            self.assertEqual(sorted(names - set(lock)), [], arch)

    def test_both_architectures_pin_the_same_packages(self):
        self.assertEqual(sorted(self.locks["arm64"]), sorted(self.locks["amd64"]))

    def test_every_pin_is_hashed(self):
        for arch, lock in self.locks.items():
            for name, (_, hashes) in lock.items():
                self.assertTrue(hashes, f"{arch}: {name} has no --hash")

    def test_pins_match_requirements_lock(self):
        for arch, lock in self.locks.items():
            for name, (version, hashes) in lock.items():
                self.assertIn(name, self.bazel, f"{arch}: {name} is not in requirements.lock")
                bazel_version, bazel_hashes = self.bazel[name]
                if arch == "amd64" and name == "torch":
                    # The same release, built for CPU only, from the PyTorch index.
                    self.assertEqual(version, bazel_version + "+cpu")
                    continue
                self.assertEqual(version, bazel_version, f"{arch}: {name}")
                self.assertEqual(hashes, bazel_hashes, f"{arch}: {name} hashes")


class KicadBaseTest(unittest.TestCase):
    def test_tag_names_the_dockerfile_kicad_version(self):
        tag = _read("docker/yapnr-kicad/TAG").strip()
        match = re.fullmatch(r"(\d+\.\d+\.\d+)-([1-9]\d*)", tag)
        self.assertIsNotNone(match, f"docker/yapnr-kicad/TAG {tag!r} is not <kicad version>-<N>")
        kicad = _dockerfile_arg("docker/yapnr-kicad/Dockerfile", "KICAD_VERSION")
        deb = _dockerfile_arg("docker/yapnr-kicad/Dockerfile", "KICAD_DEB")
        self.assertEqual(match.group(1), kicad)
        self.assertTrue(deb.startswith(kicad + "~"), deb)
        self.assertEqual(
            _dockerfile_arg("docker/yapnr/Dockerfile", "KICAD_BASE"),
            f"ghcr.io/studio-fug/yapnr-kicad:{tag}",
        )

    def test_controller_python_is_bazels(self):
        # This test runs on Bazel's hermetic interpreter.
        self.assertEqual(
            _dockerfile_arg("docker/yapnr/Dockerfile", "PYTHON_VERSION"),
            platform.python_version(),
        )


if __name__ == "__main__":
    unittest.main()
