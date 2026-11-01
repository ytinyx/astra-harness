from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from astra_harness.application import Application
from astra_harness.builtin.files import FilePlugin, inspect_workspace_directory
from astra_harness.builtin.editing import FileEditPlugin
from astra_harness.tools import ToolUnavailable
from astra_harness.workspace_binding import WorkspaceBinding


class NoModel:
    def complete(self, request):
        raise AssertionError("preview and direct tools must not call a model")


class CoreToolPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-preview-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.target = self.root / "guide.txt"
        self.target.write_bytes(b"hello old world\n")
        self.files = FilePlugin(self.root)
        self.history = self.base / "history"
        self.edits = FileEditPlugin(self.files, self.history)

    def app(self):
        app = Application(NoModel(), database=self.base / "runs.sqlite3", workspace=self.root,
                          tools=self.files.tools + self.edits.tools, permissions=["files.read", "files.write"])
        self.addCleanup(app.close)
        return app

    def args(self):
        return {"path": "guide.txt", "old_text": "old", "new_text": "new",
                "expected_sha256": hashlib.sha256(self.target.read_bytes()).hexdigest()}

    def pending(self, app, name="files.edit", arguments=None):
        run = app.wait(app.submit_tool(name, arguments or self.args())["id"])
        self.assertEqual(run["status"], "waiting_approval")
        return run

    def test_edit_plugin_does_not_disable_base_browser_and_preview_is_readonly(self):
        app = self.app()
        self.assertEqual(app.read_file("guide.txt")["content"], "hello old world\n")
        self.assertEqual(app.list_files()["entries"][0]["path"], "guide.txt")
        run = self.pending(app)
        preview = app.preview_write(run["id"])
        self.assertEqual((preview["before"], preview["after"]), ("hello old world\n", "hello new world\n"))
        self.assertEqual(self.target.read_text(encoding="utf-8"), preview["before"])
        self.assertFalse(self.history.exists())
        self.assertEqual(app.get_run(run["id"])["tool_calls"], 0)

    def test_restore_preview_uses_registered_owner_without_creating_another_backup(self):
        app = self.app(); run = self.pending(app)
        app.approve(run["id"]); result = app.wait(run["id"])
        value = result["tool_result"]["value"]
        before = {path: path.read_bytes() for path in self.history.rglob("*") if path.is_file()}
        restore = self.pending(app, "files.restore", {"backup_id": value["backup_id"],
            "expected_sha256": hashlib.sha256(self.target.read_bytes()).hexdigest()})
        preview = app.preview_write(restore["id"])
        self.assertEqual((preview["before"], preview["after"]), ("hello new world\n", "hello old world\n"))
        self.assertEqual(before, {path: path.read_bytes() for path in self.history.rglob("*") if path.is_file()})
        self.assertEqual(self.target.read_text(encoding="utf-8"), "hello new world\n")
        app.approve(restore["id"])
        self.assertEqual(app.wait(restore["id"])["status"], "completed")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "hello old world\n")

    def test_stale_preview_and_subsequent_approval_fail_without_overwriting_new_text(self):
        app = self.app(); run = self.pending(app)
        self.target.write_text("external change", encoding="utf-8")
        with self.assertRaises(ToolUnavailable):
            app.preview_write(run["id"])
        app.approve(run["id"]); outcome = app.wait(run["id"])
        self.assertEqual((outcome["status"], outcome["error"]), ("failed", "edit_rejected"))
        self.assertEqual(outcome["tool_result"]["value"], {"reason": "stale_file", "applied": False})
        self.assertEqual(self.target.read_text(encoding="utf-8"), "external change")
        self.assertFalse(self.history.exists())

    def test_preview_checks_approval_key_name_and_state_read_permission(self):
        app = self.app(); run = self.pending(app)
        original = app.store.get(run["id"])
        for mutate in [lambda state: state["approval"].update(key="wrong"),
                       lambda state: state["approval"].update(name="files.restore"),
                       lambda state: state.update(permissions=["files.write"])]:
            state = app.store.get(run["id"]); mutate(state); app.store.save(state)
            with self.assertRaises(RuntimeError):
                app.preview_write(run["id"])
            app.store.save(original)
        self.assertFalse(self.history.exists())

    def test_preview_checks_snapshot_and_argument_schema_even_with_matching_call_hash(self):
        app = self.app(); run = self.pending(app)
        original = app.store.get(run["id"])
        state = app.store.get(run["id"]); state["tool_snapshot"] = []
        app.store.save(state)
        with self.assertRaises(RuntimeError): app.preview_write(run["id"])
        app.store.save(original)
        state = app.store.get(run["id"]); call = state["pending"][state["index"]]
        call["arguments"]["old_text"] = 42
        state["approval"] = {**call, "key": app.engine.approval_key(call)}
        app.store.save(state)
        with self.assertRaises(RuntimeError): app.preview_write(run["id"])
        self.assertFalse(self.history.exists())

    def test_base_file_owner_guard_rejects_replaced_base_handler_but_accepts_edit_owner(self):
        self.assertIs(Application._bound_file_plugin(self.files.tools + self.edits.tools, self.root), self.files)
        altered = tuple(replace(tool, handler=lambda **arguments: None) if tool.name == "files.read" else tool for tool in self.files.tools)
        self.assertIsNone(Application._bound_file_plugin(altered + self.edits.tools, self.root))
        other = self.base / "other"; other.mkdir()
        self.assertIsNone(Application._bound_file_plugin(self.files.tools, other))

    def test_edit_preview_and_approval_remain_bound_to_original_workspace_after_restart(self):
        other = self.base / "other"; other.mkdir(); (other / "guide.txt").write_text("other text", encoding="utf-8")
        specs = {key: {"id": key, **inspect_workspace_directory(path)} for key, path in (("a", self.root), ("b", other))}
        def factory(spec):
            files = FilePlugin(spec["path"])
            editing = FileEditPlugin(files, self.base / ("history-" + spec["id"]))
            return WorkspaceBinding(spec["id"], spec["path"], spec["identity"], files.tools + editing.tools,
                ("files.read", "files.write"), files)
        def create():
            app = Application(NoModel(), database=self.base / "scoped.sqlite3", workspace=self.root,
                              workspace_factory=factory, workspace_spec=specs["a"])
            self.addCleanup(app.close)
            return app
        app = create(); run = self.pending(app)
        app.select_workspace(specs["b"], app.workspaces()["revision"])
        app.close(); app = create()
        self.assertEqual(app.selected_workspace_id, "b")
        self.assertEqual(app.preview_write(run["id"])["after"], "hello new world\n")
        self.assertFalse((self.base / "history-a").exists())
        app.approve(run["id"])
        self.assertEqual(app.wait(run["id"])["status"], "completed")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "hello new world\n")
        self.assertEqual((other / "guide.txt").read_text(encoding="utf-8"), "other text")
