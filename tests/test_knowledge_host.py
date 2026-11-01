"""Static host composition and the old single-index CLI remain usable."""
from argparse import Namespace
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.cli import build_application, main
from astra_harness.config import diagnose_startup


class KnowledgeHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "materials"
        for name, content in (("alpha", "alpha-only phrase"), ("beta", "beta-only phrase")):
            directory = self.workspace / name
            directory.mkdir(parents=True)
            (directory / "guide.md").write_text(content, encoding="utf-8")
        self.database = self.root / "runs.sqlite3"
        self.config = self.root / "knowledge.json"
        self.config.write_text(json.dumps({"collections": [
            {"id": name, "label": name.title(), "path": name, "index": name + ".sqlite3"}
            for name in ("alpha", "beta")]}), encoding="utf-8")

    def args(self, **changes):
        values = dict(config=None, mcp_config=None, env_file=None, workspace=self.workspace,
                      database=self.database, no_files=False, live=False, demo_files=False,
                      knowledge_index=None, knowledge_config=self.config)
        values.update(changes)
        return Namespace(**values)

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_doctor_collection_config_is_readonly_and_does_not_open_sqlite(self):
        before = self.snapshot()
        with patch("sqlite3.connect", side_effect=AssertionError("doctor opened SQLite")):
            report = diagnose_startup(workspace=self.workspace, database=self.database,
                                      knowledge_config=self.config, environ={})
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(next(item["status"] for item in report["checks"] if item["code"] == "knowledge_config"), "ok")

    def test_both_config_forms_or_no_files_fail_before_host_changes(self):
        for options in ({"knowledge_index": self.root / "single.sqlite3"}, {"no_files": True}):
            with self.subTest(options=options):
                before = self.snapshot()
                with ExitStack() as stack, patch("astra_harness.cli.Application") as application:
                    with self.assertRaises(ValueError):
                        build_application(self.args(**options), stack)
                application.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_default_index_uses_same_registered_names_and_exposes_web_adapter(self):
        index = self.root / "default.sqlite3"
        with ExitStack() as stack:
            interfaces = {}
            app = build_application(self.args(knowledge_index=index, knowledge_config=None), stack,
                                    web_services=interfaces)
            manager = interfaces["knowledge"]
            descriptor = manager.collections()[0]
            self.assertEqual((descriptor["id"], descriptor["source_scope"]), ("default", "."))
            self.assertEqual(descriptor["tools"]["import_file"], "knowledge.import_file")
            self.assertEqual(descriptor["tools"]["inspect"], "knowledge.inspect")
            self.assertTrue({"add", "files.read", "knowledge.search"}.issubset({tool.name for tool in app.tools}))
            run = app.wait(app.submit_tool("knowledge.search", {"query": "alpha"})["id"])
            self.assertEqual((run["status"], run["model_calls"]), ("completed", 0))

    def test_two_collections_compose_and_approved_import_stays_in_selected_index(self):
        with ExitStack() as stack:
            interfaces = {}
            app = build_application(self.args(), stack, web_services=interfaces)
            manager = interfaces["knowledge"]
            self.assertEqual([item["id"] for item in manager.collections()], ["alpha", "beta"])
            source = (self.workspace / "alpha/guide.md").read_bytes()
            run = app.wait(app.submit_tool("knowledge.alpha.import_file", {
                "path": "guide.md", "expected_source_sha256": hashlib.sha256(source).hexdigest(),
                "expected_index_sha256": ""})["id"])
            self.assertEqual(run["status"], "waiting_approval")
            app.approve(run["id"])
            done = app.wait(run["id"])
            self.assertEqual((done["status"], done["model_calls"]), ("completed", 0))
            self.assertTrue(done["tool_result"]["value"]["ok"])
            for collection, expected_hits in (("alpha", True), ("beta", False)):
                found = app.wait(app.submit_tool("knowledge." + collection + ".search", {"query": "alpha-only"})["id"])
                self.assertEqual(bool(found["tool_result"]["value"]["hits"]), expected_hits)

    def test_cli_serve_passes_adapter_and_closes_it_after_return(self):
        captured = []

        def serve(app, *, port, knowledge, workspaces, mcp_profiles, skills):
            captured.append(knowledge)
            self.assertEqual(port, 8765)
            self.assertEqual(len(knowledge.collections()), 2)
            self.assertEqual(app.mode, "demo")
            self.assertTrue(workspaces.describe(app)["enabled"])
            self.assertEqual(workspaces.describe(app)["selected_id"], app.startup_workspace_id)
            self.assertEqual(mcp_profiles.list()["items"], [])
            binding = app.require_workspace(app.startup_workspace_id)
            catalog = skills.list({key: getattr(binding, key) for key in ("id", "path", "identity")})
            self.assertEqual(catalog["workspace_id"], app.startup_workspace_id)
            self.assertEqual(catalog["items"], [])

        output = io.StringIO()
        with patch("astra_harness.web.serve", side_effect=serve), redirect_stdout(output):
            code = main(["serve", "--workspace", str(self.workspace), "--database", str(self.database),
                         "--knowledge-config", str(self.config)])
        self.assertEqual(code, 0)
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].get("alpha").catalog()["ok"])

    def test_eval_and_benchmark_reject_collection_configuration(self):
        before = self.snapshot()
        for command in ("eval", "benchmark"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main([command, "--knowledge-config", str(self.config)])
            self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
