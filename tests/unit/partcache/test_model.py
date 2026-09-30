"""Part records: file names, .ato facts, content ids and upload validation (synthetic data)."""

from __future__ import annotations

import unittest

from yapnr.frontends.atopile import testing
from yapnr.partcache import model

SHA = "a" * 64


def request(**overrides):
    doc = {
        "name": testing.SYNTHETIC_PART,
        "files": [
            {"path": f"{testing.SYNTHETIC_PART}.ato", "sha256": SHA, "size": 10},
            {"path": "SYN_R0603.kicad_mod", "sha256": "b" * 64, "size": 20},
            {"path": "SR1K.kicad_sym", "sha256": "c" * 64, "size": 30},
        ],
        "provenance": {"source": "self"},
        "licence": {"spdx": "CC0-1.0"},
    }
    doc.update(overrides)
    return doc


class FileNameTest(unittest.TestCase):
    def test_plain_part_files(self):
        for name in (
            "A.ato",
            "LTC4421HUHE#PBF.kicad_sym",
            "SOT-23_L2.9-W1.3.kicad_mod",
            "Model (rev 2).STEP",
            "NOTES.md",
            "a,b+c.wrl",
        ):
            self.assertEqual(model.check_file_path(name), name)

    def test_refused_names(self):
        for name in (
            "../x.ato",
            "a/b.ato",
            "a\\b.ato",
            ".hidden.ato",
            "x.py",
            "x.html",
            "",
            "x.ato\n",
            "..",
            "x" * 200 + ".ato",
        ):
            with self.assertRaises(model.InvalidPart, msg=repr(name)):
                model.check_file_path(name)

    def test_part_names(self):
        model.check_name("Texas_Instruments_TPS552882")
        for name in ("a-b", "a.b", "", "a/b", "x" * 129):
            with self.assertRaises(model.InvalidPart, msg=name):
                model.check_name(name)


class AtoFactsTest(unittest.TestCase):
    def test_synthetic_part(self):
        facts = model.ato_facts(testing.SYNTHETIC_PART, testing.part_ato())
        self.assertEqual(facts["lcsc"], testing.SYNTHETIC_LCSC)
        self.assertEqual(facts["manufacturer"], "yapnr-synthetic")
        self.assertEqual(facts["mpn"], "SR1K")
        self.assertEqual(facts["references"], ["SYN_R0603.kicad_mod", "SR1K.kicad_sym"])
        self.assertIsNone(facts["generated_from"])

    def test_generated_part_and_escapes(self):
        text = (
            testing.part_ato()
            .replace(
                "    trait is_atomic_part",
                '    trait is_auto_generated<system="ato_part", source="easyeda:C990000001",'
                ' date="2026-01-01T00:00:00+00:00", checksum="00">\n    trait is_atomic_part',
            )
            .replace(
                'manufacturer="yapnr-synthetic", partnumber', 'manufacturer="A \\"B\\"", partnumber'
            )
        )
        facts = model.ato_facts(testing.SYNTHETIC_PART, text)
        self.assertEqual(facts["generated_from"], "easyeda:C990000001")
        self.assertEqual(facts["manufacturer"], 'A "B"')

    def test_a_part_file_needs_a_component_and_an_atomic_part(self):
        # A hand-written part may name its component freely.
        facts = model.ato_facts("Other_Name", testing.part_ato())
        self.assertEqual(facts["lcsc"], testing.SYNTHETIC_LCSC)
        with self.assertRaises(model.InvalidPart):
            model.ato_facts(testing.SYNTHETIC_PART, "component X_package:\n    pass\n")
        with self.assertRaises(model.InvalidPart):
            model.ato_facts(testing.SYNTHETIC_PART, "module M:\n    trait is_atomic_part<>\n")

    def test_a_missing_model_is_a_warning(self):
        text = testing.part_ato().replace(
            'symbol="SR1K.kicad_sym"', 'symbol="SR1K.kicad_sym", model="m.step"'
        )
        manifest = model.build_manifest(model.check_manifest_request(request()), text)
        self.assertEqual(len(manifest["warnings"]), 1)
        self.assertIn("m.step", manifest["warnings"][0])


class ManifestTest(unittest.TestCase):
    def test_id_depends_on_content_not_order_or_metadata(self):
        a = request()
        b = request(files=list(reversed(a["files"])), provenance={"source": "elsewhere"})
        self.assertEqual(model.part_id(a["name"], a["files"]), model.part_id(b["name"], b["files"]))
        c = request()
        c["files"][0] = {**c["files"][0], "sha256": "d" * 64}
        self.assertNotEqual(
            model.part_id(a["name"], a["files"]), model.part_id(c["name"], c["files"])
        )

    def test_request_validation(self):
        model.check_manifest_request(request())
        bad = [
            request(name="x/y"),
            request(files=[]),
            request(files=request()["files"][1:]),  # no .ato
            request(files=request()["files"] + [request()["files"][0]]),  # repeated
            request(files=[{**request()["files"][0], "sha256": "XYZ"}]),
            request(files=[{**request()["files"][0], "size": -1}]),
            request(provenance={}),
            request(licence={"notes": "no spdx"}),
            request(licence={"spdx": "MIT", "other": "x"}),
            {**request(), "extra": 1},
        ]
        for doc in bad:
            with self.assertRaises(model.InvalidPart, msg=doc):
                model.check_manifest_request(doc)
        with self.assertRaises(model.InvalidPart):
            model.check_manifest_request(request(), max_file_bytes=15)

    def test_build_manifest_checks_references(self):
        manifest = model.build_manifest(model.check_manifest_request(request()), testing.part_ato())
        self.assertEqual(manifest["lcsc"], testing.SYNTHETIC_LCSC)
        self.assertEqual(model.check_manifest(manifest), manifest)
        missing = request(files=request()["files"][:2])
        with self.assertRaisesRegex(model.InvalidPart, "SR1K.kicad_sym"):
            model.build_manifest(model.check_manifest_request(missing), testing.part_ato())
        tampered = {**manifest, "id": "0" * 64}
        with self.assertRaises(model.InvalidPart):
            model.check_manifest(tampered)

    def test_distribution(self):
        for value in ("shareable", "local-only"):
            doc = request(licence={"spdx": "NOASSERTION", "distribution": value})
            self.assertEqual(model.check_manifest_request(doc)["licence"]["distribution"], value)
        with self.assertRaisesRegex(model.InvalidPart, "distribution"):
            model.check_manifest_request(request(licence={"spdx": "MIT", "distribution": "x"}))
        self.assertTrue(model.is_local_only({"licence": {"distribution": "local-only"}}))
        self.assertFalse(model.is_local_only({"licence": {"spdx": "MIT"}}))


class FileContentTest(unittest.TestCase):
    def test_files_must_match_their_type(self):
        good = {
            "P.ato": b"component P:\n",
            "fp.kicad_mod": testing.FOOTPRINT.encode(),
            "old.kicad_mod": b"(module old (layer F.Cu))",
            "s.kicad_sym": testing.SYMBOL.encode(),
            "Model.STEP": b"ISO-10303-21;\nHEADER;",
            "bom.stp": b"\xef\xbb\xbf\r\nISO-10303-21;",
            "body.wrl": b"#VRML V2.0 utf8\n",
            "NOTES.md": "notes \u00b5\n".encode(),
            "empty.txt": b"",
        }
        for path, data in good.items():
            model.check_file_content(path, data)
        bad = {
            "P.ato": b"\xff\xfe not utf-8",
            "fp.kicad_mod": b"<html><script>alert(1)</script>",
            "s.kicad_sym": b"(footprint x)",
            "Model.step": b"<html>ISO-10303-21",
            "body.wrl": b"MZ\x90\x00",
            "NOTES.md": b"\x89PNG\r\n",
        }
        for path, data in bad.items():
            with self.assertRaises(model.InvalidPart, msg=path):
                model.check_file_content(path, data)
        self.assertTrue(model.needs_full_content("x.kicad_sym"))
        self.assertFalse(model.needs_full_content("x.STEP"))


if __name__ == "__main__":
    unittest.main()
