"""Real local management HTTP and MCP services; all inputs and effects are synthetic."""
import http.client
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from astra_harness.config import load_model_environment
from astra_harness.web import create_server
from examples._mcp_profiles_fixture import DiscoveryFixture
from examples.mcp_profiles_demo import run_demo
from tests.test_workspace_host import WorkspaceHostFixture


class MCPProfilesWebTests(WorkspaceHostFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fixture = DiscoveryFixture().__enter__()
        self.addCleanup(self.fixture.close)
        self.open_host()

    def open_host(self):
        with patch("astra_harness.cli.load_model_environment", side_effect=lambda path: load_model_environment(path, environ={})):
            self.app, self.services, self.resources = self.build()
        self.controller = self.services["mcp_profiles"]
        self.http = create_server(self.app, port=0, workspaces=self.services["workspaces"], mcp_profiles=self.controller)
        self.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        http, thread = self.http, self.thread
        def close():
            http.shutdown()
            http.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)

    def reopen_host(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.resources.close()
        self.open_host()

    def request(self, action="", body=None, *, path=None, headers=None):
        path = path or "/api/mcp-profiles" + ("/" + action if action else "")
        fields = {"Host": "127.0.0.1:%s" % self.port}
        if body is not None:
            body = json.dumps(body).encode() if not isinstance(body, bytes) else body
            fields["Content-Type"] = "application/json"
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=12)
        try:
            connection.request("POST" if body is not None else "GET", path, body, fields)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def catalog(self):
        status, value = self.request()
        self.assertEqual(status, 200, value)
        return value

    def save(self, **changes):
        profile = {"name": "synthetic", "transport": "http", "endpoint": self.fixture.url, "timeout": 3, **changes}
        status, value = self.request("save", {"profile": profile, "expected_revision": self.catalog()["revision"]})
        self.assertEqual(status, 200, value)
        return value["items"][-1]

    def body(self, item, *, confirmed=None):
        result = {"id": item["id"], "expected_identity": item["identity"], "expected_revision": self.catalog()["revision"]}
        if confirmed is not None:
            result["confirmed"] = confirmed
        return result

    def enable(self, item):
        status, value = self.request("enable", self.body(item, confirmed=True))
        self.assertEqual(status, 200, value)
        self.assertEqual(value["items"][0]["state"], "ready")
        return value

    def test_saved_metadata_test_enable_approval_disable_delete_complete_loop(self):
        with patch("astra_harness.builtin.mcp_profile_connection.ProfileConnection.start", side_effect=AssertionError("save started service")):
            item = self.save()
        self.assertEqual(self.fixture.requests, [])
        self.assertFalse(item["enabled"])
        self.assertEqual(item["state"], "disabled")
        status, tested = self.request("test", self.body(item, confirmed=True))
        self.assertEqual(status, 200, tested)
        self.assertTrue(tested["ok"])
        self.assertFalse(tested["registered"])
        self.assertEqual(tested["tools"], ["mcp.synthetic.record"])
        self.assertEqual(self.fixture.receipts, [])
        self.assertNotIn("mcp.synthetic.record", self.app.info()["tools"])
        self.assertEqual([request["method"] for request in self.fixture.requests],
            ["initialize", "notifications/initialized", "tools/list", "DELETE"])
        self.enable(item)
        waiting = self.app.wait(self.app.submit_tool("mcp.synthetic.record", {"value": 27})["id"])
        self.assertEqual(waiting["status"], "waiting_approval")
        self.assertEqual(self.fixture.receipts, [])
        status, _ = self.request(body={"approved": True}, path="/api/runs/" + waiting["id"] + "/approve")
        self.assertEqual(status, 200)
        final = self.app.wait(waiting["id"])
        self.assertEqual((final["status"], final["model_calls"], final["tool_calls"]), ("completed", 0, 1))
        self.assertEqual(self.fixture.receipts, [{"value": 27, "sequence": 1}])
        status, disabled = self.request("disable", self.body(item))
        self.assertEqual(status, 200, disabled)
        self.assertNotIn("mcp.synthetic.record", self.app.info()["tools"])
        self.assertEqual(self.request("delete", self.body(item))[0], 200)
        self.assertEqual(self.catalog()["items"], [])

    def test_confirmation_strict_types_revision_identity_and_origin_reject_before_start(self):
        item = self.save()
        for action in ("test", "enable"):
            for confirmation in (None, False, 1, "true"):
                self.assertEqual(self.request(action, self.body(item, confirmed=confirmation))[0], 400)
        for action in ("test", "enable", "disable", "delete"):
            body = self.body(item, confirmed=True if action in {"test", "enable"} else None)
            for change in ({"expected_revision": 0}, {"expected_identity": "0" * 64}):
                status, result = self.request(action, {**body, **change})
                self.assertEqual((status, result), (409, {"error": "mcp_profile_conflict"}))
            for change in ({"expected_revision": True}, {"expected_revision": float("nan")}, {"expected_identity": True}):
                self.assertEqual(self.request(action, {**body, **change})[0], 400)
        for headers in ({"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}):
            self.assertEqual(self.request("enable", self.body(item, confirmed=True), headers=headers)[0], 403)
        self.assertEqual(self.fixture.requests, [])

    def test_secret_fields_and_remote_http_rejected_without_echo(self):
        sentinel = "synthetic-secret-must-not-echo"
        for change in ({"api_key": sentinel}, {"env": {"KEY": sentinel}}, {"endpoint": "http://evil.invalid/mcp"}):
            profile = {"name": "synthetic", "transport": "http", "endpoint": self.fixture.url, **change}
            status, value = self.request("save", {"profile": profile, "expected_revision": 0})
            self.assertEqual((status, value), (400, {"error": "invalid_mcp_profile"}))
            self.assertNotIn(sentinel, json.dumps(value))
        self.assertEqual(self.catalog()["items"], [])
        self.assertEqual(self.fixture.requests, [])

    def test_stdio_save_is_pure_and_test_discovers_without_registering(self):
        fixture_path = Path(__file__).resolve().parents[1] / "examples/_mcp_profiles_fixture.py"
        profile = {"name": "stdiofixture", "transport": "stdio", "command": str(Path(sys._base_executable).resolve()),
            "args": ["-I", "-S", str(fixture_path), "--stdio"], "cwd": ".", "env_refs": {}, "timeout": 3}
        with patch("subprocess.Popen", side_effect=AssertionError("save spawned process")):
            status, value = self.request("save", {"profile": profile, "expected_revision": 0})
        self.assertEqual(status, 200, value)
        item = value["items"][0]
        status, tested = self.request("test", self.body(item, confirmed=True))
        self.assertEqual(status, 200, tested)
        self.assertEqual(tested["tools"], ["mcp.stdiofixture.record"])
        self.assertFalse(tested["registered"])
        self.assertNotIn("mcp.stdiofixture.record", self.app.info()["tools"])

    def test_management_is_startup_bound_and_other_workspace_only_reads_catalog(self):
        item = self.save()
        selected = self.select(self.app, self.services["workspaces"], self.other)
        catalog = self.catalog()
        self.assertEqual(catalog["bound_workspace"], self.app.startup_workspace_id)
        self.assertEqual(catalog["current_workspace"], selected["selected_id"])
        self.assertFalse(catalog["actions_allowed"])
        for action in ("test", "enable", "disable", "delete"):
            body = self.body(item, confirmed=True if action in {"test", "enable"} else None)
            self.assertEqual(self.request(action, body), (409, {"error": "workspace_not_configured"}))
        self.assertEqual(self.request("save", {"profile": {"name": "other", "transport": "http", "endpoint": self.fixture.url},
            "expected_revision": catalog["revision"]}), (409, {"error": "workspace_not_configured"}))
        self.assertEqual(self.fixture.requests, [])

    def test_pending_approval_blocks_runtime_changes_and_denial_is_side_effect_free(self):
        item = self.save()
        self.enable(item)
        waiting = self.app.wait(self.app.submit_tool("mcp.synthetic.record", {"value": 41})["id"])
        requests = len(self.fixture.requests)
        status, result = self.request("disable", self.body(item))
        self.assertEqual((status, result), (409, {"error": "runtime_update_busy"}))
        self.assertEqual(len(self.fixture.requests), requests)
        self.assertEqual(self.fixture.receipts, [])
        self.app.approve(waiting["id"], approved=False)
        self.assertEqual(self.app.wait(waiting["id"])["error"], "approval_denied")
        self.assertEqual(self.request("disable", self.body(item))[0], 200)
        self.assertEqual(self.fixture.receipts, [])

    def test_restart_is_offline_and_old_approval_cannot_follow_new_connection(self):
        item = self.save()
        self.enable(item)
        waiting = self.app.wait(self.app.submit_tool("mcp.synthetic.record", {"value": 55})["id"])
        self.reopen_host()
        after_reopen = len(self.fixture.requests)
        restored = self.catalog()["items"][0]
        self.assertTrue(restored["enabled"])
        self.assertEqual(restored["state"], "offline")
        self.assertTrue(restored["activation_required"])
        self.assertNotIn("mcp.synthetic.record", self.app.info()["tools"])
        self.assertEqual(self.app.get_run(waiting["id"])["status"], "failed")
        self.assertEqual(self.request(body={"approved": True}, path="/api/runs/" + waiting["id"] + "/approve")[0], 409)
        self.assertEqual(len(self.fixture.requests), after_reopen)
        self.enable(restored)
        self.assertEqual(self.request(body={"approved": True}, path="/api/runs/" + waiting["id"] + "/approve")[0], 409)
        self.assertEqual(self.fixture.receipts, [])

    def test_unknown_receipt_blocks_update_and_restart_does_not_replay(self):
        self.fixture.server.mode = "drop_after_effect"
        item = self.save()
        self.enable(item)
        waiting = self.app.wait(self.app.submit_tool("mcp.synthetic.record", {"value": 63})["id"])
        self.app.approve(waiting["id"])
        final = self.app.wait(waiting["id"])
        self.assertEqual(final["status"], "unknown")
        self.assertEqual(self.fixture.receipts, [{"value": 63, "sequence": 1}])
        self.assertEqual(self.request("disable", self.body(item)), (409, {"error": "runtime_update_busy"}))
        self.reopen_host()
        self.assertEqual(self.app.get_run(waiting["id"])["status"], "unknown")
        self.assertEqual(self.request("enable", self.body(item, confirmed=True)), (409, {"error": "runtime_update_busy"}))
        self.assertEqual(self.fixture.receipts, [{"value": 63, "sequence": 1}])

    def test_host_close_during_issued_tool_keeps_unknown_without_replay(self):
        self.fixture.server.mode = "trickle"
        item = self.save()
        self.enable(item)
        waiting = self.app.wait(self.app.submit_tool("mcp.synthetic.record", {"value": 71})["id"])
        self.app.approve(waiting["id"])
        self.assertTrue(self.fixture.server.called.wait(3))
        self.reopen_host()
        final = self.app.get_run(waiting["id"])
        self.assertEqual((final["status"], final["model_calls"], final["tool_calls"]), ("unknown", 0, 1))
        self.assertEqual(self.fixture.receipts, [{"value": 71, "sequence": 1}])
        self.assertNotIn("mcp.synthetic.record", self.app.info()["tools"])

    def test_disconnected_discovery_cleans_socket_and_releases_runtime_reservation(self):
        self.fixture.hold_initialize = True
        item = self.save()
        body = json.dumps(self.body(item, confirmed=True)).encode()
        client = socket.create_connection(("127.0.0.1", self.port), timeout=4)
        try:
            client.sendall(("POST /api/mcp-profiles/test HTTP/1.1\r\nHost: 127.0.0.1:%s\r\n"
                "Content-Type: application/json\r\nContent-Length: %s\r\n\r\n" % (self.port, len(body))).encode() + body)
            self.assertTrue(self.fixture.initializing.wait(3))
            client.shutdown(socket.SHUT_RDWR)
        finally:
            client.close()
        self.assertTrue(self.fixture.disconnected.wait(5), "discovery socket remained open")
        deadline = time.monotonic() + 5
        while self.catalog()["operation"] is not None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIsNone(self.catalog()["operation"])
        self.assertFalse(self.catalog()["cleanup_required"])
        self.fixture.hold_initialize = False
        self.assertEqual(self.request("test", self.body(item, confirmed=True))[0], 200)
        self.assertEqual(self.fixture.receipts, [])
        self.assertEqual(self.app.wait(self.app.submit_tool("add", {"a": 2, "b": 3})["id"])["status"], "completed")


class MCPProfilesDemoTests(unittest.TestCase):
    def test_retained_demo_matches_persisted_runs_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "demo"
            summary = run_demo(output)
            self.assertTrue(summary["passed"])
            self.assertTrue(summary["retained"])
            self.assertEqual((summary["run_count"], summary["tool_calls"], summary["model_calls"]), (5, 3, 0))
            self.assertEqual(summary["external_provider_requests"], 0)
            stored = []
            for case in ("lifecycle", "uncertain"):
                with closing(sqlite3.connect(output / case / "runs.sqlite3")) as database:
                    stored.extend(json.loads(row[0]) for row in database.execute("SELECT state FROM runs"))
            self.assertEqual(len(stored), 5)
            self.assertEqual(sorted(run["status"] for run in stored), ["completed", "completed", "failed", "failed", "unknown"])
            self.assertEqual(sum(run["tool_calls"] for run in stored), 3)
            before = (output / "summary.json").read_bytes()
            with self.assertRaises(FileExistsError):
                run_demo(output)
            self.assertEqual((output / "summary.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
