"""Real synthetic files and persistent approval; no model provider is contacted."""
import json
from pathlib import Path
import tempfile
import unittest

from examples.extension_demo import open_application, report_writer, run_demo


class ExtensionDemoTests(unittest.TestCase):
    def test_approved_reopen_writes_one_independently_verified_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "demo"
            result = run_demo(output)
            self.assertEqual(result["states"], ["waiting_approval", "application_closed", "waiting_approval", "completed"])
            self.assertEqual((result["model_calls"], result["tool_calls"], result["observed_writes"]), (2, 1, 1))
            self.assertFalse(result["write_before_approval"])
            expected = {"team": "合成支持组", "daily_counts": [3, 5, 4], "days": 3, "resolved_total": 12}
            self.assertEqual(json.loads((output / "workspace/report.json").read_text(encoding="utf-8")), expected)
            self.assertEqual(result["schema"]["required"], ["daily_counts"])
            self.assertEqual(result["schema"]["properties"]["team"]["default"], "合成支持组")
            trace = json.loads((output / "trace.json").read_text(encoding="utf-8"))
            writes = [event for event in trace if event["type"] == "tool.end"]
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0]["result"]["value"], {"path": "report.json", "resolved_total": 12})
            # Reopening a completed task does not repeat the successful write.
            raw = (output / "workspace/report.json").read_bytes()
            observed = []
            with open_application(output, observed) as app:
                state = app.list_runs()[0]
                self.assertEqual(state["status"], "completed")
                with self.assertRaises(RuntimeError):
                    app.approve(state["id"])
            self.assertEqual(observed, [])
            self.assertEqual((output / "workspace/report.json").read_bytes(), raw)
            self.assertNotIn(str(output), json.dumps(result, ensure_ascii=False))

    def test_rejected_approval_writes_nothing_even_after_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "denied"
            result = run_demo(output, approve=False)
            self.assertTrue(result["passed"])
            self.assertEqual((result["status"], result["error"]), ("failed", "approval_denied"))
            self.assertEqual((result["model_calls"], result["tool_calls"], result["observed_writes"]), (1, 0, 0))
            self.assertFalse((output / "workspace/report.json").exists())

    def test_changed_pending_arguments_do_not_reuse_original_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "workspace").mkdir()
            writes = []
            with open_application(directory, writes) as app:
                run = app.submit("合成报告")
                waiting = app.wait(run["id"])
                self.assertEqual(waiting["status"], "waiting_approval")
            with open_application(directory, writes, autostart=False) as app:
                state = app.get_run(run["id"])
                # Test-only state tampering; the public host should not edit it.
                state["pending"][state["index"]]["arguments"]["daily_counts"] = [99]
                app.store.save(state)
                with self.assertRaisesRegex(RuntimeError, "approval does not match call"):
                    app.approve(run["id"])
                self.assertEqual(app.get_run(run["id"])["status"], "waiting_approval")
            self.assertEqual(writes, [])
            self.assertFalse((directory / "workspace/report.json").exists())

    def test_direct_python_call_is_ordinary_code_not_intercepted_by_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            observed = []
            writer = report_writer(workspace, observed)
            from astra_harness import tool_from_function
            tool = tool_from_function(writer, permissions=frozenset({"reports.write"}), side_effect=True,
                                      requires_approval=True, implementation_version="test-direct-v1")
            self.assertIs(tool.handler, writer)
            result = writer([1, 2], team="合成验证组")
            self.assertEqual(result["resolved_total"], 3)
            self.assertEqual(observed, ["report.json"])
            self.assertTrue((workspace / "report.json").exists())

    def test_existing_output_is_preserved_and_default_output_is_portable(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory / "keep.txt"
            sentinel.write_text("existing user work", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                run_demo(directory)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing user work")
            self.assertEqual(sorted(path.name for path in directory.iterdir()), ["keep.txt"])
        result = run_demo()
        self.assertTrue(result["passed"])
        self.assertFalse(result["retained"])
        self.assertEqual(result["artifact"], "workspace/report.json")


if __name__ == "__main__":
    unittest.main()
