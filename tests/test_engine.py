import asyncio
import inspect
import json
import threading
import unittest
from unittest.mock import patch

from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness import Harness
from astra_harness.engine import Engine
from astra_harness.tools import Tool, ToolExecutor, ToolRegistry, ToolExecutionUnknown


class Model:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        answer = next(self.answers)
        return answer(request) if callable(answer) else answer


def calls(*items):
    return ModelResponse(tool_calls=tuple(items))


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executed = []

        def add(a, b):
            self.executed.append((a, b))
            return a + b

        self.tool = Tool("add", "sum", ("a", "b"), add)
        self.call = ToolCall("c1", "add", {"a": 2, "b": 3})

    async def test_serialized_transitions_pair_real_result(self):
        model = Model(calls(self.call), lambda req: ModelResponse(text=str(req.messages[-1].tool_result.value)))
        events = []
        engine = Engine(model, (self.tool,), system_instruction="Compute", emit=events.append)
        state = engine.create("sum", limits={"max_model_calls": 2, "max_tool_calls": 1})
        self.assertEqual(state["turn_start"], 1)
        for _ in range(3):
            state = await engine.advance(json.loads(json.dumps(state)))
        self.assertEqual((state["status"], state["output"], state["model_calls"], state["tool_calls"]), ("completed", "5", 2, 1))
        self.assertEqual(model.requests[1].messages[-1].tool_result.call_id, "c1")
        self.assertEqual(self.executed, [(2, 3)])
        self.assertEqual([item["type"] for item in events], ["model.start", "model.end", "tool.start", "tool.end", "model.start", "model.end", "run.end"])

    async def test_permission_denied_before_handler(self):
        tool = Tool("add", "sum", ("a", "b"), self.tool.handler, permissions=frozenset({"compute"}))
        engine = Engine(Model(calls(self.call)), (tool,))
        state = engine.create("sum")
        await engine.advance(state)
        await engine.advance(state)
        self.assertEqual(state["messages"][-1]["tool_result"]["error_code"], "permission_denied")
        self.assertEqual(self.executed, [])

    async def test_approval_binds_arguments_and_preserves_budget(self):
        tool = Tool("add", "sum", ("a", "b"), self.tool.handler, requires_approval=True)
        engine = Engine(Model(calls(self.call)), (tool,))
        state = engine.create("sum")
        await engine.advance(state)
        await engine.advance(state)
        self.assertEqual((state["status"], state["tool_calls"]), ("waiting_approval", 0))
        key = state["approval"]["key"]
        state["approved"].append(key)
        state["pending"][0]["arguments"]["b"] = 4
        state["status"] = "pending"
        await engine.advance(state)
        self.assertEqual(state["status"], "waiting_approval")
        self.assertNotEqual(state["approval"]["key"], key)
        state["approved"].append(state["approval"]["key"])
        state["status"] = "pending"
        await engine.advance(state)
        self.assertEqual(state["tool_calls"], 1)
        self.assertEqual(self.executed, [(2, 4)])

    async def test_changed_tool_definition_refuses_resume(self):
        state = Engine(Model(calls(self.call)), (self.tool,)).create("sum")
        changed = Tool("add", "changed description", ("a", "b"), self.tool.handler)
        await Engine(Model(), (changed,)).advance(state)
        self.assertEqual((state["status"], state["error"]), ("failed", "tool_snapshot_changed"))
        self.assertEqual(self.executed, [])

    async def test_batch_limit_and_zero_limit_do_not_execute(self):
        for budget in (0, 1):
            engine = Engine(Model(calls(self.call, ToolCall("c2", "add", {"a": 1, "b": 1}))), (self.tool,))
            state = engine.create("sum", limits={"max_tool_calls": budget})
            await engine.advance(state)
            self.assertEqual((state["status"], state["error"], state["tool_calls"]), ("failed", "tool_limit", 0))
        self.assertEqual(self.executed, [])

    async def test_model_limit_survives_serialization(self):
        model = Model(calls(self.call))
        engine = Engine(model, (self.tool,))
        state = engine.create("sum", limits={"max_model_calls": 1})
        await engine.advance(state)
        await engine.advance(state)
        state = json.loads(json.dumps(state))
        await engine.advance(state)
        self.assertEqual((state["error"], len(model.requests)), ("model_limit", 1))

    async def test_pre_cancel_does_not_start_model(self):
        model = Model()
        engine = Engine(model, ())
        state = engine.create("sum")
        await engine.advance(state, cancelled=lambda: True)
        self.assertEqual((state["status"], state["model_calls"]), ("cancelled", 0))

    async def test_model_timeout_is_bounded(self):
        release = threading.Event()
        model = Model(lambda req: release.wait(2))
        engine = Engine(model, ())
        state = engine.create("sum", limits={"model_timeout": 0.03})
        try:
            await asyncio.wait_for(engine.advance(state), 0.5)
            self.assertEqual((state["status"], state["error"]), ("failed", "model_timeout"))
            await engine.advance(state)
            self.assertEqual(len(model.requests), 1)
        finally:
            release.set()

    async def _assert_stopped_before_async_start(self, *, cancel):
        loop = asyncio.get_running_loop()
        entered, finished = asyncio.Event(), asyncio.Event()
        release, cancelled = threading.Event(), threading.Event()
        created, executed = [], []
        actual_run = asyncio.run

        async def answer():
            executed.append("model_started")
            return ModelResponse(text="late")

        def make_answer(request):
            coroutine = answer()
            created.append(coroutine)
            return coroutine

        def delayed_run(coroutine):
            # Hold the worker before await_value registers its cancellation target.
            loop.call_soon_threadsafe(entered.set)
            try:
                if not release.wait(5):
                    coroutine.close()
                    raise AssertionError("worker release handshake missing")
                return actual_run(coroutine)
            finally:
                loop.call_soon_threadsafe(finished.set)

        engine = Engine(Model(make_answer), ())
        state = engine.create("synthetic", limits={"model_timeout": 10 if cancel else 0.03})
        with patch("astra_harness.engine.asyncio.run", side_effect=delayed_run):
            task = asyncio.create_task(engine.advance(state, cancelled=cancelled.is_set))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if cancel:
                    cancelled.set()
                await asyncio.wait_for(task, 2)
                expected = ("cancelled", "cancelled") if cancel else ("failed", "model_timeout")
                self.assertEqual((state["status"], state["error"]), expected)
                self.assertEqual(executed, [])
                self.assertEqual(len(created), 1)
            finally:
                release.set()
                await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(executed, [])
        self.assertEqual(inspect.getcoroutinestate(created[0]), inspect.CORO_CLOSED)
        self.assertEqual((state["status"], state["error"]), expected)

    async def test_timeout_before_async_start_does_not_enter_coroutine(self):
        await self._assert_stopped_before_async_start(cancel=False)

    async def test_cancel_before_async_start_does_not_enter_coroutine(self):
        await self._assert_stopped_before_async_start(cancel=True)

    async def test_side_effect_timeout_is_unknown_without_replay(self):
        started, release = threading.Event(), threading.Event()

        def write(a, b):
            self.executed.append((a, b))
            started.set()
            release.wait(2)
            return a + b

        tool = Tool("add", "write", ("a", "b"), write, side_effect=True)
        engine = Engine(Model(calls(self.call)), (tool,))
        state = engine.create("sum", limits={"tool_timeout": 0.03})
        await engine.advance(state)
        try:
            await asyncio.wait_for(engine.advance(state), 0.5)
            self.assertTrue(started.is_set())
            self.assertEqual((state["status"], state["error"]), ("unknown", "tool_timeout"))
            await engine.advance(state)
            self.assertEqual(self.executed, [(2, 3)])
        finally:
            release.set()

    async def test_inflight_cancel_stops_future_tools(self):
        started, release, cancel = threading.Event(), threading.Event(), threading.Event()

        def wait(a, b):
            started.set()
            release.wait(2)
            return a + b

        tool = Tool("add", "wait", ("a", "b"), wait, side_effect=True)
        engine = Engine(Model(calls(self.call)), (tool,))
        state = engine.create("sum")
        await engine.advance(state)
        task = asyncio.create_task(engine.advance(state, cancelled=cancel.is_set))
        try:
            while not started.is_set():
                await asyncio.sleep(.001)
            cancel.set()
            await asyncio.wait_for(task, .5)
            self.assertEqual((state["status"], state["error"]), ("unknown", "cancelled"))
        finally:
            release.set()

    async def test_async_handlers_supported(self):
        async def add(a, b):
            await asyncio.sleep(0)
            return a + b

        engine = Engine(Model(calls(self.call)), (Tool("add", "sum", ("a", "b"), add),))
        state = engine.create("sum")
        await engine.advance(state)
        await engine.advance(state)
        self.assertEqual(state["messages"][-1]["tool_result"]["value"], 5)

    async def test_explicit_unknown_and_side_effect_exception_end_unknown(self):
        for error in (ToolExecutionUnknown("private detail"), RuntimeError("private detail")):
            def broken(a, b):
                raise error

            tool = Tool("add", "write", ("a", "b"), broken, side_effect=True)
            engine = Engine(Model(calls(self.call)), (tool,))
            state = engine.create("sum")
            await engine.advance(state)
            await engine.advance(state)
            self.assertEqual(state["status"], "unknown")
            self.assertNotIn("private detail", json.dumps(state))

    async def test_run_budget_persists_and_expiry_prevents_request(self):
        model = Model()
        engine = Engine(model, ())
        state = engine.create("sum", limits={"run_timeout": 1})
        state["elapsed"] = 1.01
        await engine.advance(json.loads(json.dumps(state)))
        self.assertEqual(model.requests, [])

    async def test_protocol_duplicates_reject_before_tools(self):
        engine = Engine(Model(calls(self.call, self.call)), (self.tool,))
        state = engine.create("sum")
        await engine.advance(state)
        self.assertEqual(state["error"], "protocol_error")
        self.assertEqual(self.executed, [])

    async def test_observer_failure_does_not_retry_model(self):
        def fail(event):
            raise RuntimeError()

        model = Model(ModelResponse(text="done"))
        engine = Engine(model, (), emit=fail)
        state = engine.create("sum")
        await engine.advance(state)
        self.assertEqual((state["status"], len(model.requests)), ("completed", 1))

    async def test_invalid_limits_fail_before_run(self):
        engine = Engine(Model(), ())
        for limits in ({"max_model_calls": True}, {"max_tool_calls": -1},
                       {"tool_timeout": float("inf")}, {"run_timeout": 0}, {"unused": 2}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                engine.create("sum", limits=limits)

    async def test_sync_facade_rejects_active_event_loop(self):
        model = Model()
        with self.assertRaisesRegex(RuntimeError, "await Engine.advance"):
            Harness(model).run("sum")
        self.assertEqual(model.requests, [])

    async def test_bound_handler_preserves_resource_identity(self):
        class Counter:
            def __init__(self):
                self.lock = threading.Lock()
                self.count = 0

            def add(self, a, b):
                with self.lock:
                    self.count += 1
                return a + b

        counter = Counter()
        tool = Tool("add", "sum", ("a", "b"), counter.add)
        engine = Engine(Model(calls(self.call)), (tool,))
        state = engine.create("sum")
        await engine.advance(state)
        await engine.advance(state)
        self.assertEqual(counter.count, 1)

    async def test_cancel_at_start_does_not_start_handler(self):
        cancelled = threading.Event()

        def emit(event):
            if event["type"] == "tool.start":
                cancelled.set()

        tool = Tool("add", "sum", ("a", "b"), self.tool.handler, side_effect=True)
        engine = Engine(Model(calls(self.call)), (tool,), emit=emit)
        state = engine.create("sum")
        await engine.advance(state, cancelled=cancelled.is_set)
        await engine.advance(state, cancelled=cancelled.is_set)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(self.executed, [])

    async def test_cancel_at_model_end_wins_over_final_answer(self):
        cancelled = threading.Event()

        def emit(event):
            if event["type"] == "model.end":
                cancelled.set()

        engine = Engine(Model(ModelResponse(text="done")), (), emit=emit)
        state = engine.create("sum")
        await engine.advance(state, cancelled=cancelled.is_set)
        self.assertEqual((state["status"], state["output"]), ("cancelled", ""))

    async def test_cancel_after_confirmed_tool_result_preserves_result(self):
        cancelled = threading.Event()

        def emit(event):
            if event["type"] == "tool.end":
                cancelled.set()

        tool = Tool("add", "sum", ("a", "b"), self.tool.handler, side_effect=True)
        engine = Engine(Model(calls(self.call)), (tool,), emit=emit)
        state = engine.create("sum")
        await engine.advance(state, cancelled=cancelled.is_set)
        await engine.advance(state, cancelled=cancelled.is_set)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["messages"][-1]["tool_result"]["value"], 5)

    async def test_invalid_unicode_response_stops_before_checkpoint(self):
        for response in (ModelResponse(text="\ud800"), calls(ToolCall("c1", "add", {"a": "\ud800", "b": 3}))):
            engine = Engine(Model(response), (self.tool,))
            state = engine.create("sum")
            await engine.advance(state)
            self.assertEqual((state["status"], state["error"]), ("failed", "protocol_error"))
            json.dumps(state, ensure_ascii=False).encode("utf-8")


class ExecutorPolicyTests(unittest.TestCase):
    def test_sync_facade_stops_at_approval(self):
        seen = []
        tool = Tool("write", "write", (), lambda: seen.append(1) or 1,
                    side_effect=True, requires_approval=True)
        model = Model(calls(ToolCall("c", "write", {})))
        result = Harness(model, (tool,)).run("write")
        self.assertEqual((result.reason, result.model_calls, result.tool_calls), ("approval_required", 1, 0))
        self.assertEqual(seen, [])

    def test_invalid_unicode_arguments_and_results(self):
        schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        tool = Tool("echo", "echo", (), lambda text: text, input_schema=schema)
        executor = ToolExecutor(ToolRegistry((tool,)))
        self.assertEqual(executor.execute(ToolCall("c", "echo", {"text": "\ud800"})).error_code, "invalid_arguments")
        tool = Tool("echo", "echo", (), lambda text: "\ud800", input_schema=schema)
        executor = ToolExecutor(ToolRegistry((tool,)))
        self.assertEqual(executor.execute(ToolCall("c", "echo", {"text": "valid"})).error_code, "invalid_tool_result")

    def test_direct_executor_cannot_skip_approval(self):
        calls_seen = []
        tool = Tool("add", "sum", ("a", "b"), lambda **kw: calls_seen.append(kw) or 5,
                    requires_approval=True)
        executor = ToolExecutor(ToolRegistry((tool,)))
        call = ToolCall("c", "add", {"a": 2, "b": 3})
        self.assertEqual(executor.execute(call).error_code, "approval_required")
        self.assertEqual(calls_seen, [])
        self.assertEqual(executor.execute(call, approved=True).value, 5)

    def test_nested_schema_and_unknown_keywords(self):
        schema = {"type": "object", "properties": {"items": {"type": "array", "maxItems": 2,
                  "items": {"type": "string", "minLength": 1}}}, "required": ["items"],
                  "additionalProperties": False}
        seen = []
        tool = Tool("echo", "echo", (), lambda **kw: seen.append(kw) or kw, input_schema=schema)
        executor = ToolExecutor(ToolRegistry((tool,)))
        for args in ({"items": [""]}, {"items": ["a", "b", "c"]}, {"items": [3]}, {"items": ["a"], "extra": 2}):
            self.assertEqual(executor.execute(ToolCall("c", "echo", args)).error_code, "invalid_arguments")
        self.assertEqual(seen, [])
        self.assertEqual(executor.execute(ToolCall("c", "echo", {"items": ["a"]})).value, {"items": ["a"]})
        with self.assertRaises(ValueError):
            Tool("bad", "bad", (), lambda: None, input_schema={"type": "object", "$ref": "https://example.invalid"})


if __name__ == "__main__":
    unittest.main()
