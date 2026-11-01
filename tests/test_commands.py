"""Trusted synthetic children only: no service credentials, network or user files."""

import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from astra_harness.builtin.commands import CommandPlugin, validate_executables
from astra_harness.builtin._command_process import LAUNCH_FAILURE
from astra_harness.tools import ToolExecutionUnknown, ToolUnavailable, ToolRegistry, ToolExecutor
from astra_harness.contracts import ToolCall


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="astra-commands-")
        self.root = Path(self.temp.name)
        self.python = str(Path(sys.executable).resolve())
        self.plugins = []

    def tearDown(self):
        for plugin in self.plugins:
            plugin.close()
        self.temp.cleanup()

    def plugin(self, **kwargs):
        result = CommandPlugin(self.root, [self.python], **kwargs)
        self.plugins.append(result)
        return result

    def command(self, code, *args):
        return [self.python, "-I", "-S", "-X", "utf8", "-c", code, *args]

    async def wait_file(self, path):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    return int(path.read_text())
                except ValueError:
                    pass
            await asyncio.sleep(0.01)
        self.fail("trusted child did not publish pid")

    def process_handle(self, pid):
        if os.name != "nt":
            return pid
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only.
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

    def spawn_code(self, *, parent_wait=True):
        child = "import time; time.sleep(60)"
        return ("import subprocess,sys,pathlib,time; "
                f"p=subprocess.Popen([sys.executable,'-I','-S','-c',{child!r}]); "
                "pathlib.Path('child.pid').write_text(str(p.pid)); "
                + ("time.sleep(60)" if parent_wait else "time.sleep(.15)"))

    async def test_construct_is_effect_free_and_policy_is_mandatory(self):
        with patch("subprocess.Popen", side_effect=AssertionError("spawn during setup")):
            plugin = self.plugin()
        self.assertEqual(list(self.root.iterdir()), [])
        tool = plugin.tools[0]
        self.assertEqual(tool.name, "commands.run")
        self.assertEqual(tool.permissions, frozenset({"commands.execute"}))
        self.assertTrue(tool.requires_approval and tool.side_effect)
        registry = ToolRegistry()
        self.assertTrue(callable(plugin.activate(registry)))
        executor = ToolExecutor(registry)
        call = ToolCall("c1", tool.name, {"argv": self.command("print('never')")})
        self.assertEqual(executor.check(call).error_code, "permission_denied")
        self.assertEqual(executor.check(call, frozenset({"commands.execute"})).error_code, "approval_required")
        self.assertIsNone(executor.check(call, frozenset({"commands.execute"}), approved=True))

    async def test_exact_output_exit_and_literal_arguments(self):
        value = "literal & | > $(not-a-shell) 中文"
        plugin = self.plugin()
        # Windows process-signalled and Job-accounting updates are not atomic.
        for _ in range(5):
            result = await plugin.run(self.command("import sys; print(sys.argv[1]); print('error',file=sys.stderr)", value))
            self.assertTrue(result["ok"])
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"].strip(), value)
            self.assertEqual(result["stderr"].strip(), "error")
            self.assertTrue(result["cleanup_complete"])
            self.assertFalse(result["truncated"])

    async def test_executable_alias_and_changed_identity_are_rejected(self):
        plugin = self.plugin()
        alias = self.root / "alias.exe"
        alias.symlink_to(self.python)
        with self.assertRaises(ToolUnavailable) as error:
            await plugin.run([str(alias), "-V"])
        self.assertEqual(error.exception.details["reason"], "executable_not_allowed")
        fake = self.root / ("trusted.exe" if os.name == "nt" else "trusted")
        fake.write_bytes(b"synthetic-not-executed")
        fake.chmod(0o700)
        configured = CommandPlugin(self.root, [str(fake)])
        self.plugins.append(configured)
        fake.write_bytes(b"changed-synthetic-never-executed")
        with patch("subprocess.Popen", side_effect=AssertionError("modified executable ran")):
            with self.assertRaises(ToolUnavailable) as error:
                await configured.run([str(fake)])
        self.assertEqual(error.exception.details["reason"], "executable_changed")

    async def test_nonzero_exit_is_known_and_has_no_automatic_retry(self):
        result = await self.plugin().run(self.command("import pathlib,sys; pathlib.Path('effect.txt').write_text('once'); sys.exit(7)"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual((self.root / "effect.txt").read_text(), "once")

    async def test_clean_environment_stdin_and_unicode_cwd(self):
        (self.root / "中文 folder").mkdir()
        code = "import json,os,sys; print(json.dumps({'keys':sorted(os.environ),'cwd':os.path.basename(os.getcwd()),'stdin':sys.stdin.read()}))"
        with patch.dict(os.environ, {"ASTRA_API_KEY": "synthetic-sentinel", "PRIVATE_SECRET": "synthetic-other", "PYTHONPATH": str(self.root)}):
            result = await self.plugin().run(self.command(code), "中文 folder")
        value = json.loads(result["stdout"])
        self.assertEqual(value["cwd"], "中文 folder")
        self.assertEqual(value["stdin"], "")
        self.assertNotIn("ASTRA_API_KEY", value["keys"])
        self.assertNotIn("PRIVATE_SECRET", value["keys"])
        self.assertNotIn("PATH", value["keys"])
        self.assertNotIn("PYTHONPATH", value["keys"])

    async def test_preflight_rejections_do_not_spawn(self):
        plugin = self.plugin()
        with patch("subprocess.Popen", side_effect=AssertionError("rejected command spawned")):
            for argv, cwd in [(["python", "-V"], "."), ([], "."), ([self.python, "\0"], "."),
                              ([self.python], ".."), ([self.python], str(self.root)),
                              ([self.python], "missing"), ([self.python], ".env"),
                              ([self.python, "x" * 9000], ".")]:
                with self.subTest(argv=argv[:1], cwd=cwd), self.assertRaises(ToolUnavailable) as raised:
                    await plugin.run(argv, cwd)
                self.assertEqual(raised.exception.code, "command_rejected")
        plugin.close()
        with self.assertRaises(ToolUnavailable):
            await plugin.run(self.command("print('never')"))

    async def test_config_limits_identity_and_replacement(self):
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"max_output_bytes": 1000000}, {"revision": "bad space"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.plugin(**kwargs)
        self.assertEqual(validate_executables([]), ())
        with self.assertRaises(ValueError):
            validate_executables(["relative.exe"])
        plugin = self.plugin()
        self.assertNotEqual(plugin.recovery_identity, self.plugin(revision="2").recovery_identity)
        # Root replacement is tested without moving unrelated files.
        self.root.rename(self.root.with_name(self.root.name + "-old"))
        self.root.mkdir()
        try:
            with self.assertRaises(ToolUnavailable) as error:
                await plugin.run(self.command("print('never')"))
            self.assertEqual(error.exception.details["reason"], "workspace_changed")
        finally:
            self.root.rmdir()
            self.root.with_name(self.root.name + "-old").rename(self.root)

    async def test_timeout_keeps_effect_unknown_and_kills_descendants(self):
        plugin = self.plugin(timeout=.8)
        task = asyncio.create_task(plugin.run(self.command(self.spawn_code())))
        handle = self.process_handle(await self.wait_file(self.root / "child.pid"))
        with self.assertRaises(ToolExecutionUnknown) as raised:
            await task
        self.assertEqual(raised.exception.details["reason"], "timeout")
        self.assertTrue(raised.exception.details["cleanup_complete"])
        self.assert_dead(handle)

    async def test_cancellation_cleans_real_tree_and_keeps_unknown(self):
        plugin = self.plugin()
        task = asyncio.create_task(plugin.run(self.command(self.spawn_code())))
        handle = self.process_handle(await self.wait_file(self.root / "child.pid"))
        task.cancel()
        task.cancel()
        with self.assertRaises(ToolExecutionUnknown) as raised:
            await task
        self.assertEqual(raised.exception.details["reason"], "cancelled")
        self.assertTrue(raised.exception.details["cleanup_complete"])
        self.assert_dead(handle)

    async def test_cancel_before_dispatch_has_no_effect(self):
        plugin = self.plugin()
        with patch("subprocess.Popen", side_effect=AssertionError("cancelled before launch")):
            task = asyncio.create_task(plugin.run(self.command("pass")))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_concurrent_dispatch_is_rejected_without_replacing_owned_tree(self):
        plugin = self.plugin()
        task = asyncio.create_task(plugin.run(self.command(self.spawn_code())))
        handle = self.process_handle(await self.wait_file(self.root / "child.pid"))
        try:
            with self.assertRaises(ToolUnavailable) as raised:
                await plugin.run(self.command("print('never')"))
            self.assertEqual(raised.exception.details["reason"], "command_busy")
        finally:
            task.cancel()
            with self.assertRaises(ToolExecutionUnknown):
                await task
            self.assert_dead(handle)

    async def test_host_close_cleans_and_prevents_future_dispatch(self):
        plugin = self.plugin()
        task = asyncio.create_task(plugin.run(self.command(self.spawn_code())))
        handle = self.process_handle(await self.wait_file(self.root / "child.pid"))
        plugin.close()
        with self.assertRaises(ToolExecutionUnknown):
            await task
        self.assert_dead(handle)
        with self.assertRaises(ToolUnavailable):
            await plugin.run(self.command("pass"))

    async def test_output_flood_bounded_and_unknown(self):
        plugin = self.plugin(max_output_bytes=1024)
        with self.assertRaises(ToolExecutionUnknown) as raised:
            await plugin.run(self.command("import os;\nwhile True: os.write(1,b'x'*4096); os.write(2,b'y'*4096)"))
        value = raised.exception.details
        self.assertEqual(value["reason"], "output_limit")
        self.assertLessEqual(len(value["stdout"]) + len(value["stderr"]), 1024)
        self.assertEqual(value["output_bytes"], 1025)
        self.assertTrue(value["truncated"] and value["cleanup_complete"])

    async def test_background_child_with_inherited_pipes_is_not_success(self):
        task = asyncio.create_task(self.plugin().run(self.command(self.spawn_code(parent_wait=False))))
        handle = self.process_handle(await self.wait_file(self.root / "child.pid"))
        with self.assertRaises(ToolExecutionUnknown) as raised:
            await task
        self.assertEqual(raised.exception.details["reason"], "background_processes")
        self.assert_dead(handle)

    @unittest.skipUnless(os.name == "nt", "Windows job assignment gate")
    async def test_job_assignment_failure_never_releases_user_argv(self):
        with patch("astra_harness.builtin.commands.WindowsJob.assign", side_effect=OSError("job_assignment_failed")):
            with self.assertRaises(ToolUnavailable) as raised:
                await self.plugin().run(self.command("import pathlib; pathlib.Path('must-not-exist').write_text('bad')"))
        self.assertEqual(raised.exception.details["reason"], "job_assignment_failed")
        self.assertFalse((self.root / "must-not-exist").exists())

    @unittest.skipUnless(os.name == "nt", "Windows launcher frozen source")
    async def test_launcher_change_rejects_before_starting_process(self):
        plugin = self.plugin()
        synthetic = self.root / "changed-helper.py"
        synthetic.write_text("raise RuntimeError('must not execute')")
        plugin._launcher = synthetic
        with patch("subprocess.Popen", side_effect=AssertionError("changed helper executed")):
            with self.assertRaises(ToolUnavailable) as raised:
                await plugin.run(self.command("pass"))
        self.assertEqual(raised.exception.details["reason"], "launcher_changed")

    @unittest.skipUnless(os.name == "nt", "Windows launcher reserved status")
    async def test_reserved_exit_is_conservatively_unknown(self):
        with self.assertRaises(ToolExecutionUnknown) as raised:
            await self.plugin().run(self.command(f"import sys; sys.exit({LAUNCH_FAILURE})"))
        self.assertEqual(raised.exception.details["reason"], "launch_status_unknown")


if __name__ == "__main__":
    unittest.main()
