"""Host-selected operations use the existing durable execution path, not an LLM."""
import json
from pathlib import Path
import tempfile
import unittest

from astra_harness.application import Application
from astra_harness.contracts import ModelResponse
from astra_harness.tools import Tool, ToolExecutionUnknown


class PoisonModel:
    def __init__(self):
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise AssertionError("a manual tool run reached the model")


class ToolRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "runs.sqlite3"
        self.model = PoisonModel()
        self.calls = []
        self.read = Tool("read", "read", ("value",), self.perform)
        self.write = Tool("write", "write", ("value",), self.perform,
                          permissions=frozenset({"write"}), side_effect=True, requires_approval=True)

    def perform(self, value):
        self.calls.append(value)
        return value

    def app(self, **kwargs):
        options = {"tools": (self.read, self.write), "permissions": ["write"]}
        options.update(kwargs)
        app = Application(self.model, database=self.database, **options)
        self.addCleanup(app.close)
        return app

    def test_direct_read_has_actual_result_trace_and_no_model(self):
        app = self.app()
        run = app.wait(app.submit_tool("read", {"value": 42}, request_id="read-once")["id"])
        self.assertEqual((run["kind"], run["status"], run["model_calls"], run["tool_calls"]),
                         ("tool", "completed", 0, 1))
        self.assertEqual(run["tool_result"]["value"], 42)
        self.assertEqual(json.loads(run["output"]), 42)
        self.assertEqual(self.calls, [42])
        self.assertEqual(self.model.calls, 0)
        events = app.events(run["id"])
        self.assertTrue(any(event["type"] == "tool.end" for event in events))
        self.assertFalse(any(event["type"].startswith("model.") for event in events))

    def test_approval_survives_restart_and_runs_once_without_model(self):
        app = self.app()
        run = app.wait(app.submit_tool("write", {"value": 7}, request_id="approved")["id"])
        self.assertEqual(run["status"], "waiting_approval")
        self.assertEqual((run["model_calls"], run["tool_calls"]), (0, 0))
        self.assertEqual(self.calls, [])
        app.close()
        reopened = self.app()
        self.assertEqual(reopened.get_run(run["id"])["status"], "waiting_approval")
        reopened.approve(run["id"])
        done = reopened.wait(run["id"])
        self.assertEqual((done["status"], done["model_calls"], done["tool_calls"]), ("completed", 0, 1))
        self.assertEqual(reopened.submit_tool("write", {"value": 7}, request_id="approved")["id"], run["id"])
        self.assertEqual(self.calls, [7])
        self.assertEqual(self.model.calls, 0)

    def test_request_identity_rejects_argument_tool_and_kind_changes(self):
        app = self.app(autostart=False)
        first = app.submit_tool("read", {"value": 1}, request_id="bound")
        self.assertEqual(app.submit_tool("read", {"value": 1}, request_id="bound")["id"], first["id"])
        for name, arguments in (("read", {"value": 2}), ("write", {"value": 1})):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ValueError):
                app.submit_tool(name, arguments, request_id="bound")
        # Python equality collapses these values; the persisted JSON contract
        # must not reuse an approval/run for a different argument type.
        for value in (True, 1.0):
            with self.subTest(changed_type=type(value)), self.assertRaises(ValueError):
                app.submit_tool("read", {"value": value}, request_id="bound")
        with self.assertRaises(ValueError):
            app.submit(first["prompt"], request_id="bound")
        agent = app.submit("工具操作：read", request_id="agent-bound")
        with self.assertRaises(ValueError):
            app.submit_tool("read", {"value": 1}, request_id="agent-bound")
        self.assertNotEqual(agent["id"], first["id"])

    def test_canonical_key_order_is_same_request_and_input_is_detached(self):
        app = self.app(autostart=False)
        args = {"value": {"a": [1, 2], "b": 3}}
        first = app.submit_tool("read", args, request_id="canonical")
        args["value"]["a"].append(99)
        same = app.submit_tool("read", {"value": {"b": 3, "a": [1, 2]}}, request_id="canonical")
        self.assertEqual(first["id"], same["id"])
        self.assertEqual(same["operation"]["arguments"]["value"]["a"], [1, 2])

    def test_invalid_schema_unknown_tool_and_permission_are_safe_results(self):
        app = self.app(permissions=[])
        cases = [("read", {"bad": 3}, "invalid_arguments"),
                 ("not_registered", {}, "unknown_tool"),
                 ("write", {"value": 2}, "permission_denied")]
        for name, arguments, error in cases:
            with self.subTest(name=name):
                run = app.wait(app.submit_tool(name, arguments)["id"])
                self.assertEqual((run["status"], run["error"], run["model_calls"]), ("failed", error, 0))
                self.assertEqual(run["tool_result"]["error_code"], error)
        self.assertEqual(self.calls, [])

    def test_refused_approval_and_cancel_never_execute(self):
        app = self.app()
        denied = app.wait(app.submit_tool("write", {"value": 2})["id"])
        app.approve(denied["id"], approved=False)
        self.assertEqual(app.get_run(denied["id"])["error"], "approval_denied")
        cancelled = app.wait(app.submit_tool("write", {"value": 3})["id"])
        app.cancel(cancelled["id"])
        self.assertEqual(app.get_run(cancelled["id"])["status"], "cancelled")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.model.calls, 0)

    def test_pause_resume_keeps_same_tool_and_requires_approval(self):
        app = self.app(autostart=False)
        run = app.submit_tool("write", {"value": 5})
        app.pause(run["id"])
        self.assertEqual(app.get_run(run["id"])["status"], "paused")
        app.resume(run["id"])
        app.start()
        waiting = app.wait(run["id"])
        self.assertEqual(waiting["status"], "waiting_approval")
        app.approve(run["id"])
        self.assertEqual(app.wait(run["id"])["tool_result"]["value"], 5)
        self.assertEqual(self.calls, [5])

    def test_context_and_tool_budget_stop_before_handler(self):
        app = self.app(max_context_chars=10)
        run = app.wait(app.submit_tool("read", {"value": 4})["id"])
        self.assertEqual((run["status"], run["error"], run["tool_calls"]), ("failed", "context_limit", 0))
        app.close()
        app = self.app(limits={"max_tool_calls": 0})
        run = app.wait(app.submit_tool("read", {"value": 4})["id"])
        self.assertEqual((run["status"], run["error"], run["tool_calls"]), ("failed", "tool_limit", 0))
        self.assertEqual(self.calls, [])

    def test_unknown_write_blocks_later_operations(self):
        def uncertain(value):
            self.calls.append(value)
            raise ToolExecutionUnknown("synthetic ambiguous completion")
        tool = Tool("uncertain", "uncertain", ("value",), uncertain, side_effect=True)
        app = self.app(tools=(tool, self.read))
        run = app.wait(app.submit_tool("uncertain", {"value": 8})["id"])
        self.assertEqual(run["status"], "unknown")
        self.assertEqual(self.calls, [8])
        with self.assertRaises(RuntimeError):
            app.submit_tool("read", {"value": 9})
        self.assertEqual(self.model.calls, 0)

    def test_business_failure_stays_visible_without_model_inference(self):
        tool = Tool("business", "business", (), lambda: {"ok": False, "error_code": "source_changed"},
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False})
        app = self.app(tools=(tool,))
        run = app.wait(app.submit_tool("business", {})["id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["tool_result"]["value"], {"ok": False, "error_code": "source_changed"})
        self.assertEqual(self.model.calls, 0)

    def test_non_json_and_overlarge_arguments_rejected_without_saved_run(self):
        app = self.app(autostart=False)
        for arguments in ({"value": float("nan")}, {1: 2}, {"value": {1: 2}}, {"value": "x" * 262145}):
            with self.subTest(kind=str(type(arguments))), self.assertRaises((ValueError, TypeError)):
                app.submit_tool("read", arguments)
        self.assertEqual(app.list_runs(), [])

    def test_manual_result_is_not_promoted_to_assistant_chat_history(self):
        class RecordingModel:
            def __init__(self):
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                return ModelResponse(text="real model answer")

        self.model = RecordingModel()
        app = self.app()
        first = app.wait(app.submit("first chat")["id"])
        direct = app.wait(app.submit_tool("read", {"value": 42}, session_id=first["session_id"])["id"])
        self.assertEqual(direct["tool_result"]["value"], 42)
        self.assertEqual(len(app.get_session(first["session_id"])["messages"]), 2)
        self.assertTrue(any(message["role"] == "tool" for message in direct["messages"]))
        app.wait(app.submit("second chat", session_id=first["session_id"])["id"])
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual([message.text for message in self.model.requests[-1].messages if message.role != "system"],
                         ["first chat", "real model answer", "second chat"])

    def test_active_session_archived_session_and_closed_host_constraints_remain(self):
        app = self.app(autostart=False)
        run = app.submit_tool("read", {"value": 2})
        with self.assertRaises(RuntimeError):
            app.submit_tool("read", {"value": 3}, session_id=run["session_id"])
        app.cancel(run["id"])
        app.update_session(run["session_id"], archived=True)
        with self.assertRaises(RuntimeError):
            app.submit_tool("read", {"value": 3}, session_id=run["session_id"])
        app.close()
        with self.assertRaises(RuntimeError):
            app.submit_tool("read", {"value": 3})


if __name__ == "__main__":
    unittest.main()
