import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regression"))
from run import source_inputs


class InputsTest(unittest.TestCase):
    def test_missing_scanner_stops_before_circuit_work(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(FileNotFoundError, "scan_via_proximity"):
                source_inputs(Path(folder))

    def test_scanner_is_part_of_immutable_manifest(self):
        repo = Path(__file__).resolve().parents[3]
        self.assertIn(repo / "hardware/tools/scan_via_proximity.py", source_inputs(repo))


if __name__ == "__main__":
    unittest.main()
