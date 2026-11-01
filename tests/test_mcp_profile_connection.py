"""Profile transports use only trusted local synthetic processes and HTTP fixtures."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from astra_harness.builtin.mcp import MCPError
from astra_harness.builtin.mcp_profile_connection import ProfileConnection, validate_profile_config
from astra_harness.contracts import ToolCall
from astra_harness.tools import ToolExecutor, ToolRegistry, ToolExecutionUnknown
from examples.mcp_http_server import LocalMCPHTTPServer

FIXTURE = Path(__file__).parent / "fixtures/mcp_service_server.py"

class MCPProfileConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="astra-mcp-profiles-")
        self.root = Path(self.temp.name).resolve()
        self.python = str(Path(sys.executable).resolve())
        self.connections = []

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.temp.cleanup()

    def profile(self, **changes):
        return {"name": "fixture", "transport": "stdio", "command": self.python,
            "args": ["-I", "-S", "-X", "utf8", str(FIXTURE.resolve()), "--directory", str(self.root / "markers"),
                     "--mode", "normal"], "timeout": 2, **changes}

    def connection(self, profile=None, **kwargs):
        connection = ProfileConnection(profile or self.profile(), self.root, **kwargs)
        self.connections.append(connection)
        return connection

    async def marker(self, name, limit=4):
        path = self.root / "markers" / name
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                # Wait for readable complete JSON within the same deadline;
                # transient Windows access errors must not race the fixture.
                pass
            await asyncio.sleep(.01)
        self.fail("trusted fixture marker missing: " + name)

    def process_handle(self, pid):
        if os.name != "nt":
            return pid
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes, api.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes, api.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        handle = api.OpenProcess(0x100000, False, pid)
        self.assertTrue(handle)
        return api, handle

    def assert_dead(self, handle):
        if os.name == "nt":
            api, value = handle
            try:
                self.assertEqual(api.WaitForSingleObject(value, 1500), 0)
            finally:
                api.CloseHandle(value)
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(handle, 0)

    async def test_validation_and_constructor_do_not_spawn_or_read_references(self):
        reads = []
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected spawn")):
            normalized = validate_profile_config(self.profile(), self.root)
            connection = self.connection(environment=lambda: reads.append(True))
        self.assertEqual((reads, connection.tools, connection.status()["state"]), ([], (), "new"))
        self.assertEqual(normalized["cwd"], ".")
        self.assertEqual(normalized["env_refs"], {})
        self.assertEqual(list(self.root.iterdir()), [])
        copied = connection.profile
        copied["args"].append("changed")
        self.assertNotIn("changed", connection.profile["args"])
        profile = self.profile()
        with patch("pathlib.Path.stat", side_effect=AssertionError("metadata during syntax validation")):
            validate_profile_config(profile, self.root, inspect=False)

    async def test_strict_fields_paths_types_environment_and_transport_separation(self):
        invalid = [{"command": "python"}, {"command": r"\\server\share\python.exe"},
            {"command": "http://127.0.0.1:1/run"},
            {"cwd": "../elsewhere"}, {"cwd": str(self.root)}, {"args": "-u server.py"},
            {"args": ["bad\0arg"]}, {"args": [""]}, {"timeout": True}, {"timeout": 31}, {"timeout": float("nan")},
            {"env": {"KEY": "secret-value"}}, {"env_refs": {"KEY": "ASTRA_API_KEY"}},
            {"env_refs": {"PATH": "ASTRA_MCP_PATH"}}, {"env_refs": {"PYTHONPATH": "ASTRA_MCP_CODE"}},
            {"env_refs": {"A": "ASTRA_MCP_A", "a": "ASTRA_MCP_A"}}, {"endpoint": "http://127.0.0.1:1/mcp"}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "^invalid_mcp_profile$"):
                validate_profile_config(self.profile(**changes), self.root, inspect=False)
        for endpoint in ("http://localhost:1/mcp", "http://127.0.0.1/mcp", "http://127.0.0.1:1/mcp?token=x",
                         "http://x:pw@127.0.0.1:1/mcp", "https://example.com/mcp"):
            with self.assertRaises(ValueError):
                validate_profile_config({"name": "http", "transport": "http", "endpoint": endpoint}, self.root, inspect=False)

    async def test_command_suffix_and_existence_checks_are_distinct(self):
        command = self.root / "script.cmd"
        profile = self.profile(command=str(command))
        # Windows requires .exe at syntax validation; POSIX commands need no
        # suffix. Metadata validation must still reject a missing executable.
        if os.name == "nt":
            with self.assertRaisesRegex(ValueError, "^invalid_mcp_profile$"):
                validate_profile_config(profile, self.root, inspect=False)
        else:
            self.assertEqual(validate_profile_config(profile, self.root, inspect=False)["command"], str(command))
        missing = self.root / ("missing.exe" if os.name == "nt" else "missing")
        with patch("subprocess.Popen", side_effect=AssertionError("validation must not spawn")):
            with self.assertRaisesRegex(ValueError, "^invalid_mcp_profile$"):
                validate_profile_config(self.profile(command=str(missing)), self.root)

    async def test_discovery_only_then_registered_permission_and_approval(self):
        connection = self.connection()
        connection.start()
        self.assertEqual([tool.name for tool in connection.tools], ["mcp.fixture.work"])
        self.assertFalse((self.root / "markers/sent.json").exists())
        worker = await self.marker("worker.json")
        handle = self.process_handle(worker["pid"])
        registry = ToolRegistry(connection.tools)
        executor = ToolExecutor(registry)
        call = ToolCall("reviewed", "mcp.fixture.work", {"value": 17})
        self.assertEqual(executor.check(call).error_code, "permission_denied")
        permission = frozenset({"mcp.fixture.call"})
        self.assertEqual(executor.check(call, permission).error_code, "approval_required")
        value = await executor.aexecute(call, permission, approved=True)
        self.assertIsNone(value.error_code)
        self.assertEqual(value.value["structuredContent"], {"value": 17})
        connection.close()
        self.assert_dead(handle)
        self.assertIsNone(connection.status()["error_code"])
        self.assertEqual((await executor.aexecute(call, permission, approved=True)).error_code, "tool_unavailable")

    async def test_minimal_environment_explicit_reference_and_relative_unicode_cwd(self):
        directory = self.root / "中文 空格"
        directory.mkdir()
        script = self.root / "environment_server.py"
        prelude = ("import os,json,pathlib\npathlib.Path('environment.json').write_text(json.dumps({"
            "'secret_inherited':'ASTRA_API_KEY' in os.environ, 'other_role':'ASTRA_EMBEDDING_API_KEY' in os.environ,"
            "'path':'PATH' in os.environ, 'explicit':os.environ.get('SERVICE_TOKEN')=='synthetic-only',"
            "'cwd':str(pathlib.Path.cwd())}),encoding='utf-8')\n")
        script.write_text(prelude + FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        args = self.profile()["args"]
        args[4] = str(script)
        reads = []
        def environment():
            reads.append(True)
            return {"ASTRA_MCP_FIXTURE_TOKEN": "synthetic-only"}
        connection = self.connection(self.profile(args=args, cwd="中文 空格",
            env_refs={"SERVICE_TOKEN": "ASTRA_MCP_FIXTURE_TOKEN"}), environment=environment)
        with patch.dict(os.environ, {"ASTRA_API_KEY": "not-for-child", "ASTRA_EMBEDDING_API_KEY": "not-for-child"}):
            connection.start()
        observed = json.loads((directory / "environment.json").read_text(encoding="utf-8"))
        self.assertEqual(reads, [True])
        self.assertEqual(observed, {"secret_inherited": False, "other_role": False, "path": False,
                                   "explicit": True, "cwd": str(directory)})
        self.assertNotIn("synthetic-only", json.dumps(connection.status()))

    async def test_missing_reference_rejects_before_spawn_and_never_echoes_value(self):
        for value in (None, "", "bad\0value", "x" * 8193):
            connection = self.connection(self.profile(env_refs={"KEY": "ASTRA_MCP_KEY"}),
                environment=lambda: {"ASTRA_MCP_KEY": value})
            with patch("subprocess.Popen", side_effect=AssertionError("spawn before credentials")):
                with self.assertRaisesRegex(MCPError, "^mcp_environment_unavailable$"):
                    connection.start()
            self.assertEqual(connection.status()["error_code"], "mcp_environment_unavailable")

    async def test_new_activation_has_different_tool_identity(self):
        first = self.connection().start()
        identity = first.tools[0].recovery_identity
        first.close()
        second = self.connection().start()
        self.assertNotEqual(first.identity, second.identity)
        self.assertNotEqual(identity, second.tools[0].recovery_identity)

    async def test_close_before_start_is_terminal_and_effect_free(self):
        connection = self.connection()
        connection.close()
        with patch("subprocess.Popen", side_effect=AssertionError("late process")):
            with self.assertRaisesRegex(MCPError, "^mcp_closed$"):
                connection.start()

    async def test_directory_replacement_after_validation_prevents_dispatch(self):
        cwd = self.root / "selected"
        cwd.mkdir()
        connection = self.connection(self.profile(cwd="selected"))
        cwd.rename(self.root / "prior")
        cwd.mkdir()
        with patch("subprocess.Popen", side_effect=AssertionError("changed cwd dispatch")):
            with self.assertRaisesRegex(MCPError, "^mcp_configuration_changed$"):
                connection.start()

    async def test_failed_initialize_and_discovery_close_processes(self):
        for mode in ("initfailure", "listfailure"):
            args = self.profile()["args"]
            args[-1] = mode
            connection = self.connection(self.profile(args=args))
            with self.assertRaises(MCPError):
                connection.start()
            self.assertEqual(connection.tools, ())
            self.assertEqual(connection.status()["state"], "failed")
            transport = connection._manager._services[0].client
            self.assertIsNotNone(transport._last_connection.process.poll())
            self.assertFalse(transport._last_connection.reader.is_alive())
            connection.close()

    async def test_executable_replacement_rejects_before_spawn(self):
        executable = self.root / ("fixture.exe" if os.name == "nt" else "fixture")
        executable.write_bytes(b"synthetic metadata fixture; never execute")
        executable.chmod(0o700)
        connection = self.connection(self.profile(command=str(executable)))
        executable.write_bytes(b"changed synthetic metadata fixture; never execute")
        with patch("subprocess.Popen", side_effect=AssertionError("changed executable dispatch")):
            with self.assertRaisesRegex(MCPError, "^mcp_configuration_changed$"):
                connection.start()

    async def test_environment_resolution_cannot_hide_a_directory_change(self):
        directory = self.root / "selected"
        directory.mkdir()
        def environment():
            directory.rename(self.root / "previous")
            directory.mkdir()
            return {"ASTRA_MCP_TOKEN": "synthetic"}
        connection = self.connection(self.profile(cwd="selected", env_refs={"TOKEN": "ASTRA_MCP_TOKEN"}), environment=environment)
        with patch("subprocess.Popen", side_effect=AssertionError("changed directory dispatch")):
            with self.assertRaisesRegex(MCPError, "^mcp_configuration_changed$"):
                connection.start()

    async def test_close_while_initializing_kills_owned_process(self):
        script = self.root / "waiting.py"
        script.write_text("import os,pathlib,time,json\npathlib.Path('waiting.json').write_text(json.dumps({'pid':os.getpid()}))\ntime.sleep(60)\n")
        connection = self.connection(self.profile(args=["-I", "-S", str(script)], timeout=30))
        outcome = []
        def start():
            try:
                connection.start()
            except MCPError as error:
                outcome.append(str(error))
        thread = threading.Thread(target=start)
        thread.start()
        deadline = time.monotonic() + 4
        while not (self.root / "waiting.json").exists() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        handle = self.process_handle(json.loads((self.root / "waiting.json").read_text())["pid"])
        began = time.monotonic()
        connection.close()
        thread.join(1)
        self.assertLess(time.monotonic() - began, 5)
        self.assertFalse(thread.is_alive())
        self.assert_dead(handle)
        self.assertTrue(outcome)

    async def test_parent_exit_with_descendant_pipe_is_still_cleaned(self):
        args = self.profile()["args"]
        args[-1] = "orphanstdout"
        connection = self.connection(self.profile(args=args, timeout=1)).start()
        pending = asyncio.create_task(connection.tools[0].handler(value=7))
        marker = await self.marker("orphan-ready.json")
        handle = self.process_handle(marker["pid"])
        with self.assertRaises(ToolExecutionUnknown):
            await pending
        self.assert_dead(handle)
        connection.close()
        self.assertNotEqual(connection.status()["error_code"], "mcp_cleanup_incomplete")

    async def test_cancel_issued_tool_cleans_connection_and_does_not_retry(self):
        args = self.profile()["args"]
        args[-1] = "hang"
        connection = self.connection(self.profile(args=args, timeout=30)).start()
        handle = self.process_handle((await self.marker("worker.json"))["pid"])
        pending = asyncio.create_task(connection.tools[0].handler(value=7))
        await self.marker("sent.json")
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assert_dead(handle)
        self.assertEqual(connection.status()["state"], "failed")

    @unittest.skipUnless(os.name == "nt", "Windows Job failure boundary")
    async def test_job_assignment_failure_never_releases_command_and_cleans_gate(self):
        processes = []
        original = subprocess.Popen
        def observe(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        connection = self.connection()
        with patch("subprocess.Popen", side_effect=observe), patch(
            "astra_harness.builtin.mcp_profile_connection.WindowsJob.assign", side_effect=OSError("fixture")):
            with self.assertRaises(MCPError):
                connection.start()
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse((self.root / "markers").exists())

    async def test_http_discovery_and_close_do_not_execute_tools_or_read_environment(self):
        with LocalMCPHTTPServer() as server:
            connection = self.connection({"name": "http", "transport": "http", "endpoint": server.url, "timeout": 2},
                environment=lambda: self.fail("HTTP has no credential references"))
            self.assertEqual(server.requests, [])
            connection.start()
            self.assertEqual([tool.name for tool in connection.tools], ["mcp.http.record"])
            connection.close()
            self.assertEqual([item["method"] for item in server.requests],
                             ["initialize", "notifications/initialized", "tools/list", "DELETE"])
            self.assertEqual(server.receipts, [])

if __name__ == "__main__":
    unittest.main()
