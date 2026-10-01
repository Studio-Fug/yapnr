"""The committed ladder and showcase animations stay small, described and referenced
(docs/animations/).

The large-file hook skips docs/animations/; this check bounds it instead:

- every file there is a .webp, a .gif or one of the two JSON files, and every animation is in
  manifest.json (its ``animations`` or its ``showcases``) with its size and SHA-256;
- a WebP is at most 2.5 MB, a GIF at most 5 MB, the folder at most 30 MB; widths (read from the
  file headers) are 480 to 960 px. The hierarchical showcase's WebP may use 3.5 MB (below);
- no EXIF, XMP or ICC chunk in a WebP and no comment or application block other than the loop
  count in a GIF, so no tool metadata reaches the repository;
- every ladder case (hardware/pnr/regression/designs.py) has an animation and a result in
  ladder-results.json; every showcase file names its cases, and their results agree with the
  ``showcases`` array of ladder-results.json;
- README.md, docs/regression-ladder.md and docs/constraints-and-hierarchy.md show only
  animations that exist, and README.md shows the manifest's README animation and its README
  showcase.

The folder budget was 20 MB for the ladder's nine files (about 12 MB). It is 30 MB since the
showcases (docs/design/constraint-and-hier-animations.md) added four files of about 13 MB: two
side-by-side comparisons and the hierarchical chapters.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import struct
import unittest
from typing import Dict, List

from tools.repo_root import workspace_root

ROOT = workspace_root()
FOLDER = os.path.join(ROOT, "docs", "animations")
JSON_FILES = {"manifest.json", "ladder-results.json"}
BUDGETS = {".webp": 2.5 * 1024 * 1024, ".gif": 5 * 1024 * 1024}
# The hierarchical showcase is over 2.5 MB even at the encoder's last step (640 px, quality 60,
# 80 ms frames): about 31 s in three chapters, with block copper moving through the whole
# top-level placement. The design's showcase-only WebP budget applies to it alone.
SHOWCASE_WEBP_BUDGET = 3.5 * 1024 * 1024
SHOWCASE_BUDGET_FILES = {"showcase-hier-twin-bank.webp"}
TOTAL = 30 * 1024 * 1024
PAGES = (
    "README.md",
    os.path.join("docs", "regression-ladder.md"),
    os.path.join("docs", "constraints-and-hierarchy.md"),
)
REFERENCE = re.compile(r"""(?:src|href)="([^"]*animations/[^"]+)\"""")


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _json(name: str) -> dict:
    return json.loads(_read(os.path.join(FOLDER, name)))


def webp_chunks(data: bytes) -> List[bytes]:
    """The chunk tags of a RIFF WebP file."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP file")
    tags, at = [], 12
    while at + 8 <= len(data):
        tag, size = data[at : at + 4], struct.unpack("<I", data[at + 4 : at + 8])[0]
        tags.append(tag)
        at += 8 + size + (size & 1)
    return tags


def webp_width(data: bytes) -> int:
    if webp_chunks(data)[0] != b"VP8X":
        raise ValueError("an animated WebP starts with VP8X")
    return 1 + int.from_bytes(data[24:27], "little")


def gif_blocks(data: bytes) -> List[str]:
    """The block kinds of a GIF file: "image", "graphic", "comment", "app:<id>", "plain"."""
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("not a GIF file")
    packed = data[10]
    at = 13 + (3 * 2 ** ((packed & 7) + 1) if packed & 0x80 else 0)
    kinds: List[str] = []

    def skip_sub_blocks(at: int) -> int:
        while data[at]:
            at += 1 + data[at]
        return at + 1

    while at < len(data):
        byte = data[at]
        if byte == 0x3B:
            return kinds
        if byte == 0x2C:
            flags = data[at + 9]
            at += 10 + (3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else 0)
            at = skip_sub_blocks(at + 1)
            kinds.append("image")
        elif byte == 0x21:
            label = data[at + 1]
            if label == 0xFF:
                kinds.append("app:" + data[at + 3 : at + 3 + data[at + 2]].decode("latin-1"))
            else:
                kinds.append({0xF9: "graphic", 0xFE: "comment", 0x01: "plain"}.get(label, "?"))
            at = skip_sub_blocks(at + 2)
        else:
            raise ValueError("bad GIF block at byte %d" % at)
    raise ValueError("GIF without a trailer")


def gif_width(data: bytes) -> int:
    return struct.unpack("<H", data[6:8])[0]


def ladder_cases() -> List[str]:
    path = os.path.join(ROOT, "hardware", "pnr", "regression", "designs.py")
    spec = importlib.util.spec_from_file_location("ladder_designs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [design["name"] for design in module.designs()]


class HeaderParserTest(unittest.TestCase):
    def test_gif_blocks(self):
        gif = b"GIF89a" + struct.pack("<HH", 640, 480) + bytes([0x80, 0, 0]) + bytes(6)
        gif += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
        gif += b"\x21\xfe\x02hi\x00"
        gif += b"\x2c" + bytes(8) + b"\x00" + b"\x02\x02\x44\x01\x00" + b"\x3b"
        self.assertEqual(gif_width(gif), 640)
        self.assertEqual(gif_blocks(gif), ["app:NETSCAPE2.0", "comment", "image"])

    def test_webp_chunks(self):
        body = (
            b"VP8X" + struct.pack("<I", 10) + bytes([0x02, 0, 0, 0]) + (799).to_bytes(3, "little")
        )
        body += (599).to_bytes(3, "little") + b"EXIF" + struct.pack("<I", 1) + b"x\x00"
        webp = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body
        self.assertEqual(webp_width(webp), 800)
        self.assertEqual(webp_chunks(webp), [b"VP8X", b"EXIF"])


@unittest.skipUnless(os.path.isdir(FOLDER), "no docs/animations/ in this checkout")
class AnimationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = sorted(os.listdir(FOLDER))
        cls.manifest = _json("manifest.json")
        cls.results = _json("ladder-results.json")
        cls.entries: Dict[str, dict] = {e["file"]: e for e in cls.manifest["animations"]}
        cls.showcases: Dict[str, dict] = {e["file"]: e for e in cls.manifest.get("showcases", [])}

    def test_only_animations_and_their_json(self):
        for name in self.files:
            ext = os.path.splitext(name)[1]
            self.assertTrue(name in JSON_FILES or ext in BUDGETS, name)
        self.assertTrue(JSON_FILES <= set(self.files))

    def test_every_animation_is_in_the_manifest(self):
        animations = [n for n in self.files if n not in JSON_FILES]
        self.assertFalse(set(self.entries) & set(self.showcases))
        self.assertEqual(sorted(set(self.entries) | set(self.showcases)), animations)
        for name in animations:
            data = _read(os.path.join(FOLDER, name))
            entry = self.entries.get(name) or self.showcases[name]
            self.assertEqual(entry["bytes"], len(data), name)
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest(), name)

    def test_budgets(self):
        total = 0
        for name in self.files:
            size = os.path.getsize(os.path.join(FOLDER, name))
            total += size
            ext = os.path.splitext(name)[1]
            if name in SHOWCASE_BUDGET_FILES:
                self.assertLessEqual(size, SHOWCASE_WEBP_BUDGET, name)
            elif ext in BUDGETS:
                self.assertLessEqual(size, BUDGETS[ext], name)
        self.assertLessEqual(total, TOTAL)

    def test_widths_and_no_metadata(self):
        entries = dict(self.entries, **self.showcases)
        for name in entries:
            data = _read(os.path.join(FOLDER, name))
            if name.endswith(".webp"):
                width = webp_width(data)
                extra = set(webp_chunks(data)) & {b"EXIF", b"XMP ", b"ICCP"}
                self.assertEqual(extra, set(), name)
            else:
                width = gif_width(data)
                blocks = set(gif_blocks(data)) - {"image", "graphic", "app:NETSCAPE2.0"}
                self.assertEqual(blocks, set(), name)
            self.assertTrue(480 <= width <= 960, (name, width))
            self.assertEqual(width, entries[name]["width"], name)

    def test_every_ladder_case_is_animated_and_reported(self):
        cases = ladder_cases()
        webp = {e["case"] for e in self.entries.values() if e["file"].endswith(".webp")}
        self.assertEqual(sorted(webp), sorted(cases))
        self.assertEqual([c["case"] for c in self.results["cases"]], sorted(cases))

    def test_the_manifest_agrees_with_the_results(self):
        results = {c["case"]: c for c in self.results["cases"]}
        for entry in self.entries.values():
            result = results[entry["case"]]
            self.assertEqual(entry["result"]["passed"], result["passed"], entry["file"])
            self.assertEqual(entry["result"]["vias"], result["vias"], entry["file"])

    def test_every_showcase_names_its_cases_and_results(self):
        results = {c["case"]: c for c in self.results.get("showcases", [])}
        self.assertTrue(not self.showcases or results)
        for entry in self.showcases.values():
            self.assertIn(entry["kind"], ("compare", "hier"), entry["file"])
            self.assertEqual(len(entry["cases"]), 2 if entry["kind"] == "compare" else 1)
            self.assertEqual(sorted(entry["trace_sha256"]), sorted(entry["cases"]), entry["file"])
            for case in entry["cases"]:
                result = results[case]
                self.assertEqual(entry["results"][case]["passed"], result["passed"], case)
                self.assertEqual(entry["results"][case]["vias"], result["vias"], case)
        self.assertEqual(
            [c["case"] for c in self.results["cases"]], sorted(ladder_cases())
        )  # the ladder's own results stay the eight cases

    def test_pages_show_existing_animations(self):
        for page in PAGES:
            text = _read(os.path.join(ROOT, page)).decode("utf-8")
            base = os.path.dirname(os.path.join(ROOT, page))
            found = REFERENCE.findall(text)
            self.assertTrue(found, page)
            for ref in found:
                self.assertTrue(os.path.isfile(os.path.normpath(os.path.join(base, ref))), ref)
        text = _read(os.path.join(ROOT, "README.md")).decode()
        self.assertIn('src="docs/animations/%s"' % self.manifest["readme"]["file"], text)
        showcase = self.manifest.get("readme_showcase")
        if showcase:
            self.assertIn(showcase["file"], self.showcases)
            self.assertIn('src="docs/animations/%s"' % showcase["file"], text)


if __name__ == "__main__":
    unittest.main()
