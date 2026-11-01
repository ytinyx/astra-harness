"""Host composition and lifecycle; all material is synthetic and local."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from astra_harness.application import Application
from astra_harness.cli import build_application, main
from astra_harness.config import ConfigurationError, diagnose_startup
from astra_harness.tools import Tool
from astra_harness.workspace_binding import WorkspaceError
from tests.test_workspace_host import WorkspaceHostFixture


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CoreToolsHostTests(WorkspaceHostFixture, unittest.TestCase):
    def perform(self, app, name, arguments, *, approved=False):
        state = app.wait(app.submit_tool(name, arguments)["id"])
        if approved:
            self.assertEqual(state["status"], "waiting_approval")
            app.approve(state["id"])
            state = app.wait(state["id"])
        return state

    def test_default_file_extensions_follow_selected_root_without_eager_history(self):
        app, services, _ = self.build()
        names = set(app.info()["tools"])
        self.assertTrue({"files.inspect", "files.edit", "files.restore"} <= names)
        self.assertNotIn("commands.run", names)
        history = self.data / "file-history"
        self.assertFalse(history.exists())
        first = self.perform(app, "files.inspect", {"path": "same.txt"})
        self.assertEqual(first["tool_result"]["value"]["sha256"], digest(self.startup / "same.txt"))
        self.select(app, services["workspaces"], self.other)
        second = self.perform(app, "files.inspect", {"path": "same.txt"})
        self.assertEqual(second["tool_result"]["value"]["sha256"], digest(self.other / "same.txt"))
        self.assertEqual(set(app.info()["tools"]), names)
        self.assertFalse(history.exists())

    def test_pending_edit_keeps_A_after_selection_and_reopen_with_isolated_history(self):
        app, services, resources = self.build()
        startup_id = app.startup_workspace_id
        first = self.perform(app, "files.edit", {"path": "same.txt", "old_text": "startup-content",
            "new_text": "edited-a", "expected_sha256": digest(self.startup / "same.txt")})
        self.assertEqual(first["status"], "waiting_approval")
        self.assertFalse((self.data / "file-history").exists())
        selected = self.select(app, services["workspaces"], self.other)
        resources.close()
        app, services, _ = self.build()
        self.assertEqual(app.selected_workspace_id, selected["selected_id"])
        app.approve(first["id"])
        edited = app.wait(first["id"])
        self.assertEqual(edited["status"], "completed")
        self.assertEqual((self.startup / "same.txt").read_text(), "edited-a")
        self.assertEqual((self.other / "same.txt").read_text(), "other-content")
        backup = edited["tool_result"]["value"]["backup_id"]
        self.assertTrue((self.data / "file-history" / startup_id / (backup + ".json")).is_file())
        wrong = self.perform(app, "files.restore", {"backup_id": backup,
            "expected_sha256": digest(self.other / "same.txt")}, approved=True)
        self.assertEqual(wrong["status"], "failed")
        self.assertFalse((self.data / "file-history" / selected["selected_id"]).exists())
        self.select(app, services["workspaces"], self.startup)
        restored = self.perform(app, "files.restore", {"backup_id": backup,
            "expected_sha256": digest(self.startup / "same.txt")}, approved=True)
        self.assertEqual(restored["status"], "completed")
        self.assertEqual((self.startup / "same.txt").read_text(), "startup-content")
        self.assertEqual((self.other / "same.txt").read_text(), "other-content")

    def test_history_root_and_ancestors_are_not_selectable_or_valid_startup(self):
        app, services, _ = self.build()
        history = self.data / "file-history"
        for candidate in (history, history / "child", self.data):
            with self.subTest(candidate=candidate), self.assertRaises(WorkspaceError) as error:
                services["workspaces"].inspect(candidate)
            self.assertEqual(error.exception.code, "protected_workspace")
        self.assertFalse(history.exists())
        with ExitStack() as resources, self.assertRaises(WorkspaceError):
            build_application(self.args(workspace=history / "chosen"), resources)
        self.assertFalse(history.exists())

    def test_no_files_leaves_all_extensions_disabled(self):
        app, services, _ = self.build(no_files=True)
        self.assertEqual(app.info()["tools"], ["add"])
        self.assertFalse(services["workspaces"].enabled)
        self.assertFalse((self.data / "file-history").exists())

    def test_invalid_command_allowlist_rejects_before_any_startup_side_effect(self):
        for overrides in ({"allow_command": ["python"]},
                          {"allow_command": [str(self.root / "missing.exe")]},
                          {"allow_command": [sys.executable], "no_files": True}):
            with self.subTest(overrides=overrides), patch("pathlib.Path.mkdir") as mkdir, \
                 patch("astra_harness.cli.seed_workspace") as seed, \
                 patch("astra_harness.cli.Application") as application, ExitStack() as resources, \
                 self.assertRaises(ConfigurationError):
                build_application(self.args(**overrides), resources)
            mkdir.assert_not_called()
            seed.assert_not_called()
            application.assert_not_called()

    def test_doctor_command_validation_is_read_only_and_never_dispatches(self):
        before = set(self.root.rglob("*"))
        executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
        with patch("subprocess.Popen", side_effect=AssertionError("doctor launched a process")), \
             patch("pathlib.Path.mkdir", side_effect=AssertionError("doctor created a directory")):
            valid = diagnose_startup(workspace=self.startup, database=self.database, allow_commands=[executable])
            invalid = diagnose_startup(workspace=self.startup, database=self.database, allow_commands=["python"])
            disabled = diagnose_startup(workspace=self.startup, database=self.database,
                                        allow_commands=[executable], no_files=True)
        self.assertTrue(valid["ok"])
        self.assertFalse(invalid["ok"])
        self.assertFalse(disabled["ok"])
        self.assertNotIn(executable, json.dumps(valid))
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_cli_repeated_allowlist_reaches_host_and_is_rejected_for_isolated_evaluators(self):
        executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["doctor", "--workspace", str(self.startup), "--database", str(self.database),
                         "--allow-command", executable, "--allow-command", executable])
        self.assertEqual(code, 0)
        self.assertEqual(next(row["status"] for row in json.loads(output.getvalue())["checks"]
                              if row["code"] == "command_allowlist"), "ok")
        for operation in ("eval", "benchmark"):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main([operation, "--allow-command", executable])
            self.assertEqual(error.exception.code, 2)

    def test_enabled_commands_bind_each_workspace_and_cleanup_after_application(self):
        events, instances = [], []
        executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())

        class TrackingCommands:
            def __init__(self, files, allowed_executables, **limits):
                self.files, self.allowed, self.limits = files, allowed_executables, limits
                instances.append(self)
                self.tools = (Tool("commands.run", "Synthetic registration fixture", (), lambda: None,
                    permissions=frozenset({"commands.execute"}), side_effect=True, requires_approval=True,
                    recovery_identity=files.workspace_identity),)

            def close(self):
                events.append(self.files.root)

        close = Application.close
        def app_close(app):
            events.append("application")
            return close(app)

        with patch("astra_harness.builtin.commands.CommandPlugin", TrackingCommands), \
             patch.object(Application, "close", app_close):
            app, services, resources = self.build(allow_command=[executable])
            self.assertIn("commands.run", app.info()["tools"])
            self.assertIn("commands.execute", app.require_workspace().permissions)
            self.select(app, services["workspaces"], self.other)
            self.assertIn("commands.execute", app.require_workspace().permissions)
            self.assertEqual({instance.files.root for instance in instances}, {self.startup, self.other})
            self.assertTrue(all(instance.allowed == (executable,) for instance in instances))
            self.assertTrue(all(instance.limits == {"timeout": 5.0, "max_output_bytes": 16384, "revision": "1"}
                                for instance in instances))
            resources.close()
        self.assertEqual(events, ["application", self.other, self.startup])
        self.assertFalse((self.data / "file-history").exists())

    def test_actual_allowlisted_command_waits_for_approval_and_keeps_original_cwd(self):
        executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
        app, services, _ = self.build(allow_command=[executable])
        body = "from pathlib import Path; Path('command-result.txt').write_text('owned fixture', encoding='utf-8'); print(Path.cwd().name)"
        waiting = self.perform(app, "commands.run", {"argv": [executable, "-I", "-S", "-c", body]})
        self.assertEqual(waiting["status"], "waiting_approval")
        self.assertFalse((self.startup / "command-result.txt").exists())
        self.select(app, services["workspaces"], self.other)
        app.approve(waiting["id"])
        done = app.wait(waiting["id"])
        self.assertEqual(done["status"], "completed", done.get("tool_result"))
        self.assertEqual((self.startup / "command-result.txt").read_text(), "owned fixture")
        self.assertFalse((self.other / "command-result.txt").exists())
        self.assertIn(self.startup.name, done["tool_result"]["value"]["stdout"])


if __name__ == "__main__":
    unittest.main()
