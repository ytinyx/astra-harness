"""Explicit user MCP connections; metadata-only construction and owned cleanup.

Command selection is permission to execute a trusted program under the host
account, not an OS sandbox. Profile STDIO gets only essential OS environment and
explicit role-scoped references. No configured program is run while saving.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from uuid import uuid4

from .commands import _environment, _executable, _launcher_signature
from ._command_process import WindowsJob
from .files import FilePlugin, inspect_workspace_directory, _relative_file_parts
from .mcp import MCPError, StdioMCPClient
from .mcp_http import validate_http_endpoint
from .mcp_services import MCPServiceConfig, MCPServiceManager, _safe_error
from ..tools import ToolRegistry

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_REF = re.compile(r"ASTRA_MCP_[A-Z][A-Z0-9_]{0,79}\Z")
_RESERVED = frozenset({"PATH", "COMSPEC", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP",
                      "NODE_OPTIONS", "RUBYOPT", "PERL5OPT", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "BASH_ENV", "ENV"})
MAX_PAYLOAD = 16384

def _files(workspace):
    return workspace if isinstance(workspace, FilePlugin) else FilePlugin(workspace)

def _metadata(profile, workspace):
    files = _files(workspace)
    files.validate_workspace()
    root = files.workspace_identity
    if profile["transport"] == "http":
        return {"workspace": root}, None
    executable, identity = _executable(profile["command"])
    # Reject aliases rather than silently changing the reviewed spelling.
    if os.path.normcase(os.path.normpath(profile["command"])) != os.path.normcase(executable):
        raise ValueError("invalid_mcp_profile")
    directory = files._directory("" if profile["cwd"] == "." else profile["cwd"])
    inspected = inspect_workspace_directory(directory)
    payload = json.dumps({"argv": [executable, *profile["args"]], "cwd": str(directory)},
                         ensure_ascii=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("invalid_mcp_profile")
    return {"workspace": root, "executable": identity, "directory": inspected}, str(directory)

def validate_profile_config(value, workspace, *, inspect=True):
    """Normalize only connection fields; never read credential references or spawn."""
    try:
        if type(value) is not dict or type(inspect) is not bool:
            raise ValueError()
        name, transport = value.get("name"), value.get("transport", "stdio")
        timeout, revision = value.get("timeout", 10), value.get("service_revision", "1")
        if (not isinstance(name, str) or not _NAME.fullmatch(name) or transport not in {"stdio", "http"}
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 30
                or not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", revision)):
            raise ValueError()
        result = {"name": name, "transport": transport, "timeout": float(timeout), "service_revision": revision}
        if transport == "http":
            if set(value) - {"name", "transport", "timeout", "service_revision", "endpoint"}:
                raise ValueError()
            result["endpoint"] = validate_http_endpoint(value.get("endpoint"))
        else:
            if set(value) - {"name", "transport", "timeout", "service_revision", "command", "args", "cwd", "env_refs"}:
                raise ValueError()
            command, args, cwd, refs = value.get("command"), value.get("args", []), value.get("cwd", "."), value.get("env_refs", {})
            if (not isinstance(command, str) or not command or len(command) > 4096 or "\0" in command
                    or not Path(command).is_absolute() or command.replace("\\", "/").startswith("//")
                    or ".." in command.replace("\\", "/").split("/")
                    or (os.name == "nt" and Path(command).suffix.lower() != ".exe")
                    or type(args) is not list or len(args) > 63
                    or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in args)
                    or type(refs) is not dict or len(refs) > 16 or not isinstance(cwd, str)):
                raise ValueError()
            if cwd != ".":
                cwd = "/".join(_relative_file_parts(cwd))
            seen = set()
            for child, reference in refs.items():
                if (not isinstance(child, str) or not _ENV.fullmatch(child) or child.upper() in seen
                        or child.upper() in _RESERVED or child.upper().startswith(("PYTHON", "LD_", "DYLD_"))
                        or not isinstance(reference, str) or not _REF.fullmatch(reference)):
                    raise ValueError()
                seen.add(child.upper())
            result.update(command=command, args=list(args), cwd=cwd, env_refs=dict(sorted(refs.items())))
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_PAYLOAD - 4096:
            raise ValueError()
        if inspect:
            _metadata(result, workspace)
        return result
    except (ValueError, TypeError, OSError, OverflowError, RuntimeError, UnicodeError, RecursionError):
        raise ValueError("invalid_mcp_profile") from None

class _OwnedProcess:
    """Popen-compatible pipes plus handle/group ownership, never reported PID kill."""
    def __init__(self, process, job):
        self.process, self.job = process, job
        self._lock, self._closed = threading.Lock(), threading.Event()
        self._clean = False

    def __getattr__(self, name):
        return getattr(self.process, name)

    def close_tree(self):
        if not self._lock.acquire(blocking=False):
            return self._closed.wait(2) and self._clean
        try:
            if self._closed.is_set():
                return self._clean
            clean = True
            if self.job is not None:
                clean = self.job.close(1)
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    clean = False
            try:
                self.process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                clean = False
            self._clean = clean
            return clean
        finally:
            self._closed.set()
            self._lock.release()

def _spawn_owned(argv, *, env, cwd, **options):
    """No task process executes before Job assignment; POSIX owns a process group."""
    process = owner = job = None
    try:
        if os.name == "posix":
            options.pop("creationflags", None)
            process = subprocess.Popen(argv, executable=argv[0], env=env, cwd=cwd,
                                       start_new_session=True, close_fds=True, **options)
            return _OwnedProcess(process, None)
        if os.name != "nt":
            raise OSError("unsupported_platform")
        helper = Path(__file__).with_name("_mcp_profile_process.py")
        _launcher_signature(helper)
        base, _ = _executable(getattr(sys, "_base_executable", None) or sys.executable)
        payload = json.dumps({"argv": argv, "cwd": cwd}, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(payload) > MAX_PAYLOAD:
            raise OSError("mcp_config_limit")
        job = WindowsJob()
        process = subprocess.Popen([base, "-I", "-S", str(helper)], executable=base,
            env=env, cwd=str(helper.parent), close_fds=True, **options)
        job.assign(process)
        owner = _OwnedProcess(process, job)
        done, errors = threading.Event(), []
        def release():
            try:
                view = memoryview(payload)
                while view:
                    count = process.stdin.write(view)
                    if not count:
                        raise OSError()
                    view = view[count:]
            except (OSError, ValueError):
                errors.append(True)
            finally:
                done.set()
        writer = threading.Thread(target=release, daemon=True, name="astra-mcp-profile-gate")
        writer.start()
        if not done.wait(1) or errors:
            owner.close_tree()
            writer.join(.5)
            raise OSError("mcp_gate_failed")
        return owner
    except BaseException:
        if owner is not None:
            owner.close_tree()
        elif job is not None:
            job.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        raise

class ProfileConnection:
    def __init__(self, profile, workspace, environment=None):
        self._files = _files(workspace)
        self._profile = validate_profile_config(profile, self._files)
        self._binding, self._cwd = _metadata(self._profile, self._files)
        self._environment = environment if environment is not None else lambda: os.environ
        if not callable(self._environment):
            raise ValueError("invalid_mcp_environment")
        self.identity = hashlib.sha256(json.dumps({"profile": self._profile, "binding": self._binding,
            "activation": uuid4().hex, "adapter": "mcp-profile-v1"}, sort_keys=True).encode()).hexdigest()
        self.tools = ()
        self._manager = None
        self._state, self._error = "new", None
        self._guard = threading.RLock()
        self._closed = self._closing = False
        self._start_done, self._close_done = threading.Event(), threading.Event()
        self._start_done.set()

    @property
    def profile(self):
        return deepcopy(self._profile)

    def _env(self):
        result = _environment()
        references = self._profile["env_refs"]
        if not references:
            return result
        available = self._environment()
        try:
            for child, reference in references.items():
                value = available.get(reference)
                if not isinstance(value, str) or not value or "\0" in value or len(value.encode("utf-8")) > 8192:
                    raise ValueError()
                result[child] = value
            if sum(len(k.encode()) + len(v.encode("utf-8")) + 2 for k, v in result.items()) > 32768:
                raise ValueError()
        except (TypeError, ValueError, AttributeError, UnicodeError):
            raise MCPError("mcp_environment_unavailable") from None
        return result

    def start(self):
        with self._guard:
            if self._closed or self._state != "new":
                raise MCPError("mcp_closed")
            self._state = "starting"
            self._start_done.clear()
        manager = None
        try:
            observed, directory = _metadata(self._profile, self._files)
            if observed != self._binding or directory != self._cwd:
                raise MCPError("mcp_configuration_changed")
            profile = self._profile
            if profile["transport"] == "stdio":
                environment = self._env()
                if _metadata(profile, self._files) != (self._binding, self._cwd):
                    raise MCPError("mcp_configuration_changed")
                config = MCPServiceConfig(profile["name"], (profile["command"], *profile["args"]),
                    timeout=profile["timeout"], revision=self.identity)
                def factory(command, *, timeout, max_message_bytes, cwd):
                    # The manager's legacy cwd capture is not authority here;
                    # activation identity already binds this reviewed directory.
                    if _metadata(profile, self._files) != (self._binding, self._cwd):
                        raise MCPError("mcp_initialization_failed")
                    return StdioMCPClient(command, timeout, max_message_bytes, cwd=self._cwd,
                                          env=environment, process_factory=_spawn_owned)
                manager = MCPServiceManager((config,), client_factory=factory)
            else:
                config = MCPServiceConfig(profile["name"], timeout=profile["timeout"], revision=self.identity,
                    transport="http", endpoint=profile["endpoint"])
                manager = MCPServiceManager((config,))
            with self._guard:
                if self._closed:
                    raise MCPError("mcp_closed")
                self._manager = manager
            registry = ToolRegistry()
            manager.activate(registry)
            state = manager.status()[0]
            if state["state"] != "ready":
                raise MCPError(state["error_code"] or "mcp_service_failed")
            registry.freeze()
            with self._guard:
                if self._closed:
                    raise MCPError("mcp_closed")
                self.tools = tuple(registry.tools())
                self._state = "ready"
            return self
        except BaseException as error:
            code = str(error) if isinstance(error, MCPError) and str(error) in {
                "mcp_environment_unavailable", "mcp_configuration_changed", "mcp_closed"} else _safe_error(error)
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    code = "mcp_cleanup_failed"
            with self._guard:
                self._state, self._error = ("closed" if self._closed else "failed"), code
                self.tools = ()
            raise MCPError(code) from None
        finally:
            self._start_done.set()

    def status(self):
        with self._guard:
            result = {"name": self._profile["name"], "transport": self._profile["transport"],
                      "state": self._state, "error_code": self._error, "tools": [tool.name for tool in self.tools]}
            manager = self._manager
        if manager is not None:
            observed = manager.status()[0]
            if observed["error_code"]:
                result["error_code"] = observed["error_code"]
            if result["state"] == "ready":
                result["state"] = observed["state"]
        return result

    def close(self):
        with self._guard:
            owner = not self._closing
            self._closing = self._closed = True
            self._state = "closed"
            manager = self._manager
        if not owner:
            if not self._close_done.wait(5):
                raise MCPError("mcp_cleanup_failed")
            return
        failed = False
        try:
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    failed = True
            if not self._start_done.wait(4):
                failed = True
            if failed:
                with self._guard:
                    self._error = "mcp_cleanup_failed"
                raise MCPError("mcp_cleanup_failed")
        finally:
            self._close_done.set()
