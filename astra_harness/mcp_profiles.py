"""Saved MCP settings and explicitly owned connections, outside the agent loop.

The SQLite intent is separate from process state: reopening never starts a
saved command. Tool-set changes reserve an idle application boundary.
"""
import asyncio
import concurrent.futures
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import threading
import time
from uuid import uuid4

from .builtin.mcp_profile_connection import ProfileConnection, validate_profile_config
from .tools import ToolRegistry

CATALOG_KEY = "mcp_profiles.catalog_v1"
MAX_PROFILES, MAX_ACTIVE, MAX_TOOLS = 16, 8, 200
MAX_REVISION = 9007199254740991
CLEANUP_SECONDS = 5.0
_META = {"id", "config_revision", "enabled"}
_FIELDS = {"name", "transport", "command", "args", "cwd", "env_refs", "endpoint", "timeout", "service_revision"}


class MCPProfileError(RuntimeError):
    STATUS = {"invalid_mcp_profile": 400, "mcp_profile_confirmation_required": 400,
              "mcp_profile_not_found": 404, "mcp_profile_conflict": 409,
              "mcp_profile_in_use": 409, "mcp_profile_busy": 409, "mcp_profile_limit": 409,
              "mcp_profile_workspace_mismatch": 409,
              "mcp_profile_unavailable": 503, "mcp_profile_connection_failed": 502,
              "mcp_profile_timeout": 504, "mcp_profile_cleanup_failed": 503}

    def __init__(self, code):
        self.code = code if code in self.STATUS else "mcp_profile_unavailable"
        self.status = self.STATUS[self.code]
        super().__init__(self.code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _identity(item):
    return hashlib.sha256(_json({key: value for key, value in item.items() if key != "enabled"}).encode("utf-8")).hexdigest()


def _revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise MCPProfileError("invalid_mcp_profile")


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise MCPProfileError("invalid_mcp_profile")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _thread_call(function):
    # A daemon owner avoids binding a possibly stuck transport to asyncio's
    # default executor shutdown. Its resource is still explicitly closed below.
    future = concurrent.futures.Future()

    def execute():
        try:
            result = function()
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)

    thread = threading.Thread(target=execute, daemon=True, name="astra-mcp-profile-operation")
    thread.start()
    return future, thread


async def _wait(future, timeout):
    pending = asyncio.wrap_future(future)
    pending.add_done_callback(lambda item: None if item.cancelled() else item.exception())
    return await asyncio.wait_for(asyncio.shield(pending), timeout)


class MCPProfiles:
    def __init__(self, application, *, workspace, base_tools, base_permissions,
                 workspace_factory_builder=None, service_status=None, reserved_names=(),
                 connection_factory=ProfileConnection, environment=None):
        self.app, self.store, self.workspace = application, application.store, workspace
        self.base_tools, self.base_permissions = tuple(base_tools), tuple(base_permissions)
        self._factory_builder, self._service_status = workspace_factory_builder, service_status
        self._reserved_names = frozenset(reserved_names)
        self._connection_factory, self._environment = connection_factory, environment
        self._lock = threading.RLock()
        self._commit_lock = threading.RLock()
        self._active, self._owned, self._errors = {}, {}, {}
        self._operation = self._token = None
        self._closed = self._poisoned = False
        self._start_threads = {}

    def _read(self):
        try:
            raw = self.store.host_setting(CATALOG_KEY)
            if raw is None:
                return raw, {"format": 1, "revision": 0, "items": []}
            value = json.loads(raw, object_pairs_hook=_unique)
            if (type(value) is not dict or set(value) != {"format", "revision", "items"}
                    or type(value["format"]) is not int or value["format"] != 1
                    or type(value["items"]) is not list or len(value["items"]) > MAX_PROFILES):
                raise ValueError()
            _revision(value["revision"])
            names, ids = set(), set()
            for item in value["items"]:
                if type(item) is not dict or not _META <= item.keys() or set(item) - (_FIELDS | _META):
                    raise ValueError()
                _identifier(item["id"])
                _revision(item["config_revision"])
                config = validate_profile_config({key: val for key, val in item.items() if key not in _META},
                                                 self.workspace, inspect=False)
                if (_json(config) != _json({key: val for key, val in item.items() if key not in _META})
                        or type(item["enabled"]) is not bool or not item["config_revision"]
                        or item["name"] in names or item["id"] in ids):
                    raise ValueError()
                names.add(item["name"])
                ids.add(item["id"])
            return raw, value
        except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error, MCPProfileError):
            raise MCPProfileError("mcp_profile_unavailable") from None

    def _mutable(self):
        if self._closed or self._poisoned:
            raise MCPProfileError("mcp_profile_unavailable")
        if self._operation is not None:
            raise MCPProfileError("mcp_profile_busy")

    def _checked(self, revision):
        _revision(revision)
        raw, value = self._read()
        if value["revision"] != revision or revision == MAX_REVISION:
            raise MCPProfileError("mcp_profile_conflict")
        return raw, value

    @staticmethod
    def _item(value, identifier, identity=None):
        _identifier(identifier)
        item = next((item for item in value["items"] if item["id"] == identifier), None)
        if item is None:
            raise MCPProfileError("mcp_profile_not_found")
        if identity is not None and (type(identity) is not str or identity != _identity(item)):
            raise MCPProfileError("mcp_profile_conflict")
        return item

    def _write(self, raw, value):
        value["revision"] += 1
        try:
            encoded = _json(value)
            if len(encoded.encode("utf-8")) > 65536:
                raise MCPProfileError("mcp_profile_limit")
            if not self.store.compare_and_swap_host_setting(CATALOG_KEY, raw, encoded):
                raise MCPProfileError("mcp_profile_conflict")
        except (ValueError, UnicodeError, sqlite3.Error):
            raise MCPProfileError("mcp_profile_unavailable") from None

    def list(self):
        with self._lock:
            if self._closed:
                raise MCPProfileError("mcp_profile_unavailable")
            value = self._read()[1]
            active, errors, operation = dict(self._active), dict(self._errors), deepcopy(self._operation)
            closed, poisoned = self._closed, self._poisoned
        items = []
        for item in value["items"]:
            state = "offline" if item["enabled"] else "disabled"
            error, tools = errors.get(item["id"]), []
            connection = active.get(item["id"])
            if connection is not None:
                status = connection.status()
                state = status["state"] if status.get("state") in {"ready", "failed", "closed", "starting"} else "failed"
                tools = [tool.name for tool in connection.tools]
                if state == "failed":
                    error = "mcp_profile_connection_failed"
            elif error:
                state = "failed"
            if operation and operation["id"] == item["id"]:
                state = "closing" if operation["action"] == "disable" else "connecting"
            if closed:
                state = "closed"
            items.append({**deepcopy(item), "identity": _identity(item), "state": state, "error_code": error,
                          "tools": tools, "activation_required": item["enabled"] and connection is None})
        return {"revision": value["revision"], "items": items, "operation": operation,
                "limits": {"saved": MAX_PROFILES, "active": MAX_ACTIVE, "tools": MAX_TOOLS},
                "restart_policy": "explicit_activation", "cleanup_required": poisoned}

    def status(self):
        return [{key: item[key] for key in ("name", "transport", "state", "error_code", "tools")}
                for item in self.list()["items"]]

    def save(self, profile, expected_revision):
        if type(profile) is not dict or set(profile) - (_FIELDS | {"id"}):
            raise MCPProfileError("invalid_mcp_profile")
        with self._lock:
            self._mutable()
            raw, value = self._checked(expected_revision)
            old = self._item(value, profile["id"]) if "id" in profile else None
            if old and (old["enabled"] or old["id"] in self._active):
                raise MCPProfileError("mcp_profile_in_use")
            try:
                config = validate_profile_config({key: val for key, val in profile.items() if key != "id"}, self.workspace)
            except Exception:
                raise MCPProfileError("invalid_mcp_profile") from None
            if config["name"] in self._reserved_names or any(item["name"] == config["name"] and item is not old for item in value["items"]):
                raise MCPProfileError("mcp_profile_conflict")
            if old:
                revision = old["config_revision"]
                if {key: val for key, val in old.items() if key not in _META} != config:
                    if revision == MAX_REVISION:
                        raise MCPProfileError("mcp_profile_conflict")
                    revision += 1
                item = {**config, "id": old["id"], "config_revision": revision, "enabled": False}
                value["items"] = [item if current is old else current for current in value["items"]]
            else:
                if len(value["items"]) >= MAX_PROFILES:
                    raise MCPProfileError("mcp_profile_limit")
                item = {**config, "id": uuid4().hex, "config_revision": 1, "enabled": False}
                value["items"].append(item)
            self._write(raw, value)
            self._errors.pop(item["id"], None)
        return self.list()

    def delete(self, identifier, expected_revision, expected_identity):
        if type(expected_identity) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_identity) is None:
            raise MCPProfileError("invalid_mcp_profile")
        with self._lock:
            self._mutable()
            raw, value = self._checked(expected_revision)
            item = self._item(value, identifier, expected_identity)
            if item["enabled"] or identifier in self._active:
                raise MCPProfileError("mcp_profile_in_use")
            value["items"].remove(item)
            self._write(raw, value)
            self._errors.pop(identifier, None)
        return self.list()

    def _compose(self, active):
        tools = self.base_tools + tuple(tool for identifier in sorted(active) for tool in active[identifier].tools)
        if sum(tool.name.startswith("mcp.") for tool in tools) > MAX_TOOLS:
            raise MCPProfileError("mcp_profile_limit")
        # Reuse the same registration and validation entry as every host tool.
        registry = ToolRegistry(tools)
        registry.freeze()
        permissions = tuple(sorted(set(self.base_permissions) | {
            permission for connection in active.values() for tool in connection.tools for permission in tool.permissions}))
        factory = self._factory_builder(tools, permissions) if self._factory_builder is not None else None
        return {"tools": tools, "permissions": permissions, "workspace_factory": factory,
                "service_status": self._service_status or self.status}

    def _publish(self, token, active):
        self.app.publish_tool_update(token, **self._compose(active))

    def _commit(self, token, old_active, active, raw, value):
        # No await or transport work belongs in this short commit section.
        # close cannot release its gate/close a candidate halfway through CAS.
        with self._commit_lock:
            with self._lock:
                if self._closed:
                    raise MCPProfileError("mcp_profile_unavailable")
            self._publish(token, active)
            try:
                self._write(raw, value)
            except BaseException:
                try:
                    self._publish(token, old_active)
                except BaseException:
                    with self._lock:
                        self._poisoned = True
                    raise MCPProfileError("mcp_profile_cleanup_failed") from None
                raise
            with self._lock:
                self._active = active

    def _release(self, token):
        with self._lock:
            if self._token != token or token is None:
                return
            self._token = None
        self.app.release_tool_update(token)

    async def _cleanup(self, connection):
        future, thread = _thread_call(connection.close)
        try:
            await _wait(future, CLEANUP_SECONDS)
            start = self._start_threads.get(id(connection))
            deadline = time.monotonic() + 1
            while start is not None and start.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            if start is not None and start.is_alive():
                raise RuntimeError()
            status = connection.status()
            if status.get("error_code") in {"mcp_cleanup_failed", "mcp_cleanup_incomplete"}:
                raise RuntimeError()
        except BaseException:
            with self._lock:
                self._poisoned = True
            raise MCPProfileError("mcp_profile_cleanup_failed") from None
        else:
            with self._lock:
                self._owned.pop(id(connection), None)
                self._start_threads.pop(id(connection), None)

    async def test(self, identifier, expected_revision, expected_identity, *, confirmed=False):
        return await self._operate("test", identifier, expected_revision, expected_identity, confirmed)

    async def enable(self, identifier, expected_revision, expected_identity, *, confirmed=False):
        return await self._operate("enable", identifier, expected_revision, expected_identity, confirmed)

    async def disable(self, identifier, expected_revision, expected_identity):
        return await self._operate("disable", identifier, expected_revision, expected_identity, True)

    async def _operate(self, action, identifier, expected_revision, expected_identity, confirmed):
        if confirmed is not True:
            raise MCPProfileError("mcp_profile_confirmation_required")
        if type(expected_identity) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_identity) is None:
            raise MCPProfileError("invalid_mcp_profile")
        with self._lock:
            self._mutable()
            raw, value = self._checked(expected_revision)
            item = self._item(value, identifier, expected_identity)
            if action != "disable" and identifier in self._active:
                raise MCPProfileError("mcp_profile_in_use")
            if item["name"] in self._reserved_names:
                raise MCPProfileError("mcp_profile_conflict")
            if action == "enable" and len(self._active) + len(self._reserved_names) >= MAX_ACTIVE:
                raise MCPProfileError("mcp_profile_limit")
            if action == "disable" and identifier not in self._active:
                item["enabled"] = False
                self._write(raw, value)
                self._errors.pop(identifier, None)
                return self.list()
            self._operation = {"id": identifier, "action": action}
            old_active = dict(self._active)
            self._errors.pop(identifier, None)
        token = None
        candidate = None
        try:
            # close must observe either no reservation or its recorded token;
            # it cannot finish between acquiring the App gate and owning it.
            with self._commit_lock:
                with self._lock:
                    if self._closed:
                        raise MCPProfileError("mcp_profile_unavailable")
                token = self.app.reserve_tool_update()
                with self._lock:
                    self._token = token
                if self.app.selected_workspace_id != self.app.startup_workspace_id:
                    raise MCPProfileError("mcp_profile_workspace_mismatch")
            if action == "disable":
                remaining = {key: val for key, val in old_active.items() if key != identifier}
                item["enabled"] = False
                self._commit(token, old_active, remaining, raw, value)
                await self._cleanup(old_active[identifier])
            else:
                config = {key: val for key, val in item.items() if key not in _META}
                options = {} if self._environment is None else {"environment": self._environment}
                candidate = self._connection_factory(config, self.workspace, **options)
                with self._lock:
                    self._owned[id(candidate)] = candidate
                    if self._closed:
                        raise MCPProfileError("mcp_profile_unavailable")
                future, thread = _thread_call(candidate.start)
                with self._lock:
                    self._start_threads[id(candidate)] = thread
                try:
                    await _wait(future, item["timeout"])
                except asyncio.TimeoutError:
                    raise MCPProfileError("mcp_profile_timeout") from None
                with self._lock:
                    if self._closed:
                        raise MCPProfileError("mcp_profile_unavailable")
                combined = {**old_active, identifier: candidate}
                self._compose(combined)
                if action == "test":
                    result = {"ok": True, "id": identifier, "identity": expected_identity,
                              "revision": expected_revision, "tools": [tool.name for tool in candidate.tools],
                              "tool_count": len(candidate.tools), "registered": False}
                    await self._cleanup(candidate)
                    candidate = None
                    return result
                item["enabled"] = True
                self._commit(token, old_active, combined, raw, value)
                candidate = None  # Ownership remains in _owned until disable/host close.
        except asyncio.CancelledError:
            if candidate is not None and not self._poisoned:
                await self._cleanup(candidate)
                candidate = None
            raise
        except Exception as error:
            if candidate is not None and not self._poisoned:
                try:
                    await self._cleanup(candidate)
                finally:
                    candidate = None
            if isinstance(error, MCPProfileError) or getattr(error, "code", "").startswith("runtime_update_"):
                code = error.code
                with self._lock:
                    self._errors[identifier] = code if code in MCPProfileError.STATUS else "mcp_profile_busy"
                raise
            with self._lock:
                self._errors[identifier] = "mcp_profile_connection_failed"
            raise MCPProfileError("mcp_profile_connection_failed") from None
        finally:
            # Uncertain cleanup keeps the host gate closed until explicit host
            # shutdown; a timed-out discovery cannot outlive a released gate.
            if not self._poisoned:
                self._release(token)
            with self._lock:
                self._operation = None
                if self._poisoned:
                    self._errors[identifier] = "mcp_profile_cleanup_failed"
        return self.list()

    def close(self):
        with self._commit_lock:
            with self._lock:
                self._closed = True
                owned = tuple(self._owned.values())
                starts = tuple(self._start_threads.values())
                token = self._token
        closes = [_thread_call(connection.close) for connection in owned]
        deadline = time.monotonic() + CLEANUP_SECONDS
        failed = False
        for future, thread in closes:
            try:
                future.result(timeout=max(0, deadline - time.monotonic()))
            except BaseException:
                failed = True
        for connection in owned:
            if connection.status().get("error_code") in {"mcp_cleanup_failed", "mcp_cleanup_incomplete"}:
                failed = True
        for thread in starts:
            thread.join(timeout=max(0, deadline - time.monotonic()))
            failed = failed or thread.is_alive()
        self._release(token)
        if failed:
            with self._lock:
                self._poisoned = True
            raise MCPProfileError("mcp_profile_cleanup_failed")
        with self._lock:
            self._owned.clear()
            self._active.clear()
