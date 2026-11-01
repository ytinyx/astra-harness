import json
import hashlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from astra_harness.builtin.mcp import StdioMCPClient
from astra_harness.builtin.models import ModelAdapterError, OpenAICompatibleModel
from astra_harness.contracts import Message, ModelRequest, ToolCall, ToolResult, ToolSpec
from astra_harness.tools import ToolExecutor, ToolRegistry


class ModelPayloadTests(unittest.TestCase):
    def test_official_deepseek_disables_thinking_for_text_and_tool_rounds(self):
        spec = ToolSpec("add", "add", ("a", "b"))
        call = ToolCall("c1", "add", {"a": 2, "b": 3})
        requests = (
            ModelRequest((Message("user", "2+3"),), (spec,)),
            ModelRequest((Message("user", "2+3"), Message("assistant", tool_calls=(call,)),
                          Message("tool", tool_result=ToolResult("c1", "add", 5))), (spec,)),
        )
        for base_url in ("https://api.deepseek.com", "https://API.DEEPSEEK.COM/v1/"):
            model = OpenAICompatibleModel("offline-test-key", "deepseek-flash", base_url)
            for stream in (False, True):
                for request in requests:
                    with self.subTest(base_url=base_url, stream=stream, messages=len(request.messages)):
                        payload, _ = model._payload(request, stream=stream)
                        sent = json.loads(model._request(payload).data)
                        self.assertEqual(sent["thinking"], {"type": "disabled"})
                        self.assertEqual(sent["stream"], stream)
                        self.assertEqual(sent["model"], "deepseek-flash")
                        self.assertTrue(all("reasoning_content" not in item for item in sent["messages"]))
                        if len(request.messages) > 1:
                            self.assertEqual(sent["messages"][-2]["tool_calls"][0]["id"],
                                             sent["messages"][-1]["tool_call_id"])

    def test_other_endpoints_never_receive_deepseek_thinking_extension(self):
        request = ModelRequest((Message("user", "hello"),), ())
        for base_url in ("https://example.com/v1", "http://127.0.0.1:8000/v1",
                         "https://api.deepseek.com.example.com", "https://proxy.api.deepseek.com",
                         "https://example.com/api.deepseek.com"):
            model = OpenAICompatibleModel("offline-test-key", "deepseek-flash", base_url)
            for stream in (False, True):
                with self.subTest(base_url=base_url, stream=stream):
                    payload, _ = model._payload(request, stream=stream)
                    self.assertNotIn("thinking", json.loads(model._request(payload).data))


class ModelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.response = {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                if owner.status == 302:
                    self.send_header("Location", "/redirected")
                self.end_headers()
                body = owner.response if isinstance(owner.response, bytes) else json.dumps(owner.response).encode()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.model = OpenAICompatibleModel("test-key", "test-model", f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_simple_request_and_answer(self):
        result = self.model.complete(ModelRequest((Message("user", "hello"),), ()))
        self.assertEqual(result.text, "hello")
        self.assertNotIn("tools", self.requests[0])
        self.assertEqual(self.requests[0]["messages"], [{"role": "user", "content": "hello"}])

    def test_tool_schema_call_and_result_round_trip(self):
        self.response = {"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "c1", "type": "function", "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'}}]}}]}
        spec = ToolSpec("add", "add", ("a", "b"))
        result = self.model.complete(ModelRequest((Message("user", "2+3"),), (spec,)))
        self.assertEqual(result.tool_calls, (ToolCall("c1", "add", {"a": 2, "b": 3}),))
        self.model.complete(ModelRequest((Message("assistant", tool_calls=result.tool_calls),
                                         Message("tool", tool_result=ToolResult("c1", "add", 5))), (spec,)))
        sent = self.requests[-1]["messages"]
        self.assertEqual(sent[0]["tool_calls"][0]["id"], sent[1]["tool_call_id"])
        self.assertEqual(json.loads(sent[1]["content"])["value"], 5)
        self.assertEqual(self.requests[0]["tools"][0]["function"]["parameters"], spec.schema())

    def test_http_errors_do_not_leak_body_or_key(self):
        self.status, self.response = 401, b"private-error-and-test-key"
        with self.assertRaises(ModelAdapterError) as caught:
            self.model.complete(ModelRequest((Message("user", "x"),), ()))
        self.assertEqual(str(caught.exception), "model_transport_error")

    def test_dotted_tool_names_are_mapped_and_restored(self):
        encoded_name = "t_" + hashlib.sha256(b"files.read").hexdigest()[:40]
        self.response = {"choices": [{"message": {"tool_calls": [{"id": "c1", "function": {
            "name": encoded_name, "arguments": '{"path": "meeting.md"}'}}]}}]}
        spec = ToolSpec("files.read", "read", (), {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"], "additionalProperties": False})
        result = self.model.complete(ModelRequest((Message("user", "read"),), (spec,)))
        self.assertEqual(self.requests[0]["tools"][0]["function"]["name"], encoded_name)
        self.assertEqual(result.tool_calls[0].name, "files.read")

    def test_redirect_is_not_followed(self):
        self.status = 302
        with self.assertRaises(ModelAdapterError):
            self.model.complete(ModelRequest((Message("user", "x"),), ()))
        self.assertEqual(len(self.requests), 1)

    def test_malformed_truncated_and_nonfinite_response_rejected(self):
        cases = [b"not json", {"choices": []},
                 {"choices": [{"finish_reason": "length", "message": {"content": "half"}}]},
                 {"choices": [{"message": {"tool_calls": [{"id": "c", "function": {
                     "name": "add", "arguments": '{"a": NaN}'}}]}}]},
                 {"choices": [{"message": {"tool_calls": [{"id": "c", "function": {
                     "name": "add", "arguments": '{"a": 1, "a": 2}'}}]}}]}]
        for body in cases:
            with self.subTest(body=body):
                self.response = body
                with self.assertRaisesRegex(ModelAdapterError, "model_protocol_error"):
                    self.model.complete(ModelRequest((Message("user", "x"),), ()))

    def test_response_limit(self):
        self.response = b"x" * 101
        self.model.max_response_bytes = 100
        with self.assertRaisesRegex(ModelAdapterError, "model_response_too_large"):
            self.model.complete(ModelRequest((Message("user", "x"),), ()))

    def test_nonlocal_plain_http_and_url_credentials_rejected(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OpenAICompatibleModel("test", "model", url)


class MCPTests(unittest.TestCase):
    def test_actual_stdio_discovery_and_tool_call(self):
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        with StdioMCPClient([sys.executable, "-X", "utf8", str(fixture)]) as client:
            tools = client.discover_tools()
            process = client.process
            self.assertEqual([tool.name for tool in tools], ["mcp.multiply"])
            self.assertTrue(tools[0].side_effect and tools[0].requires_approval)
            self.assertEqual(tools[0].permissions, frozenset({"mcp.call"}))
            executor = ToolExecutor(ToolRegistry(tools))
            call = ToolCall("c1", "mcp.multiply", {"a": 6, "b": 7})
            self.assertEqual(executor.execute(call).error_code, "permission_denied")
            self.assertEqual(executor.execute(call, frozenset({"mcp.call"})).error_code, "approval_required")
            result = executor.execute(call, frozenset({"mcp.call"}), approved=True)
            self.assertIsNone(result.error_code)
            self.assertEqual(result.value["content"][0]["text"], "42")
        self.assertIsNotNone(process.poll())

    def test_failed_initialization_closes_process(self):
        client = StdioMCPClient([sys.executable, "-c", "print('invalid')"], timeout=1)
        with self.assertRaisesRegex(RuntimeError, "mcp_initialization_failed"):
            client.start()
        self.assertIsNone(client.process)

    def test_remote_timeout_reports_unknown_and_closes_server(self):
        fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
        with StdioMCPClient([sys.executable, "-X", "utf8", str(fixture), "--hang-call"], timeout=1) as client:
            tools = client.discover_tools()
            process = client.process
            executor = ToolExecutor(ToolRegistry(tools))
            result = executor.execute(ToolCall("c1", "mcp.multiply", {"a": 6, "b": 7}),
                                      frozenset({"mcp.call"}), approved=True)
            self.assertEqual(result.error_code, "execution_unknown")
            self.assertIsNone(client.process)
            self.assertIsNotNone(process.poll())
