"""Trusted loopback-only MCP fixture with synthetic in-memory receipts.

This is a test/example server, not an authenticated deployment service. Fault
modes exercise clients without accessing files, credentials or external hosts.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time


class LocalMCPHTTPServer:
    def __init__(self, mode="json", *, session=True):
        self.mode, self.session = mode, session
        self.requests, self.receipts = [], []
        self.closed = False
        self.called = threading.Event()
        self._guard = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send(self, status, data=b"", content_type="application/json", extra=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if data:
                    self.wfile.write(data)
                    self.wfile.flush()

            def valid(self):
                return (self.headers.get("Host") == owner.url.split("//", 1)[1].split("/", 1)[0]
                        and self.headers.get("Origin") == owner.url.rsplit("/", 1)[0]
                        and self.path == "/mcp")

            def do_GET(self):
                self.send(405 if self.valid() else 403)

            def do_DELETE(self):
                if not self.valid():
                    self.send(403)
                    return
                with owner._guard:
                    owner.requests.append({"method": "DELETE", "headers": dict(self.headers)})
                self.send(405 if owner.mode == "delete_405" else 204)

            def do_POST(self):
                try:
                    size = int(self.headers.get("Content-Length", "-1"))
                    if not self.valid() or not 0 <= size <= 1_000_000:
                        self.send(403)
                        return
                    message = json.loads(self.rfile.read(size))
                    method = message.get("method", "response")
                    with owner._guard:
                        owner.requests.append({"method": method, "id": message.get("id"),
                                               "params": message.get("params"), "headers": dict(self.headers)})
                    if method != "initialize":
                        if (self.headers.get("MCP-Protocol-Version") != "2025-06-18"
                                or (owner.session and self.headers.get("Mcp-Session-Id") != "fixture-session")):
                            self.send(400)
                            return
                    if method in {"notifications/initialized", "notifications/cancelled", "response"}:
                        self.send(202, b"not empty" if owner.mode == "notification_body" else b"")
                        return
                    extra = {}
                    if method == "initialize":
                        if owner.session:
                            extra["Mcp-Session-Id"] = "fixture-session"
                        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "astra-loopback-fixture", "version": "1"}}
                        if owner.mode == "bad_protocol":
                            result["protocolVersion"] = "unsupported"
                    elif method == "tools/list":
                        spec = {"name": "record", "description": "Record a synthetic integer receipt",
                                "inputSchema": {"type": "object", "properties": {"value": {"type": "integer"}},
                                                "required": ["value"], "additionalProperties": False}}
                        result = {"tools": [spec, spec] if owner.mode == "duplicate_tools" else [spec]}
                        if owner.mode == "cursor_loop":
                            result["nextCursor"] = "same"
                        if owner.mode == "cookie":
                            extra["Set-Cookie"] = "must_not_forward=secret; Path=/"
                    elif method == "tools/call":
                        with owner._guard:
                            receipt = {"value": message["params"]["arguments"]["value"],
                                       "sequence": len(owner.receipts) + 1}
                            owner.receipts.append(receipt)
                        owner.called.set()
                        result = {"content": [{"type": "text", "text": "Synthetic receipt: 合成收据"}],
                                  "structuredContent": receipt}
                        if owner.mode == "drop_after_effect":
                            self.close_connection = True
                            self.connection.shutdown(socket.SHUT_RDWR)
                            return
                        if owner.mode == "slow":
                            time.sleep(1)
                        if owner.mode == "tool_error":
                            result["isError"] = True
                        if owner.mode == "invalid_result":
                            result = {"content": "invalid"}
                        if owner.mode == "session_expired":
                            self.send(404)
                            return
                        if owner.mode == "redirect":
                            self.send(307, extra={"Location": owner.url + "-redirect"})
                            return
                        if owner.mode == "large":
                            result["content"][0]["text"] = "x" * 1_001_000
                    else:
                        self.send(400)
                        return
                    response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                    if owner.mode == "wrong_id" and method == "tools/call":
                        response["id"] += 100
                    raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
                    if owner.mode in {"sse", "ping"}:
                        # A comment, notification and multi-line UTF-8 data.
                        payload = (b": fixture\r\n\r\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\r\n\r\n"
                                   + b"event: message\r\ndata: " + raw[:raw.index(b'"result"')]
                                   + b"\r\ndata: " + raw[raw.index(b'"result"'):] + b"\r\n\r\n")
                        if owner.mode == "ping":
                            payload = b'data: {"jsonrpc":"2.0","id":"server-ping","method":"ping"}\n\n' + payload
                        self.send(200, payload, "text/event-stream", extra)
                    elif owner.mode == "trickle" and method == "tools/call":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        for _ in range(50):
                            self.wfile.write(b": tick\n\n")
                            self.wfile.flush()
                            time.sleep(.03)
                    else:
                        self.send(200, raw, extra=extra)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def close(self):
        if not self.closed:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(1)
            self.closed = True

    def __exit__(self, *args):
        self.close()
