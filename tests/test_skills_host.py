"""Skill host composition: no-files, local CLI, scope and retry boundaries."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import http.client
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from astra_harness.cli import build_application, main
from astra_harness.config import load_model_environment
from astra_harness.skill_catalog import SkillCatalogError
from astra_harness.web import create_server
from astra_harness.workspace_binding import WorkspaceError
from tests.test_workspace_host import WorkspaceHostFixture


class SkillsHostTests(WorkspaceHostFixture, unittest.TestCase):
    def write_skill(self, root, body="Use only the selected task materials."):
        path = root / "skills/check/SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: Check\ndescription: Synthetic local guidance\n---\n" + body,
                        encoding="utf-8")
        return path

    def open_host(self, **changes):
        resources = ExitStack()
        self.addCleanup(resources.close)
        services = {}
        with patch("astra_harness.cli.load_model_environment",
                   side_effect=lambda path: load_model_environment(path, environ={})):
            app = build_application(self.args(**changes), resources, autostart=False, web_services=services)
        return app, services

    def scope(self, app):
        binding = app.require_workspace()
        return {key: getattr(binding, key) for key in ("id", "path", "identity")}

    def enable(self, app, catalog):
        scope = self.scope(app)
        listing = catalog.list(scope)
        item, = listing["items"]
        catalog.set_enabled(item["id"], True, listing["revision"], item["identity"], scope)
        return {"id": item["id"], "expected_identity": item["identity"]}

    def test_no_files_endpoints_fail_closed_without_constructing_loader(self):
        self.write_skill(self.startup)
        with patch("astra_harness.builtin.local_skills.LocalSkillLoader",
                   side_effect=AssertionError("no-files must not construct a loader")):
            app, services = self.open_host(no_files=True)
            self.assertIsNone(services["skills"])
            server = create_server(app, port=0, workspaces=services["workspaces"], skills=services["skills"])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(path, body=None):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    headers = {"Host": f"127.0.0.1:{server.server_port}"}
                    if body is not None:
                        headers["Content-Type"] = "application/json"
                    try:
                        connection.request("GET" if body is None else "POST", path,
                            body=None if body is None else json.dumps(body), headers=headers)
                        response = connection.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        connection.close()
                scope = app.selected_workspace_id
                status, listing = request("/api/skills?" + urlencode({"workspace_id": scope}))
                self.assertEqual(status, 200)
                self.assertFalse(listing["available"])
                self.assertEqual(listing["items"], [])
                status, document = request("/api/skills/check?" + urlencode({
                    "workspace_id": scope, "expected_identity": "a" * 64}))
                self.assertEqual((status, document), (409, {"error": "skill_catalog_unavailable"}))
                status, result = request("/api/skills/enabled", {
                    "workspace_id": scope, "id": "check", "expected_identity": "a" * 64,
                    "expected_revision": 0, "enabled": True})
                self.assertEqual((status, result), (409, {"error": "skill_catalog_unavailable"}))
                with self.assertRaises(WorkspaceError) as caught:
                    app.submit("synthetic", task_context_selection={"id": "check", "expected_identity": "a" * 64})
                self.assertEqual(caught.exception.code, "workspace_disabled")
                self.assertEqual(app.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
                self.assertFalse(thread.is_alive())

    def test_workspace_switch_keeps_content_and_enable_consent_separate(self):
        self.write_skill(self.startup, "Instructions A")
        self.write_skill(self.other, "Instructions B")
        app, services = self.open_host()
        catalog = services["skills"]
        first = self.enable(app, catalog)
        self.select(app, services["workspaces"], self.other)
        other, = catalog.list(self.scope(app))["items"]
        self.assertFalse(other["enabled"])
        self.assertNotEqual(first["expected_identity"], other["identity"])
        with self.assertRaisesRegex(SkillCatalogError, "^skill_changed$"):
            app.submit("new task", workspace_id=app.selected_workspace_id, task_context_selection=first)
        second = self.enable(app, catalog)
        state = app.submit("new task", workspace_id=app.selected_workspace_id, task_context_selection=second)
        self.assertEqual(state["task_context"]["text"], "Instructions B")
        self.assertEqual(state["task_context"]["workspace"]["id"], app.selected_workspace_id)
        self.assertEqual(set(app.require_workspace().permissions), {"files.read", "files.write"})

    def test_known_request_confirmation_never_reloads_deleted_skill_or_changes_scope(self):
        source = self.write_skill(self.startup, "Original guidance")
        app, services = self.open_host()
        selection = self.enable(app, services["skills"])
        original_workspace = app.selected_workspace_id
        first = app.submit("same prompt", workspace_id=original_workspace,
                           request_id="skill-confirmation", task_context_selection=selection)
        source.unlink()
        self.select(app, services["workspaces"], self.other)
        with patch("astra_harness.builtin.local_skills.LocalSkillLoader.load",
                   side_effect=AssertionError("accepted request must not reload files")), \
             patch("astra_harness.builtin.local_skills.LocalSkillLoader.catalog",
                   side_effect=AssertionError("accepted request must not rescan files")):
            confirmed = app.submit("same prompt", workspace_id=original_workspace,
                                   request_id="skill-confirmation", task_context_selection=selection)
        self.assertEqual(confirmed["id"], first["id"])
        self.assertEqual(confirmed["task_context"], first["task_context"])
        self.assertEqual(confirmed["workspace_id"], original_workspace)
        self.assertEqual(app.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)

    def test_cli_management_does_not_build_application_read_environment_or_connect(self):
        self.write_skill(self.startup)
        common = ["--workspace", str(self.startup), "--database", str(self.database)]
        with patch("astra_harness.cli.Application", side_effect=AssertionError("must not recover runs")), \
             patch("astra_harness.cli.load_model_environment", side_effect=AssertionError("must not read model environment")), \
             patch("subprocess.Popen", side_effect=AssertionError("must not start MCP")), \
             patch("socket.socket.connect", side_effect=AssertionError("must not connect")):
            for arguments, expected_enabled in ((["skill-list"], False), (["skill-show", "check"], False),
                                                (["skill-enable", "check"], True), (["skill-disable", "check"], False)):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([*arguments, *common]), 0)
                result = json.loads(output.getvalue())
                item = result if "body" in result else result["items"][0]
                self.assertEqual(item["enabled"], expected_enabled)
        # A fresh management process can reopen the same store; no lease leaked.
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["skill-list", *common]), 0)

    def test_cli_management_rejects_no_files_or_live_flags_before_any_host_setup(self):
        before = set(self.root.rglob("*"))
        with patch("astra_harness.cli._skill_command", side_effect=AssertionError("must reject first")):
            for flag in ("--no-files", "--live", "--demo-files"):
                with self.subTest(flag=flag), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main(["skill-list", "--workspace", str(self.startup), flag])
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_cli_management_sanitizes_os_exception(self):
        with patch("astra_harness.builtin.files.FilePlugin", side_effect=PermissionError("private path or token")):
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["skill-list", "--workspace", str(self.startup), "--database", str(self.database)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "skill_catalog_unavailable"})
        self.assertNotIn("private", output.getvalue())


if __name__ == "__main__":
    unittest.main()
