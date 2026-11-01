"""Host assembly with synthetic local STDIO services; no provider or user data."""

from contextlib import ExitStack, redirect_stdout
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from astra_harness.application import Application
from astra_harness.builtin.mcp_services import MCPServiceManager
from astra_harness.cli import build_application, main
from astra_harness.contracts import ModelResponse, ToolCall


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_service_server.py"
LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


class RemoteModel:
    def complete(self, request):
        result = request.messages[-1].tool_result
        if result is None:
            return ModelResponse(tool_calls=(ToolCall("remote-1", "mcp.healthy.work", {"value": 7}),))
        return ModelResponse(text="remote result: " + str(result.value["structuredContent"]["value"]))


def fixture_command(directory, mode="normal"):
    return [sys.executable, "-X", "utf8", str(FIXTURE.resolve()),
            "--directory", str(directory.resolve()), "--mode", mode]


def arguments(root, config):
    return SimpleNamespace(config=None, env_file=None, workspace=root / "workspace",
                           database=root / "database" / "runs.sqlite3", demo_files=False,
                           live=False, no_files=True, mcp_config=config)


class MCPIntegrationTests(unittest.TestCase):
    def write_config(self, root, value):
        path = root / "mcp.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_complete_invalid_configuration_prevents_any_launch_or_workspace(self):
        # In particular, a valid first server must not start before a later
        # invalid entry has been checked. The marker represents private argv.
        marker = "private-argv-marker"
        valid = {"name": "healthy", "command": [marker]}
        invalid = [
            {"servers": [valid, {"name": "broken", "command": []}]},
            {"servers": [valid, valid]},
            {"servers": [{**valid, "timeout": True}]},
            {"servers": [{**valid, "timeout": 301}]},
            {"servers": [{**valid, "revision": "bad revision"}]},
            {"servers": [{**valid, "env": {"PRIVATE": marker}}]},
            {"servers": [valid] * 9}, {"command": []},
        ]
        for value in invalid:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = self.write_config(root, value)
                output = io.StringIO()
                with patch("subprocess.Popen") as launch, patch("astra_harness.cli.seed_workspace") as seed, \
                        patch("astra_harness.cli.Application") as application, redirect_stdout(output):
                    code = main(["run", "synthetic task", "--mcp-config", str(config), "--demo-files",
                                 "--workspace", str(root / "workspace"),
                                 "--database", str(root / "database" / "runs.sqlite3")])
                self.assertEqual(code, 1)
                launch.assert_not_called()
                seed.assert_not_called()
                application.assert_not_called()
                self.assertEqual(list(root.iterdir()), [config])
                self.assertNotIn(marker, output.getvalue())
                self.assertNotIn(str(root), output.getvalue())

    def test_missing_and_malformed_configuration_are_safe_cli_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / "invalid.json"
            malformed.write_text('{"servers": ["private-argv-marker"', encoding="utf-8")
            for config in (root / "missing.json", malformed):
                with self.subTest(config=config.name), patch("subprocess.Popen") as launch:
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = main(["run", "synthetic task", "--mcp-config", str(config),
                                     "--workspace", str(root / "workspace"),
                                     "--database", str(root / "database" / "runs.sqlite3")])
                    self.assertEqual(code, 1)
                    launch.assert_not_called()
                    self.assertNotIn("private-argv-marker", output.getvalue())
                    self.assertNotIn(str(root), output.getvalue())
                    self.assertEqual(list(root.iterdir()), [malformed])

    def test_doctor_validates_both_formats_without_starting_services(self):
        marker = "private-command-marker"
        for config_value in ({"command": [marker]},
                             {"servers": [{"name": "healthy", "command": [marker],
                                           "timeout": 2, "revision": "fixture-v1"}]}):
            with self.subTest(config=config_value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = self.write_config(root, config_value)
                output = io.StringIO()
                with patch("subprocess.Popen") as launch, patch("astra_harness.cli.Application") as application, \
                        redirect_stdout(output):
                    code = main(["doctor", "--mcp-config", str(config), "--demo-files",
                                 "--workspace", str(root / "workspace"),
                                 "--database", str(root / "database" / "runs.sqlite3")])
                self.assertEqual(code, 0)
                report = json.loads(output.getvalue())
                self.assertTrue(report["ok"])
                self.assertEqual(next(check["status"] for check in report["checks"] if check["code"] == "mcp"), "ok")
                self.assertNotIn(marker, output.getvalue())
                self.assertNotIn(str(root), output.getvalue())
                self.assertEqual(list(root.iterdir()), [config])
                launch.assert_not_called()
                application.assert_not_called()

    def test_doctor_rejects_invalid_named_configuration_without_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root, {"servers": [{"name": "bad name", "command": ["private-marker"]}]})
            output = io.StringIO()
            with patch("subprocess.Popen") as launch, redirect_stdout(output):
                code = main(["doctor", "--mcp-config", str(config), "--workspace", str(root / "workspace"),
                             "--database", str(root / "runs.sqlite3")])
            self.assertEqual(code, 1)
            report = json.loads(output.getvalue())
            self.assertEqual(next(check["status"] for check in report["checks"] if check["code"] == "mcp"), "error")
            self.assertNotIn("private-marker", output.getvalue())
            self.assertEqual(list(root.iterdir()), [config])
            launch.assert_not_called()

    def test_application_callback_is_checked_before_store_and_default_info_is_unchanged(self):
        with patch("astra_harness.application.Store") as store:
            with self.assertRaisesRegex(TypeError, "service_status"):
                Application(RemoteModel(), service_status=[])
            store.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            with Application(RemoteModel(), database=Path(temporary) / "runs.sqlite3", autostart=False) as app:
                self.assertNotIn("services", app.info())

    def test_application_returns_independent_generic_service_information(self):
        status = [{"name": "local", "state": "ready", "tools": ["local.tool"]}]
        provider = Mock(return_value=status)
        with tempfile.TemporaryDirectory() as temporary:
            with Application(RemoteModel(), database=Path(temporary) / "runs.sqlite3",
                             autostart=False, service_status=provider) as app:
                provider.assert_not_called()
                info = app.info()
                info["services"][0]["tools"].append("changed")
                info["services"][0]["state"] = "changed"
                self.assertEqual(app.info()["services"], status)
                self.assertEqual(status[0]["tools"], ["local.tool"])
                self.assertEqual(status[0]["state"], "ready")

    def test_two_real_services_keep_healthy_task_and_http_info_available(self):
        from astra_harness.web import create_server

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private-argv-marker"
            config = self.write_config(root, {"servers": [
                {"name": "broken", "command": fixture_command(private / "broken", "initfailure"), "timeout": 2},
                {"name": "healthy", "command": fixture_command(private / "healthy"), "timeout": 2},
            ]})
            with patch("astra_harness.builtin.models.DemoModel", return_value=RemoteModel()), ExitStack() as resources:
                app = build_application(arguments(root, config), resources)
                self.assertEqual(set(app.permissions), {"mcp.healthy.call", "mcp.broken.call"})
                server = create_server(app, port=0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()

                def request(path, body=None, expected=200):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    try:
                        connection.request("GET" if body is None else "POST", path,
                                           body=None if body is None else json.dumps(body).encode("utf-8"),
                                           headers={"Content-Type": "application/json"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, expected)
                        return json.loads(response.read())
                    finally:
                        connection.close()

                try:
                    info = request("/api/info")
                    services = {item["name"]: item for item in info["services"]}
                    self.assertEqual(services["healthy"]["state"], "ready")
                    self.assertEqual(services["healthy"]["tools"], ["mcp.healthy.work"])
                    self.assertEqual(services["broken"]["state"], "failed")
                    self.assertEqual(services["broken"]["tools"], [])
                    self.assertTrue(services["broken"]["error_code"].startswith("mcp_"))
                    for service in services.values():
                        self.assertEqual(set(service), {"name", "state", "error_code", "tools", "transport"})
                    self.assertNotIn("private-argv-marker", json.dumps(info["services"]))
                    self.assertNotIn(str(FIXTURE.resolve()), json.dumps(info["services"]))
                    submitted = request("/api/runs", {"prompt": "synthetic remote task"}, expected=201)
                    waiting = app.wait(submitted["id"], timeout=10)
                    self.assertEqual(waiting["status"], "waiting_approval")
                    self.assertFalse((private / "healthy" / "sent.json").exists())
                    request(f"/api/runs/{submitted['id']}/approve", {"approved": True})
                    final = app.wait(submitted["id"], timeout=10)
                    self.assertEqual(final["status"], "completed")
                    self.assertEqual(final["output"], "remote result: 7")
                    self.assertEqual(final["tool_calls"], 1)
                    self.assertTrue((private / "healthy" / "sent.json").exists())
                    self.assertFalse((private / "broken" / "sent.json").exists())
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())

    def test_legacy_config_keeps_old_namespace_permission_and_fail_fast(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root, {"command": [sys.executable, "-X", "utf8", str(LEGACY_FIXTURE.resolve())]})
            with ExitStack() as resources:
                app = build_application(arguments(root, config), resources, autostart=False)
                self.assertIn("mcp.multiply", [tool.name for tool in app.tools])
                self.assertEqual(app.permissions, ["mcp.call"])
            failed = self.write_config(root, {"command": fixture_command(root / "failed", "initfailure")})
            with patch("astra_harness.cli.Application") as application, ExitStack() as resources:
                with self.assertRaisesRegex(RuntimeError, "mcp_"):
                    build_application(arguments(root, failed), resources)
                application.assert_not_called()

    def test_constructor_failure_closes_owned_service_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.write_config(root, {"servers": [
                {"name": "healthy", "command": fixture_command(root / "service"), "timeout": 2}]})
            managers, processes = [], []
            original_popen = subprocess.Popen

            def launch(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                if str(FIXTURE.resolve()) in args[0]:
                    processes.append(process)
                return process

            def manager_factory(configs):
                manager = MCPServiceManager(configs)
                managers.append(manager)
                return manager

            with patch("subprocess.Popen", side_effect=launch), \
                    patch("astra_harness.builtin.mcp_services.MCPServiceManager", side_effect=manager_factory), \
                    patch("astra_harness.cli.Application", side_effect=RuntimeError("constructor failure")):
                with self.assertRaisesRegex(RuntimeError, "constructor failure"), ExitStack() as resources:
                    build_application(arguments(root, config), resources)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertEqual(managers[0].status()[0]["state"], "closed")
            self.assertFalse((root / "database").exists())

    def test_shutdown_closes_application_before_services_even_after_close_error(self):
        for close_error in (False, True):
            with self.subTest(close_error=close_error), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = self.write_config(root, {"servers": [{"name": "healthy", "command": ["synthetic-only"]}]})
                events = []
                # Editing composes the real file adapter. Keep its type and
                # workspace checks while observing only its shutdown order.
                from astra_harness.builtin.files import FilePlugin

                activate_files = FilePlugin.activate

                def observe_files_close(plugin, registry):
                    activate_files(plugin, registry)
                    return lambda: events.append("files")
                services = SimpleNamespace(name="mcp", permissions=frozenset({"mcp.healthy.call"}),
                                           status=lambda: [], activate=lambda registry: lambda: events.append("mcp"))

                class ClosingApplication:
                    def __init__(self, *args, **kwargs):
                        self.store = object()  # SkillCatalog construction is lazy.
                        if not callable(kwargs.get("task_context_preparer")):
                            raise AssertionError("missing task-context preparation callback")

                    def close(self):
                        events.append("application")
                        if close_error:
                            raise RuntimeError("application close failed")

                    def start(self):
                        pass

                class ClosingProfiles:
                    def __init__(self, *args, **kwargs):
                        pass

                    def close(self):
                        events.append("profiles")

                def build_and_close():
                    args = arguments(root, config)
                    args.no_files = False
                    with ExitStack() as resources:
                        build_application(args, resources)

                with patch.object(FilePlugin, "activate", observe_files_close), \
                        patch("astra_harness.builtin.mcp_services.MCPServiceManager", return_value=services), \
                        patch("astra_harness.mcp_profiles.MCPProfiles", ClosingProfiles), \
                        patch("astra_harness.cli.Application", ClosingApplication):
                    if close_error:
                        with self.assertRaisesRegex(RuntimeError, "application close failed"):
                            build_and_close()
                    else:
                        build_and_close()
                self.assertEqual(events, ["profiles", "application", "mcp", "files"])


if __name__ == "__main__":
    unittest.main()
