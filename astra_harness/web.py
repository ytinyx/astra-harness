"""Loopback-only FastAPI workbench; the caller owns the injected Application."""

import asyncio
import json
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse
import uvicorn

from .contracts import ToolCall
from .workspace_binding import WorkspaceError
from .model_binding import ModelProfileError
from .mcp_profiles import MCPProfileError
from .application import RuntimeUpdateError
from .skill_catalog import SkillCatalogError
from .task_context import TaskContextError
from .builtin.local_skills import SkillLoadError
from .knowledge_library import KnowledgeLibraryError, upload_material


MAX_BODY = 64 * 1024
ID = r"[A-Za-z0-9_-]{1,128}"
TERMINAL = {"completed", "failed", "cancelled", "unknown"}
STATIC_FILES = {"styles.css": "text/css", "app.mjs": "text/javascript",
                "markdown.mjs": "text/javascript", "state.mjs": "text/javascript",
                "knowledge.mjs": "text/javascript", "knowledge-state.mjs": "text/javascript",
                "workspace.mjs": "text/javascript", "workspace-state.mjs": "text/javascript"}
STATIC_FILES.update({"model-profiles.mjs": "text/javascript", "model-profiles-state.mjs": "text/javascript"})
STATIC_FILES.update({"mcp-profiles.mjs": "text/javascript", "mcp-profiles-state.mjs": "text/javascript"})
STATIC_FILES.update({"local-skills.mjs": "text/javascript", "local-skills-state.mjs": "text/javascript"})
STATIC_FILES.update({"server-control.mjs": "text/javascript"})
_POLICY = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": _POLICY}


class StrictBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ShutdownBody(StrictBody):
    instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmed: bool

    @field_validator("confirmed")
    @classmethod
    def require_confirmation(cls, value):
        if value is not True:
            raise ValueError("explicit shutdown confirmation required")
        return value


class ServerControl:
    """HTTP admission and shutdown intent; the CLI still owns resource cleanup.

    State is accessed only on the ASGI event loop. Freeze admission before the
    threaded run inspection, so a concurrent submit cannot pass the idle check.
    """
    def __init__(self, request_exit):
        self.request_exit = request_exit
        self.instance_id = secrets.token_hex(16)
        self.state = "running"
        self.active_writes = 0

    def snapshot(self):
        return {"supported": True, "instance_id": self.instance_id, "state": self.state}


class SkillSelectionBody(StrictBody):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    expected_identity: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)


class SkillEnabledBody(SkillSelectionBody):
    workspace_id: str = Field(pattern=f"^{ID}$")
    expected_revision: int = Field(ge=0, le=9007199254740991)
    enabled: bool


class RunBody(StrictBody):
    prompt: str = Field(min_length=1, max_length=16000)
    session_id: str | None = Field(default=None, pattern=f"^{ID}$")
    request_id: str | None = Field(default=None, pattern=f"^{ID}$")
    workspace_id: str | None = Field(default=None, pattern=f"^{ID}$")
    expected_model_identity: str | None = Field(default=None, pattern=r"^(startup|[0-9a-f]{64})$")
    skill: SkillSelectionBody | None = None

    @field_validator("prompt")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("blank prompt")
        return value

    @field_validator("session_id", "request_id", "workspace_id")
    @classmethod
    def valid_identifier(cls, value):
        if value is not None and not re.fullmatch(ID, value):
            raise ValueError("invalid identifier")
        return value


class ApprovalBody(StrictBody):
    approved: bool


class WorkspaceValidationBody(StrictBody):
    path: str = Field(min_length=1, max_length=1000)


class WorkspaceSelectionBody(WorkspaceValidationBody):
    expected_identity: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0, le=9007199254740991)


class KnowledgeOperationBody(StrictBody):
    action: Literal["import_file", "remove", "search", "embedding_test", "embedding_rebuild", "semantic_search"]
    arguments: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=128, pattern=f"^{ID}$")
    workspace_id: str | None = Field(default=None, pattern=f"^{ID}$")

    @field_validator("request_id")
    @classmethod
    def valid_identifier(cls, value):
        if not re.fullmatch(ID, value):
            raise ValueError("invalid identifier")
        return value

    @model_validator(mode="after")
    def bind_document_revision(self):
        # The browser cannot silently downgrade a reviewed import/removal to
        # the backwards-compatible path-only model tool form.
        args = self.arguments
        if self.action == "embedding_test" and args:
            raise ValueError("connection test accepts no content")
        if self.action in {"embedding_rebuild", "semantic_search"}:
            digest = args.get("expected_corpus_sha256")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("corpus revision required")
            allowed = {"expected_corpus_sha256"}
            if self.action == "semantic_search":
                allowed.update(("query", "limit"))
            if set(args) - allowed:
                raise ValueError("unexpected semantic operation field")
        if self.action in {"import_file", "remove"}:
            fields = {"path", "expected_index_sha256"}
            if self.action == "import_file":
                fields.add("expected_source_sha256")
            if set(args) != fields or not isinstance(args.get("path"), str) or not args["path"]:
                raise ValueError("revision-bound document operation required")
            index = args["expected_index_sha256"]
            if not isinstance(index, str) or not (re.fullmatch(r"[0-9a-f]{64}", index)
                    or (self.action == "import_file" and index == "")):
                raise ValueError("invalid index revision")
            if self.action == "import_file" and (not isinstance(args["expected_source_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", args["expected_source_sha256"])):
                raise ValueError("invalid source revision")
        return self


class EmbeddingConfigurationBody(StrictBody):
    embedding: dict[str, Any]
    expected_revision: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class KnowledgeCreateBody(StrictBody):
    id: str = Field(pattern=r"^kb_[a-f0-9]{24}$")
    label: str = Field(min_length=1, max_length=64)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: str = Field(pattern=f"^{ID}$")


class KnowledgeUploadBody(StrictBody):
    filename: str = Field(min_length=1, max_length=120)
    content_base64: str = Field(min_length=1, max_length=170668)
    expected_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: str = Field(pattern=f"^{ID}$")


class ModelProfileSaveBody(StrictBody):
    profile: dict[str, Any]
    expected_revision: int = Field(ge=0, le=9007199254740991)


class ModelProfileSelectionBody(StrictBody):
    id: str | None = Field(pattern=f"^{ID}$")
    expected_revision: int = Field(ge=0, le=9007199254740991)


class ModelProfileItemBody(StrictBody):
    id: str = Field(min_length=1, pattern=f"^{ID}$")
    expected_revision: int = Field(ge=0, le=9007199254740991)


class ModelProfileTestBody(ModelProfileItemBody):
    confirmed: bool

    @field_validator("confirmed")
    @classmethod
    def require_confirmation(cls, value):
        if value is not True:
            raise ValueError("explicit connection test confirmation required")
        return value


class MCPProfileItemBody(ModelProfileItemBody):
    expected_identity: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class MCPProfileConnectBody(MCPProfileItemBody):
    confirmed: bool

    @field_validator("confirmed")
    @classmethod
    def require_confirmation(cls, value):
        if value is not True:
            raise ValueError("explicit MCP launch confirmation required")
        return value


class ResolutionBody(StrictBody):
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("blank note")
        return value


class SessionBody(StrictBody):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None

    @model_validator(mode="after")
    def provided_fields(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("missing session change")
        if self.title is not None and not self.title.strip():
            raise ValueError("blank title")
        return self


def _json(status, value):
    # Framework error handlers can run outside user middleware. They must retain
    # security headers and never return exception details or validation input.
    return JSONResponse(value, status_code=status, headers={**_HEADERS, "Content-Type": "application/json; charset=utf-8"})


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON")


def _query(request, allowed):
    query = parse_qs(request.scope["query_string"].decode("ascii"), keep_blank_values=True)
    if set(query) - allowed or any(len(items) != 1 for items in query.values()):
        raise ValueError("invalid query")
    return {key: items[0] for key, items in query.items()}


def _identifier(value):
    if not re.fullmatch(ID, value):
        raise KeyError("not found")
    return value


def _no_query(request):
    if request.scope["query_string"]:
        raise KeyError("not found")


class LocalProtocol:
    """Validate raw headers and bounded JSON before FastAPI dispatches a write."""
    def __init__(self, app, *, port, server_control=None):
        self.app, self.port = app, port
        self.server_control = server_control

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = {name.lower(): value for name, value in message.get("headers", [])}
                for name, value in _HEADERS.items():
                    headers.setdefault(name.lower().encode("ascii"), value.encode("ascii"))
                message = {**message, "headers": list(headers.items())}
            await send(message)

        async def reject(status, code):
            await _json(status, {"error": code})(scope, receive, safe_send)

        def values(name):
            return [value.decode("latin-1") for key, value in scope["headers"] if key.lower() == name]

        async def dispatch(reader):
            control = self.server_control
            server_path = scope["path"] in {"/api/server", "/api/server/shutdown"}
            if control and control.state != "running" and scope["path"].startswith("/api/") and not server_path:
                await reject(503, "server_stopping")
                return
            write = control and scope["method"] in {"POST", "PUT", "DELETE", "PATCH"} and not server_path
            if write:
                control.active_writes += 1
            try:
                await self.app(scope, reader, safe_send)
            finally:
                if write:
                    control.active_writes -= 1

        hosts = values(b"host")
        if len(hosts) != 1 or hosts[0] not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}:
            await reject(403, "invalid_host")
            return
        origins = values(b"origin")
        if len(origins) > 1 or (origins and origins[0] != f"http://{hosts[0]}"):
            await reject(403, "invalid_origin")
            return
        # The former router matched literal paths, not percent-decoded routes.
        if b"%" in scope.get("raw_path", b""):
            await reject(404, "not_found")
            return
        if scope["method"] in {"POST", "PUT"}:
            if scope["query_string"]:
                await reject(400, "invalid_request")
                return
            lengths, types = values(b"content-length"), values(b"content-type")
            if (any(values(b"transfer-encoding")) or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]+", lengths[0])
                    or len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json"):
                await reject(400, "invalid_request")
                return
            normalized = lengths[0].lstrip("0") or "0"
            body_limit = 180_000 if re.fullmatch(r"/api/knowledge/[a-z][a-z0-9_-]{0,31}/files", scope["path"]) else MAX_BODY
            if len(normalized) > 6 or not 0 < int(normalized) <= body_limit:
                await reject(413, "body_too_large")
                return
            expected, chunks, size = int(normalized), [], 0
            try:
                while True:
                    message = await asyncio.wait_for(receive(), timeout=5)
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > body_limit or size > expected:
                        raise ValueError("body length mismatch")
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                if size != expected:
                    raise ValueError("incomplete body")
                data = b"".join(chunks)
                body = json.loads(data.decode("utf-8"), parse_constant=_invalid_constant, object_pairs_hook=_unique_pairs)
                if not isinstance(body, dict):
                    raise ValueError("object required")
            except (ValueError, UnicodeError, TimeoutError, asyncio.TimeoutError, RecursionError):
                await reject(400, "invalid_request")
                return
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": data, "more_body": False}
                return await receive()

            await dispatch(replay)
        else:
            await dispatch(receive)


async def _stream(application, run_id, after, server_control=None):
    deadline, previous = time.monotonic() + 25, None
    while time.monotonic() < deadline:
        if server_control and server_control.state == "stopping":
            return
        # Observe state first, then drain every event page before its snapshot.
        run = await run_in_threadpool(application.get_run, run_id)
        while True:
            events = await run_in_threadpool(application.events, run_id, after=after)
            for event in events:
                seq = event["seq"]
                if seq <= after:
                    continue
                payload = json.dumps(event, ensure_ascii=False, allow_nan=False)
                yield f"id: {seq}\nevent: run\ndata: {payload}\n\n".encode("utf-8")
                after = seq
            if len(events) < 500:
                break
            if time.monotonic() >= deadline:
                return
        snapshot = {key: run.get(key) for key in (
            "id", "session_id", "prompt", "status", "phase", "output", "error", "approval", "model_calls", "tool_calls",
            "usage", "metrics", "resolution", "can_resolve_unknown", "pause_requested", "resume_status", "limits", "elapsed",
            "kind", "operation", "tool_result"
        )}
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        if payload != previous:
            yield f"event: snapshot\ndata: {payload}\n\n".encode("utf-8")
            previous = payload
        yield b": heartbeat\n\n"
        if run.get("status") in TERMINAL:
            return
        # Disconnect cancels only this subscriber, never the Application run.
        await asyncio.sleep(0.25)


def create_app(application, port, *, knowledge=None, workspaces=None, mcp_profiles=None, skills=None, server_control=None):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid bound port")
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    api.add_middleware(LocalProtocol, port=port, server_control=server_control)

    def selected_workspace(identifier=None):
        if workspaces is not None and identifier is not None:
            if identifier != application.selected_workspace_id:
                raise WorkspaceError("workspace_not_selected")
        return application.selected_workspace_id if workspaces is not None else None

    def knowledge_enabled():
        return knowledge is not None and (workspaces is None
            or application.selected_workspace_id == application.startup_workspace_id)

    def collection(identifier):
        _identifier(identifier)
        if not knowledge_enabled():
            if knowledge is not None:
                raise WorkspaceError("workspace_not_configured")
            raise KeyError("knowledge disabled")
        for item in knowledge.collections():
            if item["id"] == identifier:
                return item
        raise KeyError("collection not found")

    def knowledge_read(identifier, action, arguments):
        item = collection(identifier)
        name = item["tools"][action]
        tool = application.engine.registry.get(name)
        if tool is None or tool.side_effect or tool.requires_approval:
            raise RuntimeError("invalid readonly knowledge binding")
        result = application.engine.executor.execute(
            ToolCall(secrets.token_hex(16), name, arguments), frozenset(application.permissions))
        if result.error_code:
            code = result.error_code
            return _json(403 if code == "permission_denied" else 400 if code == "invalid_arguments" else 409,
                         {"error": code})
        if not isinstance(result.value, dict) or result.value.get("ok") is not True:
            # Do not disguise unavailable/stale metadata as an empty collection.
            code = result.value.get("error_code", "knowledge_read_failed") if isinstance(result.value, dict) else "invalid_tool_result"
            return _json(409, {"error": code})
        return _json(200, result.value)

    def knowledge_control(identifier, *, write=False):
        # Host configuration is not agent-generated content. Access still uses
        # the selected collection's already registered permission requirements.
        item = collection(identifier)
        action = "import_file" if write else "catalog"
        tool = application.engine.registry.get(item["tools"][action])
        if tool is None:
            raise RuntimeError("invalid knowledge control binding")
        if not tool.permissions.issubset(frozenset(application.permissions)):
            raise PermissionError("permission_denied")
        return item

    async def error(request, exception):
        if isinstance(exception, (WorkspaceError, ModelProfileError, MCPProfileError, RuntimeUpdateError,
                                  SkillCatalogError, TaskContextError, KnowledgeLibraryError)):
            status, code = exception.status, exception.code
        elif isinstance(exception, SkillLoadError):
            status, code = 409, exception.code
        elif isinstance(exception, (KeyError, HTTPException)):
            status, code = 404, "not_found"
        elif isinstance(exception, (ValueError, UnicodeError, RequestValidationError)):
            status, code = 400, "invalid_request"
        elif isinstance(exception, PermissionError):
            status, code = 403, "permission_denied"
        elif isinstance(exception, RuntimeError):
            status, code = 409, "conflict"
        else:
            status, code = 500, "internal_error"
        return _json(status, {"error": code})

    for kind in (KeyError, HTTPException, ValueError, UnicodeError, RequestValidationError, PermissionError, RuntimeError, Exception):
        api.add_exception_handler(kind, error)

    @api.get("/")
    def index(request: Request):
        _no_query(request)
        nonce = secrets.token_urlsafe(24)
        content = (Path(__file__).parent / "static/index.html").read_text(encoding="utf-8").replace("__NONCE__", nonce)
        policy = _POLICY + f"; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:"
        return Response(content, media_type="text/html", headers={**_HEADERS, "Content-Security-Policy": policy})

    @api.get("/static/{name}")
    def static(name: str, request: Request):
        _no_query(request)
        if name not in STATIC_FILES:
            raise KeyError("not found")
        return Response((Path(__file__).parent / "static" / name).read_bytes(), media_type=STATIC_FILES[name] + "; charset=utf-8")

    @api.get("/api/info")
    def info(request: Request):
        _no_query(request)
        return _json(200, application.info())

    @api.get("/api/server")
    async def server_status(request: Request):
        _no_query(request)
        return _json(200, server_control.snapshot() if server_control else {"supported": False})

    @api.post("/api/server/shutdown")
    async def shutdown(body: ShutdownBody):
        if server_control is None:
            return _json(503, {"error": "server_shutdown_unavailable"})
        if body.instance_id != server_control.instance_id:
            return _json(409, {"error": "server_changed"})
        if server_control.state == "stopping":
            return _json(202, server_control.snapshot())
        if server_control.state != "running" or server_control.active_writes:
            return _json(409, {"error": "server_busy"})
        server_control.state = "checking"
        try:
            runs = await run_in_threadpool(application.list_runs)
            if any(run["status"] in {"pending", "running"} for run in runs):
                return _json(409, {"error": "server_busy"})
            server_control.state = "stopping"
        finally:
            if server_control.state == "checking":
                server_control.state = "running"
        response = _json(202, server_control.snapshot())
        # Send the acknowledgement before asking Uvicorn to stop. serve() then
        # returns to the existing ExitStack: MCP, Application, and owned tools.
        response.background = BackgroundTask(server_control.request_exit)
        return response

    @api.get("/api/model-profiles")
    def model_profiles(request: Request):
        _no_query(request)
        return _json(200, application.model_profiles())

    @api.post("/api/model-profiles/save")
    def save_model_profile(body: ModelProfileSaveBody):
        return _json(200, application.save_model_profile(body.profile, body.expected_revision))

    @api.post("/api/model-profiles/select")
    def select_model_profile(body: ModelProfileSelectionBody):
        return _json(200, application.select_model_profile(body.id, body.expected_revision))

    @api.post("/api/model-profiles/delete")
    def delete_model_profile(body: ModelProfileItemBody):
        return _json(200, application.delete_model_profile(body.id, body.expected_revision))

    @api.post("/api/model-profiles/delete-key")
    def delete_model_profile_key(body: ModelProfileItemBody):
        return _json(200, application.delete_model_profile_key(body.id, body.expected_revision))

    @api.post("/api/model-profiles/test")
    async def test_model_profile(body: ModelProfileTestBody, request: Request):
        # The explicit test owns only this synthetic request. Disconnect must
        # close it; it must never cancel an unrelated agent run or retry a bill.
        task = asyncio.create_task(application.test_model_profile(body.id, body.expected_revision))
        try:
            while not task.done():
                if await request.is_disconnected():
                    return Response(status_code=499)
                await asyncio.wait({task}, timeout=0.05)
            return _json(200, task.result())
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def mcp_control():
        if mcp_profiles is None:
            raise KeyError("MCP profiles unavailable")
        if workspaces is not None and application.selected_workspace_id != application.startup_workspace_id:
            raise WorkspaceError("workspace_not_configured")
        return mcp_profiles

    def mcp_catalog_result(value):
        return {**value, "bound_workspace": application.startup_workspace_id,
                "current_workspace": application.selected_workspace_id,
                "actions_allowed": mcp_profiles is not None and (workspaces is None
                    or application.selected_workspace_id == application.startup_workspace_id)}

    @api.get("/api/mcp-profiles")
    def mcp_catalog(request: Request):
        _no_query(request)
        value = (mcp_profiles.list() if mcp_profiles is not None else
                 {"available": False, "revision": 0, "items": [], "reason": "mcp_profiles_unavailable"})
        return _json(200, mcp_catalog_result(value))

    @api.post("/api/mcp-profiles/save")
    def save_mcp_profile(body: ModelProfileSaveBody):
        return _json(200, mcp_catalog_result(mcp_control().save(body.profile, body.expected_revision)))

    @api.post("/api/mcp-profiles/delete")
    def delete_mcp_profile(body: MCPProfileItemBody):
        return _json(200, mcp_catalog_result(mcp_control().delete(
            body.id, body.expected_revision, body.expected_identity)))

    async def mcp_operation(request, operation, body):
        controller = mcp_control()
        parameters = {"confirmed": body.confirmed} if isinstance(body, MCPProfileConnectBody) else {}
        task = asyncio.create_task(getattr(controller, operation)(body.id, body.expected_revision,
                                                                  body.expected_identity, **parameters))
        try:
            while not task.done():
                if await request.is_disconnected():
                    return Response(status_code=499)
                await asyncio.wait({task}, timeout=0.05)
            result = task.result()
            return _json(200, result if operation == "test" else mcp_catalog_result(result))
        finally:
            if not task.done():
                task.cancel()
            # The lifecycle owner closes the candidate before releasing its
            # runtime gate; an abandoned browser request cannot leave a worker.
            await asyncio.gather(task, return_exceptions=True)

    @api.post("/api/mcp-profiles/test")
    async def test_mcp_profile(body: MCPProfileConnectBody, request: Request):
        return await mcp_operation(request, "test", body)

    @api.post("/api/mcp-profiles/enable")
    async def enable_mcp_profile(body: MCPProfileConnectBody, request: Request):
        return await mcp_operation(request, "enable", body)

    @api.post("/api/mcp-profiles/disable")
    async def disable_mcp_profile(body: MCPProfileItemBody, request: Request):
        return await mcp_operation(request, "disable", body)

    @api.get("/api/workspaces")
    def workspace_catalog(request: Request):
        _no_query(request)
        if workspaces is None:
            return _json(200, {"enabled": False, "reason": "workspace_unavailable", "startup_id": None,
                               "selected_id": None, "revision": 0, "items": []})
        return _json(200, workspaces.describe(application))

    def skill_scope(identifier):
        if skills is None or workspaces is None:
            raise SkillCatalogError("skill_catalog_unavailable")
        if identifier is None or not re.fullmatch(ID, identifier):
            raise ValueError("workspace required")
        selected_workspace(identifier)
        binding = application.require_workspace(identifier)
        if binding.file_plugin is None:
            raise WorkspaceError("workspace_disabled")
        return {key: getattr(binding, key) for key in ("id", "path", "identity")}

    @api.get("/api/skills")
    def skill_catalog(request: Request):
        query = _query(request, {"workspace_id"})
        if skills is None or workspaces is None:
            return _json(200, {"available": False, "reason": "skill_catalog_unavailable", "items": [], "issues": []})
        return _json(200, {"available": True, **skills.list(skill_scope(query.get("workspace_id")))})

    @api.get("/api/skills/{skill_id}")
    def skill_document(skill_id: str, request: Request):
        query = _query(request, {"workspace_id", "expected_identity"})
        scope = skill_scope(query.get("workspace_id"))
        return _json(200, skills.view(skill_id, query.get("expected_identity"), scope))

    @api.post("/api/skills/enabled")
    def skill_enabled(body: SkillEnabledBody):
        scope = skill_scope(body.workspace_id)
        return _json(200, {"available": True, **skills.set_enabled(body.id, body.enabled,
            body.expected_revision, body.expected_identity, scope)})

    @api.post("/api/workspaces/validate")
    def validate_workspace(body: WorkspaceValidationBody):
        if workspaces is None:
            raise WorkspaceError("workspace_disabled")
        return _json(200, workspaces.inspect(body.path, items=application.workspaces()["items"]))

    @api.post("/api/workspaces/select")
    def select_workspace(body: WorkspaceSelectionBody):
        if workspaces is None:
            raise WorkspaceError("workspace_disabled")
        return _json(200, workspaces.select(application, **body.model_dump()))

    @api.get("/api/knowledge")
    def knowledge_collections(request: Request):
        query = _query(request, {"workspace_id"})
        selected_workspace(query.get("workspace_id"))
        if knowledge is not None and not knowledge_enabled():
            return _json(200, {"enabled": False, "collections": [], "reason": "workspace_not_configured"})
        if knowledge is None:
            return _json(200, {"enabled": False, "collections": []})
        descriptors = []
        for item in knowledge.collections() if knowledge is not None else ():
            tool = application.engine.registry.get(item["tools"]["search"])
            if tool is None:
                raise RuntimeError("knowledge collection is not registered")
            # This is the same opaque host binding stored in run snapshots,
            # not an absolute source/index path or a new identity mechanism.
            identities = {}
            for action, name in item["tools"].items():
                registered = application.engine.registry.get(name)
                if registered is None:
                    raise RuntimeError("knowledge collection is not registered")
                identities[action] = registered.recovery_identity
            descriptors.append({**item, "recovery_identity": tool.recovery_identity,
                                "tool_recovery_identities": identities})
        return _json(200, {"enabled": knowledge is not None,
                           "collections": descriptors,
                           "workspace_id": application.selected_workspace_id,
                           "management": knowledge.management() if callable(getattr(knowledge, "management", None))
                           else {"can_create": False}})

    @api.post("/api/knowledge/collections")
    def create_knowledge_collection(body: KnowledgeCreateBody):
        selected_workspace(body.workspace_id)
        if not knowledge_enabled() or not callable(getattr(knowledge, "create", None)):
            raise WorkspaceError("workspace_not_configured")
        return _json(201, knowledge.create(**body.model_dump()))

    @api.post("/api/knowledge/{collection_id}/files")
    def upload_knowledge_material(collection_id: str, body: KnowledgeUploadBody):
        selected_workspace(body.workspace_id)
        item = knowledge_control(collection_id, write=True)
        if "files.write" not in application.permissions:
            raise PermissionError("permission_denied")
        tool = application.engine.registry.get(item["tools"]["search"])
        if tool.recovery_identity != body.expected_identity:
            raise KnowledgeLibraryError("knowledge_conflict")
        plugin = knowledge.get(collection_id)
        if plugin is None:
            raise KnowledgeLibraryError("knowledge_unavailable")
        return _json(201, upload_material(plugin, body.filename, body.content_base64))

    @api.get("/api/knowledge/{collection_id}")
    def knowledge_catalog(collection_id: str, request: Request):
        _no_query(request)
        return knowledge_read(collection_id, "catalog", {})

    @api.get("/api/knowledge/{collection_id}/inspect")
    def knowledge_inspect(collection_id: str, request: Request):
        query = _query(request, {"path"})
        return knowledge_read(collection_id, "inspect", {"path": query.get("path", "")})

    @api.get("/api/knowledge/{collection_id}/embedding-config")
    def embedding_configuration(collection_id: str, request: Request):
        _no_query(request)
        knowledge_control(collection_id)
        return _json(200, knowledge.embedding_configuration(collection_id))

    @api.put("/api/knowledge/{collection_id}/embedding-config")
    def save_embedding_configuration(collection_id: str, body: EmbeddingConfigurationBody):
        knowledge_control(collection_id, write=True)
        return _json(200, knowledge.save_embedding_configuration(collection_id, body.embedding, body.expected_revision))

    @api.get("/api/knowledge/{collection_id}/embedding-status")
    def embedding_status(collection_id: str, request: Request):
        _no_query(request)
        item = knowledge_control(collection_id)
        if "embedding_status" not in item["tools"]:
            return _json(200, {"ok": True, "state": "not_configured", "provider_status": "not_configured",
                               "key_present": False, "corpus_sha256": None, "document_count": 0,
                               "chunk_count": 0, "max_chunks": 128, "batch_size": 8,
                               "estimated_requests": 0, "dimensions": None, "index_compatible": False})
        return knowledge_read(collection_id, "embedding_status", {})

    @api.post("/api/knowledge/{collection_id}/operations")
    def knowledge_operation(collection_id: str, body: KnowledgeOperationBody):
        selected_workspace(body.workspace_id)
        item = collection(collection_id)
        if body.action in {"embedding_test", "embedding_rebuild", "semantic_search"}:
            if knowledge.embedding_configuration(collection_id)["restart_required"]:
                raise RuntimeError("embedding_configuration_restart_required")
        return _json(201, application.submit_tool(item["tools"][body.action], body.arguments,
                                                 request_id=body.request_id,
                                                 workspace_id=application.startup_workspace_id if workspaces else None))

    @api.get("/api/sessions")
    def sessions(request: Request):
        query = _query(request, {"archived", "workspace_id"})
        if query.get("archived", "0") not in {"0", "1"}:
            raise ValueError("invalid archived flag")
        options = {"workspace_id": query["workspace_id"]} if "workspace_id" in query else {}
        if query.get("archived") == "1":
            options["include_archived"] = True
        result = application.list_sessions(**options)
        return _json(200, result)

    @api.get("/api/files")
    def files(request: Request):
        query = _query(request, {"path", "workspace_id"})
        options = {"workspace_id": query["workspace_id"]} if "workspace_id" in query else {}
        return _json(200, application.list_files(query.get("path", ""), **options))

    @api.get("/api/files/read")
    def read_file(request: Request):
        query = _query(request, {"path", "workspace_id"})
        options = {"workspace_id": query["workspace_id"]} if "workspace_id" in query else {}
        return _json(200, application.read_file(query.get("path", ""), **options))

    @api.get("/api/sessions/{session_id}")
    def session(session_id: str, request: Request):
        _no_query(request)
        return _json(200, application.get_session(_identifier(session_id)))

    @api.get("/api/runs/{run_id}")
    def run(run_id: str, request: Request):
        _no_query(request)
        return _json(200, application.get_run(_identifier(run_id)))

    @api.get("/api/runs/{run_id}/preview")
    def preview(run_id: str, request: Request):
        _no_query(request)
        return _json(200, application.preview_write(_identifier(run_id)))

    @api.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        identifier = _identifier(run_id)
        query = _query(request, {"after"})
        cursors = request.headers.getlist("last-event-id")
        if len(cursors) > 1:
            raise ValueError("invalid cursor")
        cursor = (cursors[0] if cursors else "") or query.get("after", "0")
        if not re.fullmatch(r"[0-9]{1,18}", cursor):
            raise ValueError("invalid cursor")
        await run_in_threadpool(application.get_run, identifier)
        return StreamingResponse(_stream(application, identifier, int(cursor), server_control), media_type="text/event-stream", headers=_HEADERS)

    @api.post("/api/runs")
    def submit(body: RunBody):
        return _json(201, application.submit(body.prompt, session_id=body.session_id, request_id=body.request_id,
                                             **({"workspace_id": body.workspace_id} if body.workspace_id is not None else {}),
                                             **({"expected_model_identity": body.expected_model_identity}
                                                 if body.expected_model_identity is not None else {}),
                                              **({"task_context_selection": body.skill.model_dump()}
                                                 if body.skill is not None else {})))

    @api.post("/api/sessions/{session_id}")
    def update_session(session_id: str, body: SessionBody):
        return _json(200, application.update_session(_identifier(session_id), **body.model_dump(exclude_unset=True)))

    @api.post("/api/sessions/{session_id}/delete")
    def delete_session(session_id: str, body: StrictBody):
        return _json(200, application.delete_session(_identifier(session_id)))

    @api.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: ApprovalBody):
        return _json(200, application.approve(_identifier(run_id), approved=body.approved))

    @api.post("/api/runs/{run_id}/resolve")
    def resolve(run_id: str, body: ResolutionBody):
        return _json(200, application.resolve_unknown(_identifier(run_id), body.note))

    @api.post("/api/runs/{run_id}/pause")
    def pause(run_id: str, body: StrictBody):
        return _json(200, application.pause(_identifier(run_id)))

    @api.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, body: StrictBody):
        return _json(200, application.resume(_identifier(run_id)))

    @api.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, body: StrictBody):
        return _json(200, application.cancel(_identifier(run_id)))

    return api


class LocalServer:
    """Lifecycle shell for existing CLI/tests; Uvicorn is the only HTTP server."""
    def __init__(self, application, host, port, *, knowledge=None, workspaces=None, mcp_profiles=None, skills=None):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._socket.bind((host, port))
            self._socket.listen(128)
            self._socket.setblocking(False)
            self.server_address = self._socket.getsockname()
            self.server_port = self.server_address[1]
            self.control = ServerControl(self.request_exit)
            config = uvicorn.Config(create_app(application, self.server_port, knowledge=knowledge, workspaces=workspaces,
                                               mcp_profiles=mcp_profiles, skills=skills, server_control=self.control), host=host, port=self.server_port,
                                    http="h11", loop="asyncio", lifespan="off", workers=1, reload=False,
                                    proxy_headers=False, access_log=False, ws="none", log_config=None,
                                    log_level="critical", server_header=False, date_header=False,
                                    timeout_keep_alive=5, timeout_graceful_shutdown=1)
            self._server = uvicorn.Server(config)
        except BaseException:
            self._socket.close()
            raise
        self._stopped = threading.Event()
        self._running = False

    def serve_forever(self, poll_interval=0.5):
        self._running = True
        try:
            self._server.run(sockets=[self._socket])
        finally:
            self._running = False
            self._stopped.set()

    def shutdown(self):
        self.request_exit()
        if self._running and not self._stopped.wait(timeout=5):
            raise RuntimeError("HTTP server did not stop")

    def request_exit(self):
        self._server.should_exit = True

    def server_close(self):
        self.shutdown()
        self._socket.close()


def create_server(app, host="127.0.0.1", port=8765, *, knowledge=None, workspaces=None, mcp_profiles=None, skills=None):
    if host != "127.0.0.1":
        raise ValueError("local server must bind to 127.0.0.1")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid port")
    return LocalServer(app, host, port, knowledge=knowledge, workspaces=workspaces, mcp_profiles=mcp_profiles, skills=skills)


def serve(app, port=8765, *, knowledge=None, workspaces=None, mcp_profiles=None, skills=None):
    server = create_server(app, port=port, knowledge=knowledge, workspaces=workspaces, mcp_profiles=mcp_profiles, skills=skills)
    print(f"Astra Harness: http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
