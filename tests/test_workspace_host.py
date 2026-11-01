"""Host selection policy and assembly using only owned temporary directories."""
from argparse import Namespace
from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from astra_harness.cli import build_application
from astra_harness.tools import Tool
from astra_harness.workspace_binding import WorkspaceError
from astra_harness.workspaces import WorkspacePolicy, local_workspace_path


class WorkspaceHostFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.startup = self.root / "startup"
        self.other = self.root / "另一工作区 with spaces"
        self.data = self.root / "service-data"
        for directory in (self.startup, self.other, self.data):
            directory.mkdir()
        (self.startup / "same.txt").write_text("startup-content", encoding="utf-8")
        (self.other / "same.txt").write_text("other-content", encoding="utf-8")
        self.database = self.data / "runs.sqlite3"

    def args(self, **changes):
        values = dict(command="serve", config=None, mcp_config=None, env_file=None,
                      workspace=self.startup, database=self.database, no_files=False,
                      live=False, demo_files=False, knowledge_index=None, knowledge_config=None)
        values.update(changes)
        return Namespace(**values)

    def build(self, **changes):
        resources = ExitStack()
        self.addCleanup(resources.close)
        services = {}
        app = build_application(self.args(**changes), resources, web_services=services)
        return app, services, resources

    def select(self, app, policy, path):
        before = policy.describe(app)
        spec = policy.inspect(path, items=before["items"])
        return policy.select(app, path=spec["path"], expected_identity=spec["identity"], expected_revision=before["revision"])


class WorkspaceHostTests(WorkspaceHostFixture, unittest.TestCase):
    def test_home_protection_survives_missing_environment_variables(self):
        expected_home = Path.home().resolve()
        with patch.dict(os.environ, {}, clear=True):
            policy = WorkspacePolicy(self.startup, database=self.database)
            self.assertEqual(policy.home, expected_home)
            with self.assertRaises(WorkspaceError) as error:
                policy.inspect(expected_home)
            self.assertEqual(error.exception.code, "protected_workspace")

    def test_unresolvable_windows_profile_fails_closed(self):
        if os.name != "nt":
            self.skipTest("Windows known-folder fallback")
        with patch("pathlib.Path.home", side_effect=RuntimeError("unavailable")), \
             patch("ctypes.windll.shell32.SHGetKnownFolderPath", return_value=1), self.assertRaises(WorkspaceError) as error:
            WorkspacePolicy(self.startup, database=self.database)
        self.assertEqual(error.exception.code, "workspace_unavailable")

    def test_validation_is_metadata_only_and_blocks_protected_and_remote_paths(self):
        policy = WorkspacePolicy(self.startup, database=self.database)
        before = set(self.root.rglob("*"))
        with patch("os.scandir", side_effect=AssertionError("selection scanned contents")), \
             patch("pathlib.Path.open", side_effect=AssertionError("selection opened content")):
            spec = policy.inspect(self.other)
        self.assertEqual(spec["path"], str(self.other.resolve()))
        self.assertRegex(spec["identity"], "^[0-9a-f]{64}$")
        self.assertEqual(set(self.root.rglob("*")), before)
        with patch("pathlib.Path.lstat", side_effect=AssertionError("remote metadata queried")):
            for value in (r"\\server\share\folder", "//server/share", r"\\?\C:\private", r"\\.\device",
                          "file:///C:/folder", "smb://server/share", "C:relative", "relative", "C:/safe/../folder"):
                with self.subTest(value=value), self.assertRaises(WorkspaceError):
                    policy.inspect(value)
        for target in (self.data, self.data / "nested", self.root, Path(self.startup.anchor),
                       Path.home(), policy.project_root, policy.project_root / "docs"):
            with self.subTest(target=target), self.assertRaises(WorkspaceError) as error:
                policy.inspect(target)
            self.assertEqual(error.exception.code, "protected_workspace")

    def test_mapped_network_drive_is_rejected_before_directory_metadata(self):
        if os.name != "nt":
            self.skipTest("Windows mapped-drive admission")
        with patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=4), \
             patch("pathlib.Path.lstat", side_effect=AssertionError("network stat")), self.assertRaises(WorkspaceError):
            local_workspace_path("Z:/documents")

    def test_startup_exception_never_allows_database_or_configuration_exposure(self):
        for protected in (self.startup / "runs.sqlite3", self.startup / "nested" / "runs.sqlite3"):
            with self.subTest(protected=protected), self.assertRaises(WorkspaceError):
                WorkspacePolicy(self.startup, database=protected)
        with self.assertRaises(WorkspaceError):
            WorkspacePolicy(self.startup, database=self.database, config_paths=[self.startup / "runtime.json"])
        # The normal dedicated startup directory below a service parent remains compatible.
        policy = WorkspacePolicy(self.startup, database=self.root / "runs.sqlite3")
        self.assertEqual(policy.inspect(self.startup)["path"], str(self.startup))

    def test_selection_cas_and_nested_roots_do_not_change_user_files(self):
        app, services, _ = self.build()
        policy = services["workspaces"]
        first = policy.describe(app)
        spec = policy.inspect(self.other)
        selected = policy.select(app, path=spec["path"], expected_identity=spec["identity"], expected_revision=first["revision"])
        self.assertEqual(selected["selected_id"], spec["id"])
        self.assertEqual(app.read_file("same.txt", workspace_id=spec["id"])["content"], "other-content")
        with self.assertRaises(WorkspaceError) as error:
            policy.select(app, path=str(self.startup), expected_identity=first["items"][0]["identity"], expected_revision=first["revision"])
        self.assertEqual(error.exception.code, "workspace_conflict")
        nested = self.other / "nested"
        nested.mkdir()
        with self.assertRaises(WorkspaceError) as error:
            policy.inspect(nested, items=selected["items"])
        self.assertEqual(error.exception.code, "workspace_overlap")
        self.assertEqual((self.startup / "same.txt").read_text(), "startup-content")
        self.assertEqual((self.other / "same.txt").read_text(), "other-content")

    def test_directory_replaced_after_preview_cannot_be_selected(self):
        app, services, _ = self.build()
        policy = services["workspaces"]
        before = policy.describe(app)
        preview = policy.inspect(self.other)
        self.other.rename(self.root / "old-directory")
        self.other.mkdir()
        with self.assertRaises(WorkspaceError) as error:
            policy.select(app, path=preview["path"], expected_identity=preview["identity"], expected_revision=before["revision"])
        self.assertEqual(error.exception.code, "workspace_changed")
        self.assertEqual(policy.describe(app)["selected_id"], before["selected_id"])

    def test_serve_restores_selection_but_cli_does_not_replace_saved_selection(self):
        app, services, resources = self.build()
        selected = self.select(app, services["workspaces"], self.other)
        resources.close()
        cli, _, cli_resources = self.build(command="run")
        self.assertEqual(cli.selected_workspace_id, cli.startup_workspace_id)
        cli_resources.close()
        reopened, _, _ = self.build()
        self.assertEqual(reopened.selected_workspace_id, selected["selected_id"])
        self.assertEqual(reopened.read_file("same.txt")["content"], "other-content")

    def test_no_files_cannot_gain_permissions_through_selection(self):
        app, services, _ = self.build(no_files=True)
        policy = services["workspaces"]
        self.assertFalse(policy.describe(app)["enabled"])
        with self.assertRaises(WorkspaceError) as error:
            policy.inspect(self.other)
        self.assertEqual(error.exception.code, "workspace_disabled")
        self.assertEqual(app.info()["tools"], ["add"])

    def test_mcp_and_knowledge_stay_on_startup_without_rediscovery(self):
        materials = self.startup / "materials"
        materials.mkdir()
        (materials / "guide.md").write_text("synthetic startup knowledge", encoding="utf-8")
        knowledge_path = self.data / "knowledge.json"
        knowledge_path.write_text(json.dumps({"collections": [{"id": "manual", "label": "Manual",
            "path": "materials", "index": "manual.sqlite3"}]}), encoding="utf-8")
        mcp_path = self.data / "mcp.json"
        mcp_path.write_text(json.dumps({"command": ["synthetic-never-started"]}), encoding="utf-8")
        created = []
        class StaticMCP:
            permissions = ("mcp.call",)
            def __init__(self, configs):
                created.append(self)
            def activate(self, registry):
                registry.register(Tool("mcp.fixture", "Synthetic host fixture", (), lambda: "ok",
                                       permissions=frozenset({"mcp.call"})))
            def status(self):
                return []
            def close(self):
                pass
        with patch("astra_harness.builtin.mcp_services.MCPServiceManager", StaticMCP):
            app, services, _ = self.build(knowledge_config=knowledge_path, mcp_config=mcp_path)
            initial_tools = app.info()["tools"]
            self.assertIn("knowledge.manual.search", initial_tools)
            self.assertIn("mcp.fixture", initial_tools)
            self.select(app, services["workspaces"], self.other)
            self.assertFalse(any(name.startswith(("knowledge.", "mcp.")) for name in app.info()["tools"]))
            self.select(app, services["workspaces"], self.startup)
            self.assertEqual(app.info()["tools"], initial_tools)
            self.assertEqual(len(created), 1)

    def test_invalid_ancestor_is_rejected_before_demo_or_service_start(self):
        blocked = self.root / "not-directory"
        blocked.write_text("fixture", encoding="utf-8")
        with patch("astra_harness.cli.seed_workspace") as seed, \
             patch("astra_harness.builtin.mcp_services.MCPServiceManager") as mcp, \
             ExitStack() as resources, self.assertRaises(WorkspaceError):
            build_application(self.args(workspace=blocked / "nested", no_files=True, demo_files=True), resources)
        seed.assert_not_called()
        mcp.assert_not_called()
        self.assertEqual(blocked.read_text(), "fixture")

    def test_link_ancestor_is_rejected_before_mkdir_even_without_file_tools(self):
        apparent_link = self.root / "junction"
        original_lstat = Path.lstat
        def metadata(path, *args, **kwargs):
            if path == apparent_link:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)
        with patch("pathlib.Path.lstat", metadata), patch("pathlib.Path.mkdir") as mkdir, \
             patch("astra_harness.cli.seed_workspace") as seed, \
             patch("astra_harness.builtin.mcp_services.MCPServiceManager") as mcp, \
             ExitStack() as resources, self.assertRaises(WorkspaceError):
            build_application(self.args(workspace=apparent_link / "new", no_files=True, demo_files=True), resources)
        mkdir.assert_not_called()
        seed.assert_not_called()
        mcp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
