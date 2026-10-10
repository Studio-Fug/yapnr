"""Official plugin file handoff with mocked network; never uploads a design."""

import io
import json
import unittest
import zipfile
from unittest.mock import patch

from tests.unit.agent.test_manufacturing_packages import PrototypePackageTest
from yapnr.agent import manufacturing, reviews, vendor_uploads, workspace


class VendorUploadTest(unittest.TestCase):
    setUp = PrototypePackageTest.setUp
    publish = PrototypePackageTest.publish

    def test_approved_upload_is_cached_and_receipt_binds_exact_archive(self):
        handoff = self.prepare()
        with patch.object(
            vendor_uploads,
            "post",
            return_value={"redirect": "https://www.pcbway.com/orderonline.aspx?file=fixture"},
        ) as post:
            with self.assertRaisesRegex(ValueError, "Approve"):
                vendor_uploads.upload(self.root, handoff["id"])
            post.assert_not_called()
            reviews.approve(self.root, [handoff["id"]])
            first = vendor_uploads.upload(self.root, handoff["id"])
            second = vendor_uploads.upload(self.root, handoff["id"])
            self.assertEqual(first, second)
            post.assert_called_once()
            with zipfile.ZipFile(io.BytesIO(post.call_args.args[0])) as archive:
                self.assertIn("PCBWay_bom.csv", archive.namelist())
                self.assertIn("PCBWay_positions.csv", archive.namelist())
                self.assertNotIn("cad/board.kicad_pcb", archive.namelist())
                self.assertIn(
                    b"pos_x,pos_y,rotation,side,designator", archive.read("PCBWay_positions.csv")
                )
            self.assertEqual(first["handoff_artifact"], handoff["id"])
            (self.design / "board.kicad_pcb").write_text("changed")
            with self.assertRaisesRegex(ValueError, "Approve"):
                vendor_uploads.upload(self.root, handoff["id"])

    def prepare(self):
        from yapnr.agent import manufacturing_packages

        return manufacturing_packages.prepare(self.root, self.item["id"], "pcbway", 5, "ENIG")

    def test_vendor_failure_and_untrusted_links_never_claim_success(self):
        handoff = self.prepare()
        reviews.approve(self.root, [handoff["id"]])
        for reply in [
            {"redirect": "https://example.invalid/"},
            {"redirect": "http://www.pcbway.com/"},
            {},
        ]:
            with patch.object(vendor_uploads, "post", return_value=reply), self.assertRaisesRegex(
                ValueError, "not confirmed"
            ):
                vendor_uploads.upload(self.root, handoff["id"])
        with patch.object(
            vendor_uploads, "post", side_effect=TimeoutError()
        ), self.assertRaisesRegex(ValueError, "No automatic retry"):
            vendor_uploads.upload(self.root, handoff["id"])

    def test_upload_archive_tampering_blocks_network(self):
        handoff = self.prepare()
        reviews.approve(self.root, [handoff["id"]])
        release = next(
            r for r in manufacturing.state(self.root)["releases"] if r["artifact"] == handoff["id"]
        )
        file = next(d for d in release["downloads"] if d["label"] == "PCBWay upload package")
        artifact = reviews.artifact(self.root, file["artifact"])
        workspace.relative_file(self.root, artifact["path"]).write_bytes(b"changed")
        with patch.object(vendor_uploads, "post") as post, self.assertRaises(ValueError):
            vendor_uploads.upload(self.root, handoff["id"])
        post.assert_not_called()

    def test_multipart_protocol_and_redirects_are_bounded(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, limit):
                self_limit.append(limit)
                return json.dumps({"redirect": "https://www.pcbway.com/"}).encode()

        self_limit = []
        with patch.object(vendor_uploads.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = Response()
            vendor_uploads.post(
                b"synthetic ZIP", {"board": {"size_mm": [30, 20], "layers": ["F.Cu", "B.Cu"]}}
            )
            args = opener.return_value.open.call_args
            self.assertEqual(args.kwargs["timeout"], 45)
            self.assertIn(b'name="upload[file]"', args.args[0].data)
            self.assertIn(b'name="boardLayer"\r\n\r\n2', args.args[0].data)
            self.assertEqual(self_limit, [65537])
