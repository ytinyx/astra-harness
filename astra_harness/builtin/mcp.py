"""Bounded MCP 2025-06-18 STDIO tools; launch only host-trusted commands.

Each connection owns its pipes, queues and threads. Cancellation is best effort,
never evidence that a remote side effect did not happen. No automatic reconnect,
HTTP transport, sampling, resources or dynamic tool-list changes are provided.
"""
import asyncio
import json
import math
import os
import queue
import re
import subprocess
import threading
import time

from ..tools import Tool, ToolExecutionUnknown, ToolUnavailable


class MCPError(RuntimeError):
    """Only fixed diagnostic codes, never subprocess output or command values."""


class _Operation:
    def __init__(self, method, timeout):
        self.method, self.deadline = method, time.monotonic() + timeout
        self.cancelled, self.guard = threading.Event(), threading.Lock()
        self.issued = False
        self.request_id = self.connection = None


class _Connection:
    def __init__(self, process, generation):
        self.process, self.generation = process, generation
        self.messages, self.writes = queue.Queue(maxsize=64), queue.Queue(maxsize=1)
        self.stop, self.closed = threading.Event(), threading.Event()
        self.retiring = threading.Event()
        self.close_lock = threading.Lock()
        self.writer_idle = threading.Event()
        self.writer_idle.set()
        self.reader = self.writer = None
        self.error = None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite JSON")


class StdioMCPClient:
    def __init__(self, command: list[str], timeout: float = 10, max_message_bytes: int = 1_000_000, *, cwd=None,
                 env=None, process_factory=None):
        if not isinstance(command, (list, tuple)) or not command or not all(isinstance(v, str) and v and '\x00' not in v for v in command):
            raise ValueError("invalid_mcp_command")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300
                or type(max_message_bytes) is not int or not 256 <= max_message_bytes <= 10_000_000):
            raise ValueError("invalid_mcp_limits")
        self.command, self.timeout, self.max_message_bytes = list(command), timeout, max_message_bytes
        if env is not None and (type(env) is not dict or any(not isinstance(k, str) or not isinstance(v, str)
                or not k or "=" in k or "\0" in k or "\0" in v for k, v in env.items())):
            raise ValueError("invalid_mcp_environment")
        if process_factory is not None and not callable(process_factory):
            raise ValueError("invalid_mcp_process_factory")
        # Explicit host-owned profiles can supply a minimal environment and an
        # owned process tree. The existing trusted static CLI remains compatible.
        self._environment = dict(env) if env is not None else None
        self._process_factory = process_factory
        try:
            directory = os.fspath(cwd) if cwd is not None else None
            if directory is not None and (not isinstance(directory, str) or not directory or '\x00' in directory):
                raise ValueError
            self.cwd = os.path.abspath(directory) if directory is not None else None
        except (TypeError, ValueError):
            raise ValueError("invalid_mcp_cwd") from None
        self.process = None
        self._state, self._last_error = "new", None
        self._guard, self._request_lock = threading.Lock(), threading.Lock()
        self._connection = self._last_connection = None
        self._generation = self._next_id = 0
        self._workers = set()
        self._start_done = threading.Event()
        self._start_done.set()

    @property
    def state(self):
        with self._guard:
            return self._state

    @property
    def last_error(self):
        with self._guard:
            return self._last_error

    @property
    def is_alive(self):
        with self._guard:
            connection = self._connection
        return bool(connection and not connection.stop.is_set() and connection.process.poll() is None)

    def start(self):
        with self._guard:
            if self._state == "ready":
                return self
            if self._state == "starting":
                raise MCPError("mcp_start_in_progress")
            if self._state != "new":
                raise MCPError("mcp_closed")
            self._generation += 1
            generation = self._generation
            self._state, self._last_error = "starting", None
            self._start_done.clear()
        connection = None
        try:
            options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, bufsize=0, cwd=self.cwd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if self._environment is not None:
                options["env"] = dict(self._environment)
            factory = self._process_factory or subprocess.Popen
            process = factory(self.command, **options)
            connection = _Connection(process, generation)
            with self._guard:
                # Even a spawn racing close remains owned for cleanup reporting.
                self._last_connection = connection
                if self._state != "starting" or self._generation != generation:
                    raise MCPError("mcp_closed")
                self._connection = connection
                self.process = process
            connection.reader = threading.Thread(target=self._read, args=(connection,), daemon=True,
                                                  name=f"astra-mcp-reader-{generation}")
            connection.writer = threading.Thread(target=self._write, args=(connection,), daemon=True,
                                                  name=f"astra-mcp-writer-{generation}")
            connection.reader.start()
            connection.writer.start()
            result = self._request(connection, _Operation("initialize", self.timeout), {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "astra-harness", "version": "0.5.0"},
            })
            if (result.get("protocolVersion") != "2025-06-18" or not isinstance(result.get("capabilities"), dict)
                    or not isinstance(result["capabilities"].get("tools"), dict)):
                raise MCPError("mcp_unsupported_capabilities")
            self._send(connection, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                       time.monotonic()+self.timeout)
            with self._guard:
                if self._connection is not connection or connection.stop.is_set():
                    raise MCPError("mcp_closed")
                self._state = "ready"
            return self
        except Exception:
            if connection is not None:
                self._abort(connection, "mcp_initialization_failed")
            else:
                with self._guard:
                    if self._generation == generation:
                        self._state, self._last_error = "failed", "mcp_initialization_failed"
            raise MCPError("mcp_initialization_failed") from None
        finally:
            self._start_done.set()

    def _read(self, connection):
        buffer = bytearray()
        try:
            while not connection.stop.is_set():
                chunk = connection.process.stdout.read(65536)
                if not chunk:
                    raise MCPError("mcp_transport_closed")
                buffer.extend(chunk)
                while True:
                    end = buffer.find(b"\n")
                    if end < 0:
                        break
                    if end+1 > self.max_message_bytes:
                        raise MCPError("mcp_message_limit")
                    raw = bytes(buffer[:end])
                    del buffer[:end+1]
                    message = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise MCPError("mcp_invalid_message")
                    connection.messages.put_nowait(message)
                if len(buffer) >= self.max_message_bytes:
                    raise MCPError("mcp_message_limit")
        except Exception as error:
            code = str(error) if isinstance(error, MCPError) else "mcp_invalid_message"
            if not connection.stop.is_set():
                self._abort(connection, code)
        finally:
            # Close from the I/O owner after read returns. On Windows even raw
            # CRT file close can wait for a concurrent blocked read.
            try:
                connection.process.stdout.close()
            except OSError:
                pass

    def _write(self, connection):
        while not connection.stop.is_set():
            try:
                data, finished, errors, operation = connection.writes.get(timeout=.02)
            except queue.Empty:
                continue
            connection.writer_idle.clear()
            try:
                if operation is not None:
                    # Cancellation-before-issue and issue ownership are atomic.
                    # An abandoned queued job must never be sent later.
                    with operation.guard:
                        if connection.stop.is_set() or connection.retiring.is_set() or operation.cancelled.is_set():
                            raise MCPError("mcp_cancelled")
                        operation.issued = True
                elif connection.stop.is_set():
                    raise MCPError("mcp_closed")
                offset = 0
                while offset < len(data):
                    count = connection.process.stdin.write(memoryview(data)[offset:])
                    if not count:
                        raise MCPError("mcp_send_failed")
                    offset += count
            except Exception:
                errors.append("mcp_send_failed")
            finally:
                connection.writer_idle.set()
                finished.set()
        try:
            connection.process.stdin.close()
        except OSError:
            pass

    def _send(self, connection, message, deadline, operation=None):
        try:
            data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")+b"\n"
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MCPError("mcp_invalid_request") from None
        if len(data) > self.max_message_bytes:
            raise MCPError("mcp_message_limit")
        finished, errors = threading.Event(), []
        job, queued = (data, finished, errors, operation), False
        while time.monotonic() < deadline:
            if operation and operation.cancelled.is_set():
                raise MCPError("mcp_cancelled")
            if connection.stop.is_set():
                raise MCPError(connection.error or "mcp_closed")
            if operation and connection.retiring.is_set():
                raise MCPError("mcp_closed")
            if not queued:
                try:
                    connection.writes.put_nowait(job)
                    queued = True
                except queue.Full:
                    pass
            if queued and finished.wait(.01):
                if errors:
                    raise MCPError(errors[0])
                return
        raise MCPError("mcp_send_timeout")

    def _request(self, connection, operation, params):
        acquired = False
        try:
            while time.monotonic() < operation.deadline:
                if operation.cancelled.is_set():
                    raise MCPError("mcp_cancelled")
                if connection.stop.is_set() or connection.retiring.is_set():
                    raise MCPError(connection.error or "mcp_closed")
                acquired = self._request_lock.acquire(timeout=.01)
                if acquired:
                    break
            if not acquired:
                raise MCPError("mcp_timeout")
            operation.connection = connection
            self._next_id += 1
            operation.request_id = self._next_id
            self._send(connection, {"jsonrpc": "2.0", "id": operation.request_id,
                       "method": operation.method, "params": params}, operation.deadline, operation)
            while time.monotonic() < operation.deadline:
                if operation.cancelled.is_set():
                    raise MCPError("mcp_cancelled")
                try:
                    message = connection.messages.get(timeout=.01)
                except queue.Empty:
                    if connection.stop.is_set():
                        raise MCPError(connection.error or "mcp_closed")
                    continue
                if "method" in message:
                    if not isinstance(message["method"], str):
                        raise MCPError("mcp_invalid_message")
                    if "id" in message:
                        self._send(connection, {"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "Unsupported client method"}}, operation.deadline)
                    continue
                if type(message.get("id")) is not int or message["id"] != operation.request_id:
                    raise MCPError("mcp_response_mismatch")
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise MCPError("mcp_remote_error")
                return message["result"]
            raise MCPError("mcp_timeout")
        except MCPError:
            if operation.issued:
                connection.retiring.set()
            raise
        finally:
            if acquired:
                self._request_lock.release()

    def _cancel_notification(self, connection, operation):
        # Never cancel initialize. The sole writer prevents frame interleaving;
        # if a prior write is stuck this short best-effort attempt simply expires.
        if operation.method == "tools/call" and operation.issued and not connection.stop.is_set():
            try:
                self._send(connection, {"jsonrpc": "2.0", "method": "notifications/cancelled",
                    "params": {"requestId": operation.request_id, "reason": "Client stopped waiting"}},
                    time.monotonic()+.1)
            except MCPError:
                pass

    def _abort(self, connection, code):
        connection.retiring.set()
        connection.error = connection.error or code
        with self._guard:
            if self._connection is connection:
                self._state, self._last_error = "failed", code
                self._connection, self.process = None, None
        self._close_connection(connection)

    def _close_connection(self, connection):
        connection.retiring.set()
        connection.stop.set()
        if connection.closed.is_set():
            for thread in (connection.reader, connection.writer):
                if thread and thread.ident is not None and thread is not threading.current_thread():
                    thread.join(.1)
                    if thread.is_alive():
                        with self._guard:
                            if self._last_connection is connection:
                                self._last_error = "mcp_cleanup_incomplete"
            return
        if not connection.close_lock.acquire(blocking=False):
            # The closer joins these I/O owners. Waiting for that same closer
            # here would form a join -> closed.wait cycle after a reader error.
            if threading.current_thread() in (connection.reader, connection.writer):
                return
            if not connection.closed.wait(2.5):
                with self._guard:
                    if self._last_connection is connection:
                        self._last_error = "mcp_cleanup_incomplete"
            return
        clean = True
        process = connection.process
        try:
            owned_close = getattr(process, "close_tree", None) if self._process_factory is not None else None
            if callable(owned_close):
                # A profile connection owns descendants even if the immediate
                # MCP process already exited. Never replace this with pid kill.
                clean &= owned_close()
            # Raw FileIO avoids BufferedWriter/Reader close locks. A busy writer
            # is unblocked by owned-process termination rather than close/flush.
            if connection.writer_idle.is_set():
                process.stdin.close()
                try:
                    process.wait(timeout=.05)
                except subprocess.TimeoutExpired:
                    pass
            if process.poll() is None and not callable(owned_close):
                if os.name == "nt":
                    # Only the still-live owned Popen root; never a reported PID.
                    killer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
                    try:
                        result = subprocess.run([killer, "/PID", str(process.pid), "/T", "/F"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=1, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        clean &= result.returncode == 0 or process.poll() is not None
                    except (OSError, subprocess.TimeoutExpired):
                        clean = False
                else:
                    process.terminate()
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=.5)
            deadline = time.monotonic()+.5
            for thread in (connection.reader, connection.writer):
                if thread and thread.ident is not None and thread is not threading.current_thread():
                    thread.join(max(0, deadline-time.monotonic()))
                    # Judge a late EOF at this final bound, not a shorter
                    # preliminary wait. An inherited open pipe still fails it.
                    clean &= not thread.is_alive()
            for stream, owner in ((process.stdin, connection.writer), (process.stdout, connection.reader)):
                if owner is None or owner.ident is None or not owner.is_alive():
                    stream.close()
                # A live owner closes its own raw stream when I/O finally ends.
                # Never wait inside close on an unowned inherited pipe handle.
        except (OSError, subprocess.TimeoutExpired):
            clean = False
        finally:
            if not clean:
                connection.error = "mcp_cleanup_incomplete"
                with self._guard:
                    if self._last_connection is connection:
                        self._last_error = "mcp_cleanup_incomplete"
            connection.closed.set()
            connection.close_lock.release()

    def _ready(self):
        with self._guard:
            connection, ready = self._connection, self._state == "ready"
        if not ready or connection is None or connection.stop.is_set() or connection.retiring.is_set():
            raise MCPError("mcp_not_ready")
        return connection

    def _call_tool(self, name, arguments, operation):
        connection = None
        try:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or not isinstance(arguments, dict):
                raise MCPError("mcp_invalid_request")
            connection = self._ready()
            result = self._request(connection, operation, {"name": name, "arguments": arguments})
        except MCPError as error:
            with operation.guard:
                operation.cancelled.set()
                issued = operation.issued
            if issued:
                self._cancel_notification(connection, operation)
                self._abort(connection, str(error))
                raise ToolExecutionUnknown("mcp_execution_unknown") from None
            raise ToolUnavailable("mcp_tool_unavailable") from None
        if (type(result.get("isError", False)) is not bool or not isinstance(result.get("content"), list)
                or any(not isinstance(item, dict) or not isinstance(item.get("type"), str) for item in result["content"])
                or ("structuredContent" in result and not isinstance(result["structuredContent"], dict))):
            self._abort(connection, "mcp_invalid_result")
            raise ToolExecutionUnknown("mcp_execution_unknown")
        if result.get("isError"):
            raise MCPError("mcp_tool_error")
        return result

    def call_tool(self, name: str, arguments: dict):
        return self._call_tool(name, arguments, _Operation("tools/call", self.timeout))

    async def acall_tool(self, name: str, arguments: dict):
        loop, operation = asyncio.get_running_loop(), _Operation("tools/call", self.timeout)
        future = loop.create_future()
        future.add_done_callback(lambda item: None if item.cancelled() else item.exception())

        def finish(value, error):
            if not future.done():
                if error is None:
                    future.set_result(value)
                else:
                    future.set_exception(error)

        def work():
            value = error = None
            try:
                value = self._call_tool(name, arguments, operation)
            except BaseException as caught:
                error = caught
            finally:
                with self._guard:
                    self._workers.discard(threading.current_thread())
                try:
                    loop.call_soon_threadsafe(finish, value, error)
                except RuntimeError:
                    pass

        worker = threading.Thread(target=work, daemon=True, name="astra-mcp-async-call")
        with self._guard:
            self._workers.add(worker)
        worker.start()
        try:
            # wait() preserves the worker future when this caller is cancelled.
            # Unlike shield(), Python 3.14 does not log its expected late error.
            await asyncio.wait({future})
            return future.result()
        except asyncio.CancelledError:
            with operation.guard:
                operation.cancelled.set()
            # A dedicated worker polls cancellation and closes its captured
            # connection. There is no default executor task to delay loop exit.
            try:
                await asyncio.wait({future}, timeout=3.5)
            except asyncio.CancelledError:
                pass
            if operation.connection is not None and not future.done():
                self._abort(operation.connection, "mcp_cancelled")
            raise

    def discover_tools(self, prefix: str = "mcp") -> tuple[Tool, ...]:
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
            raise ValueError("invalid_mcp_prefix")
        if self.state == "new":
            self.start()
        connection = self._ready()
        tools, names, cursors, cursor = [], set(), set(), None
        try:
            for _ in range(20):
                page = self._request(connection, _Operation("tools/list", self.timeout), {"cursor": cursor} if cursor else {})
                specs = page.get("tools")
                if not isinstance(specs, list):
                    raise MCPError("mcp_invalid_tools")
                for spec in specs:
                    name = spec.get("name", "") if isinstance(spec, dict) else ""
                    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or name in names or len(tools) >= 200:
                        raise MCPError("mcp_invalid_tools")
                    schema = spec.get("inputSchema")
                    if not isinstance(schema, dict) or schema.get("type") != "object":
                        raise MCPError("mcp_invalid_schema")
                    description = spec.get("description", "MCP tool")
                    if not isinstance(description, str):
                        raise MCPError("mcp_invalid_tools")
                    names.add(name)
                    def handler(remote_name):
                        return lambda **arguments: self.call_tool(remote_name, arguments)
                    tools.append(Tool(f"{prefix}.{name}", description[:2000], (), handler(name),
                        input_schema=schema, permissions=frozenset({"mcp.call"}), requires_approval=True, side_effect=True))
                cursor = page.get("nextCursor")
                if cursor is None:
                    return tuple(tools)
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise MCPError("mcp_invalid_cursor")
                cursors.add(cursor)
            raise MCPError("mcp_page_limit")
        except Exception as error:
            code = str(error) if isinstance(error, MCPError) else "mcp_invalid_tools"
            self._abort(connection, code)
            raise MCPError(code) from None

    def activate(self, registry):
        try:
            for tool in self.discover_tools():
                registry.register(tool)
        except Exception:
            self.close()
            raise
        return self.close

    def close(self):
        with self._guard:
            connection = self._connection or self._last_connection
            self._connection, self.process, self._state = None, None, "closed"
        if connection is not None:
            self._close_connection(connection)
        if not self._start_done.wait(2.5):
            with self._guard:
                self._last_error = "mcp_cleanup_incomplete"
        with self._guard:
            workers = list(self._workers)
        deadline = time.monotonic()+.25
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(max(0, deadline-time.monotonic()))
                if worker.is_alive():
                    with self._guard:
                        self._last_error = "mcp_cleanup_incomplete"

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
