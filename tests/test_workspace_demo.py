import json
from pathlib import Path
import tempfile
import unittest

from examples.workspace_demo import run_demo


class WorkspaceDemoTests(unittest.TestCase):
    def test_real_host_routes_approval_and_persistent_selection_to_the_original_directory(self):
        with tempfile.TemporaryDirectory(prefix="astra-workspace-check-") as directory:
            target = Path(directory) / "retained"
            result = run_demo(target)
            self.assertTrue(result["passed"])
            self.assertEqual((result["model_calls"], result["provider_requests"]), (0, 0))
            self.assertEqual((target / "工作区 A/value.txt").read_bytes(), b"A approved")
            self.assertEqual((target / "original B preserved/value.txt").read_bytes(), b"B approved")
            self.assertFalse((target / "工作区 B/must-not-exist.txt").exists())
            runs = json.loads((target / "runs.json").read_text(encoding="utf-8"))
            self.assertEqual(len(runs), 4)
            self.assertEqual(sum(run["status"] == "completed" for run in runs), 3)
            self.assertEqual(sum(run["tool_calls"] for run in runs), 3)

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="astra-workspace-preserve-") as directory:
            target = Path(directory)
            marker = target / "keep.txt"
            marker.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                run_demo(target)
            self.assertEqual(marker.read_bytes(), b"keep")
            self.assertEqual([p.name for p in target.iterdir()], ["keep.txt"])


if __name__ == "__main__":
    unittest.main()
