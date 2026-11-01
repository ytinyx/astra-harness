import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from astra_harness.application import Application
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.engine import DEFAULT_LIMITS, Engine, normalize_limits
from astra_harness.tools import Tool


class PlannedModel:
    def __init__(self, calls=(), gated=False):
        self.calls = calls
        self.requests = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not gated:
            self.release.set()

    def complete(self, request):
        self.requests.append(request)
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test model was not released")
        if request.messages[-1].role == "tool" or not self.calls:
            return ModelResponse(text="done")
        return ModelResponse(tool_calls=self.calls)


class PauseResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "runs.sqlite3"
        self.executed = []
        self.tool = Tool("write", "record a value", ("value",), self.perform,
                         side_effect=True, permissions=frozenset({"write"}))
        self.call = ToolCall("first", "write", {"value": 1})

    def perform(self, value):
        self.executed.append(value)
        return value

    def app(self, model=None, **options):
        app = Application(model or PlannedModel(), database=self.database, **options)
        self.addCleanup(app.close)
        return app

    def gate(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        return entered, release

    def event_types(self, app, identifier):
        return [event["type"] for event in app.events(identifier)]

    def test_pending_pause_is_durable_idempotent_and_does_not_occupy_worker(self):
        model = PlannedModel()
        app = self.app(model, autostart=False)
        run = app.submit("first")
        paused = app.pause(run["id"])
        self.assertEqual((paused["status"], paused["resume_status"], paused["pause_requested"]),
                         ("paused", "pending", False))
        self.assertEqual(app.pause(run["id"]), paused)
        for operation in (lambda: app.submit("again", session_id=run["session_id"]),
                          lambda: app.delete_session(run["session_id"]),
                          lambda: app.update_session(run["session_id"], archived=True)):
            with self.assertRaises(RuntimeError):
                operation()
        app.start()
        self.assertEqual(app.wait(run["id"])["status"], "paused")
        self.assertEqual(model.requests, [])
        other = app.wait(app.submit("independent session")["id"])
        self.assertEqual(other["status"], "completed")
        app.resume(run["id"])
        self.assertEqual(app.wait(run["id"])["status"], "completed")
        kinds = self.event_types(app, run["id"])
        self.assertEqual((kinds.count("run.paused"), kinds.count("run.resumed")), (1, 1))

    def test_pause_during_model_stops_before_first_tool_and_merges_request(self):
        model = PlannedModel((self.call,), gated=True)
        app = self.app(model, tools=(self.tool,), permissions=["write"])
        self.addCleanup(model.release.set)
        run = app.submit("write")
        self.assertTrue(model.entered.wait(2))
        requested = app.pause(run["id"])
        self.assertEqual((requested["status"], requested["pause_requested"]), ("running", True))
        app.pause(run["id"])
        app.resume(run["id"])  # 尚未到达暂停边界，不能撤销或跳过暂停请求。
        model.release.set()
        paused = app.wait(run["id"])
        self.assertEqual((paused["status"], paused["phase"], paused["model_calls"], paused["tool_calls"]),
                         ("paused", "tool", 1, 0))
        self.assertFalse(paused["pause_requested"])
        self.assertEqual(self.executed, [])
        self.assertEqual(self.event_types(app, run["id"]).count("run.pause_requested"), 1)
        app.resume(run["id"])
        result = app.wait(run["id"])
        self.assertEqual((result["status"], result["model_calls"], result["tool_calls"]), ("completed", 2, 1))
        self.assertEqual(self.executed, [1])

    def test_tool_pause_waits_for_effect_and_resumes_next_tool_once(self):
        entered, release = self.gate()

        def write(value):
            self.executed.append(value)
            if value == 1:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test tool was not released")
            return value

        tool = Tool("write", "record", ("value",), write, side_effect=True)
        model = PlannedModel((self.call, ToolCall("second", "write", {"value": 2})))
        app = self.app(model, tools=(tool,))
        run = app.submit("two writes")
        self.assertTrue(entered.wait(2))
        app.pause(run["id"])
        self.assertEqual(app.get_run(run["id"])["status"], "running")
        self.assertEqual(self.executed, [1])
        release.set()
        paused = app.wait(run["id"])
        self.assertEqual((paused["status"], paused["phase"], paused["index"], paused["tool_calls"]),
                         ("paused", "tool", 1, 1))
        self.assertEqual(paused["messages"][-1]["tool_result"]["call_id"], "first")
        app.resume(run["id"])
        self.assertEqual(app.wait(run["id"])["status"], "completed")
        self.assertEqual(self.executed, [1, 2])

    def test_approval_pause_restart_preserves_approval_and_original_budget(self):
        tool = Tool("write", "record", ("value",), self.perform, side_effect=True, requires_approval=True)
        app = self.app(PlannedModel((self.call,)), tools=(tool,), limits={"max_model_calls": 1})
        waiting = app.wait(app.submit("write")["id"])
        paused = app.pause(waiting["id"])
        self.assertEqual(paused["resume_status"], "waiting_approval")
        with self.assertRaises(RuntimeError):
            app.approve(waiting["id"])
        app.close()
        reopened = self.app(PlannedModel((self.call,)), tools=(tool,), limits={"max_model_calls": 99})
        recovered = reopened.wait(waiting["id"])
        self.assertEqual((recovered["status"], recovered["model_calls"], recovered["elapsed"]),
                         ("paused", 1, paused["elapsed"]))
        self.assertEqual(recovered["limits"]["max_model_calls"], 1)
        resumed = reopened.resume(waiting["id"])
        self.assertEqual((resumed["status"], resumed["approval"]), ("waiting_approval", waiting["approval"]))
        reopened.resume(waiting["id"])
        self.assertEqual(self.executed, [])
        reopened.approve(waiting["id"])
        result = reopened.wait(waiting["id"])
        self.assertEqual((result["status"], result["error"], result["model_calls"], result["tool_calls"]),
                         ("failed", "model_limit", 1, 1))
        self.assertEqual(self.executed, [1])

    def test_restart_keeps_pending_paused_without_calling_model(self):
        app = self.app(autostart=False)
        run = app.submit("pause before model")
        app.pause(run["id"])
        app.close()
        model = PlannedModel()
        reopened = self.app(model)
        self.assertEqual(reopened.wait(run["id"])["status"], "paused")
        self.assertEqual(model.requests, [])
        reopened.resume(run["id"])
        self.assertEqual(reopened.wait(run["id"])["model_calls"], 1)

    def test_saved_pause_request_without_inflight_recovers_paused(self):
        app = self.app(autostart=False)
        run = app.submit("checkpoint")
        run.update(status="running", pause_requested=True)
        app.store.save(run)
        app.close()
        reopened = self.app()
        result = reopened.wait(run["id"])
        self.assertEqual((result["status"], result["resume_status"], result["model_calls"]),
                         ("paused", "pending", 0))

    def test_resume_is_idempotent_while_pending(self):
        app = self.app(autostart=False)
        run = app.submit("pause")
        self.assertEqual(app.resume(run["id"]), app.store.get(run["id"]))
        app.pause(run["id"])
        resumed = app.resume(run["id"])
        self.assertEqual(app.resume(run["id"]), resumed)
        self.assertEqual(self.event_types(app, run["id"]).count("run.resumed"), 1)

    def test_cancel_paused_run_clears_pause_and_refuses_resume(self):
        app = self.app(autostart=False)
        run = app.submit("cancel")
        app.pause(run["id"])
        result = app.cancel(run["id"])
        self.assertEqual((result["status"], result["pause_requested"], result["resume_status"]),
                         ("cancelled", False, None))
        with self.assertRaises(RuntimeError):
            app.resume(run["id"])
        app.start()
        self.assertEqual(app.model.requests, [])

    def test_cancel_at_model_boundary_wins_over_pause_request(self):
        entered, release = self.gate()
        app = self.app(PlannedModel((self.call,)), tools=(self.tool,), permissions=["write"], autostart=False)
        advance = app.engine.advance

        async def gated_advance(state, **options):
            result = await advance(state, **options)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test boundary was not released")
            return result

        app.engine.advance = gated_advance
        run = app.submit("write")
        app.start()
        self.assertTrue(entered.wait(2))
        app.pause(run["id"])
        app.cancel(run["id"])
        release.set()
        result = app.wait(run["id"])
        self.assertEqual((result["status"], result["pause_requested"], result["resume_status"]),
                         ("cancelled", False, None))
        self.assertEqual(self.executed, [])
        self.assertNotIn("run.paused", self.event_types(app, run["id"]))

    def test_completion_wins_over_pause_request(self):
        model = PlannedModel(gated=True)
        app = self.app(model)
        self.addCleanup(model.release.set)
        run = app.submit("answer")
        self.assertTrue(model.entered.wait(2))
        app.pause(run["id"])
        model.release.set()
        result = app.wait(run["id"])
        self.assertEqual((result["status"], result["output"], result["pause_requested"], result["resume_status"]),
                         ("completed", "done", False, None))
        self.assertNotIn("run.paused", self.event_types(app, run["id"]))
        self.assertEqual(app.pause(run["id"]), result)

    def test_unknown_effect_wins_over_pause_request_and_cannot_resume(self):
        entered, release = self.gate()

        def failing_write(value):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test tool was not released")
            raise RuntimeError("effect cannot be verified")

        tool = Tool("write", "record", ("value",), failing_write, side_effect=True)
        app = self.app(PlannedModel((self.call,)), tools=(tool,))
        run = app.submit("write")
        self.assertTrue(entered.wait(2))
        app.pause(run["id"])
        release.set()
        result = app.wait(run["id"])
        self.assertEqual((result["status"], result["pause_requested"], result["resume_status"]),
                         ("unknown", False, None))
        with self.assertRaises(RuntimeError):
            app.resume(run["id"])

    def test_paused_checkpoint_configuration_change_fails_closed(self):
        app = self.app(autostart=False, permissions=["write"])
        run = app.submit("pause")
        app.pause(run["id"])
        app.close()
        reopened = self.app(permissions=[])
        state = reopened.get_run(run["id"])
        self.assertEqual((state["status"], state["error"], state["resume_status"]),
                         ("failed", "configuration_changed", None))
        self.assertEqual(reopened.model.requests, [])

    def test_paused_checkpoint_tool_definition_change_fails_closed(self):
        app = self.app(autostart=False, tools=(self.tool,))
        run = app.submit("pause")
        app.pause(run["id"])
        app.close()
        changed = Tool("write", "new definition", ("value",), self.perform,
                       side_effect=True, permissions=frozenset({"write"}))
        reopened = self.app(tools=(changed,))
        state = reopened.get_run(run["id"])
        self.assertEqual((state["status"], state["error"], state["resume_status"]),
                         ("failed", "tool_snapshot_changed", None))
        self.assertEqual(self.executed, [])

    def test_crash_during_requested_pause_keeps_conservative_recovery(self):
        for effect in (False, True):
            with self.subTest(effect=effect):
                self.database = Path(self.temp.name) / f"crash-{effect}.sqlite3"
                app = self.app(autostart=False)
                run = app.submit("in flight")
                phase = "tool" if effect else "model"
                run.update(status="running", phase=phase, pause_requested=True,
                           inflight={"phase": phase, "side_effect": effect})
                app.store.save(run)
                app.close()
                reopened = self.app()
                state = reopened.get_run(run["id"])
                self.assertEqual(state["status"], "unknown" if effect else "failed")
                self.assertEqual(state["tool_calls" if effect else "model_calls"], 1)
                self.assertFalse(state["pause_requested"])
                self.assertEqual(reopened.model.requests, [])


class PauseEngineAndLimitsTests(unittest.TestCase):
    def test_engine_does_not_advance_paused_run_but_allows_cancel(self):
        model = PlannedModel()
        engine = Engine(model, ())
        state = engine.create("paused")
        state.update(status="paused", resume_status="pending")
        asyncio.run(engine.advance(state))
        self.assertEqual((state["status"], state["elapsed"], model.requests), ("paused", 0.0, []))
        asyncio.run(engine.advance(state, cancelled=lambda: True))
        self.assertEqual((state["status"], state["resume_status"]), ("cancelled", None))

    def test_limits_reject_invalid_shapes_values_and_return_independent_defaults(self):
        invalid = [[], False, "limits", {"extra": 1}, {"max_model_calls": 0},
                   {"max_tool_calls": -1}, {"max_model_calls": True}, {"max_tool_calls": 1.5},
                   {"model_timeout": False}, {"tool_timeout": 0}, {"run_timeout": -1},
                   {"model_timeout": float("nan")}, {"run_timeout": float("inf")},
                   {"tool_timeout": 10 ** 1000}]
        for limits in invalid:
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                normalize_limits(limits)
        defaults = normalize_limits()
        self.assertEqual(defaults, DEFAULT_LIMITS)
        defaults["max_tool_calls"] = 0
        self.assertEqual(normalize_limits()["max_tool_calls"], 24)
        self.assertEqual(normalize_limits({"max_tool_calls": 0})["max_tool_calls"], 0)

    def test_application_validates_limits_before_creating_database(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "nested" / "invalid.sqlite3"
            with self.assertRaises(ValueError):
                Application(PlannedModel(), database=database, limits={"model_timeout": 0})
            self.assertFalse(database.parent.exists())


if __name__ == "__main__":
    unittest.main()
