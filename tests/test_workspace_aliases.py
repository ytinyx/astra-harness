"""Real Windows short aliases preserve directory ownership and protection."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from astra_harness.application import Application
from astra_harness.builtin.files import FilePlugin
from astra_harness.builtin.editing import FileEditPlugin
from astra_harness.workspaces import WorkspacePolicy
from astra_harness.workspace_binding import WorkspaceError
from tests.fixtures.windows_paths import short_path


class NoModel:
    def complete(self, request):
        raise AssertionError("file checks must not call a model")


@unittest.skipUnless(os.name == "nt", "Windows short path API")
class WorkspaceAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-workspace-alias-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.workspace = self.base / "workspace long name"
        self.workspace.mkdir()
        self.alias = short_path(self.workspace)
        (self.workspace / "guide.txt").write_text("old content", encoding="utf-8")

    def test_file_browser_and_edit_preview_accept_both_safe_spellings(self):
        for index, (plugin_path, app_path) in enumerate(((self.alias,self.workspace),(self.workspace,self.alias))):
            with self.subTest(index=index):
                files = FilePlugin(plugin_path)
                edits = FileEditPlugin(files, self.base / ("history-"+str(index)))
                with Application(NoModel(),files.tools+edits.tools,database=self.base / (str(index)+".sqlite3"),
                                 workspace=app_path,permissions=["files.read","files.write"]) as app:
                    self.assertEqual(app.read_file("guide.txt")["content"],"old content")
                    run = app.wait(app.submit_tool("files.edit",{"path":"guide.txt","old_text":"old","new_text":"new",
                        "expected_sha256":hashlib.sha256(b"old content").hexdigest()})["id"])
                    self.assertEqual(run["status"],"waiting_approval")
                    preview = app.preview_write(run["id"])
                    self.assertTrue(preview)
                    self.assertEqual((self.workspace/"guide.txt").read_text(encoding="utf-8"),"old content")

    def test_startup_alias_keeps_only_the_same_configured_exception(self):
        policy = WorkspacePolicy(self.alias,database=self.base/"runs.sqlite3")
        self.assertEqual(policy.inspect(self.alias,allow_startup=True)["path"],str(self.workspace))
        with patch("astra_harness.workspaces._home_directory",return_value=self.workspace):
            with self.assertRaises(WorkspaceError):
                WorkspacePolicy(self.alias,database=self.base/"runs.sqlite3")
        for protected in (self.base,self.alias):
            if protected == self.alias:
                with self.assertRaises(WorkspaceError):
                    WorkspacePolicy(self.alias,database=self.workspace/"runs.sqlite3")
            else:
                with self.assertRaises(WorkspaceError):
                    policy.inspect(protected)

    def test_matching_alias_still_rejects_reparse_ancestors_and_replacement(self):
        files = FilePlugin(self.workspace)
        original = Path.lstat
        def observed(path):
            info = original(path)
            if path == self.alias:
                return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=0x400)
            return info
        with patch.object(Path,"lstat",observed):
            with self.assertRaises(ValueError):
                files.matches_workspace(self.alias)
            self.assertIsNone(Application._bound_file_plugin(files.tools,self.alias))
        self.workspace.rename(self.base/"previous workspace")
        self.workspace.mkdir()
        with self.assertRaises(ValueError):
            files.matches_workspace(self.workspace)
        self.assertIsNone(Application._bound_file_plugin(files.tools,self.workspace))
