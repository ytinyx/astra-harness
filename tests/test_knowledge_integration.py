"""Optional knowledge host wiring; synthetic local data, no model provider."""

from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from astra_harness.builtin.knowledge import KnowledgePlugin
from astra_harness.cli import build_application, main
from astra_harness.config import ConfigurationError, knowledge_index_path
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.tools import ToolExecutor, ToolRegistry
from astra_harness.workspace_binding import WorkspaceError


SOURCE = "合成项目规范\n撤销窗口为七日。\n人工复核记录应保留。\n"


class ImportSearchModel:
    def complete(self, request):
        result = request.messages[-1].tool_result
        if result is None:
            return ModelResponse(tool_calls=(ToolCall("import", "knowledge.import_file", {"path": "policy.txt"}),))
        if result.error_code:
            return ModelResponse(text="blocked: " + result.error_code)
        if result.name == "knowledge.import_file":
            return ModelResponse(tool_calls=(ToolCall("search", "knowledge.search", {"query": "撤销窗口"}),))
        return ModelResponse(text=json.dumps(result.value, ensure_ascii=False))


def arguments(root, *, knowledge=True):
    return SimpleNamespace(config=None, env_file=None, live=False, no_files=False, demo_files=False,
                           mcp_config=None, workspace=root / "workspace",
                           database=root / "runtime" / "runs.sqlite3",
                           knowledge_index=root / "index" / "knowledge.sqlite3" if knowledge else None)


class KnowledgeIntegrationTests(unittest.TestCase):
    def test_conflicts_fail_before_workspace_seed_index_or_application(self):
        for conflict in ("no_files", "workspace", "workspace_root", "runtime_database",
                         "runtime_wal", "runtime_lock", "index_journal"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                args = arguments(root)
                args.demo_files = True
                if conflict == "no_files":
                    args.no_files = True
                elif conflict == "workspace":
                    args.knowledge_index = args.workspace / "subdirectory" / "index.sqlite3"
                elif conflict == "workspace_root":
                    args.knowledge_index = args.workspace
                elif conflict == "runtime_database":
                    args.knowledge_index = args.database
                elif conflict == "runtime_wal":
                    args.knowledge_index = Path(str(args.database) + "-wal")
                elif conflict == "runtime_lock":
                    args.knowledge_index = Path(str(args.database) + ".lock")
                else:
                    args.database = Path(str(args.knowledge_index) + "-journal")
                with patch("astra_harness.cli.Application") as application, \
                        patch("astra_harness.cli.seed_workspace") as seed, ExitStack() as resources:
                    with self.assertRaises(ConfigurationError):
                        build_application(args, resources)
                application.assert_not_called()
                seed.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])

    def test_path_validation_rejects_directory_parent_file_and_database_hardlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, database = root / "workspace", root / "runtime.sqlite3"
            database.write_bytes(b"synthetic-existing-runtime")
            for path in (root, database / "nested.sqlite3", root / "invalid\x00.sqlite3"):
                with self.subTest(path=path), self.assertRaises(ConfigurationError):
                    knowledge_index_path(path, workspace=workspace, database=database)
            alias = root / "index-alias.sqlite3"
            try:
                os.link(database, alias)
            except OSError as error:
                self.skipTest("local filesystem does not support test hardlinks: " + type(error).__name__)
            with self.assertRaisesRegex(ConfigurationError, "knowledge_index_is_database"):
                knowledge_index_path(alias, workspace=workspace, database=database)
            self.assertEqual(database.read_bytes(), b"synthetic-existing-runtime")
            self.assertFalse(workspace.exists())

    def test_doctor_validates_paths_without_opening_index_or_creating_anything(self):
        for conflict in (None, "no_files", "runtime_database"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                index = root / "future-index" / "knowledge.sqlite3"
                database = root / "runtime" / "runs.sqlite3"
                command = ["doctor", "--workspace", str(root / "workspace"), "--database", str(database),
                           "--knowledge-index", str(database if conflict == "runtime_database" else index),
                           "--demo-files"]
                if conflict == "no_files":
                    command.append("--no-files")
                output = io.StringIO()
                with patch("sqlite3.connect", side_effect=AssertionError("doctor opened a database")), \
                        patch("astra_harness.cli.build_application", side_effect=AssertionError("doctor built a host")), \
                        redirect_stdout(output):
                    code = main(command)
                report = json.loads(output.getvalue())
                self.assertEqual(code, 0 if conflict is None else 1)
                status = next(item["status"] for item in report["checks"] if item["code"] == "knowledge_index")
                self.assertEqual(status, "ok" if conflict is None else "error")
                self.assertNotIn(str(root), output.getvalue())
                self.assertEqual(list(root.iterdir()), [])

    def test_fixed_evaluators_reject_knowledge_option_instead_of_ignoring_it(self):
        for command in ("eval", "benchmark"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main([command, "--knowledge-index", "synthetic-unused-index.sqlite3"])
                self.assertEqual(raised.exception.code, 2)

    def test_default_application_has_basic_file_tools_and_no_knowledge_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = arguments(root, knowledge=False)
            # Older embedding callers need not add the new optional attribute.
            del args.knowledge_index
            with ExitStack() as resources:
                app = build_application(args, resources, autostart=False)
                self.assertEqual(set(app.info()["tools"]),
                                 {"add", "files.list", "files.read", "files.search", "files.read_range", "files.write",
                                  "files.inspect", "files.edit", "files.restore"})
                self.assertEqual(app.permissions, ["files.read", "files.write"])
            self.assertFalse((root / "index").exists())

    def test_application_constructor_failure_closes_activated_knowledge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugins = []
            def construct(*args, **kwargs):
                plugin = KnowledgePlugin(*args, **kwargs)
                plugin.close = Mock(wraps=plugin.close)
                plugins.append(plugin)
                return plugin
            with patch("astra_harness.builtin.knowledge_collections.KnowledgePlugin", side_effect=construct), \
                    patch("astra_harness.cli.Application", side_effect=RuntimeError("synthetic constructor failure")):
                with self.assertRaisesRegex(RuntimeError, "synthetic constructor failure"), ExitStack() as resources:
                    build_application(arguments(root), resources)
            self.assertEqual(len(plugins), 1)
            plugins[0].close.assert_called_once_with()
            self.assertFalse((root / "runtime").exists())

    def test_application_closes_before_plugins_even_if_application_close_raises(self):
        for close_error in (False, True):
            with self.subTest(close_error=close_error), tempfile.TemporaryDirectory() as temporary:
                root, events = Path(temporary), []
                # Keep the concrete file adapter required by the composed
                # editing plugin; observe cleanup without replacing its type.
                from astra_harness.builtin.files import FilePlugin
                activate_files = FilePlugin.activate

                def observe_files_close(plugin, registry):
                    activate_files(plugin, registry)
                    return lambda: events.append("files")
                knowledge = SimpleNamespace(name="knowledge", permissions={"files.read", "knowledge.read", "knowledge.write"},
                                            activate=lambda registry: lambda: events.append("knowledge"))
                class ClosingApplication:
                    def __init__(application, *args, **kwargs):
                        application.store = object()
                        self.assertTrue(callable(kwargs["task_context_preparer"]))
                    def close(self):
                        events.append("application")
                        if close_error:
                            raise RuntimeError("synthetic close failure")
                    def start(self):
                        pass
                class ClosingProfiles:
                    def __init__(self, *args, **kwargs):
                        pass
                    def close(self):
                        events.append("profiles")
                def run():
                    with ExitStack() as resources:
                        build_application(arguments(root), resources)
                with patch.object(FilePlugin, "activate", observe_files_close), \
                        patch("astra_harness.builtin.knowledge_collections.KnowledgePlugin", return_value=knowledge), \
                        patch("astra_harness.mcp_profiles.MCPProfiles", ClosingProfiles), \
                        patch("astra_harness.cli.Application", ClosingApplication):
                    if close_error:
                        with self.assertRaisesRegex(RuntimeError, "synthetic close failure"):
                            run()
                    else:
                        run()
                self.assertEqual(events, ["profiles", "application", "knowledge", "files"])

    def test_real_registered_tools_preserve_permission_approval_reopen_and_source_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = arguments(root)
            args.workspace.mkdir()
            source = args.workspace / "policy.txt"
            source.write_bytes(SOURCE.encode("utf-8"))
            with patch("astra_harness.builtin.models.DemoModel", ImportSearchModel):
                with ExitStack() as resources:
                    app = build_application(args, resources)
                    self.assertTrue({"knowledge.import_file", "knowledge.search", "knowledge.remove"} <= set(app.info()["tools"]))
                    self.assertEqual(app.permissions, ["files.read", "files.write", "knowledge.read", "knowledge.write"])
                    executor = ToolExecutor(ToolRegistry(app.tools))
                    rejected = executor.execute(ToolCall("denied", "knowledge.import_file", {"path": "policy.txt"}),
                                                frozenset(), approved=True)
                    self.assertEqual(rejected.error_code, "permission_denied")
                    empty = executor.execute(ToolCall("empty", "knowledge.search", {"query": "撤销窗口"}),
                                             frozenset(app.permissions))
                    self.assertEqual(empty.value["hits"], [])
                    state = app.wait(app.submit("导入合成规范并检索撤销窗口")["id"])
                    self.assertEqual((state["status"], state["model_calls"], state["tool_calls"]), ("waiting_approval", 1, 0))
                    approval = state["approval"]
                    self.assertEqual(executor.execute(ToolCall("still-empty", "knowledge.search", {"query": "撤销窗口"}),
                                                      frozenset(app.permissions)).value["hits"], [])
                with ExitStack() as resources:
                    app = build_application(args, resources)
                    self.assertEqual(app.get_run(state["id"])["approval"], approval)
                    final = app.wait(app.approve(state["id"])["id"])
                    self.assertEqual((final["status"], final["model_calls"], final["tool_calls"]), ("completed", 3, 2))
                    value = json.loads(final["output"])
                    self.assertTrue(value["ok"])
                    self.assertEqual(len(value["hits"]), 1)
                    hit = value["hits"][0]
                    self.assertEqual(hit["path"], "policy.txt")
                    self.assertEqual(hit["document_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
                    self.assertEqual(hit["text"], SOURCE[hit["start_offset"]:hit["end_offset"]])
                    self.assertIn("撤销窗口为七日。", hit["text"])
                    self.assertNotIn(str(root), final["output"])

    def test_pending_run_rejects_changed_index_or_source_root_before_execution(self):
        for change in ("index", "workspace"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                args = arguments(root)
                args.workspace.mkdir()
                (args.workspace / "policy.txt").write_bytes(SOURCE.encode("utf-8"))
                with patch("astra_harness.builtin.models.DemoModel", ImportSearchModel):
                    with ExitStack() as resources:
                        app = build_application(args, resources)
                        waiting = app.wait(app.submit("导入合成规范")["id"])
                        self.assertEqual(waiting["status"], "waiting_approval")
                    args.knowledge_index = root / "different-index" / "knowledge.sqlite3"
                    if change == "workspace":
                        args.workspace = root / "different-workspace"
                        # A persisted host catalog has one immutable startup
                        # anchor. Reopening its database with another root is
                        # rejected before recovery can rewrite a pending run.
                        with closing(sqlite3.connect(args.database)) as connection:
                            original = connection.execute("SELECT state FROM runs WHERE id=?", (waiting["id"],)).fetchone()[0]
                        with ExitStack() as resources, self.assertRaises(WorkspaceError) as caught:
                            build_application(args, resources, autostart=False)
                        self.assertEqual(caught.exception.code, "workspace_changed")
                        with closing(sqlite3.connect(args.database)) as connection:
                            preserved = connection.execute("SELECT state FROM runs WHERE id=?", (waiting["id"],)).fetchone()[0]
                        self.assertEqual(preserved, original)
                        self.assertEqual(json.loads(preserved)["status"], "waiting_approval")
                        self.assertEqual(json.loads(preserved)["tool_calls"], 0)
                        continue
                    with ExitStack() as resources:
                        app = build_application(args, resources, autostart=False)
                        failed = app.get_run(waiting["id"])
                        self.assertEqual(failed["status"], "failed")
                        self.assertEqual(failed["error"], "tool_snapshot_changed")
                        self.assertEqual(failed["tool_calls"], 0)


if __name__ == "__main__":
    unittest.main()
