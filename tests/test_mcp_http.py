import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from astra_harness.builtin.mcp import MCPError
from astra_harness.builtin.mcp_http import StreamableHTTPMCPClient, validate_http_endpoint
from astra_harness.tools import ToolExecutionUnknown, ToolUnavailable
from examples.mcp_http_server import LocalMCPHTTPServer


class HTTPMCPTests(unittest.TestCase):
    def test_explicit_numeric_loopback_endpoint_only(self):
        for url in ["http://127.0.0.1:1234/mcp", "http://[::1]:1234/mcp"]:
            self.assertEqual(validate_http_endpoint(url), url)
        for url in [None, "http://localhost:1234/mcp", "http://127.1:1234/mcp", "http://127.0.0.2:1234/mcp",
                    "https://127.0.0.1:1234/mcp", "http://example.com:1234/mcp", "http://169.254.169.254:80/mcp",
                    "http://127.0.0.1/mcp", "http://127.0.0.1:0/mcp", "http://127.0.0.1:99999/mcp",
                    "http://user@127.0.0.1:1234/mcp", "http://127.0.0.1:1234/mcp?key=x",
                    "http://127.0.0.1:1234/mcp#x", "http://127.0.0.1:1234/a/../mcp",
                    "http://127.0.0.1:1234/%2f/mcp", "http://127.0.0.1:1234//mcp", "http://127.0.0.1:1234/mcp\n"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_http_endpoint(url)

    def test_invalid_limits_rejected_before_loop(self):
        for kwargs in [{"timeout": True}, {"timeout": float("inf")}, {"timeout": 0},
                       {"max_message_bytes": True}, {"max_message_bytes": 255}]:
            with self.assertRaises(ValueError):
                StreamableHTTPMCPClient("http://127.0.0.1:1234/mcp", **kwargs)

    def test_json_lifecycle_tool_schema_and_headers(self):
        with LocalMCPHTTPServer() as server:
            with StreamableHTTPMCPClient(server.url) as client:
                tools = client.discover_tools()
                self.assertEqual([t.name for t in tools], ["mcp.record"])
                self.assertTrue(tools[0].requires_approval and tools[0].side_effect)
                self.assertEqual(client.call_tool("record", {"value": 42})["structuredContent"], {"value": 42, "sequence": 1})
            methods = [x["method"] for x in server.requests]
            self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"])
            for request in server.requests[1:]:
                headers = {k.lower(): v for k,v in request["headers"].items()}
                self.assertEqual(headers["mcp-protocol-version"], "2025-06-18")
                self.assertEqual(headers["mcp-session-id"], "fixture-session")
            self.assertFalse(client._thread.is_alive())

    def test_sse_multiline_unicode_notifications(self):
        with LocalMCPHTTPServer("sse") as server, StreamableHTTPMCPClient(server.url) as client:
            self.assertEqual(len(client.discover_tools()), 1)
            value = asyncio.run(client.acall_tool("record", {"value": 7}))
            self.assertIn("合成收据", value["content"][0]["text"])
            self.assertEqual(server.receipts, [{"value": 7, "sequence": 1}])

    def test_server_ping_gets_result_without_business_dispatch(self):
        with LocalMCPHTTPServer("ping") as server, StreamableHTTPMCPClient(server.url) as client:
            client.discover_tools()
            self.assertTrue(any(r["method"] == "response" and r["id"] == "server-ping" for r in server.requests))
            self.assertEqual(server.receipts, [])

    def test_enter_failure_stops_its_loop_without_exit(self):
        with LocalMCPHTTPServer("bad_protocol") as server:
            client = StreamableHTTPMCPClient(server.url)
            with self.assertRaises(MCPError):
                with client:
                    self.fail("bad protocol must not enter")
            self.assertFalse(client._thread.is_alive())
            client.close()

    def test_stateless_service_no_session_header_or_delete(self):
        with LocalMCPHTTPServer(session=False) as server:
            with StreamableHTTPMCPClient(server.url) as client:
                client.call_tool("record", {"value": 1})
            self.assertNotIn("DELETE", [r["method"] for r in server.requests])
            self.assertTrue(all("mcp-session-id" not in {k.lower() for k in r["headers"]} for r in server.requests))

    def test_accepted_delete_405_is_not_cleanup_failure(self):
        with LocalMCPHTTPServer("delete_405") as server:
            client = StreamableHTTPMCPClient(server.url).start()
            client.close()
            self.assertIsNone(client.last_error)
            client.close()

    def test_drop_after_effect_unknown_no_replay(self):
        with LocalMCPHTTPServer("drop_after_effect") as server, StreamableHTTPMCPClient(server.url) as client:
            with self.assertRaises(ToolExecutionUnknown):
                client.call_tool("record", {"value": 9})
            with self.assertRaises(ToolUnavailable):
                client.call_tool("record", {"value": 9})
            self.assertEqual(server.receipts, [{"value": 9, "sequence": 1}])
            self.assertEqual(client.state, "failed")

    def test_uncertain_response_retires_before_queued_dispatch(self):
        for mode in ["drop_after_effect", "invalid_result"]:
            with self.subTest(mode=mode), LocalMCPHTTPServer(mode) as server, StreamableHTTPMCPClient(server.url) as client:
                async def run():
                    return await asyncio.gather(client.acall_tool("record", {"value": 1}),
                                                client.acall_tool("record", {"value": 2}), return_exceptions=True)
                first, second = asyncio.run(run())
                self.assertIsInstance(first, ToolExecutionUnknown)
                self.assertIsInstance(second, ToolUnavailable)
                self.assertEqual(server.receipts, [{"value": 1, "sequence": 1}])

    def test_response_failures_unknown_and_no_redirect(self):
        for mode in ["redirect", "wrong_id", "invalid_result", "session_expired", "large"]:
            with self.subTest(mode=mode), LocalMCPHTTPServer(mode) as server, StreamableHTTPMCPClient(server.url) as client:
                with self.assertRaises(ToolExecutionUnknown):
                    client.call_tool("record", {"value": 3})
                self.assertEqual(len(server.receipts), 1)
                self.assertEqual(sum(r["method"] == "tools/call" for r in server.requests), 1)
                self.assertEqual(client.state, "failed")

    def test_remote_tool_error_is_reported_without_unknown(self):
        with LocalMCPHTTPServer("tool_error") as server, StreamableHTTPMCPClient(server.url) as client:
            with self.assertRaisesRegex(MCPError, "^mcp_tool_error$"):
                client.call_tool("record", {"value": 2})
            self.assertEqual(client.state, "ready")

    def test_request_serialization_limit_rejects_before_dispatch(self):
        with LocalMCPHTTPServer() as server, StreamableHTTPMCPClient(server.url, max_message_bytes=512) as client:
            for args in [{"value": "x"*600}, {"value": float("nan")}]:
                with self.assertRaises(ToolUnavailable):
                    client.call_tool("record", args)
            self.assertEqual(server.receipts, [])
            self.assertEqual(client.state, "ready")

    def test_slow_trickle_absolute_deadline(self):
        with LocalMCPHTTPServer("trickle") as server, StreamableHTTPMCPClient(server.url, timeout=.2) as client:
            started = time.monotonic()
            with self.assertRaises(ToolExecutionUnknown):
                client.call_tool("record", {"value": 4})
            self.assertLess(time.monotonic()-started, 1.2)
            self.assertEqual(len(server.receipts), 1)

    def test_async_cancellation_notifies_and_cleans(self):
        with LocalMCPHTTPServer("slow") as server:
            client = StreamableHTTPMCPClient(server.url, timeout=2).start()
            async def run():
                task = asyncio.create_task(client.acall_tool("record", {"value": 6}))
                while not server.called.is_set():
                    await asyncio.sleep(.005)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            asyncio.run(run())
            self.assertEqual(len(server.receipts), 1)
            self.assertIn("notifications/cancelled", [r["method"] for r in server.requests])
            self.assertEqual(client.state, "failed")
            client.close()
            self.assertFalse(client._thread.is_alive())

    def test_cancel_queued_call_never_dispatches(self):
        with LocalMCPHTTPServer("slow") as server, StreamableHTTPMCPClient(server.url, timeout=2) as client:
            async def run():
                first = asyncio.create_task(client.acall_tool("record", {"value": 1}))
                while not server.called.is_set():
                    await asyncio.sleep(.005)
                second = asyncio.create_task(client.acall_tool("record", {"value": 2}))
                await asyncio.sleep(.02)
                second.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await second
                await first
            asyncio.run(run())
            self.assertEqual(server.receipts, [{"value": 1, "sequence": 1}])
            self.assertEqual(client.state, "ready")

    def test_failed_discovery_and_initialization_closed(self):
        for mode in ["bad_protocol", "notification_body", "duplicate_tools", "cursor_loop"]:
            with self.subTest(mode=mode), LocalMCPHTTPServer(mode) as server:
                client = StreamableHTTPMCPClient(server.url)
                try:
                    with self.assertRaises(MCPError):
                        client.discover_tools()
                finally:
                    client.close()
                self.assertFalse(client._thread.is_alive())
                self.assertEqual(server.receipts, [])

    def test_cookies_and_proxy_environment_not_forwarded(self):
        with LocalMCPHTTPServer("cookie") as server:
            with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                with StreamableHTTPMCPClient(server.url) as client:
                    client.discover_tools()
                    client.call_tool("record", {"value": 8})
            self.assertTrue(all("cookie" not in {k.lower() for k in r["headers"]} for r in server.requests))

    def test_client_not_started_or_closed_is_unavailable(self):
        client = StreamableHTTPMCPClient("http://127.0.0.1:1/mcp")
        with self.assertRaises(ToolUnavailable):
            client.call_tool("record", {"value": 1})
        client.close()
        with self.assertRaises(ToolUnavailable):
            asyncio.run(client.acall_tool("record", {"value": 1}))

    def test_strict_json_and_utf8(self):
        for raw in [b'{"jsonrpc":"2.0","id":1,"id":2}', b'{"jsonrpc":"2.0","id":NaN}',
                    '{"jsonrpc":"2.0","id":1}'.encode("utf-16")]:
            with self.assertRaises(MCPError):
                StreamableHTTPMCPClient._decode(raw)


if __name__ == "__main__":
    unittest.main()
