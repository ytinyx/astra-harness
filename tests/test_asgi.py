"""ASGI lifecycle and real h11 framing checks; fixtures never call a provider."""

import asyncio
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from astra_harness.application import Application
from astra_harness.builtin.models import DemoModel
from astra_harness.web import MAX_BODY, create_app, create_server


class Probe:
    def __init__(self):
        self.calls = []
        self.thread_ids = []
        self.status = "running"
        self.closed = False

    def info(self):
        self.thread_ids.append(threading.get_ident())
        return {"mode": "fixture"}

    def submit(self, prompt, session_id=None, request_id=None):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(("submit", prompt, session_id, request_id))
        return self.get_run("run-1")

    def get_run(self, identifier):
        self.thread_ids.append(threading.get_ident())
        return {"id": identifier, "status": self.status, "model_calls": 0, "tool_calls": 0}

    def events(self, identifier, after=0):
        self.thread_ids.append(threading.get_ident())
        return []

    def cancel(self, identifier):
        self.calls.append(("cancel", identifier))
        return self.get_run(identifier)

    def approve(self, identifier, approved):
        self.calls.append(("approve", identifier, approved))
        return self.get_run(identifier)

    def update_session(self, identifier, **changes):
        self.calls.append(("session", identifier, changes))
        return {"id": identifier, **changes}

    def close(self):
        self.closed = True


def scope(path="/api/runs", method="POST", headers=(), query=b""):
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "scheme": "http", "method": method,
            "path": path, "raw_path": path.encode("ascii"), "query_string": query,
            "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 12345),
            "headers": list(headers)}


class ASGITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.probe = Probe()
        self.api = create_app(self.probe, 8765)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api),
                                        base_url="http://127.0.0.1:8765")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def invoke(self, headers, messages, path="/api/runs"):
        sent = []
        queue = asyncio.Queue()
        for message in messages:
            queue.put_nowait(message)
        await self.api(scope(path=path, headers=headers), queue.get, self._collector(sent))
        return sent

    @staticmethod
    def _collector(sent):
        async def send(message):
            sent.append(message)
        return send

    async def test_factory_and_lifespan_do_not_open_or_close_application(self):
        with patch("sqlite3.connect", side_effect=AssertionError("factory opened DB")):
            api = create_app(self.probe, 8765)
            async with api.router.lifespan_context(api):
                self.assertEqual(self.probe.calls, [])
        self.assertFalse(self.probe.closed)
        self.assertEqual(self.probe.thread_ids, [])

    async def test_sync_application_methods_run_outside_event_loop(self):
        event_loop_thread = threading.get_ident()
        self.assertEqual((await self.client.get("/api/info")).status_code, 200)
        self.assertTrue(self.probe.thread_ids)
        self.assertNotIn(event_loop_thread, self.probe.thread_ids)

    async def test_raw_asgi_duplicates_and_framing_are_rejected_without_dispatch(self):
        good = [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json"),
                (b"content-length", b"2")]
        variants = [(good + [(b"host", b"127.0.0.1:8765")], 403),
                    (good + [(b"origin", b"http://127.0.0.1:8765")] * 2, 403),
                    (good + [(b"content-length", b"2")], 400),
                    (good + [(b"content-type", b"application/json")], 400),
                    (good + [(b"transfer-encoding", b"chunked")], 400),
                    (good[:-1] + [(b"content-length", b"65537")], 413)]
        for headers, expected in variants:
            with self.subTest(headers=headers):
                result = await self.invoke(headers, [{"type": "http.request", "body": b"{}"}])
                self.assertEqual(result[0]["status"], expected)
        self.assertEqual(self.probe.calls, [])

    async def test_partial_and_overflow_asgi_body_never_dispatch(self):
        headers = [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json"),
                   (b"content-length", b"2")]
        for messages in ([{"type": "http.request", "body": b"{"}],
                         [{"type": "http.request", "body": b"{" * (MAX_BODY + 1)}],
                         [{"type": "http.request", "body": b"{", "more_body": True},
                          {"type": "http.disconnect"}]):
            result = await self.invoke(headers, messages)
            if result:
                self.assertEqual(result[0]["status"], 400)
        self.assertEqual(self.probe.calls, [])

    async def test_exact_body_byte_limit_accepts_bounded_chunks(self):
        payload = json.dumps({"prompt": "实际文本"}, ensure_ascii=False).encode("utf-8")
        data = payload + b" " * (MAX_BODY - len(payload))
        headers = [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json"),
                   (b"content-length", str(MAX_BODY).encode("ascii"))]
        messages = [{"type": "http.request", "body": data[:4096], "more_body": True},
                    {"type": "http.request", "body": data[4096:], "more_body": False}]
        result = await self.invoke(headers, messages)
        self.assertEqual(result[0]["status"], 201)
        self.assertEqual(self.probe.calls, [("submit", "实际文本", None, None)])
        self.assertTrue(all(name == name.lower() for name, value in result[0]["headers"]))

    async def test_strict_models_keep_null_bool_and_text_boundaries(self):
        invalid = [("/api/runs", {"prompt": "x", "request_id": "valid\n"}),
                   ("/api/runs", {"prompt": "x" * 16001}),
                   ("/api/runs/run-1/approve", {"approved": 1}),
                   ("/api/runs/run-1/approve", {"approved": "true"}),
                   ("/api/sessions/s-1", {"archived": None}),
                   ("/api/sessions/s-1", {"archived": 0}),
                   ("/api/sessions/s-1", {"title": None}),
                   ("/api/sessions/s-1", {"title": " "}),
                   ("/api/sessions/s-1", {"title": "x" * 121}),
                   ("/api/runs/run-1/resolve", {"note": "x" * 2001})]
        for path, body in invalid:
            with self.subTest(path=path, body=str(body)[:80]):
                response = await self.client.post(path, json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"error": "invalid_request"})
        self.assertEqual(self.probe.calls, [])
        prompt = "  keep spaces\n"
        self.assertEqual((await self.client.post("/api/runs", json={"prompt": prompt, "request_id": "r-1"})).status_code, 201)
        self.assertEqual(self.probe.calls, [("submit", prompt, None, "r-1")])

    async def test_json_is_validated_before_pydantic(self):
        for data in (b'{"prompt":"a","prompt":"b"}', b'{"prompt":NaN}',
                     b'{"prompt":Infinity}', b'[]', b'null', b'\xff',
                     b'{"prompt":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'):
            with self.subTest(data=data):
                response = await self.client.post("/api/runs", content=data, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.probe.calls, [])

    async def test_routes_are_closed_and_nonce_is_per_response(self):
        for path in ("/docs", "/redoc", "/openapi.json", "/api/info/", "/api/info?x=1",
                     "/static/index.html", "/%61pi/info"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn("location", response.headers)
            self.assertEqual(response.json(), {"error": "not_found"})
        response = await self.client.post("/api/runs/run-1/not-a-control", json={"extra": 1})
        self.assertEqual(response.status_code, 404)
        first, second = await self.client.get("/"), await self.client.get("/")
        self.assertNotEqual(first.headers["content-security-policy"], second.headers["content-security-policy"])
        for response in (first, second):
            self.assertNotIn("__NONCE__", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-frame-options"], "DENY")

    async def test_sse_disconnect_leaves_run_alive_and_post_is_not_blocked(self):
        queue, messages, snapshot = asyncio.Queue(), [], asyncio.Event()
        queue.put_nowait({"type": "http.request", "body": b""})

        async def send(message):
            messages.append(message)
            if b"event: snapshot" in message.get("body", b""):
                snapshot.set()

        request = scope("/api/runs/run-1/events", "GET", [(b"host", b"127.0.0.1:8765")])
        subscriber = asyncio.create_task(self.api(request, queue.get, send))
        try:
            await asyncio.wait_for(snapshot.wait(), timeout=2)
            self.assertFalse(subscriber.done())
            response = await asyncio.wait_for(self.client.post("/api/runs", json={"prompt": "concurrent"}), timeout=2)
            self.assertEqual(response.status_code, 201)
            queue.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(subscriber, timeout=2)
        finally:
            if not subscriber.done():
                subscriber.cancel()
                await asyncio.gather(subscriber, return_exceptions=True)
        self.assertEqual(self.probe.status, "running")
        self.assertFalse(self.probe.closed)
        self.assertEqual(self.probe.calls, [("submit", "concurrent", None, None)])
        self.assertNotIn(threading.get_ident(), self.probe.thread_ids)


class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = Probe()
        cls.server = create_server(cls.probe, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        if cls.thread.is_alive():
            raise AssertionError("HTTP server thread remained alive")

    def setUp(self):
        self.probe.calls.clear()

    def raw(self, headers, body=b"{}", *, half_close=False):
        with socket.create_connection(self.server.server_address, timeout=8) as client:
            wire = (f"POST /api/runs/run-1/cancel HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                    "Content-Type: application/json\r\nConnection: close\r\n" + headers + "\r\n\r\n").encode("ascii")
            client.sendall(wire + body)
            if half_close:
                client.shutdown(socket.SHUT_WR)
            result = b""
            while True:
                piece = client.recv(4096)
                if not piece:
                    return result
                result += piece

    def test_equivalent_content_lengths_normalize_and_dispatch_once(self):
        # h11's RFC normalization differs from the previous stdlib server.
        for header in ("Content-Length: 2\r\nContent-Length: 2", "Content-Length: 2,2"):
            before = len(self.probe.calls)
            response = self.raw(header)
            self.assertTrue(response.startswith(b"HTTP/1.1 200"), response)
            self.assertEqual(len(self.probe.calls), before + 1)
        self.assertTrue(self.raw("Content-Length: 2").startswith(b"HTTP/1.1 200"))
        self.assertEqual(self.probe.calls, [("cancel", "run-1")] * 3)

    def test_ambiguous_framing_is_rejected_before_dispatch(self):
        for header in ("Content-Length: 2\r\nContent-Length: 3", "Content-Length: 2,3",
                       "Content-Length: +2", "Content-Length: bad", "Content-Length: 2\r\nTransfer-Encoding: chunked"):
            with self.subTest(header=header):
                response = self.raw(header)
                self.assertTrue(response.startswith(b"HTTP/1.1 400"), response)
        self.assertEqual(self.probe.calls, [])

    def test_duplicate_host_and_origin_are_rejected_before_dispatch(self):
        for header, status in ((f"Host: 127.0.0.1:{self.server.server_port}\r\nContent-Length: 2", b"400"),
                               ("Origin: null\r\nOrigin: null\r\nContent-Length: 2", b"403")):
            response = self.raw(header)
            self.assertTrue(response.startswith(b"HTTP/1.1 " + status), response)
        self.assertEqual(self.probe.calls, [])

    def test_incomplete_and_stalled_body_never_dispatch(self):
        response = self.raw("Content-Length: 2", b"{", half_close=True)
        self.assertTrue(not response or response.startswith(b"HTTP/1.1 400"), response)
        # Real receive timeout; no guessed sleep controls the outcome.
        response = self.raw("Content-Length: 2", b"{")
        self.assertTrue(response.startswith(b"HTTP/1.1 400"), response)
        self.assertEqual(self.probe.calls, [])

    def test_keepalive_request_boundaries_after_normalized_length(self):
        with socket.create_connection(self.server.server_address, timeout=3) as client:
            request = (f"POST /api/runs/run-1/cancel HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                       "Content-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}").encode()
            client.sendall(request)
            first = http.client.HTTPResponse(client)
            first.begin()
            self.assertEqual(first.status, 200)
            first.read()
            first.close()
            client.sendall(request.replace(b"Content-Length: 2\r\nContent-Length: 2", b"Content-Length: 2\r\nConnection: close"))
            second = http.client.HTTPResponse(client)
            second.begin()
            self.assertEqual(second.status, 200)
            second.read()
            second.close()
        self.assertEqual(self.probe.calls, [("cancel", "run-1")] * 2)


class OwnershipTests(unittest.TestCase):
    def test_http_shutdown_preserves_application_and_database_reopens(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "runs.sqlite3"
            with Application(DemoModel(), database=database, autostart=False) as app:
                server = create_server(app, port=0)
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                    connection.request("GET", "/api/info")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                    connection.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertFalse(app._closed)
                run = app.submit("local lifecycle check")
                self.assertEqual(app.get_run(run["id"])["status"], "pending")
            with Application(DemoModel(), database=database, autostart=False) as reopened:
                self.assertEqual(reopened.get_run(run["id"])["status"], "pending")


if __name__ == "__main__":
    unittest.main()
