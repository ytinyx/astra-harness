"""One worker, immutable directory bindings, real temporary-file oracles."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from astra_harness.application import Application, WorkspaceBinding, WorkspaceError
from astra_harness.builtin.files import FilePlugin, inspect_workspace_directory
from astra_harness.contracts import ModelResponse
from astra_harness.tools import Tool


class RecordingModel:
    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelResponse(text="synthetic answer")


class WorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-workspace-runtime-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.database = self.base / "runs.sqlite3"
        self.a, self.b = self.base / "A", self.base / "B"
        self.a.mkdir()
        self.b.mkdir()
        self.specs = {name: {"id": name, **inspect_workspace_directory(path)}
                      for name, path in (("a", self.a), ("b", self.b))}
        self.extra = {}
        self.model = RecordingModel()

    def factory(self, spec):
        files = FilePlugin(spec["path"])
        scope = spec["id"]
        tools = files.tools + (Tool("scope_" + scope, "fixture scope", (), lambda: {"scope": scope},
                                   input_schema={"type": "object", "properties": {}, "additionalProperties": False}),)
        tools += self.extra.get(scope, ())
        return WorkspaceBinding(scope, spec["path"], spec["identity"], tools,
                                ("files.read", "files.write", "scope." + scope), files)

    def app(self, **kwargs):
        defaults = {"workspace_factory": self.factory, "workspace_spec": self.specs["a"],
                    "workspace": self.a}
        defaults.update(kwargs)
        app = Application(self.model, database=self.database, **defaults)
        self.addCleanup(app.close)
        return app

    def select(self, app, scope):
        return app.select_workspace(self.specs[scope], app.workspaces()["revision"])

    def write_approval(self, app, content="approved A", **kwargs):
        run = app.wait(app.submit_tool("files.write", {"path": "result.md", "content": content}, **kwargs)["id"])
        self.assertEqual(run["status"], "waiting_approval")
        return run

    def test_approval_preview_and_restart_remain_in_original_directory(self):
        (self.a / "result.md").write_text("original A", encoding="utf-8")
        (self.b / "result.md").write_text("original B", encoding="utf-8")
        app = self.app()
        run = self.write_approval(app)
        self.select(app, "b")
        self.assertEqual(app.preview_write(run["id"])["before"], "original A")
        self.assertEqual(app.info()["workspace_id"], "b")
        app.close()
        app = self.app()
        self.assertEqual(app.selected_workspace_id, "b")
        self.assertEqual(app.get_run(run["id"])["workspace_path"], str(self.a))
        self.assertEqual(app.get_run(run["id"])["status"], "waiting_approval")
        app.approve(run["id"])
        self.assertEqual(app.wait(run["id"])["status"], "completed")
        self.assertEqual((self.a / "result.md").read_text(), "approved A")
        self.assertEqual((self.b / "result.md").read_text(), "original B")
        self.assertEqual(self.model.requests, [])

    def test_switch_during_call_preserves_one_worker_and_bound_effect(self):
        entered, release = threading.Event(), threading.Event()
        files = FilePlugin(self.a)
        def slow_write():
            entered.set()
            if not release.wait(4):
                raise RuntimeError("fixture was not released")
            return files.write_file("slow.md", "from A")
        self.extra["a"] = (Tool("slow_write", "fixture", (), slow_write,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            side_effect=True, requires_approval=True),)
        app = self.app()
        worker = app._thread
        run = app.wait(app.submit_tool("slow_write", {})["id"])
        app.approve(run["id"])
        try:
            self.assertTrue(entered.wait(2))
            self.select(app, "b")
            queued = app.submit_tool("scope_b", {})
            self.assertIs(app._thread, worker)
            self.assertEqual(app.get_run(queued["id"])["tool_calls"], 0)
        finally:
            release.set()
        self.assertEqual(app.wait(run["id"])["status"], "completed")
        result = app.wait(queued["id"])
        self.assertEqual(result["tool_result"]["value"], {"scope": "b"})
        self.assertEqual((self.a / "slow.md").read_text(), "from A")
        self.assertFalse((self.b / "slow.md").exists())

    def test_sessions_context_and_permissions_do_not_follow_new_selection(self):
        app = self.app()
        first = app.wait(app.submit("conversation A")["id"])
        self.select(app, "b")
        self.assertEqual(app.list_sessions(), [])
        with self.assertRaisesRegex(WorkspaceError, "workspace_session_mismatch"):
            app.submit("leak A history into B", session_id=first["session_id"])
        second = app.wait(app.submit("conversation B")["id"])
        request = self.model.requests[-1]
        self.assertNotIn("conversation A", [message.text for message in request.messages])
        self.assertIn("scope_b", [tool.name for tool in request.tools])
        self.assertNotIn("scope_a", [tool.name for tool in request.tools])
        self.assertIn("scope.b", second["permissions"])
        self.assertNotIn("scope.a", second["permissions"])
        self.assertEqual(app.get_session(first["session_id"])["workspace_id"], "a")
        self.select(app, "a")
        continued = app.wait(app.submit("continue A", session_id=first["session_id"])["id"])
        self.assertEqual(continued["workspace_id"], "a")
        self.assertEqual([message.text for message in self.model.requests[-1].messages if message.role == "user"],
                         ["conversation A", "continue A"])

    def test_known_request_confirmation_survives_switch_but_new_stale_request_does_not(self):
        app = self.app()
        first = app.wait(app.submit_tool("scope_a", {}, request_id="original", workspace_id="a")["id"])
        self.select(app, "b")
        confirmed = app.submit_tool("scope_a", {}, request_id="original", workspace_id="a")
        self.assertEqual(confirmed["id"], first["id"])
        with self.assertRaises(ValueError):
            app.submit_tool("scope_a", {}, request_id="original", workspace_id="b")
        with self.assertRaisesRegex(WorkspaceError, "workspace_not_selected"):
            app.submit_tool("scope_a", {}, request_id="new-stale", workspace_id="a")
        self.assertEqual(len(app.list_runs()), 1)

    def test_late_files_and_session_list_requests_reject_old_selection(self):
        (self.a / "same.txt").write_text("A", encoding="utf-8")
        (self.b / "same.txt").write_text("B", encoding="utf-8")
        app = self.app()
        self.select(app, "b")
        for operation in (lambda: app.list_files(workspace_id="a"),
                          lambda: app.read_file("same.txt", workspace_id="a"),
                          lambda: app.search_files("A", workspace_id="a"),
                          lambda: app.list_sessions(workspace_id="a")):
            with self.assertRaisesRegex(WorkspaceError, "workspace_not_selected"):
                operation()
        self.assertEqual(app.read_file("same.txt", workspace_id="b")["content"], "B")

    def test_selection_compare_and_swap_does_not_alter_pending_run(self):
        app = self.app()
        pending = self.write_approval(app)
        before = app.store.get(pending["id"])
        old_revision = app.workspaces()["revision"]
        self.select(app, "b")
        with self.assertRaisesRegex(WorkspaceError, "workspace_conflict"):
            app.select_workspace(self.specs["a"], old_revision)
        self.assertEqual(app.selected_workspace_id, "b")
        self.assertEqual(app.store.get(pending["id"]), before)

    def test_directory_replacement_invalidates_old_approval_and_cannot_overwrite_catalog(self):
        app = self.app()
        self.select(app, "b")
        pending = self.write_approval(app, "old B effect")
        app.close()
        self.b.rename(self.base / "B-before")
        self.b.mkdir()
        app = self.app()
        self.assertEqual(app.selected_workspace_id, "b")
        self.assertFalse(app.info()["workspace_available"])
        self.assertEqual(app.get_run(pending["id"])["error"], "workspace_changed")
        with self.assertRaises(RuntimeError):
            app.approve(pending["id"])
        replacement = {"id": "b", **inspect_workspace_directory(self.b)}
        with self.assertRaisesRegex(WorkspaceError, "workspace_changed"):
            app.select_workspace(replacement, app.workspaces()["revision"])
        self.select(app, "a")
        self.assertTrue(app.info()["workspace_available"])
        self.assertFalse((self.b / "result.md").exists())
        self.assertFalse((self.base / "B-before" / "result.md").exists())

    def test_pause_resume_uses_original_engine_after_selection(self):
        app = self.app(autostart=False)
        pending = app.submit_tool("scope_a", {})
        app.pause(pending["id"])
        self.select(app, "b")
        app.resume(pending["id"])
        app.start()
        result = app.wait(pending["id"])
        self.assertEqual(result["tool_result"]["value"], {"scope": "a"})
        self.assertEqual(result["workspace_id"], "a")

    def test_unknown_operation_blocks_every_workspace(self):
        app = self.app(autostart=False)
        run = app.submit_tool("scope_a", {})
        run.update(status="unknown", error="fixture_unknown", unknown_process="legacy")
        app.store.save(run)
        self.select(app, "b")
        with self.assertRaisesRegex(RuntimeError, "未知"):
            app.submit("must not start")
        app.start()
        self.assertEqual(len(app.store.runs()), 1)
        self.assertEqual(self.model.requests, [])

    def test_legacy_pending_fails_without_claiming_past_directory_identity(self):
        legacy = Application(self.model, database=self.database, autostart=False)
        pending = legacy.submit("legacy pending")
        finished = legacy.submit("legacy terminal")
        finished.update(status="failed", error="fixture_terminal")
        legacy.store.save(finished)
        before_terminal = legacy.store.get(finished["id"])
        legacy.close()
        app = self.app()
        self.assertEqual(app.get_run(pending["id"])["error"], "workspace_identity_unavailable")
        self.assertEqual(app.get_run(pending["id"])["status"], "failed")
        self.assertEqual(app.store.get(finished["id"]), before_terminal)
        self.assertEqual(self.model.requests, [])

    def test_cli_selection_override_does_not_change_persistent_web_choice(self):
        app = self.app()
        self.select(app, "b")
        revision = app.workspaces()["revision"]
        app.close()
        cli = self.app(restore_selected=False)
        self.assertEqual(cli.selected_workspace_id, "a")
        self.assertEqual(cli.store.workspaces()["selected_id"], "b")
        self.assertEqual(cli.store.workspaces()["revision"], revision)
        result = cli.wait(cli.submit_tool("scope_a", {})["id"])
        self.assertEqual(result["workspace_id"], "a")
        cli.close()
        self.assertEqual(self.app().selected_workspace_id, "b")

    def test_no_files_binding_never_gains_file_permissions_by_selection(self):
        def no_files(spec):
            def validate():
                if inspect_workspace_directory(spec["path"])["identity"] != spec["identity"]:
                    raise WorkspaceError("workspace_changed")
            return WorkspaceBinding(spec["id"], spec["path"], spec["identity"], (), (), validate=validate)
        app = self.app(workspace_factory=no_files)
        self.select(app, "b")
        self.assertEqual(app.info()["tools"], [])
        with self.assertRaises(RuntimeError):
            app.list_files()
        result = app.wait(app.submit_tool("files.read", {"path": "anything"})["id"])
        self.assertEqual(result["tool_result"]["error_code"], "unknown_tool")

    def test_mismatched_legacy_browser_is_disabled_instead_of_previewing_wrong_root(self):
        files = FilePlugin(self.a)
        app = Application(self.model, files.tools, database=self.database, workspace=self.b,
                          permissions=("files.read", "files.write"))
        self.addCleanup(app.close)
        with self.assertRaises(RuntimeError):
            app.read_file("same.txt")
        run = self.write_approval(app)
        with self.assertRaises(RuntimeError):
            app.preview_write(run["id"])
        app.approve(run["id"], approved=False)
        self.assertFalse((self.a / "result.md").exists())
        self.assertFalse((self.b / "result.md").exists())

    def test_invalid_factory_cannot_persist_a_mismatched_workspace(self):
        app = self.app()
        before = app.workspaces()
        original = app._workspace_factory
        app._workspace_factory = lambda spec: replace(original(spec), identity="0" * 64)
        with self.assertRaisesRegex(WorkspaceError, "invalid_workspace"):
            self.select(app, "b")
        self.assertEqual(app.workspaces(), before)


if __name__ == "__main__":
    unittest.main()
