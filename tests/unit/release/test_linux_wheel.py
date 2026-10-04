"""tools/release/linux_wheel.py: a wheel built elsewhere becomes a Linux wheel with the given
library, tag and a consistent RECORD."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
import zipfile

from tools.release import linux_wheel

LIB = linux_wheel.LIBRARY


class LinuxWheelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.wheel = os.path.join(self.dir, "yapnr-1.2.3-py3-none-macosx_11_0_arm64.whl")
        info = "yapnr-1.2.3.dist-info"
        with zipfile.ZipFile(self.wheel, "w") as zf:
            zf.writestr("yapnr/__init__.py", "")
            zf.writestr(LIB, b"mach-o")
            zf.writestr(
                f"{info}/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-macosx_11_0_arm64\n",
            )
            zf.writestr(f"{info}/RECORD", "stale\n")
        self.lib = os.path.join(self.dir, "libyapnr_fdtd.so")
        with open(self.lib, "wb") as handle:
            handle.write(b"\x7fELF linux")

    def tearDown(self):
        self._tmp.cleanup()

    def test_convert(self):
        out = linux_wheel.convert(self.wheel, self.lib, "arm64", self.dir)
        self.assertEqual(os.path.basename(out), "yapnr-1.2.3-py3-none-manylinux_2_34_aarch64.whl")
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.read(LIB), b"\x7fELF linux")
            wheel = zf.read("yapnr-1.2.3.dist-info/WHEEL").decode()
            self.assertIn("Tag: py3-none-manylinux_2_34_aarch64\n", wheel)
            self.assertNotIn("macosx", wheel)
            record = zf.read("yapnr-1.2.3.dist-info/RECORD").decode().splitlines()
            rows = {line.split(",")[0]: line.split(",")[1:] for line in record}
            self.assertEqual(rows["yapnr-1.2.3.dist-info/RECORD"], ["", ""])
            for name in zf.namelist():
                if name.endswith("RECORD"):
                    continue
                data = zf.read(name)
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                self.assertEqual(rows[name], ["sha256=" + digest.decode(), str(len(data))])

    def test_amd64_tag(self):
        out = linux_wheel.convert(self.wheel, self.lib, "amd64", self.dir)
        self.assertTrue(out.endswith("-py3-none-manylinux_2_34_x86_64.whl"))

    def test_usage(self):
        self.assertEqual(linux_wheel.main([self.wheel]), 2)


if __name__ == "__main__":
    unittest.main()
