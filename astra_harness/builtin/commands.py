"""Opt-in, approved commands with bounded capture and owned-process cleanup.

An executable allowlist is a trust decision, not an OS sandbox. An approved
interpreter can access anything allowed to the service account. The cwd guard
and clean environment reduce accidental exposure; they cannot restrict its code.
"""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import signal
import stat
import subprocess
import sys
import threading
import time

from .files import FilePlugin
from ._command_process import LAUNCH_FAILURE, MAX_PAYLOAD, WindowsJob, _launch
from ..tools import Tool, ToolExecutionUnknown, ToolUnavailable

MAX_ARGS = 64
MAX_ARG_BYTES = 8192
IMPLEMENTATION_VERSION = "1"
_REJECTIONS = frozenset({"invalid_argv", "argv_limit", "invalid_cwd", "invalid_executable",
    "executable_not_allowed", "executable_changed", "workspace_changed", "invalid_path",
    "absolute_path_not_allowed", "links_not_allowed", "path_missing", "path_outside_workspace",
    "parent_missing", "not_directory", "forbidden_path", "plugin_closed", "command_busy",
    "unsupported_platform", "job_unavailable", "job_assignment_failed", "launcher_changed"})


def _executable(path):
    if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path:
        raise ValueError("invalid_executable")
    normalized = path.replace("\\", "/")
    windows = PureWindowsPath(path)
    if (not Path(path).is_absolute() or normalized.startswith("//") or windows.drive.startswith("\\\\")
            or ".." in normalized.split("/") or (os.name == "nt" and Path(path).suffix.lower() != ".exe")):
        raise ValueError("invalid_executable")
    try:
        # Host configuration may resolve an explicit trusted symlink once.
        # Calls must use that canonical path, checked separately by _validate.
        target = Path(path).resolve(strict=True)
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(target, os.X_OK) or not info.st_ino:
            raise ValueError("invalid_executable")
        identity = [os.path.normcase(str(target)), str(info.st_dev), str(info.st_ino),
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        return str(target), identity
    except (OSError, RuntimeError, UnicodeError):
        raise ValueError("invalid_executable") from None


def validate_executables(paths):
    """Metadata-only host configuration validation; no processes or directories."""
    if not isinstance(paths, (list, tuple)) or len(paths) > 16:
        raise ValueError("invalid_executable_allowlist")
    result = []
    for path in paths:
        target, _ = _executable(path)
        if os.path.normcase(target) not in {os.path.normcase(x) for x in result}:
            result.append(target)
    return tuple(result)


def _environment():
    # Deliberately no PATH, user profile, temp directory or provider credentials.
    # Windows needs SystemRoot for side-by-side assemblies and the base Python.
    if os.name != "nt":
        return {}
    result = {}
    for key in ("SystemRoot", "WINDIR"):
        value = os.environ.get(key)
        if value:
            result[key] = value
    return result


def _launcher_signature(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_size > 32768):
        raise ValueError("launcher_changed")
    with path.open("rb") as stream:
        content = stream.read(32769)
    if len(content) > 32768:
        raise ValueError("launcher_changed")
    after = path.stat()
    if (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("launcher_changed")
    return hashlib.sha256(content).hexdigest()


class _Execution:
    def __init__(self, process, job, cap):
        self.process, self.job, self.cap = process, job, cap
        self.output = [bytearray(), bytearray()]
        self.total = 0
        self.overflow = threading.Event()
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self.cleanup_complete = False
        self.release_done = threading.Event()
        self.release_done.set()
        self.readers = [threading.Thread(target=self._read, args=(i, pipe), daemon=True,
                                         name="astra-command-output")
                        for i, pipe in enumerate((process.stdout, process.stderr))]
        for reader in self.readers:
            reader.start()

    def _read(self, index, pipe):
        try:
            while True:
                data = pipe.read(4096)
                if not data:
                    break
                with self.lock:
                    remaining = self.cap - sum(map(len, self.output))
                    self.output[index].extend(data[:remaining])
                    # Counter is capped as well; it is a lower bound after flood.
                    self.total = min(self.cap + 1, self.total + len(data))
                    if self.total > self.cap:
                        self.overflow.set()
                        break
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()

    def stop(self):
        with self.cleanup_lock:
            if self.closed.is_set():
                return self.cleanup_complete
            confirmed = True
            try:
                if self.job is not None:
                    confirmed = self.job.close()
                elif os.name == "posix":
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        confirmed = False
                elif self.process.poll() is None:
                    self.process.kill()
                try:
                    self.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    confirmed = False
                for reader in self.readers:
                    reader.join(timeout=0.3)
                    confirmed = confirmed and not reader.is_alive()
                if self.process.stdin is not None:
                    self.process.stdin.close()
                confirmed = self.release_done.wait(timeout=0.3) and confirmed
            except (OSError, ValueError):
                confirmed = False
            finally:
                self.cleanup_complete = confirmed
                self.closed.set()
            return confirmed

    def result(self, preview_limit=None):
        with self.lock:
            output = list(map(bytes, self.output))
            preview_truncated = False
            if preview_limit is not None:
                preview_truncated = sum(map(len, output)) > preview_limit
                output = [output[0][:preview_limit], output[1][:max(0, preview_limit - len(output[0]))]]
            return {"stdout": output[0].decode("utf-8", "replace"),
                    "stderr": output[1].decode("utf-8", "replace"),
                    "output_bytes": self.total, "truncated": self.overflow.is_set(),
                    "output_bytes_lower_bound": self.overflow.is_set(),
                    "encoding": "utf-8", "decoding_errors": "replace",
                    "output_preview_truncated": preview_truncated,
                    "cleanup_complete": self.cleanup_complete}


class CommandPlugin:
    def __init__(self, root, allowed_executables, *, timeout=5.0, max_output_bytes=16384, revision="1"):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.05 <= timeout <= 60
                or type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 32768
                or not isinstance(revision, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", revision) is None):
            raise ValueError("invalid_command_limits")
        self.files = root if isinstance(root, FilePlugin) else FilePlugin(root)
        paths = validate_executables(allowed_executables)
        if not paths:
            raise ValueError("empty_executable_allowlist")
        self._allowed = {os.path.normcase(path): _executable(path)[1] for path in paths}
        self.timeout, self.max_output_bytes = float(timeout), max_output_bytes
        self._launcher = Path(__file__).with_name("_command_process.py")
        self._launcher_signature = _launcher_signature(self._launcher) if os.name == "nt" else None
        self._base = _executable(getattr(sys, "_base_executable", sys.executable)) if os.name == "nt" else None
        self._lock, self._active, self._closed = threading.Lock(), set(), False
        binding = {"workspace": self.files.workspace_identity, "executables": self._allowed,
                   "timeout": self.timeout, "max_output_bytes": max_output_bytes, "revision": revision,
                   "launcher": self._launcher_signature, "base_interpreter": self._base}
        self.recovery_identity = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        self.tools = (Tool(
            "commands.run", "Run an approved host-allowlisted executable; account permissions still apply.",
            (), self.run, input_schema={"type": "object", "properties": {
                "argv": {"type": "array", "minItems": 1, "maxItems": MAX_ARGS,
                         "items": {"type": "string", "maxLength": MAX_ARG_BYTES}},
                "cwd": {"type": "string", "minLength": 1, "maxLength": 240, "default": "."}},
                "required": ["argv"], "additionalProperties": False},
            permissions=frozenset({"commands.execute"}), side_effect=True, requires_approval=True,
            implementation_version=IMPLEMENTATION_VERSION,
            implementation_dependencies=(_executable, _environment, _launcher_signature, self._validate,
                _Execution.__init__, _Execution.stop, _Execution._read, _Execution.result,
                WindowsJob.__init__, WindowsJob.assign, WindowsJob.active, WindowsJob.close, _launch,
                FilePlugin._directory, FilePlugin._path, FilePlugin.validate_workspace),
            implementation_constants={"max_args": MAX_ARGS, "max_arg_bytes": MAX_ARG_BYTES,
                                      "launcher_reserved_exit": LAUNCH_FAILURE, "max_payload": MAX_PAYLOAD},
            recovery_identity=self.recovery_identity),)

    def activate(self, registry):
        for tool in self.tools:
            registry.register(tool)
        return self.close

    def close(self):
        with self._lock:
            self._closed = True
            active = tuple(self._active)
        for execution in active:
            execution.stop()

    def _validate(self, argv, cwd):
        if (not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGS
                or any(not isinstance(x, str) or "\0" in x for x in argv)):
            raise ValueError("invalid_argv")
        try:
            if sum(len(x.encode("utf-8")) + 1 for x in argv) > MAX_ARG_BYTES:
                raise ValueError("argv_limit")
        except UnicodeError:
            raise ValueError("invalid_argv") from None
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("invalid_cwd")
        target, identity = _executable(argv[0])
        if os.path.normcase(os.path.normpath(argv[0])) != os.path.normcase(target):
            raise ValueError("executable_not_allowed")
        if os.path.normcase(target) not in self._allowed:
            raise ValueError("executable_not_allowed")
        if identity != self._allowed[os.path.normcase(target)]:
            raise ValueError("executable_changed")
        directory = self.files._directory("" if cwd == "." else cwd)
        return [target, *argv[1:]], str(directory)

    async def run(self, argv, cwd="."):
        execution, process, job, dispatched = None, None, None, False
        reason = None
        try:
            argv, directory = self._validate(argv, cwd)
            await asyncio.sleep(0)  # Cancellation before dispatch remains effect-free.
            with self._lock:
                if self._closed:
                    raise ValueError("plugin_closed")
                if self._active:
                    raise ValueError("command_busy")
                if os.name == "nt":
                    base, base_identity = _executable(self._base[0])
                    helper = self._launcher
                    if (base_identity != self._base[1]
                            or _launcher_signature(helper) != self._launcher_signature):
                        raise ValueError("launcher_changed")
                    payload = json.dumps({"argv": argv, "cwd": directory}, ensure_ascii=False).encode("utf-8") + b"\n"
                    if len(payload) > MAX_PAYLOAD:
                        raise ValueError("argv_limit")
                    job = WindowsJob()
                    process = subprocess.Popen([base, "-I", "-S", str(helper)], executable=base,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        bufsize=0, env=_environment(), cwd=str(helper.parent), shell=False,
                        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
                    job.assign(process)
                elif os.name == "posix":
                    # Trusted commands may create a new session and escape this
                    # group. This is explicitly not hostile-process isolation.
                    process = subprocess.Popen(argv, executable=argv[0], cwd=directory, shell=False,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        bufsize=0, env=_environment(), close_fds=True, start_new_session=True)
                    dispatched = True
                else:
                    raise ValueError("unsupported_platform")
                execution = _Execution(process, job, self.max_output_bytes)
                self._active.add(execution)
            if os.name == "nt":
                # A bounded writer keeps pipe I/O out of the cancellation loop.
                # Mark unknown before releasing any bytes; a partial send cannot
                # be safely distinguished from successful command dispatch.
                dispatched = True
                execution.release_done.clear()
                def release():
                    try:
                        remaining = memoryview(payload)
                        while remaining:
                            written = process.stdin.write(remaining)
                            if not written:
                                break
                            remaining = remaining[written:]
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass
                    finally:
                        execution.release_done.set()
                writer = threading.Thread(target=release, daemon=True, name="astra-command-release")
                writer.start()
            deadline = time.monotonic() + self.timeout
            exit_seen = None
            while True:
                if execution.closed.is_set():
                    reason = "cancelled"
                    break
                if execution.overflow.is_set():
                    reason = "output_limit"
                    break
                if process.poll() is not None:
                    active = False
                    if job is not None:
                        active = bool(job.active())
                    elif os.name == "posix":
                        try:
                            os.killpg(process.pid, 0)
                            active = True
                        except ProcessLookupError:
                            pass
                    # A process can become signalled before Job accounting has
                    # removed it. Allow that bounded exit transition; an actual
                    # lingering descendant is then terminated and stays unknown.
                    if active:
                        exit_seen = time.monotonic() if exit_seen is None else exit_seen
                        if time.monotonic() - exit_seen < 0.2:
                            await asyncio.sleep(0.01)
                            continue
                        reason = "background_processes"
                    if os.name == "nt" and process.returncode == LAUNCH_FAILURE:
                        reason = "launch_status_unknown"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            if not dispatched:
                raise
            reason = "cancelled"
        except (ValueError, OSError, RuntimeError) as error:
            if dispatched:
                reason = "launch_status_unknown"
            else:
                raise ToolUnavailable("Command rejected", code="command_rejected",
                                      details={"reason": str(error) if str(error) in _REJECTIONS else "preflight_failed"}) from None
        finally:
            if execution is not None:
                complete = execution.stop()
                if not complete:
                    reason = "cleanup_failed"
                with self._lock:
                    self._active.discard(execution)
                    if not complete:
                        self._closed = True
            else:
                if job is not None:
                    job.close()
                if process is not None:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()
        if execution is not None and execution.overflow.is_set() and reason is None:
            reason = "output_limit"
        result = execution.result(preview_limit=16384 if reason else None) if execution is not None else {"cleanup_complete": False}
        if reason is not None:
            raise ToolExecutionUnknown("Command effects are uncertain", details={"reason": reason, **result})
        return {"ok": process.returncode == 0, "exit_code": process.returncode, **result}
