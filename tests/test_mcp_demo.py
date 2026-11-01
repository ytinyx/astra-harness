"""Real self-authored STDIO children, synthetic receipts and persistent approval."""
import json
from pathlib import Path
import tempfile
import unittest

from astra_harness.contracts import Message, ModelRequest, ToolResult
from examples.mcp_services_demo import DemoModel, child_records, open_host, prepare, recorded_values, run_demo


class MCPDemoTests(unittest.TestCase):
    def test_real_children_and_pending_second_approval_survive_host_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "demo"
            result = run_demo(directory)
            self.assertTrue(result["passed"])
            self.assertEqual((result["status"], result["model_calls"], result["tool_calls"]), ("completed", 4, 3))
            self.assertEqual(result["output"], "Verified tool total: 18")
            self.assertEqual(result["recorded_values"], {"alpha": [7], "beta": [11], "broken": []})
            self.assertEqual(result["child_instances"], {"alpha": 2, "beta": 2, "broken": 2})
            self.assertFalse(result["first_call_replayed"])
            self.assertTrue(result["second_approval_preserved"])
            self.assertEqual(result["used_budget_before_and_after_reopen"], {"model_calls": 2, "tool_calls": 1})
            records = child_records(directory)
            for name, value in (("alpha", 7), ("beta", 11)):
                calls = [item for item in records[name] if item["event"] == "recorded"]
                self.assertEqual([{key: item[key] for key in ("service", "value", "receipt")} for item in calls],
                                 [{"service": name, "value": value, "receipt": 1}])
            self.assertEqual([item for item in records["broken"] if item["event"] == "recorded"], [])
            trace = json.loads((directory / "trace.json").read_text(encoding="utf-8"))
            ends = [item["result"] for item in trace if item["type"] == "tool.end"]
            self.assertEqual([item["name"] for item in ends], ["mcp.alpha.record", "mcp.beta.record", "local.total"])
            self.assertEqual(ends[-1]["value"], {"total": 18, "count": 2})
            self.assertNotIn(str(directory), json.dumps(result))

    def test_denied_second_service_does_not_write_or_replay_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "denied"
            result = run_demo(directory, approve_second=False)
            self.assertTrue(result["passed"])
            self.assertEqual((result["status"], result["error"]), ("failed", "approval_denied"))
            self.assertEqual((result["model_calls"], result["tool_calls"]), (2, 1))
            self.assertEqual(recorded_values(directory), {"alpha": [7], "beta": [], "broken": []})

    def test_service_permissions_do_not_cross_authorize(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "permission"
            prepare(directory)
            with open_host(directory, permissions=("demo.total", "mcp.beta.call")) as (app, manager):
                self.assertEqual({item["name"]: item["state"] for item in manager.status()},
                                 {"alpha": "ready", "beta": "ready", "broken": "failed"})
                run = app.submit("synthetic quantities")
                final = app.wait(run["id"])
                # The executor returns the rejection to the model. The scripted
                # plan rejects that failed ToolResult instead of claiming success;
                # the denied attempt still consumes the existing tool-call slot.
                self.assertEqual((final["status"], final["error"]), ("failed", "model_error"))
                rejected = [event["result"] for event in app.events(run["id"]) if event["type"] == "tool.end"]
                self.assertEqual([(item["name"], item["error_code"]) for item in rejected],
                                 [("mcp.alpha.record", "permission_denied")])
                self.assertEqual(final["tool_calls"], 1)
                self.assertEqual(recorded_values(directory), {"alpha": [], "beta": [], "broken": []})

    def test_model_consumes_actual_results_instead_of_hardcoding_expected_total(self):
        alpha = ToolResult("a", "mcp.alpha.record", {"structuredContent": {"value": 2}})
        beta = ToolResult("b", "mcp.beta.record", {"structuredContent": {"value": 9}})
        request = ModelRequest((Message("tool", tool_result=alpha), Message("tool", tool_result=beta)), ())
        call = DemoModel().complete(request).tool_calls[0]
        self.assertEqual((call.name, call.arguments), ("local.total", {"values": [2, 9]}))
        local = ToolResult("c", "local.total", {"total": 11, "count": 2})
        request = ModelRequest((*request.messages, Message("tool", tool_result=local)), ())
        self.assertEqual(DemoModel().complete(request).text, "Verified tool total: 11")
        failure = ToolResult("a", "mcp.alpha.record", error_code="tool_error")
        with self.assertRaises(ValueError):
            DemoModel().complete(ModelRequest((Message("tool", tool_result=failure),), ()))

    def test_existing_directory_is_preserved_and_default_run_is_portable(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory / "keep.txt"
            sentinel.write_text("existing work", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                run_demo(directory)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing work")
            self.assertEqual([item.name for item in directory.iterdir()], ["keep.txt"])
        result = run_demo()
        self.assertTrue(result["passed"])
        self.assertFalse(result["retained"])
        self.assertEqual(result["config"], "mcp.json")
        self.assertEqual(result["receipt_files"], ["children/alpha.jsonl", "children/beta.jsonl", "children/broken.jsonl"])


if __name__ == "__main__":
    unittest.main()
