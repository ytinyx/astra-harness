import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from astra_harness.cli import build_application, main
from astra_harness.config import (ConfigurationError, MAX_CONFIG_BYTES,
                                  diagnose_startup, load_model_environment, load_runtime_config)
from astra_harness.engine import DEFAULT_LIMITS


class ConfigurationTests(unittest.TestCase):
    def test_defaults_partial_overrides_and_independent_results(self):
        default = load_runtime_config()
        self.assertEqual(default, {"limits": DEFAULT_LIMITS, "context_chars": 48_000})
        default["limits"]["max_model_calls"] = 99
        self.assertEqual(load_runtime_config()["limits"]["max_model_calls"], 12)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            path.write_text('{"limits":{"max_tool_calls":0,"model_timeout":0.5},"context_chars":1000}', encoding="utf-8")
            config = load_runtime_config(path)
        self.assertEqual(config["limits"]["max_tool_calls"], 0)
        self.assertEqual(config["limits"]["model_timeout"], 0.5)
        self.assertEqual(config["limits"]["run_timeout"], DEFAULT_LIMITS["run_timeout"])
        self.assertEqual(config["context_chars"], 1000)

    def test_invalid_json_keys_types_ranges_and_credentials_are_rejected(self):
        invalid = [
            "[]", "null", '{"context_chars":10,"context_chars":20}',
            '{"limits":{"max_tool_calls":1,"max_tool_calls":2}}',
            '{"limits":{"model_timeout":NaN}}', '{"limits":{"tool_timeout":Infinity}}',
            '{"limits":{"run_timeout":1e999}}', '{"limits":{"max_model_calls":true}}',
            '{"limits":{"max_model_calls":0}}', '{"limits":{"max_tool_calls":-1}}',
            '{"limits":{"max_model_calls":2.0}}', '{"limits":{"model_timeout":false}}',
            '{"limits":{"model_timeout":0}}', '{"limits":{"tool_timeout":-1}}',
            '{"limits":{"run_timeout":"10"}}', '{"limits":null}', '{"limits":[]}',
            '{"limits":{"unknown":1}}', '{"context_chars":true}', '{"context_chars":0}',
            '{"context_chars":1.5}', '{"context_chars":"100"}', '{"context_chars":null}',
            '{"ASTRA_API_KEY":"do-not-print-me"}', '{"api_key":"do-not-print-me"}', "{",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for content in invalid:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ConfigurationError) as raised:
                        load_runtime_config(path)
                    self.assertNotIn("do-not-print-me", str(raised.exception))

    def test_size_utf8_and_file_type_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b"{}" + b" " * (MAX_CONFIG_BYTES - 2))
            self.assertEqual(load_runtime_config(path)["context_chars"], 48_000)
            path.write_bytes(b"{}" + b" " * MAX_CONFIG_BYTES)
            with self.assertRaisesRegex(ConfigurationError, "config_too_large"):
                load_runtime_config(path)
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ConfigurationError, "config_invalid_json"):
                load_runtime_config(path)
            for missing_or_directory in (Path(tmp), Path(tmp) / "missing"):
                with self.assertRaisesRegex(ConfigurationError, "config_not_readable"):
                    load_runtime_config(missing_or_directory)

    def test_shipped_example_is_accepted(self):
        config = load_runtime_config(Path(__file__).parents[1] / "astra.example.json")
        self.assertEqual(config["limits"], DEFAULT_LIMITS)


class DoctorTests(unittest.TestCase):
    def test_demo_checks_are_read_only_even_with_seed_and_mcp_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = io.StringIO()
            with patch("astra_harness.cli.build_application", side_effect=AssertionError("must not build")), \
                    patch("subprocess.Popen", side_effect=AssertionError("must not spawn")), \
                    patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("must not request")), \
                    contextlib.redirect_stdout(output):
                result = main(["doctor", "--workspace", str(root / "work"),
                               "--database", str(root / "db" / "data.sqlite3"), "--demo-files",
                               "--mcp-config", str(root / "unread-command.json")])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 1)
            self.assertFalse(report["ok"])
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(next(item["status"] for item in report["checks"] if item["code"] == "mcp"), "error")

    def test_live_needs_environment_but_never_prints_secrets_or_contacts_model(self):
        secret = "fake-private-test-key-123"
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            environment = {"ASTRA_API_KEY": secret, "ASTRA_MODEL": "fake-private-model",
                           "ASTRA_BASE_URL": "https://private.example.invalid/v1"}
            with patch.dict("os.environ", environment, clear=True), \
                    patch("astra_harness.builtin.async_models.AsyncOpenAICompatibleModel",
                          side_effect=AssertionError("must not create model")), \
                    patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("must not request")), \
                    contextlib.redirect_stdout(output):
                result = main(["doctor", "--live", "--workspace", str(Path(tmp) / "work"),
                               "--database", str(Path(tmp) / "db.sqlite3")])
            self.assertEqual(result, 0)
            for value in environment.values():
                self.assertNotIn(value, output.getvalue())
            self.assertEqual(list(Path(tmp).iterdir()), [])
            missing = diagnose_startup(live=True, workspace=tmp, database=Path(tmp) / "db", environ={})
            self.assertFalse(missing["ok"])
            self.assertEqual({item["code"] for item in missing["checks"] if item["status"] == "error"},
                             {"model_key", "model_name"})

    def test_live_url_validation_does_not_echo_invalid_values(self):
        invalid_urls = ["http://example.com", "https://key:secret@example.com", "https://example.com?key=secret",
                        "https://example.com#secret", "https://", "https://example.com:bad",
                        "https://example.com:70000", "https://example.com:0", "https://exam ple.com", ""]
        with tempfile.TemporaryDirectory() as tmp:
            for url in invalid_urls:
                with self.subTest(url=url):
                    report = diagnose_startup(live=True, workspace=tmp, database=Path(tmp) / "db",
                                              environ={"ASTRA_API_KEY": "fake", "ASTRA_MODEL": "test", "ASTRA_BASE_URL": url})
                    self.assertFalse(report["ok"])
                    self.assertEqual(next(item["status"] for item in report["checks"] if item["code"] == "model_url"), "error")
                    self.assertNotIn("secret", json.dumps(report))
            for url in ("https://example.com/v1", "http://127.0.0.1:8000/v1", "http://localhost:8080", "http://[::1]:8000"):
                self.assertTrue(diagnose_startup(live=True, workspace=tmp, database=Path(tmp) / "db",
                                               environ={"ASTRA_API_KEY": "fake", "ASTRA_MODEL": "test", "ASTRA_BASE_URL": url})["ok"])

    def test_invalid_config_and_paths_are_blockers_without_private_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private.txt"
            private.write_text('{"api_key":"sensitive-value"}', encoding="utf-8")
            report = diagnose_startup(config_path=private, workspace=private, database=root)
            self.assertFalse(report["ok"])
            self.assertEqual({item["code"] for item in report["checks"] if item["status"] == "error"},
                             {"runtime_config", "workspace", "database_path"})
            self.assertNotIn("sensitive-value", json.dumps(report))
            self.assertNotIn(str(private), json.dumps(report))

    def test_root_reparse_point_is_rejected_before_resolving(self):
        with tempfile.TemporaryDirectory() as tmp:
            from types import SimpleNamespace
            info = SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
            with patch("astra_harness.config.Path.lstat", return_value=info):
                report = diagnose_startup(workspace=tmp, database=Path(tmp) / "db")
            self.assertEqual(next(item["status"] for item in report["checks"] if item["code"] == "workspace"), "error")


class EnvironmentFileTests(unittest.TestCase):
    def test_explicit_file_literals_comments_and_quotes_without_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text("# local model\n\n ASTRA_API_KEY = 'fake-${HOME}-$(command)-a=b#literal' \n"
                            'ASTRA_MODEL="test\\nmodel"\nASTRA_BASE_URL=https://example.invalid/v1\n', encoding="utf-8-sig")
            with patch("subprocess.Popen", side_effect=AssertionError("must not execute")), \
                    patch("os.system", side_effect=AssertionError("must not execute")):
                values = load_model_environment(path, environ={})
            self.assertEqual(values["ASTRA_API_KEY"], "fake-${HOME}-$(command)-a=b#literal")
            self.assertEqual(values["ASTRA_MODEL"], "test\\nmodel")
            self.assertEqual(values["ASTRA_BASE_URL"], "https://example.invalid/v1")

    def test_environment_wins_including_empty_and_is_not_modified(self):
        environment = {"ASTRA_API_KEY": "fake-env-key", "ASTRA_MODEL": "", "OTHER_SECRET": "unused"}
        original = dict(environment)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text("ASTRA_API_KEY=fake-file-key\nASTRA_MODEL=file-model\n", encoding="utf-8")
            values = load_model_environment(path, environ=environment)
        self.assertEqual(values, {"ASTRA_API_KEY": "fake-env-key", "ASTRA_MODEL": "",
                                  "ASTRA_BASE_URL": "https://api.deepseek.com"})
        self.assertEqual(environment, original)

    def test_missing_option_never_opens_implicit_env_file(self):
        with patch("astra_harness.config._read_bounded", side_effect=AssertionError("no implicit file")):
            self.assertEqual(load_model_environment(environ={})["ASTRA_API_KEY"], "")

    def test_invalid_fields_quotes_and_encoding_have_fixed_redacted_errors(self):
        invalid = [b"ASTRA_API_KEY=fake-secret\nASTRA_API_KEY=duplicate",
                   b"UNKNOWN=fake-secret", b"export ASTRA_API_KEY=fake-secret",
                   b"ASTRA_API_KEY 'fake-secret'", b"ASTRA_API_KEY='fake-secret",
                   b'ASTRA_API_KEY=fake-secret"', b'ASTRA_API_KEY="fake-secret" # inline',
                   b'ASTRA_API_KEY="fake-secret\x00"', b"ASTRA_API_KEY=\xff"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            for content in invalid:
                with self.subTest(content=content):
                    path.write_bytes(content)
                    with self.assertRaises(ConfigurationError) as raised:
                        load_model_environment(path, environ={})
                    self.assertEqual(str(raised.exception), "env_file_invalid")
                    self.assertNotIn("fake-secret", str(raised.exception))
            path.write_bytes(b"#" + b"x" * (MAX_CONFIG_BYTES - 1))
            self.assertEqual(load_model_environment(path, environ={})["ASTRA_API_KEY"], "")
            path.write_bytes(b"#" + b"x" * MAX_CONFIG_BYTES)
            with self.assertRaisesRegex(ConfigurationError, "env_file_too_large"):
                load_model_environment(path, environ={})
            for missing_or_directory in (Path(tmp), Path(tmp) / "missing"):
                with self.assertRaisesRegex(ConfigurationError, "env_file_not_readable"):
                    load_model_environment(missing_or_directory, environ={})

    def test_doctor_explicit_credentials_are_read_only_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "local.env"
            path.write_text("ASTRA_API_KEY=fake-file-secret\nASTRA_MODEL=file-private-model\n"
                            "ASTRA_BASE_URL=https://private.example.invalid/v1\n", encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), \
                    patch("astra_harness.cli.build_application", side_effect=AssertionError("must not build")), \
                    patch("astra_harness.builtin.async_models.AsyncOpenAICompatibleModel", side_effect=AssertionError("must not create model")), \
                    patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("must not request")), \
                    contextlib.redirect_stdout(output):
                code = main(["doctor", "--live", "--env-file", str(path), "--workspace", str(root / "work"),
                             "--database", str(root / "db" / "data.sqlite3")])
                self.assertNotIn("ASTRA_API_KEY", os.environ)
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])
            for secret in ("fake-file-secret", "file-private-model", "private.example.invalid", str(path)):
                self.assertNotIn(secret, output.getvalue())
            self.assertEqual(list(root.iterdir()), [path])

    def test_live_app_uses_explicit_file_with_environment_priority(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "local.env"
            path.write_text("ASTRA_API_KEY=fake-file-key\nASTRA_MODEL=file-model\n", encoding="utf-8")
            args = SimpleNamespace(config=None, env_file=path, workspace=root / "work", database=root / "db.sqlite3",
                                   demo_files=False, live=True, no_files=True, mcp_config=None)
            with patch.dict(os.environ, {"ASTRA_API_KEY": "fake-env-key"}, clear=True), \
                    patch("urllib.request.build_opener"), \
                    patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("must not request")), \
                    contextlib.ExitStack() as resources:
                app = build_application(args, resources, autostart=False)
                self.assertEqual(app.model.api_key, "fake-env-key")
                self.assertEqual(app.model.model, "file-model")
                self.assertEqual(app.mode, "live")
                self.assertTrue(__import__("inspect").iscoroutinefunction(app.model.complete))
                self.assertNotIn("ASTRA_MODEL", os.environ)


class ConfigurationCLITests(unittest.TestCase):
    def run_cli(self, arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(arguments)
        return code, output.getvalue()

    def test_invalid_configuration_fails_before_workspace_database_or_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text('{"api_key":"keep-this-secret"}', encoding="utf-8")
            with patch("astra_harness.builtin.mcp.StdioMCPClient", side_effect=AssertionError("must not start")), \
                    patch("astra_harness.cli.Application", side_effect=AssertionError("must not open DB")):
                code, output = self.run_cli(["run", "计算 2 + 3", "--config", str(config), "--demo-files",
                                             "--workspace", str(root / "work"), "--database", str(root / "db" / "db.sqlite3"),
                                             "--mcp-config", str(root / "mcp.json")])
            self.assertEqual(code, 1)
            self.assertNotIn("keep-this-secret", output)
            self.assertEqual(list(root.iterdir()), [config])

    def test_runtime_limits_reach_actual_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text('{"limits":{"max_tool_calls":0}}', encoding="utf-8")
            code, output = self.run_cli(["run", "计算 2 + 3", "--config", str(config), "--no-files",
                                         "--workspace", str(root / "work"), "--database", str(root / "db.sqlite3")])
            state = json.loads(output)
            self.assertEqual(code, 1)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error"], "tool_limit")
            self.assertEqual(state["model_calls"], 1)
            self.assertEqual(state["tool_calls"], 0)

    def test_context_limit_reaches_actual_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text('{"context_chars":1}', encoding="utf-8")
            code, output = self.run_cli(["run", "hello", "--config", str(config), "--no-files",
                                         "--workspace", str(root / "work"), "--database", str(root / "db.sqlite3")])
            state = json.loads(output)
            self.assertEqual(code, 1)
            self.assertEqual(state["error"], "context_limit")
            self.assertEqual(state["model_calls"], 0)

    def test_eval_rejects_configuration_that_would_be_ignored(self):
        with contextlib.redirect_stderr(io.StringIO()), patch("astra_harness.evaluation.evaluate") as evaluate:
            with self.assertRaises(SystemExit) as raised:
                main(["eval", "--config", "any.json"])
        self.assertEqual(raised.exception.code, 2)
        evaluate.assert_not_called()

    def test_benchmark_receives_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text('{"context_chars":30000,"limits":{"max_model_calls":3}}', encoding="utf-8")
            with patch("astra_harness.benchmark.benchmark", return_value={"passed": True}) as benchmark:
                code, _ = self.run_cli(["benchmark", "--config", str(config)])
            self.assertEqual(code, 0)
            self.assertEqual(benchmark.call_args.kwargs["max_context_chars"], 30000)
            self.assertEqual(benchmark.call_args.kwargs["limits"]["max_model_calls"], 3)

    def test_pause_and_resume_keep_waiting_approval_across_cli_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = ["--workspace", str(root / "work"), "--database", str(root / "db.sqlite3")]
            code, output = self.run_cli(["run", "整理会议待办", "--demo-files", *options])
            original = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(original["status"], "waiting_approval")
            code, output = self.run_cli(["pause", original["id"], *options])
            paused = json.loads(output)
            self.assertEqual((code, paused["status"], paused["resume_status"]), (0, "paused", "waiting_approval"))
            code, output = self.run_cli(["resume", original["id"], *options])
            resumed = json.loads(output)
            self.assertEqual((code, resumed["status"]), (0, "waiting_approval"))
            self.assertEqual(resumed["tool_calls"], original["tool_calls"])
            self.assertEqual(resumed["approval"], original["approval"])
            self.assertFalse((root / "work" / "todos.json").exists())

    def test_pause_does_not_start_a_pending_model_before_saving_pause(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(config=None, workspace=root / "work", database=root / "db.sqlite3",
                                   demo_files=False, live=False, no_files=True, mcp_config=None)
            with contextlib.ExitStack() as resources:
                app = build_application(args, resources, autostart=False)
                pending = app.submit("计算 2 + 3")
            with patch("astra_harness.builtin.models.DemoModel.complete", side_effect=AssertionError("must not execute")) as model:
                code, output = self.run_cli(["pause", pending["id"], "--no-files", "--workspace", str(args.workspace),
                                             "--database", str(args.database)])
            paused = json.loads(output)
            self.assertEqual((code, paused["status"], paused["model_calls"]), (0, "paused", 0))
            model.assert_not_called()

    def test_pause_resume_require_id_and_help_describes_read_only_doctor(self):
        for command in ("pause", "resume"):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main([command])
            self.assertEqual(raised.exception.code, 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--config", output.getvalue())
        self.assertIn("doctor", output.getvalue())
        self.assertIn("暂停", output.getvalue())

    def test_inspect_and_cancel_do_not_execute_pending_models(self):
        from types import SimpleNamespace
        for command, expected_status in (("inspect", "pending"), ("cancel", "cancelled")):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args = SimpleNamespace(config=None, workspace=root / "work", database=root / "db.sqlite3",
                                       demo_files=False, live=False, no_files=True, mcp_config=None)
                with contextlib.ExitStack() as resources:
                    app = build_application(args, resources, autostart=False)
                    pending = app.submit("计算 2 + 3")
                with patch("astra_harness.builtin.models.DemoModel.complete", side_effect=AssertionError("must not execute")) as model:
                    code, output = self.run_cli([command, pending["id"], "--no-files", "--workspace", str(args.workspace),
                                                 "--database", str(args.database)])
                state = json.loads(output)
                self.assertEqual((code, state["status"], state["model_calls"]), (1, expected_status, 0))
                model.assert_not_called()

    def test_cli_wait_uses_returned_run_budget_including_resume(self):
        for command, operation in (("run", "submit"), ("approve", "approve"), ("resume", "resume")):
            for budget, expected_wait in ((600, 630), (10, 300)):
                with self.subTest(command=command, budget=budget):
                    app = Mock()
                    state = {"id": "test-run", "status": "pending", "limits": {"run_timeout": budget}}
                    getattr(app, operation).return_value = state
                    app.wait.return_value = {**state, "status": "completed"}
                    with patch("astra_harness.cli.build_application", return_value=app):
                        code, _ = self.run_cli([command, "test-run"])
                    self.assertEqual(code, 0)
                    app.wait.assert_called_once_with("test-run", timeout=expected_wait)

    def test_invalid_env_file_fails_before_writes_in_all_command_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "local.env"
            path.write_text("ASTRA_API_KEY=fake-private-key\nUNEXPECTED=bad\n", encoding="utf-8")
            for command in (["run", "hello"], ["benchmark"], ["doctor", "--live"]):
                with self.subTest(command=command), \
                        patch("astra_harness.cli.Application", side_effect=AssertionError("must not open DB")), \
                        patch("astra_harness.builtin.async_models.AsyncOpenAICompatibleModel", side_effect=AssertionError("must not create model")):
                    code, output = self.run_cli([*command, "--env-file", str(path), "--workspace", str(root / "work"),
                                                 "--database", str(root / "db.sqlite3"), "--output-dir", str(root / "out")])
                    self.assertEqual(code, 1)
                    self.assertNotIn("fake-private-key", output)
                    self.assertEqual(list(root.iterdir()), [path])

    def test_offline_env_file_never_selects_real_model_and_eval_rejects_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "local.env"
            path.write_text("ASTRA_API_KEY=fake-private-key\nASTRA_MODEL=file-model\n", encoding="utf-8")
            with patch("astra_harness.builtin.async_models.AsyncOpenAICompatibleModel", side_effect=AssertionError("must not create model")):
                code, output = self.run_cli(["run", "计算 2 + 3", "--env-file", str(path), "--no-files",
                                             "--workspace", str(root / "work"), "--database", str(root / "db.sqlite3")])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["status"], "completed")
            self.assertNotIn("fake-private-key", output)
            with contextlib.redirect_stderr(io.StringIO()), patch("astra_harness.evaluation.evaluate") as evaluate:
                with self.assertRaises(SystemExit) as raised:
                    main(["eval", "--env-file", str(path)])
            self.assertEqual(raised.exception.code, 2)
            evaluate.assert_not_called()

    def test_benchmark_factory_uses_explicit_model_file_without_global_mutation(self):
        captured = []

        def benchmark(*args, model_factory, **kwargs):
            model = model_factory()
            captured.append((model.api_key, model.model, model.base_url))
            return {"passed": True}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text("ASTRA_API_KEY=fake-benchmark-key\nASTRA_MODEL=test-model\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), \
                    patch("astra_harness.benchmark.benchmark", side_effect=benchmark), \
                    patch("urllib.request.build_opener"), \
                    patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("must not request")):
                code, output = self.run_cli(["benchmark", "--live", "--env-file", str(path)])
                self.assertNotIn("ASTRA_API_KEY", os.environ)
            self.assertEqual(code, 0)
            self.assertEqual(captured, [("fake-benchmark-key", "test-model", "https://api.deepseek.com")])
            self.assertNotIn("fake-benchmark-key", output)


if __name__ == "__main__":
    unittest.main()
