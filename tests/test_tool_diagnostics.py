import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from astra_harness.application import Application
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.engine import Engine
from astra_harness.tools import Tool, ToolExecutor, ToolRegistry, ToolUnavailable, ToolExecutionUnknown


SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class NoModel:
    def complete(self, request):
        raise AssertionError("manual tool operation must not call a model")


def raising(error):
    def handler():
        raise error
    return handler


def tool(handler):
    return Tool("fixture.run", "fixture", (), handler, input_schema=SCHEMA, side_effect=True)


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_exceptions_keep_default_codes_and_hide_messages(self):
        for error, code in [(ToolUnavailable("secret-url-token"), "tool_unavailable"),
                            (ToolExecutionUnknown("secret-url-token"), "execution_unknown")]:
            executor = ToolExecutor(ToolRegistry((tool(raising(error)),)))
            call = ToolCall("one", "fixture.run", {})
            for result in [executor.execute(call), await executor.aexecute(call)]:
                self.assertEqual(result.error_code, code)
                self.assertIsNone(result.value)
                self.assertNotIn("secret-url-token", json.dumps(asdict(result)))

    async def test_sync_and_async_handlers_preserve_detached_explicit_details(self):
        details = {"reason": "stale_file", "applied": False, "backup_id": "a" * 32}
        error = ToolUnavailable("private path", code="edit_rejected", details=details)
        details["reason"] = "changed by caller"
        async def async_handler():
            raise error
        call = ToolCall("one", "fixture.run", {})
        sync = ToolExecutor(ToolRegistry((tool(raising(error)),))).execute(call)
        asynchronous = await ToolExecutor(ToolRegistry((tool(async_handler),))).aexecute(call)
        self.assertEqual(sync, asynchronous)
        self.assertEqual(sync.error_code, "edit_rejected")
        self.assertEqual(sync.value["reason"], "stale_file")
        error.details["reason"] = "changed after result"
        self.assertEqual(sync.value["reason"], "stale_file")

    async def test_non_json_recursive_nonfinite_and_oversized_details_are_omitted(self):
        cycle = {}; cycle["cycle"] = cycle
        variants = [cycle, {"opaque": object()}, {"nan": float("nan")}, {"number": 2 ** 300},
                    {"text": "x" * 65537}, {"text": "\ud800"}, {"items": list(range(129))},
                    {str(i): "\0" * 65536 for i in range(3)}]
        for details in variants:
            executor = ToolExecutor(ToolRegistry((tool(raising(ToolExecutionUnknown("sensitive", details=details))),)))
            result = await executor.aexecute(ToolCall("one", "fixture.run", {}))
            self.assertEqual(result.error_code, "execution_unknown")
            self.assertEqual(result.value, {"diagnostics_omitted": True})
            self.assertLess(len(json.dumps(asdict(result))), 200)

    async def test_valid_bounded_command_output_and_false_cleanup_are_not_discarded(self):
        details = {"reason": "output_limit", "stdout": "\0" * 8192, "stderr": "截断", "output_bytes": 8195,
                   "truncated": True, "cleanup_complete": False}
        executor = ToolExecutor(ToolRegistry((tool(raising(ToolExecutionUnknown(details=details))),)))
        result = executor.execute(ToolCall("one", "fixture.run", {}))
        self.assertEqual(result.value, details)
        self.assertFalse(result.value["cleanup_complete"])

    async def test_codes_are_stable_identifiers_and_plain_exceptions_never_export_text(self):
        for code in ["private/path", "A", "", "x" * 65, None]:
            with self.assertRaises(ValueError):
                ToolUnavailable("private", code=code)
        result = ToolExecutor(ToolRegistry((tool(raising(ValueError("private traceback"))),))).execute(ToolCall("one", "fixture.run", {}))
        self.assertEqual(result.error_code, "tool_error")
        self.assertIsNone(result.value)

    async def test_unknown_saves_observation_before_terminal_without_advancing_or_model_message(self):
        events, executed = [], []
        def handler():
            executed.append(True)
            raise ToolExecutionUnknown("never expose", details={"reason": "timeout", "cleanup_complete": False})
        engine = Engine(NoModel(), (tool(handler),), emit=events.append)
        state = engine.create_tool("fixture.run", {})
        pending = json.loads(json.dumps(state["pending"]))
        messages = json.loads(json.dumps(state["messages"]))
        await engine.advance(state)
        self.assertEqual(state["status"], "unknown")
        self.assertEqual(state["pending"], pending)
        self.assertEqual(state["index"], 0)
        self.assertEqual(json.loads(json.dumps(state["messages"])), messages)
        self.assertEqual(state["tool_result"]["value"]["reason"], "timeout")
        self.assertEqual([event["type"] for event in events], ["tool.start", "tool.end", "run.end"])
        self.assertEqual(events[1]["result"], state["tool_result"])
        await engine.advance(state)
        self.assertEqual(len(executed), 1)

    async def test_agent_unknown_observations_do_not_become_tool_history_or_model_retry(self):
        requests = []
        class Model:
            def complete(self, request):
                requests.append(request)
                return ModelResponse(tool_calls=(ToolCall("one", "fixture.run", {}),))
        engine = Engine(Model(), (tool(raising(ToolExecutionUnknown(details={"reason": "cancelled"}))),))
        state = engine.create("synthetic task")
        await engine.advance(state); await engine.advance(state); await engine.advance(state)
        self.assertEqual(state["status"], "unknown")
        self.assertEqual(len(requests), 1)
        self.assertFalse(any(message["role"] == "tool" for message in state["messages"]))

    async def test_known_rejection_fails_direct_run_but_domain_false_keeps_existing_semantics(self):
        engine = Engine(NoModel(), (tool(raising(ToolUnavailable(code="edit_rejected", details={"applied": False}))),))
        state = engine.create_tool("fixture.run", {})
        await engine.advance(state)
        self.assertEqual((state["status"], state["error"]), ("failed", "edit_rejected"))
        self.assertEqual(state["tool_result"]["value"], {"applied": False})
        engine = Engine(NoModel(), (tool(lambda: {"ok": False, "exit_code": 7}),))
        state = engine.create_tool("fixture.run", {})
        await engine.advance(state)
        self.assertEqual(state["status"], "completed")
        self.assertFalse(state["tool_result"]["value"]["ok"])


class PersistentDiagnosticTests(unittest.TestCase):
    def test_unknown_result_and_event_survive_restart_without_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runs.sqlite3"
            calls = []
            def handler():
                calls.append(True)
                raise ToolExecutionUnknown(details={"reason": "timeout", "cleanup_complete": False})
            tools = (tool(handler),)
            app = Application(NoModel(), database=database, tools=tools)
            try:
                run = app.wait(app.submit_tool("fixture.run", {})["id"])
                self.assertEqual(run["status"], "unknown")
                self.assertEqual(run["tool_result"]["value"]["reason"], "timeout")
                self.assertEqual([item["type"] for item in app.events(run["id"])].count("tool.end"), 1)
            finally:
                app.close()
            app = Application(NoModel(), database=database, tools=tools)
            try:
                self.assertEqual(app.get_run(run["id"])["tool_result"], run["tool_result"])
                self.assertEqual(calls, [True])
            finally:
                app.close()
