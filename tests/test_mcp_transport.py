"""Real trusted local workers, including Windows venv launchers; no network."""
import asyncio
import ctypes
import io
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

from astra_harness.builtin.mcp import MCPError, StdioMCPClient, _Connection
from astra_harness.tools import ToolExecutionUnknown, ToolUnavailable


FIXTURE = Path(__file__).parent / 'fixtures/mcp_service_server.py'


def alive(pid):
    # Inspect only the trusted fixture's observed PID; never terminate that PID.
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 5:
                raise RuntimeError('fixture_process_inspection_denied')
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='astra-mcp-test-')
        self.root = Path(self.temporary.name)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            client.close()
        self.temporary.cleanup()

    def client(self, mode='normal'):
        directory = self.root / str(len(self.clients))
        client = StdioMCPClient([sys.executable, '-X', 'utf8', str(FIXTURE), '--directory', str(directory), '--mode', mode], timeout=3)
        self.clients.append(client)
        return client, directory

    async def marker(self, path, timeout=3):
        deadline = time.monotonic()+timeout
        while not path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(path.exists(), str(path.name)+' marker missing')
        return json.loads(path.read_text(encoding='utf-8'))

    async def cleaned(self, client, connection, pid):
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if not any(thread.is_alive() for thread in (connection.reader, connection.writer)) and not client._workers and not alive(pid):
                break
            await asyncio.sleep(.01)
        self.assertFalse(alive(pid), 'actual fixture worker survived cleanup')
        self.assertIsNotNone(connection.process.poll(), 'owned launcher survived cleanup')
        self.assertFalse(connection.reader.is_alive())
        self.assertFalse(connection.writer.is_alive())
        self.assertFalse(client._workers)
        self.assertNotEqual(client.last_error, 'mcp_cleanup_incomplete')

    async def test_sync_and_async_roundtrip_and_idempotent_ready_start(self):
        client, directory = self.client()
        tools = client.discover_tools()
        connection = client._connection
        pid = (await self.marker(directory/'worker.json'))['pid']
        self.assertTrue(alive(pid))
        self.assertIs(client.start(), client)
        self.assertIs(client._connection, connection)
        self.assertEqual([tool.name for tool in tools], ['mcp.work'])
        self.assertEqual(client.call_tool('work', {'value': 42})['structuredContent'], {'value': 42})
        self.assertEqual((await client.acall_tool('work', {'value': 7}))['structuredContent'], {'value': 7})
        client.close()
        client.close()
        await self.cleaned(client, connection, pid)

    async def test_fault_frames_disconnect_and_effect_without_response_are_unknown(self):
        for mode in ('badframe', 'oversized', 'partial', 'disconnect', 'writeexit'):
            with self.subTest(mode=mode):
                client, directory = self.client(mode)
                client.discover_tools()
                connection = client._connection
                pid = (await self.marker(directory/'worker.json'))['pid']
                client.timeout = .2
                with self.assertRaisesRegex(ToolExecutionUnknown, '^mcp_execution_unknown$'):
                    await client.acall_tool('work', {'value': 42})
                await self.marker(directory/'sent.json')
                if mode == 'writeexit':
                    self.assertEqual((await self.marker(directory/'effect.json'))['value'], 42)
                self.assertEqual(client.state, 'failed')
                with self.assertRaises(ToolUnavailable):
                    client.call_tool('work', {'value': 43})
                client.close()
                await self.cleaned(client, connection, pid)

    async def test_timeout_cancel_notification_and_no_automatic_retry(self):
        client, directory = self.client('hang')
        client.discover_tools()
        connection = client._connection
        pid = (await self.marker(directory/'worker.json'))['pid']
        client.timeout = .2
        with self.assertRaises(ToolExecutionUnknown):
            await client.acall_tool('work', {'value': 42})
        sent = await self.marker(directory/'sent.json')
        cancelled = await self.marker(directory/'cancelled.json')
        self.assertEqual(cancelled['requestId'], sent['request_id'])
        self.assertEqual(client.last_error, 'mcp_timeout')
        await self.cleaned(client, connection, pid)

    async def test_async_cancellation_cleans_venv_worker_and_only_its_connection(self):
        broken, directory = self.client('hang')
        healthy, other = self.client()
        broken.discover_tools()
        healthy.discover_tools()
        connection = broken._connection
        pid = (await self.marker(directory/'worker.json'))['pid']
        task = asyncio.create_task(broken.acall_tool('work', {'value': 42}))
        await self.marker(directory/'sent.json')
        loop, diagnostics = asyncio.get_running_loop(), []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: diagnostics.append(context))
        try:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
            await asyncio.sleep(0)
            self.assertEqual(diagnostics, [], 'expected cancellation leaked a loop error')
        finally:
            loop.set_exception_handler(previous_handler)
        await self.cleaned(broken, connection, pid)
        self.assertTrue(healthy.is_alive)
        self.assertEqual((await healthy.acall_tool('work', {'value': 9}))['structuredContent'], {'value': 9})

    async def test_cancel_while_waiting_for_request_lock_does_not_dispatch_or_close(self):
        client, directory = self.client()
        client.discover_tools()
        client._request_lock.acquire()
        try:
            task = asyncio.create_task(client.acall_tool('work', {'value': 42}))
            await asyncio.sleep(.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        finally:
            client._request_lock.release()
        self.assertFalse((directory/'sent.json').exists())
        self.assertTrue(client.is_alive)
        self.assertEqual(client.call_tool('work', {'value': 1})['structuredContent']['value'], 1)

    async def test_cancel_blocked_pipe_write_kills_owned_venv_tree_and_joins_writer(self):
        client, directory = self.client('stalledstdin')
        client.discover_tools()
        await self.marker(directory/'stalled.json')
        connection = client._connection
        pid = (await self.marker(directory/'worker.json'))['pid']
        task = asyncio.create_task(client.acall_tool('work', {'value': 42, 'text': 'x'*700_000}))
        deadline = time.monotonic()+2
        while connection.writer_idle.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertFalse(connection.writer_idle.is_set(), 'fixture did not block the writer')
        self.assertTrue(alive(pid))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 4)
        await self.cleaned(client, connection, pid)

    async def test_close_wakes_sync_waiter_without_request_lock_deadlock(self):
        client, directory = self.client('hang')
        client.discover_tools()
        connection = client._connection
        pid = (await self.marker(directory/'worker.json'))['pid']
        errors = []
        def call():
            try:
                client.call_tool('work', {'value': 42})
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        await self.marker(directory/'sent.json')
        started = time.monotonic()
        client.close()
        worker.join(1)
        self.assertLess(time.monotonic()-started, 3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ToolExecutionUnknown)
        await self.cleaned(client, connection, pid)

    async def test_competing_reader_close_never_waits_for_closer_joining_that_reader(self):
        class ExitedProcess:
            # Only thread ordering is injected; no fake PID is ever terminated.
            stdin, stdout = io.BytesIO(), io.BytesIO()
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0
        client = StdioMCPClient(['unused-ordering-test'])
        self.clients.append(client)
        connection = _Connection(ExitedProcess(), 1)
        client._last_connection = connection
        release = threading.Event()
        real_join = threading.Thread.join
        def reader():
            # _read observed stop=False before another thread acquired close.
            if release.wait(2):
                client._abort(connection, 'mcp_transport_closed')
        connection.reader = threading.Thread(target=reader, daemon=True)
        def join_after_release(timeout=None):
            if not release.is_set():
                self.assertTrue(connection.close_lock.locked())
                release.set()
            return real_join(connection.reader, timeout)
        connection.reader.join = join_after_release
        connection.reader.start()
        try:
            client.close()
        finally:
            release.set()
            real_join(connection.reader, 1)
        self.assertFalse(connection.reader.is_alive())
        self.assertTrue(connection.closed.is_set())
        self.assertNotEqual(client.last_error, 'mcp_cleanup_incomplete')

    @unittest.skipUnless(os.name == 'nt', 'Windows CRT inherited-pipe regression')
    async def test_exited_root_with_inherited_stdout_returns_incomplete_without_hanging(self):
        client, directory = self.client('orphanstdout')
        client.discover_tools()
        connection = client._connection
        client.timeout = .5
        holder_pid = None
        started = time.monotonic()
        task = asyncio.create_task(client.acall_tool('work', {'value': 42}))
        try:
            await self.marker(directory/'sent.json')
            holder_pid = (await self.marker(directory/'orphan-ready.json'))['pid']
            deadline = time.monotonic()+1
            while connection.process.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            self.assertIsNotNone(connection.process.poll(), 'owned launcher must exit first')
            self.assertTrue(alive(holder_pid), 'fixture must still hold the inherited pipe')
            done, _ = await asyncio.wait({task}, timeout=2)
            self.assertIn(task, done, 'cleanup blocked on an inherited pipe')
            with self.assertRaisesRegex(ToolExecutionUnknown, '^mcp_execution_unknown$'):
                task.result()
            self.assertLess(time.monotonic()-started, 2.5)
            self.assertEqual(client.last_error, 'mcp_cleanup_incomplete')
            self.assertTrue(connection.reader.is_alive(), 'the unowned holder has not exited')
        finally:
            # The fixture self-exits after five seconds even on test failure.
            # Release it by a scoped file signal, never by its reported PID.
            (directory/'stop-orphan').touch()
            await self.marker(directory/'orphan-exit.json', timeout=6)
            await asyncio.wait({task}, timeout=3)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            deadline = time.monotonic()+2
            while time.monotonic() < deadline:
                if (not connection.reader.is_alive() and not connection.writer.is_alive()
                        and not client._workers and (holder_pid is None or not alive(holder_pid))):
                    break
                await asyncio.sleep(.01)
            client.close()
        self.assertFalse(connection.reader.is_alive())
        self.assertFalse(connection.writer.is_alive())
        self.assertFalse(client._workers)
        self.assertFalse(alive(holder_pid), 'holder did not exit after the stop marker')
        self.assertEqual(client.last_error, 'mcp_cleanup_incomplete')

    async def test_prewrite_limits_and_invalid_json_never_dispatch(self):
        client, directory = self.client()
        client.discover_tools()
        for arguments in ({'value': float('nan')}, {'value': 1, 'text': 'x'*1_000_000}):
            with self.assertRaises(ToolUnavailable):
                client.call_tool('work', arguments)
            self.assertFalse((directory/'sent.json').exists())
        self.assertTrue(client.is_alive)
        self.assertEqual(client.call_tool('work', {'value': 2})['structuredContent']['value'], 2)

    async def test_partial_writes_are_completed_without_interleaving(self):
        client, directory = self.client()
        client.discover_tools()
        stream = client.process.stdin
        class PartialWriter:
            writes = 0
            def write(self, data):
                self.writes += 1
                return stream.write(data[:7])
            def close(self):
                stream.close()
        partial = PartialWriter()
        client.process.stdin = partial
        self.assertEqual(client.call_tool('work', {'value': 42})['structuredContent']['value'], 42)
        self.assertGreater(partial.writes, 1)

    async def test_failed_initialize_or_discovery_close_owned_process_and_never_restart(self):
        for mode in ('initfailure', 'listfailure'):
            client, directory = self.client(mode)
            with self.assertRaises(MCPError):
                client.discover_tools()
            connection = client._last_connection
            pid = (await self.marker(directory/'worker.json'))['pid']
            await self.cleaned(client, connection, pid)
            self.assertEqual(client.state, 'failed')
            with patch('astra_harness.builtin.mcp.subprocess.Popen', side_effect=AssertionError('late spawn')):
                with self.assertRaises(MCPError): client.start()
                with self.assertRaises(MCPError): client.discover_tools()

    async def test_close_before_start_is_terminal_and_does_not_spawn(self):
        client, _ = self.client()
        client.close()
        with patch('astra_harness.builtin.mcp.subprocess.Popen', side_effect=AssertionError('late spawn')):
            with self.assertRaises(MCPError): client.start()
            with self.assertRaises(ToolUnavailable): await client.acall_tool('work', {'value': 42})
        self.assertEqual(client.state, 'closed')

    async def test_explicit_working_directory_is_used_by_actual_worker(self):
        directory = self.root / 'bound-cwd'
        directory.mkdir()
        markers = directory / 'markers'
        client = StdioMCPClient([sys.executable, '-X', 'utf8', str(FIXTURE.resolve()),
            '--directory', str(markers), '--mode', 'normal'], cwd=directory)
        self.clients.append(client)
        client.discover_tools()
        marker = await self.marker(markers/'worker.json')
        self.assertEqual(Path(marker['cwd']).resolve(), directory.resolve())
        self.assertEqual(client.cwd, str(directory.resolve()))
        self.assertEqual(client.call_tool('work', {'value': 42})['structuredContent']['value'], 42)

    async def test_close_racing_process_creation_waits_for_owned_cleanup(self):
        client, directory = self.client()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        real = subprocess.Popen
        children, errors = [], []
        def paused(command, *args, **kwargs):
            if str(FIXTURE) in command:
                entered.set()
                if not release.wait(2): raise RuntimeError('test handshake timeout')
            process = real(command, *args, **kwargs)
            if str(FIXTURE) in command: children.append(process)
            return process
        def start():
            try: client.start()
            except MCPError as error: errors.append(error)
        def close():
            client.close()
            stopped.set()
        with patch('astra_harness.builtin.mcp.subprocess.Popen', side_effect=paused):
            starter = threading.Thread(target=start, daemon=True)
            starter.start()
            self.assertTrue(entered.wait(1))
            closer = threading.Thread(target=close, daemon=True)
            closer.start()
            self.assertFalse(stopped.wait(.03))
            release.set()
            starter.join(3)
            closer.join(3)
        self.assertFalse(starter.is_alive() or closer.is_alive())
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(client.state, 'closed')
        self.assertEqual(len(errors), 1)


if __name__ == '__main__':
    unittest.main()
