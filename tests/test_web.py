"""真实回环 HTTP 检查；应用替身不调用模型，也不访问真实工作区。"""

import copy
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from astra_harness.web import create_server


class FakeApp:
    def __init__(self):
        self.submissions = []
        self.actions = []
        self.run = {"id": "run-1", "session_id": "session-1", "status": "completed",
                    "output": "<script>unsafe</script>", "error": None,
                    "approval": None, "model_calls": 2, "tool_calls": 1}

    def info(self):
        return {"mode": "demo"}

    def list_sessions(self):
        return [{"id": "session-1", "title": "测试会话"}]

    def get_session(self, identifier):
        if identifier != "session-1":
            raise KeyError(identifier)
        return {"messages": [{"role": "user", "text": "测试"}], "runs": [self.run]}

    def get_run(self, identifier):
        if identifier != "run-1":
            raise KeyError(identifier)
        return copy.deepcopy(self.run)

    def submit(self, prompt, session_id=None, request_id=None):
        if prompt == "conflict":
            raise RuntimeError("private details")
        if prompt == "crash":
            raise Exception("private details")
        self.submissions.append((prompt, session_id, request_id))
        return self.get_run("run-1")

    def approve(self, identifier, approved=True):
        self.actions.append(("approve", identifier, approved))
        return self.get_run(identifier)

    def cancel(self, identifier):
        self.actions.append(("cancel", identifier))
        return self.get_run(identifier)

    def pause(self, identifier):
        self.actions.append(("pause", identifier))
        return self.get_run(identifier)

    def resume(self, identifier):
        self.actions.append(("resume", identifier))
        return self.get_run(identifier)

    def events(self, identifier, after=0):
        self.get_run(identifier)
        return [event for event in [{"seq": 1, "type": "model.start"},
                                    {"seq": 2, "type": "tool.end", "value": 5}] if event["seq"] > after]


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = FakeApp()
        cls.server = create_server(cls.app, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.app.submissions.clear()
        self.app.actions.clear()

    def request(self, path, method="GET", body=None, headers=None):
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            body = json.dumps(body).encode() if not isinstance(body, bytes) else body
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.headers, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_static_page_is_served_with_nonce_and_safe_rendering(self):
        status, headers, text = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("Astra Harness", text)
        self.assertNotIn("__NONCE__", text)
        self.assertNotIn("innerHTML", text)
        self.assertIn('/static/app.mjs', text)
        self.assertIn("nonce-", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_only_declared_static_modules_are_public(self):
        for name, kind in (("app.mjs", "javascript"), ("state.mjs", "javascript"),
                           ("markdown.mjs", "javascript"), ("styles.css", "css")):
            status, headers, body = self.request("/static/" + name)
            self.assertEqual(status, 200)
            self.assertIn(kind, headers["Content-Type"])
            self.assertTrue(body)
        for path in ("/static/../web.py", "/static/%2e%2e/web.py", "/static/index.html"):
            self.assertEqual(self.request(path)[0], 404)

    def test_duplicate_json_keys_are_rejected(self):
        self.assertEqual(self.request("/api/runs/run-1/approve", "POST",
                                     b'{"approved":false,"approved":true}')[0], 400)
        self.assertEqual(self.app.actions, [])

    def test_read_only_endpoints_do_not_submit(self):
        for path in ("/api/info", "/api/sessions", "/api/sessions/session-1", "/api/runs/run-1"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 200)
        self.assertEqual(self.app.submissions, [])

    def test_submit_preserves_request_identifier_and_session(self):
        status, _, text = self.request("/api/runs", "POST", {
            "prompt": "任务", "session_id": "session-1", "request_id": "request-1"
        })
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(text)["id"], "run-1")
        self.assertEqual(self.app.submissions, [("任务", "session-1", "request-1")])

    def test_approval_rejection_and_cancellation_are_explicit(self):
        for approved in (True, False):
            self.assertEqual(self.request("/api/runs/run-1/approve", "POST", {"approved": approved})[0], 200)
        self.assertEqual(self.request("/api/runs/run-1/cancel", "POST", {})[0], 200)
        self.assertEqual(self.app.actions, [("approve", "run-1", True), ("approve", "run-1", False), ("cancel", "run-1")])

    def test_pause_and_resume_only_accept_an_empty_object(self):
        for action in ("pause", "resume"):
            path = f"/api/runs/run-1/{action}"
            for body in ({"approved": True}, {"limits": {}}, [], b"null"):
                with self.subTest(action=action, body=body):
                    self.assertEqual(self.request(path, "POST", body)[0], 400)
            self.assertEqual(self.app.actions, [])
            status, _, body = self.request(path, "POST", {})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["id"], "run-1")
            self.assertEqual(self.app.actions, [(action, "run-1")])
            self.app.actions.clear()

    def test_host_and_origin_are_restricted_before_dispatch(self):
        for headers in ({"Host": "evil.example"}, {"Host": "127.0.0.1:1"},
                        {"Origin": "https://evil.example"}, {"Origin": "null"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/runs", "POST", {"prompt": "任务"}, headers)[0], 403)
        self.assertEqual(self.app.submissions, [])
        status = self.request("/api/info", headers={"Origin": f"http://127.0.0.1:{self.port}"})[0]
        self.assertEqual(status, 200)

    def test_oversized_and_chunked_bodies_are_not_dispatched(self):
        self.assertEqual(self.request("/api/runs", "POST", b"{}", {"Content-Length": "65537"})[0], 413)
        self.assertEqual(self.request("/api/runs", "POST", b"{}", {"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.app.submissions, [])

    def test_input_types_and_fields_are_strict(self):
        for body in ([], {"prompt": 5}, {"prompt": ""}, {"prompt": "任务", "extra": 1},
                     {"prompt": "任务", "session_id": "../bad"}, {"prompt": "任务", "request_id": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.request("/api/runs", "POST", body)[0], 400)
        for body in ({"approved": "false"}, {}, {"approved": True, "extra": 1}):
            self.assertEqual(self.request("/api/runs/run-1/approve", "POST", body)[0], 400)
        self.assertEqual(self.app.submissions, [])
        self.assertEqual(self.app.actions, [])

    def test_invalid_encoding_json_and_content_type_are_rejected(self):
        for body in (b"{", b"\xff", b'{"prompt":NaN}'):
            self.assertEqual(self.request("/api/runs", "POST", body)[0], 400)
        self.assertEqual(self.request("/api/runs", "POST", {}, {"Content-Type": "text/plain"})[0], 400)

    def test_error_responses_do_not_expose_exceptions(self):
        for prompt, expected in (("conflict", 409), ("crash", 500)):
            status, _, text = self.request("/api/runs", "POST", {"prompt": prompt})
            self.assertEqual(status, expected)
            self.assertNotIn("private", text)
        self.assertEqual(self.request("/api/runs/missing")[0], 404)

    def test_sse_replays_only_after_cursor_without_resubmitting(self):
        status, headers, text = self.request("/api/runs/run-1/events?after=1")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertNotIn("id: 1\n", text)
        self.assertIn("id: 2\n", text)
        self.assertIn("event: snapshot", text)
        self.assertEqual(self.app.submissions, [])

    def test_sse_last_event_id_takes_precedence_on_reconnect(self):
        _, _, text = self.request("/api/runs/run-1/events?after=0", headers={"Last-Event-ID": "2"})
        self.assertNotIn("event: run", text)
        self.assertIn("event: snapshot", text)
        self.assertEqual(self.app.submissions, [])

    def test_terminal_stream_drains_all_event_pages(self):
        def many_events(identifier, after=0):
            return [{"seq": seq, "type": "model.delta", "text": "a"}
                    for seq in range(after + 1, min(after + 500, 620) + 1)]
        with patch.object(self.app, "events", many_events):
            _, _, stream = self.request("/api/runs/run-1/events")
        self.assertEqual(stream.count("event: run\n"), 620)
        self.assertLess(stream.index("id: 620\n"), stream.index("event: snapshot"))

    def test_paused_snapshot_keeps_budget_and_stream_open_for_resume(self):
        paused = {**self.app.run, "status": "paused", "pause_requested": False,
                  "resume_status": "waiting_approval", "elapsed": 1.25,
                  "limits": {"max_model_calls": 5, "max_tool_calls": 3, "run_timeout": 12}}
        def complete_after_first_snapshot(identifier, after=0):
            # The server already read the paused state; a subsequent snapshot
            # must still be observed, rather than treating pause as terminal.
            self.app.run["status"] = "completed"
            return []
        with patch.object(self.app, "run", paused), patch.object(self.app, "events", complete_after_first_snapshot):
            status, _, stream = self.request("/api/runs/run-1/events")
        self.assertEqual(status, 200)
        snapshots = [json.loads(block.split("data: ", 1)[1]) for block in stream.split("\n\n")
                     if block.startswith("event: snapshot\n")]
        self.assertEqual([item["status"] for item in snapshots], ["paused", "completed"])
        self.assertEqual(snapshots[0]["resume_status"], "waiting_approval")
        self.assertIs(snapshots[0]["pause_requested"], False)
        self.assertEqual(snapshots[0]["elapsed"], 1.25)
        self.assertEqual(snapshots[0]["limits"], paused["limits"])
        self.assertEqual(self.app.submissions, [])

    def test_invalid_cursor_and_unlisted_paths_are_rejected(self):
        for query in ("after=-1", "after=abc", "after=1&after=2", "other=1"):
            self.assertEqual(self.request("/api/runs/run-1/events?" + query)[0], 400)
        for path in ("/../../README.md", "/static/index.html", "/api/runs/run-1?bad=1"):
            self.assertEqual(self.request(path)[0], 404)

    def test_public_binding_is_rejected(self):
        with self.assertRaises(ValueError):
            create_server(self.app, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
