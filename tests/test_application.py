import json
from pathlib import Path
import tempfile
import threading
import unittest

from astra_harness.application import Application
from astra_harness.context import fit_state
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.tools import Tool


class ChatModel:
    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelResponse(text="收到")


class WriteModel:
    def complete(self, request):
        if request.messages[-1].role == "tool":
            return ModelResponse(text="已完成")
        return ModelResponse(tool_calls=(ToolCall("write-1", "write", {"value": 5}),))


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "test.sqlite3"
        self.calls = []
        self.write = Tool("write", "写入", ("value",), self.perform,
                          permissions=frozenset({"write"}), side_effect=True, requires_approval=True)

    def perform(self, value):
        self.calls.append(value)
        return value

    def app(self, model=None, **kwargs):
        app = Application(model or ChatModel(), database=self.database, **kwargs)
        self.addCleanup(app.close)
        return app

    def test_session_history_and_idempotency(self):
        model = ChatModel()
        app = self.app(model)
        first = app.submit("第一轮", request_id="request1")
        self.assertEqual(app.submit("第一轮", request_id="request1")["id"], first["id"])
        app.wait(first["id"])
        next_run = app.submit("第二轮", session_id=first["session_id"])
        app.wait(next_run["id"])
        self.assertEqual([m.text for m in model.requests[1].messages if m.role == "user"], ["第一轮", "第二轮"])
        self.assertEqual(len(app.get_session(first["session_id"])["messages"]), 4)
        with self.assertRaises(ValueError):
            app.submit("另一轮", request_id="request1")

    def test_approval_survives_restart_and_does_not_reset_counts(self):
        app = self.app(WriteModel(), tools=(self.write,), permissions=["write"])
        run = app.wait(app.submit("写入")["id"])
        self.assertEqual(run["status"], "waiting_approval")
        self.assertEqual(self.calls, [])
        self.assertEqual(run["tool_calls"], 0)
        app.close()
        reopened = self.app(WriteModel(), tools=(self.write,), permissions=["write"])
        saved = reopened.get_run(run["id"])
        self.assertEqual(saved["model_calls"], 1)
        reopened.approve(run["id"])
        final = reopened.wait(run["id"])
        self.assertEqual((final["status"], final["model_calls"], final["tool_calls"]), ("completed", 2, 1))
        self.assertEqual(self.calls, [5])
        with self.assertRaises(RuntimeError):
            reopened.approve(run["id"])

    def test_denied_or_cancelled_approval_never_executes(self):
        app = self.app(WriteModel(), tools=(self.write,), permissions=["write"])
        run = app.wait(app.submit("写入")["id"])
        with self.assertRaises(RuntimeError):
            app.submit("又一次", session_id=run["session_id"])
        app.approve(run["id"], approved=False)
        other = app.wait(app.submit("写入")["id"])
        app.cancel(other["id"])
        self.assertEqual(app.get_run(other["id"])["status"], "cancelled")
        self.assertEqual(self.calls, [])

    def test_context_rejects_before_model(self):
        model = ChatModel()
        app = self.app(model, max_context_chars=100)
        result = app.wait(app.submit("x" * 200)["id"])
        self.assertEqual((result["status"], result["error"], result["model_calls"]), ("failed", "context_limit", 0))
        self.assertEqual(model.requests, [])

    def test_single_process_lock_and_close(self):
        app = self.app()
        with self.assertRaises(RuntimeError):
            Application(ChatModel(), database=self.database)
        app.close()
        with self.assertRaises(RuntimeError):
            app.submit("closed")
        reopened = self.app()
        self.assertEqual(reopened.wait(reopened.submit("ok")["id"])["status"], "completed")

    def test_pending_checkpoint_resumes(self):
        app = self.app(autostart=False)
        run = app.submit("等待启动")
        app.close()
        reopened = self.app()
        result = reopened.wait(run["id"])
        self.assertEqual((result["status"], result["model_calls"]), ("completed", 1))

    def test_changed_configuration_fails_resume(self):
        app = self.app(autostart=False)
        run = app.submit("等待启动")
        app.close()
        reopened = self.app(system_instruction="changed")
        result = reopened.get_run(run["id"])
        self.assertEqual((result["status"], result["error"]), ("failed", "configuration_changed"))

    def test_inflight_effect_is_unknown_and_blocks_new_runs(self):
        app = self.app(WriteModel(), tools=(self.write,), permissions=["write"], autostart=False)
        run = app.submit("写入")
        run.update(status="running", phase="tool", inflight={"phase": "tool", "side_effect": True})
        app.store.save(run)
        app.close()
        reopened = self.app(WriteModel(), tools=(self.write,), permissions=["write"])
        recovered = reopened.get_run(run["id"])
        self.assertEqual((recovered["status"], recovered["tool_calls"]), ("unknown", 1))
        with self.assertRaises(RuntimeError):
            reopened.submit("再来")
        self.assertEqual(self.calls, [])

    def test_inflight_model_is_not_blindly_retried(self):
        app = self.app(autostart=False)
        run = app.submit("请求中")
        run.update(status="running", inflight={"phase": "model", "side_effect": False})
        app.store.save(run)
        app.close()
        model = ChatModel()
        reopened = self.app(model)
        state = reopened.get_run(run["id"])
        self.assertEqual((state["status"], state["model_calls"]), ("failed", 1))
        self.assertEqual(model.requests, [])

    def test_running_cancel_returns_before_synchronous_model_exits(self):
        started, release = threading.Event(), threading.Event()

        class SlowModel:
            def complete(self, request):
                started.set()
                release.wait(3)
                return ModelResponse(text="迟到结果")

        self.addCleanup(release.set)
        app = self.app(SlowModel())
        run = app.submit("慢调用")
        self.assertTrue(started.wait(2))
        app.cancel(run["id"])
        result = app.wait(run["id"], timeout=2)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["output"], "")
        release.set()

    def test_event_cursor_is_monotonic_and_replay_is_read_only(self):
        app = self.app()
        run = app.wait(app.submit("你好")["id"])
        events = app.events(run["id"])
        self.assertGreater(len(events), 2)
        self.assertEqual([e["seq"] for e in events], sorted(set(e["seq"] for e in events)))
        self.assertEqual(app.events(run["id"], events[-1]["seq"]), [])
        self.assertEqual(len(app.list_runs()), 1)

    def test_invalid_identifiers_are_rejected_before_database(self):
        app = self.app()
        for options in ({"request_id": "\ud800"}, {"session_id": "\ud800"}, {"session_id": 2}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                app.submit("hello", **options)
        self.assertEqual(app.list_runs(), [])


class ContextTests(unittest.TestCase):
    def test_drop_entire_old_turn_and_keep_system_and_current_pairs(self):
        state = {"messages": [
            {"role": "system", "text": "规则"}, {"role": "user", "text": "old" * 1000},
            {"role": "assistant", "text": "old reply"}, {"role": "user", "text": "current"},
            {"role": "assistant", "tool_calls": [{"call_id": "a"}]},
            {"role": "tool", "tool_result": {"call_id": "a", "value": 5}},
        ]}
        fit_state(state, [], 1000)
        self.assertEqual([m["role"] for m in state["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(state["context"]["dropped_turns"], 1)
        self.assertEqual(state["turn_start"], 1)

    def test_oversized_current_context_and_schema_are_not_silently_dropped(self):
        state = {"messages": [{"role": "user", "text": "任务"}]}
        with self.assertRaises(ValueError):
            fit_state(state, [{"schema": "x" * 2000}], 1000)
        self.assertEqual(state["messages"][0]["text"], "任务")
