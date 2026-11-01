"""Real localhost requests verify workspace admission and run-bound operations."""
import http.client
import json
import threading
import unittest

from astra_harness.web import create_server
from tests.test_workspace_host import WorkspaceHostFixture


class WorkspaceWebTests(WorkspaceHostFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app, self.services, _ = self.build()
        self.port = self.server(self.app, self.services)
        self.original = self.catalog()
        self.startup_id = self.original["startup_id"]

    def server(self, app, services):
        server = create_server(app, port=0, knowledge=services.get("knowledge"), workspaces=services["workspaces"])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        return server.server_port

    def request(self, path, method="GET", body=None, headers=None, port=None):
        port = port or self.port
        fields = {"Host": f"127.0.0.1:{port}"}
        if body is not None:
            body = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
            fields["Content-Type"] = "application/json"
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=fields)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def catalog(self):
        status, value = self.request("/api/workspaces")
        self.assertEqual(status, 200)
        return value

    def switch(self, path):
        status, spec = self.request("/api/workspaces/validate", "POST", {"path": str(path)})
        self.assertEqual(status, 200, spec)
        status, result = self.request("/api/workspaces/select", "POST", {"path": spec["path"],
            "expected_identity": spec["identity"], "expected_revision": self.catalog()["revision"]})
        self.assertEqual(status, 200, result)
        return result

    def test_confirmed_selection_changes_browsing_and_rejects_late_old_scope(self):
        preview = self.request("/api/workspaces/validate", "POST", {"path": str(self.other)})
        self.assertEqual(preview[0], 200)
        self.assertEqual(self.catalog(), self.original)
        selected = self.switch(self.other)
        selected_id = selected["selected_id"]
        self.assertNotEqual(selected_id, self.startup_id)
        self.assertEqual(self.request(f"/api/files/read?path=same.txt&workspace_id={selected_id}")[1]["content"], "other-content")
        for path in ("/api/files", "/api/files/read?path=same.txt", "/api/sessions"):
            separator = "&" if "?" in path else "?"
            status, error = self.request(path + separator + "workspace_id=" + self.startup_id)
            self.assertEqual((status, error["error"]), (409, "workspace_not_selected"))
        self.assertEqual(self.request("/api/info")[1]["workspace_id"], selected_id)

    def test_old_approval_and_preview_keep_original_directory_after_switch(self):
        waiting = self.app.wait(self.app.submit_tool("files.write", {"path": "same.txt", "content": "approved-a"},
            request_id="write-a", workspace_id=self.startup_id)["id"])
        self.assertEqual(waiting["status"], "waiting_approval")
        selected = self.switch(self.other)
        status, preview = self.request(f'/api/runs/{waiting["id"]}/preview')
        self.assertEqual(status, 200)
        self.assertIn("startup-content", json.dumps(preview))
        self.assertNotIn("other-content", json.dumps(preview))
        self.assertEqual(self.request(f'/api/runs/{waiting["id"]}/approve', "POST", {"approved": True})[0], 200)
        completed = self.app.wait(waiting["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["workspace_id"], self.startup_id)
        self.assertEqual((self.startup / "same.txt").read_text(), "approved-a")
        self.assertEqual((self.other / "same.txt").read_text(), "other-content")
        self.assertEqual(self.request("/api/sessions?workspace_id=" + selected["selected_id"])[1], [])

    def test_new_submission_cannot_reuse_other_workspace_session_or_stale_root(self):
        first = self.app.wait(self.app.submit_tool("add", {"a": 1, "b": 2}, workspace_id=self.startup_id)["id"])
        selected_id = self.switch(self.other)["selected_id"]
        body = {"prompt": "compute", "session_id": first["session_id"], "request_id": "new-b", "workspace_id": selected_id}
        status, error = self.request("/api/runs", "POST", body)
        self.assertEqual((status, error["error"]), (409, "workspace_session_mismatch"))
        body.pop("session_id")
        body["workspace_id"] = self.startup_id
        self.assertEqual(self.request("/api/runs", "POST", body), (409, {"error": "workspace_not_selected"}))
        self.assertEqual(len(self.app.list_runs()), 1)

    def test_request_confirmation_after_switch_returns_same_run_without_resubmission(self):
        body = {"prompt": "计算 2 + 3", "request_id": "agent-a", "workspace_id": self.startup_id}
        status, first = self.request("/api/runs", "POST", body)
        self.assertEqual(status, 201)
        self.app.wait(first["id"])
        selected_id = self.switch(self.other)["selected_id"]
        status, repeated = self.request("/api/runs", "POST", body)
        self.assertEqual((status, repeated["id"]), (201, first["id"]))
        self.assertNotEqual(self.request("/api/runs", "POST", dict(body, workspace_id=selected_id))[0], 201)
        self.assertEqual(len(self.app.list_runs()), 1)

    def test_cas_forged_identity_and_unknown_fields_leave_selection_unchanged(self):
        preview = self.request("/api/workspaces/validate", "POST", {"path": str(self.other)})[1]
        body = {"path": preview["path"], "expected_identity": "0" * 64, "expected_revision": self.original["revision"]}
        self.assertEqual(self.request("/api/workspaces/select", "POST", body), (409, {"error": "workspace_changed"}))
        body["expected_identity"] = preview["identity"]
        self.assertEqual(self.request("/api/workspaces/select", "POST", dict(body, database="forged.sqlite3"))[0], 400)
        self.assertEqual(self.request("/api/workspaces/select", "POST", dict(body, expected_revision=True))[0], 400)
        self.assertEqual(self.request("/api/workspaces/select", "POST", body)[0], 200)
        self.assertEqual(self.request("/api/workspaces/select", "POST", body), (409, {"error": "workspace_conflict"}))

    def test_host_origin_json_and_path_errors_are_fixed_and_no_files_are_created(self):
        body = {"path": str(self.other)}
        path = "/api/workspaces/validate"
        self.assertEqual(self.request(path, "POST", body, {"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.request(path, "POST", body, {"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.request(path, "POST", b'{"path":"a","path":"b"}')[0], 400)
        self.assertEqual(self.request(path + "?path=forged", "POST", body)[0], 400)
        for target in (str(self.data), str(self.other / "missing"), "//server/private"):
            status, error = self.request(path, "POST", {"path": target})
            self.assertEqual(status, 400)
            self.assertEqual(set(error), {"error"})
            self.assertNotIn(target, json.dumps(error))
        self.assertFalse((self.other / "missing").exists())
        self.assertEqual(self.catalog(), self.original)

    def test_no_files_reports_disabled_and_rejects_validation_and_selection(self):
        app, services, _ = self.build(no_files=True, database=self.data / "no-files.sqlite3")
        port = self.server(app, services)
        status, state = self.request("/api/workspaces", port=port)
        self.assertEqual(status, 200)
        self.assertEqual((state["enabled"], state["reason"]), (False, "files_disabled"))
        self.assertEqual(self.request("/api/workspaces/validate", "POST", {"path": str(self.other)}, port=port)[0], 403)
        body = {"path": str(self.other), "expected_identity": "a" * 64, "expected_revision": state["revision"]}
        self.assertEqual(self.request("/api/workspaces/select", "POST", body, port=port)[0], 403)
        self.assertEqual(app.info()["tools"], ["add"])

    def test_missing_selected_directory_can_still_switch_back_using_catalog(self):
        selected = self.switch(self.other)
        self.other.rename(self.root / "offline-directory")
        self.assertEqual(self.catalog()["selected_id"], selected["selected_id"])
        status, info = self.request("/api/info")
        self.assertEqual(status, 200)
        self.assertFalse(info["workspace_available"])
        recovered = self.switch(self.startup)
        self.assertEqual(recovered["selected_id"], self.startup_id)
        self.assertEqual(self.request("/api/files/read?path=same.txt&workspace_id=" + self.startup_id)[1]["content"], "startup-content")

    def test_knowledge_routes_are_disabled_outside_startup_and_return_on_switch_back(self):
        materials = self.startup / "materials"
        materials.mkdir()
        (materials / "guide.md").write_text("synthetic text", encoding="utf-8")
        config = self.data / "knowledge.json"
        config.write_text(json.dumps({"collections": [{"id": "manual", "label": "Manual", "path": "materials", "index": "manual.sqlite3"}]}), encoding="utf-8")
        app, services, _ = self.build(knowledge_config=config, database=self.data / "knowledge-runs.sqlite3")
        port = self.server(app, services)
        self.assertTrue(self.request("/api/knowledge", port=port)[1]["enabled"])
        self.select(app, services["workspaces"], self.other)
        self.assertEqual(self.request("/api/knowledge", port=port)[1],
                         {"enabled": False, "collections": [], "reason": "workspace_not_configured"})
        for endpoint in ("", "/embedding-config", "/embedding-status"):
            self.assertEqual(self.request("/api/knowledge/manual" + endpoint, port=port),
                             (409, {"error": "workspace_not_configured"}))
        operation = {"action": "search", "arguments": {"query": "text"}, "request_id": "blocked-knowledge"}
        self.assertEqual(self.request("/api/knowledge/manual/operations", "POST", operation, port=port)[0], 409)
        self.assertEqual(app.list_runs(), [])
        self.select(app, services["workspaces"], self.startup)
        self.assertTrue(self.request("/api/knowledge", port=port)[1]["enabled"])


if __name__ == "__main__":
    unittest.main()
