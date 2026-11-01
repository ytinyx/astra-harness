"""真实回环 HTTP SSE + 状态机边界；不需要供应商密钥。"""

import asyncio
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from astra_harness.application import Application
from astra_harness.builtin.models import ModelAdapterError, OpenAICompatibleModel
from astra_harness.contracts import Message, ModelRequest, ModelResponse, ToolCall, ToolSpec
from astra_harness.engine import Engine
from astra_harness.tools import Tool


def chunk(delta=None, finish=None, usage=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], "usage": usage}


def frame(value):
    value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ("data: " + value + "\r\n\r\n").encode("utf-8")


@contextmanager
def local_model(respond):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for piece in respond(payload):
                    # 刻意跨 UTF-8 字节边界写入，readline 必须等到整行再解码。
                    for offset in range(0, len(piece), 7):
                        self.wfile.write(piece[offset:offset + 7])
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    model = OpenAICompatibleModel("local-test-key", "local-model", f"http://127.0.0.1:{server.server_port}")
    try:
        yield model, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


REQUEST = ModelRequest((Message("user", "你好"),), ())
USAGE = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
         "prompt_tokens_details": {"cached_tokens": 4}}


class StreamingHTTPTests(unittest.TestCase):
    def test_text_is_delivered_before_http_stream_finishes(self):
        received, release = threading.Event(), threading.Event()
        deltas, result = [], []

        def respond(payload):
            yield b": heartbeat\r\n\r\n"
            yield frame(chunk({"content": "你好"}))
            release.wait(2)
            yield frame(chunk({"content": "，世界"}, "stop"))
            yield frame({"choices": [], "usage": USAGE})
            yield frame("[DONE]")

        def on_delta(text):
            deltas.append(text)
            received.set()

        with local_model(respond) as (model, requests):
            worker = threading.Thread(target=lambda: result.append(model.stream(REQUEST, on_delta)), daemon=True)
            worker.start()
            try:
                self.assertTrue(received.wait(1), "must deliver text while stream remains open")
                self.assertTrue(worker.is_alive())
                self.assertEqual(deltas, ["你好"])
            finally:
                release.set()
                worker.join(2)
            self.assertEqual(result[0].text, "你好，世界")
            self.assertEqual(result[0].usage, {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                                               "cached_input_tokens": 4})
            self.assertTrue(requests[0]["stream"])
            self.assertEqual(requests[0]["stream_options"], {"include_usage": True})

    def test_tool_fragments_reassemble_and_restore_internal_name(self):
        spec = ToolSpec("files.read", "Read", (), {"type": "object", "properties": {}})
        request = ModelRequest(REQUEST.messages, (spec,))

        def respond(payload):
            name = payload["tools"][0]["function"]["name"]
            yield frame(chunk({"tool_calls": [{"index": 0, "id": "call_", "type": "function",
                                                "function": {"name": name[:15], "arguments": '{"path":'}}]}))
            yield frame(chunk({"tool_calls": [{"index": 0, "id": "1",
                                                "function": {"name": name[15:], "arguments": '"会议.md"}'}}]}, "tool_calls"))
            yield frame("[DONE]")

        with local_model(respond) as (model, _):
            result = model.stream(request, lambda text: self.fail("tool fragments are not text deltas"))
            self.assertEqual(result.tool_calls, (ToolCall("call_1", "files.read", {"path": "会议.md"}),))
            self.assertIsNone(result.usage)

    def test_missing_usage_stays_unknown(self):
        with local_model(lambda _: [frame(chunk({"content": "ok"}, "stop")), frame("[DONE]")]) as (model, _):
            self.assertIsNone(model.stream(REQUEST, lambda _: None).usage)

    def test_rejects_incomplete_or_malformed_stream(self):
        cases = [
            [frame(chunk({"content": "partial"}))],
            [frame(chunk({"content": "partial"}, "length")), frame("[DONE]")],
            [frame(chunk({"content": "partial"}, "stop"))],
            [frame("[DONE]")],
            [b'data: {"choices":[],"choices":[]}\n\n', frame("[DONE]")],
            [frame(chunk({"tool_calls": [{"index": 0, "id": "c", "function": {
                "name": "add", "arguments": '{"a":'}}]}, "tool_calls")), frame("[DONE]")],
            [frame(chunk({"tool_calls": [{"index": True, "id": "c", "function": {
                "name": "add", "arguments": '{}'}}]}, "tool_calls")), frame("[DONE]")],
            [frame(chunk({}, "stop")), frame({"choices": [], "usage": {**USAGE, "total_tokens": 0}}), frame("[DONE]")],
            [frame(chunk({}, "stop")), frame(chunk({"content": "late"})), frame("[DONE]")],
            [b'data: {"choices": []}'],
        ]
        for index, body in enumerate(cases):
            with self.subTest(index=index), local_model(lambda _: body) as (model, _):
                with self.assertRaisesRegex(ModelAdapterError, "model_protocol_error"):
                    model.stream(REQUEST, lambda _: None)

    def test_response_byte_limit_and_callback_stop(self):
        body = [frame(chunk({"content": "x" * 1000}, "stop")), frame("[DONE]")]
        with local_model(lambda _: body) as (model, _):
            model.max_response_bytes = 100
            with self.assertRaisesRegex(ModelAdapterError, "model_response_too_large"):
                model.stream(REQUEST, lambda _: None)
        with local_model(lambda _: body) as (model, _):
            with self.assertRaisesRegex(ModelAdapterError, "model_stream_closed"):
                model.stream(REQUEST, lambda _: False)

    def test_endpoint_change_invalidates_persisted_run(self):
        first = OpenAICompatibleModel("test", "same-model", "https://ENDPOINT-A.example/v1/")
        second = OpenAICompatibleModel("test", "same-model", "https://endpoint-b.example/v1")
        self.assertEqual(first.base_url, "https://endpoint-a.example/v1")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runs.sqlite3"
            with Application(first, database=database, mode="live", autostart=False) as app:
                submitted = app.submit("pending task")
                fingerprint = app.fingerprint
            with Application(second, database=database, mode="live", autostart=False) as app:
                self.assertNotEqual(app.fingerprint, fingerprint)
                state = app.get_run(submitted["id"])
                self.assertEqual((state["status"], state["error"]), ("failed", "configuration_changed"))


class StreamingEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_tool_execution_waits_for_complete_stream_and_records_usage(self):
        executed, events = [], []

        def respond(payload):
            if payload["messages"][-1]["role"] == "tool":
                yield frame(chunk({"content": "结果为 5"}, "stop", USAGE))
            else:
                yield frame(chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {
                    "name": "add", "arguments": '{"a":2,'}}]}))
                self.assertEqual(executed, [])
                yield frame(chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"b":3}'}}]}, "tool_calls", USAGE))
            yield frame("[DONE]")

        with local_model(respond) as (model, _):
            tool = Tool("add", "sum", ("a", "b"), lambda a, b: executed.append(a + b) or a + b)
            engine = Engine(model, (tool,), emit=events.append)
            state = engine.create("2+3")
            await engine.advance(state)
            self.assertEqual(executed, [])
            await engine.advance(state)
            await engine.advance(state)
            self.assertEqual((state["output"], executed), ("结果为 5", [5]))
            self.assertEqual(state["usage"], {"reported_calls": 2, "input_tokens": 20, "output_tokens": 6,
                                            "total_tokens": 26, "cached_input_tokens": 8, "cached_reported_calls": 2})
            self.assertEqual([e["text"] for e in events if e["type"] == "model.delta"], ["结果为 5"])
            self.assertIsNotNone(state["metrics"]["last_model"]["first_delta_ms"])
            self.assertGreaterEqual(state["metrics"]["model_duration_ms"], 0)
            json.dumps(state, allow_nan=False)

    async def test_partial_tool_call_is_never_executed(self):
        executed = []
        body = [frame(chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {
            "name": "add", "arguments": '{"a":2,'}}]}))]
        with local_model(lambda _: body) as (model, _):
            engine = Engine(model, (Tool("add", "sum", ("a", "b"), lambda **args: executed.append(args)),))
            state = engine.create("2+3")
            await engine.advance(state)
            await engine.advance(state)
            self.assertEqual((state["status"], state["pending"], executed), ("failed", [], []))

    async def test_cancel_and_timeout_close_delta_gate(self):
        for cancelled_run in (False, True):
            with self.subTest(cancelled=cancelled_run):
                started, release, finished, cancel = (threading.Event() for _ in range(4))
                accepted, events = [], []

                class SlowModel:
                    def stream(self, request, on_delta):
                        on_delta("before")
                        started.set()
                        release.wait(2)
                        accepted.append(on_delta("late"))
                        finished.set()
                        return ModelResponse("beforelate")

                engine = Engine(SlowModel(), (), emit=events.append)
                state = engine.create("x", limits={"model_timeout": .05 if not cancelled_run else 1})
                task = asyncio.create_task(engine.advance(state, cancelled=cancel.is_set))
                try:
                    while not started.is_set():
                        await asyncio.sleep(.001)
                    if cancelled_run:
                        cancel.set()
                    await asyncio.wait_for(task, .5)
                    count = len(events)
                    release.set()
                    while not finished.is_set():
                        await asyncio.sleep(.001)
                    self.assertEqual(accepted, [False])
                    self.assertEqual(len(events), count)
                    self.assertEqual(state["status"], "cancelled" if cancelled_run else "failed")
                    self.assertEqual(state["usage"]["reported_calls"], 0)
                finally:
                    release.set()

    async def test_invalid_delta_and_usage_fail_protocol(self):
        for value in ("\ud800", "x" * 262145, 123):
            class BadModel:
                def stream(self, request, on_delta):
                    try:
                        on_delta(value)
                    except ValueError:
                        pass
                    return ModelResponse("pretend success")
            engine = Engine(BadModel(), ())
            state = engine.create("x")
            await engine.advance(state)
            self.assertEqual((state["status"], state["error"]), ("failed", "protocol_error"))

        class BadUsage:
            def complete(self, request):
                return ModelResponse("x", usage={"input_tokens": 10, "output_tokens": 3, "total_tokens": 0})
        engine = Engine(BadUsage(), ())
        state = engine.create("x")
        await engine.advance(state)
        self.assertEqual(state["error"], "protocol_error")

    async def test_excessive_delta_count_cannot_finish_successfully(self):
        class FloodModel:
            def stream(self, request, on_delta):
                try:
                    for _ in range(8193):
                        on_delta("x")
                except ValueError:
                    pass
                return ModelResponse("pretend success")
        events = []
        engine = Engine(FloodModel(), (), emit=events.append)
        state = engine.create("x")
        await engine.advance(state)
        self.assertEqual((state["status"], state["error"]), ("failed", "protocol_error"))
        self.assertNotIn("model.end", [event["type"] for event in events])

    async def test_nonstream_model_has_no_fake_deltas_or_usage(self):
        class LegacyModel:
            def complete(self, request):
                return ModelResponse("hello")
        events = []
        engine = Engine(LegacyModel(), (), emit=events.append)
        state = engine.create("x")
        await engine.advance(state)
        self.assertEqual([e["type"] for e in events], ["model.start", "model.end", "run.end"])
        self.assertIsNone(state["metrics"]["last_model"]["usage"])
        self.assertIsNone(state["metrics"]["last_model"]["first_delta_ms"])
        self.assertEqual(state["usage"]["reported_calls"], 0)

    async def test_demo_search_uses_actual_files_with_line_references(self):
        from astra_harness.builtin.files import file_tools
        from astra_harness.builtin.models import DemoModel

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "notes.md").write_text("# 会议\nAgent harness 设计\n", encoding="utf-8")
            for query, expected in (("harness", "notes.md:2 — Agent harness 设计"), ("不存在", "没有找到")):
                engine = Engine(DemoModel(), file_tools(directory))
                state = engine.create("搜索 " + query, permissions=["files.read"])
                for _ in range(3):
                    await engine.advance(state)
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["tool_calls"], 1)
                self.assertIn(expected, state["output"])
