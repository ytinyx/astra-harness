"""Bounded, explicitly configured loopback MCP Streamable HTTP client.

One private event loop owns HTTPX connections. Synchronous host construction and
async tool execution use that same owner; cancelling a wait never proves that a
remote write was cancelled. There is no retry, redirect, session recovery or
background GET subscription. Only MCP 2025-06-18 tools are negotiated.
"""
import asyncio
import concurrent.futures
import json
import math
import re
import threading
import time

from .mcp import MCPError, _pairs, _constant
from ..tools import Tool, ToolExecutionUnknown, ToolUnavailable

PROTOCOL_VERSION = "2025-06-18"


def validate_http_endpoint(value):
    """Pure parsing: numeric loopback, explicit port and a simple absolute path."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid_mcp_endpoint")
    match = re.fullmatch(r"http://(127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})(/[A-Za-z0-9._~/-]*)", value)
    if not match or not 1 <= int(match[2]) <= 65535 or any(part in {".", ".."} for part in match[3].split("/")):
        raise ValueError("invalid_mcp_endpoint")
    if "//" in match[3]:
        raise ValueError("invalid_mcp_endpoint")
    return value


class _Operation:
    def __init__(self):
        self.issued = False
        self.request_id = None
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.guard = threading.Lock()


class StreamableHTTPMCPClient:
    def __init__(self, endpoint, timeout=10, max_message_bytes=1_000_000):
        self.endpoint = validate_http_endpoint(endpoint)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300
                or type(max_message_bytes) is not int or not 256 <= max_message_bytes <= 10_000_000):
            raise ValueError("invalid_mcp_limits")
        self.timeout, self.max_message_bytes = float(timeout), max_message_bytes
        self._origin = self.endpoint[:self.endpoint.index("/", len("http://"))]
        self._state, self._last_error = "new", None
        self._guard = threading.RLock()
        self._loop = self._thread = self._client = None
        self._session = None
        self._next_id = 0
        self._active = set()
        self._started = threading.Event()
        self._closing = False
        self._close_done = threading.Event()

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
        return self.state == "ready" and bool(self._thread and self._thread.is_alive())

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._request_lock, self._cleanup_lock = asyncio.Lock(), asyncio.Lock()
        self._started.set()
        try:
            loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.wait(tasks, timeout=.5))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _tracked(self, factory):
        task = asyncio.current_task()
        # A cancelled cross-thread Future no longer retrieves the final Task
        # exception. Consume it here while preserving normal result propagation.
        task.add_done_callback(lambda item: None if item.cancelled() else item.exception())
        self._active.add(task)
        try:
            return await factory()
        finally:
            self._active.discard(task)

    def _submit(self, factory):
        with self._guard:
            if self._loop is None or not self._thread.is_alive() or self._state == "closed":
                raise MCPError("mcp_closed")
            return asyncio.run_coroutine_threadsafe(self._tracked(factory), self._loop)

    def _sync(self, factory):
        future = self._submit(factory)
        try:
            return future.result(timeout=self.timeout + 3)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise MCPError("mcp_timeout") from None

    def start(self):
        with self._guard:
            if self._state == "ready":
                return self
            if self._state != "new":
                raise MCPError("mcp_closed")
            self._state = "starting"
            self._thread = threading.Thread(target=self._run_loop, name="astra-mcp-http", daemon=True)
            self._thread.start()
        if not self._started.wait(2):
            self.close()
            raise MCPError("mcp_initialization_failed")
        try:
            self._sync(self._initialize)
            return self
        except Exception:
            with self._guard:
                if self._state != "closed":
                    self._state = "failed"
                    self._last_error = self._last_error or "mcp_initialization_failed"
            # __enter__ failures do not run __exit__. Stop our loop here too;
            # the manager may safely call close again.
            self.close()
            raise MCPError("mcp_initialization_failed") from None

    async def _initialize(self):
        import httpx
        self._client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1)), timeout=self.timeout)
        try:
            result = await self._request("initialize", {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {}, "clientInfo": {"name": "astra-harness", "version": "0.5.0"}}, initialize=True)
            if (result.get("protocolVersion") != PROTOCOL_VERSION or not isinstance(result.get("capabilities"), dict)
                    or not isinstance(result["capabilities"].get("tools"), dict)):
                raise MCPError("mcp_unsupported_capabilities")
            await asyncio.wait_for(self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}), self.timeout)
            with self._guard:
                if self._state != "starting":
                    raise MCPError("mcp_closed")
                self._state = "ready"
        except BaseException:
            await self._retire()
            raise

    def _headers(self, initialize=False):
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "Accept-Encoding": "identity", "Origin": self._origin}
        if not initialize:
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
            if self._session is not None:
                headers["Mcp-Session-Id"] = self._session
        return headers

    async def _bytes(self, response):
        count = 0
        async for chunk in response.aiter_raw():
            count += len(chunk)
            if count > self.max_message_bytes:
                raise MCPError("mcp_message_limit")
            yield chunk

    def _validate_headers(self, response, initialize):
        if sum(len(k) + len(v) for k, v in response.headers.raw) > 16384:
            raise MCPError("mcp_invalid_response")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise MCPError("mcp_unsupported_encoding")
        lengths = response.headers.get_list("content-length")
        if lengths:
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
                raise MCPError("mcp_invalid_response")
            if int(lengths[0]) > self.max_message_bytes:
                raise MCPError("mcp_message_limit")
        sessions = response.headers.get_list("mcp-session-id")
        if sessions:
            if len(sessions) != 1 or not re.fullmatch(r"[\x21-\x7e]{1,256}", sessions[0]):
                raise MCPError("mcp_invalid_session")
            if initialize:
                self._session = sessions[0]
            elif sessions[0] != self._session:
                raise MCPError("mcp_invalid_session")

    @staticmethod
    def _decode(raw):
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise MCPError("mcp_invalid_message") from None
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise MCPError("mcp_invalid_message")
        return value

    async def _message(self, message, request_id):
        if "method" in message:
            if (not isinstance(message["method"], str) or "result" in message or "error" in message
                    or ("params" in message and not isinstance(message["params"], dict))):
                raise MCPError("mcp_invalid_message")
            if "id" in message:
                if type(message["id"]) not in (int, str):
                    raise MCPError("mcp_invalid_message")
                reply = {"jsonrpc": "2.0", "id": message["id"]}
                if message["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "Unsupported client method"}
                await self._post(reply)
            return None
        if type(message.get("id")) is not int or message["id"] != request_id:
            raise MCPError("mcp_response_mismatch")
        if "error" in message or not isinstance(message.get("result"), dict):
            raise MCPError("mcp_remote_error")
        return message["result"]

    async def _sse(self, response, request_id):
        pending, data, events = bytearray(), [], 0
        first_line = True
        async for chunk in self._bytes(response):
            pending.extend(chunk)
            while True:
                match = re.search(b"\r\n|\r|\n", pending)
                if match is None or (pending[match.start():] == b"\r"):
                    break
                line = bytes(pending[:match.start()])
                del pending[:match.end()]
                try:
                    text = line.decode("utf-8")
                except UnicodeError:
                    raise MCPError("mcp_invalid_message") from None
                if first_line:
                    text = text.removeprefix("\ufeff")
                    first_line = False
                if not text:
                    if data:
                        events += 1
                        if events > 64:
                            raise MCPError("mcp_message_limit")
                        result = await self._message(self._decode("\n".join(data)), request_id)
                        data = []
                        if result is not None:
                            return result
                elif text.startswith("data:"):
                    value = text[5:]
                    data.append(value[1:] if value.startswith(" ") else value)
                # Comments and other SSE fields do not authorize new endpoints,
                # replay, retries or independent requests.
        if pending == b"\r" and data:
            # A final CR is a valid line ending, not an incomplete CRLF.
            if events >= 64:
                raise MCPError("mcp_message_limit")
            result = await self._message(self._decode("\n".join(data)), request_id)
            if result is not None:
                return result
        raise MCPError("mcp_transport_closed")

    async def _post(self, message, operation=None, initialize=False):
        try:
            body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MCPError("mcp_invalid_request") from None
        if len(body) > self.max_message_bytes:
            raise MCPError("mcp_message_limit")
        if self._client is None:
            raise MCPError("mcp_closed")
        request = self._client.build_request("POST", self.endpoint, headers=self._headers(initialize), content=body)
        # HTTPX persists Set-Cookie automatically; this client deliberately does
        # not forward any server-supplied cookies or authentication material.
        request.headers.pop("cookie", None)
        if operation is not None:
            with operation.guard:
                if operation.cancelled.is_set() or self.state == "closed":
                    raise MCPError("mcp_cancelled")
                operation.issued = True
        response = await self._client.send(request, stream=True, follow_redirects=False)
        try:
            self._validate_headers(response, initialize)
            if response.status_code == 404 and self._session is not None:
                raise MCPError("mcp_session_expired")
            is_request = "method" in message and "id" in message
            if not is_request:
                if response.status_code != 202:
                    raise MCPError("mcp_http_status")
                async for chunk in self._bytes(response):
                    if chunk:
                        raise MCPError("mcp_invalid_response")
                return None
            if response.status_code != 200:
                raise MCPError("mcp_http_status")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type == "text/event-stream":
                return await self._sse(response, message["id"])
            if content_type != "application/json":
                raise MCPError("mcp_invalid_response")
            chunks = bytearray()
            async for chunk in self._bytes(response):
                chunks.extend(chunk)
            result = await self._message(self._decode(bytes(chunks)), message["id"])
            if result is None:
                raise MCPError("mcp_invalid_response")
            return result
        finally:
            await response.aclose()
            if self._client is not None:
                self._client.cookies.clear()

    async def _request(self, method, params, operation=None, initialize=False):
        async def run():
            async with self._request_lock:
                if not initialize and self.state != "ready":
                    raise MCPError("mcp_not_ready")
                self._next_id += 1
                if operation is not None:
                    operation.request_id = self._next_id
                try:
                    result = await self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method,
                                              "params": params}, operation, initialize)
                    if method == "tools/call" and (
                            type(result.get("isError", False)) is not bool or not isinstance(result.get("content"), list)
                            or any(not isinstance(v, dict) or not isinstance(v.get("type"), str) for v in result["content"])
                            or ("structuredContent" in result and not isinstance(result["structuredContent"], dict))):
                        raise MCPError("mcp_invalid_result")
                    return result
                except BaseException as error:
                    # Retire before releasing the serialization lock. Otherwise
                    # an already queued call can dispatch between a broken
                    # response and the outer caller's asynchronous cleanup.
                    if operation is not None and operation.issued:
                        code = (str(error) if isinstance(error, MCPError) else
                                "mcp_cancelled" if isinstance(error, asyncio.CancelledError) else "mcp_transport_error")
                        with self._guard:
                            if self._state != "closed":
                                self._state, self._last_error = "failed", code
                    raise
        try:
            return await asyncio.wait_for(run(), self.timeout)
        except asyncio.TimeoutError:
            raise MCPError("mcp_timeout") from None
        except (MCPError, asyncio.CancelledError):
            raise
        except Exception:
            raise MCPError("mcp_transport_error") from None

    async def _cancel(self, operation):
        if operation.issued and self._client is not None:
            try:
                await asyncio.wait_for(self._post({"jsonrpc": "2.0", "method": "notifications/cancelled",
                    "params": {"requestId": operation.request_id, "reason": "Client stopped waiting"}}), .2)
            except (Exception, asyncio.CancelledError):
                pass

    async def _retire(self):
        async with self._cleanup_lock:
            client, session = self._client, self._session
            self._client, self._session = None, None
            if client is None:
                return
            if session is not None:
                try:
                    request = client.build_request("DELETE", self.endpoint,
                        headers={**self._headers(), "Mcp-Session-Id": session})
                    request.headers.pop("cookie", None)
                    async def delete():
                        response = await client.send(request, stream=True, follow_redirects=False)
                        try:
                            # 404: already expired. 405: server declines deletion.
                            if response.status_code not in {200, 202, 204, 404, 405}:
                                raise MCPError("mcp_cleanup_incomplete")
                        finally:
                            await response.aclose()
                    await asyncio.wait_for(delete(), .25)
                except (Exception, asyncio.CancelledError):
                    with self._guard:
                        self._last_error = "mcp_cleanup_incomplete"
            try:
                await asyncio.wait_for(client.aclose(), .5)
            except (Exception, asyncio.CancelledError):
                with self._guard:
                    self._last_error = "mcp_cleanup_incomplete"

    async def _fail(self, code):
        with self._guard:
            if self._state != "closed":
                self._state, self._last_error = "failed", code
        await self._retire()

    async def _call(self, name, arguments, operation):
        try:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or not isinstance(arguments, dict):
                raise MCPError("mcp_invalid_request")
            result = await self._request("tools/call", {"name": name, "arguments": arguments}, operation)
        except (MCPError, asyncio.CancelledError) as error:
            if operation.issued:
                await self._cancel(operation)
                await self._fail(str(error) if isinstance(error, MCPError) else "mcp_cancelled")
                raise ToolExecutionUnknown("mcp_execution_unknown") from None
            raise ToolUnavailable("mcp_tool_unavailable") from None
        finally:
            operation.done.set()
        if result.get("isError"):
            raise MCPError("mcp_tool_error")
        return result

    def call_tool(self, name, arguments):
        operation = _Operation()
        try:
            return self._sync(lambda: self._call(name, arguments, operation))
        except MCPError as error:
            if str(error) == "mcp_tool_error":
                raise
            if operation.issued:
                raise ToolExecutionUnknown("mcp_execution_unknown") from None
            raise ToolUnavailable("mcp_tool_unavailable") from None

    async def acall_tool(self, name, arguments):
        operation = _Operation()
        try:
            future = self._submit(lambda: self._call(name, arguments, operation))
        except MCPError:
            raise ToolUnavailable("mcp_tool_unavailable") from None
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda item: None if item.cancelled() else item.exception())
        try:
            await asyncio.wait({wrapped})
            return wrapped.result()
        except asyncio.CancelledError:
            with operation.guard:
                operation.cancelled.set()
            future.cancel()
            deadline = time.monotonic() + 1.2
            while not operation.done.is_set() and time.monotonic() < deadline:
                try:
                    await asyncio.sleep(.01)
                except asyncio.CancelledError:
                    break
            raise

    async def _discover(self, prefix):
        tools, names, cursors, cursor = [], set(), set(), None
        try:
            for _ in range(20):
                page = await self._request("tools/list", {"cursor": cursor} if cursor else {})
                if not isinstance(page.get("tools"), list):
                    raise MCPError("mcp_invalid_tools")
                for spec in page["tools"]:
                    name = spec.get("name", "") if isinstance(spec, dict) else ""
                    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or name in names or len(tools) >= 200:
                        raise MCPError("mcp_invalid_tools")
                    schema, description = spec.get("inputSchema"), spec.get("description", "MCP tool")
                    if not isinstance(schema, dict) or schema.get("type") != "object":
                        raise MCPError("mcp_invalid_schema")
                    if not isinstance(description, str):
                        raise MCPError("mcp_invalid_tools")
                    names.add(name)
                    def handler(remote_name):
                        return lambda **arguments: self.call_tool(remote_name, arguments)
                    tools.append(Tool(f"{prefix}.{name}", description[:2000], (), handler(name), input_schema=schema,
                        permissions=frozenset({"mcp.call"}), requires_approval=True, side_effect=True))
                cursor = page.get("nextCursor")
                if cursor is None:
                    return tuple(tools)
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise MCPError("mcp_invalid_cursor")
                cursors.add(cursor)
            raise MCPError("mcp_page_limit")
        except (Exception, asyncio.CancelledError) as error:
            code = str(error) if isinstance(error, MCPError) else "mcp_invalid_tools"
            await self._fail(code)
            raise MCPError(code) from None

    def discover_tools(self, prefix="mcp"):
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
            raise ValueError("invalid_mcp_prefix")
        if self.state == "new":
            self.start()
        return self._sync(lambda: self._discover(prefix))

    async def _shutdown(self):
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=1.2)
            if pending:
                with self._guard:
                    self._last_error = "mcp_cleanup_incomplete"
        await self._retire()

    def close(self):
        with self._guard:
            self._state = "closed"
            owner = not self._closing
            self._closing = True
        if not owner:
            if not self._close_done.wait(4):
                with self._guard:
                    self._last_error = "mcp_cleanup_incomplete"
            return
        try:
            self._close_loop()
        finally:
            self._close_done.set()

    def _close_loop(self):
        with self._guard:
            loop, thread = self._loop, self._thread
        if thread is None or not thread.is_alive():
            return
        if loop is None and not self._started.wait(2):
            with self._guard:
                self._last_error = "mcp_cleanup_incomplete"
            return
        loop = self._loop
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=2.5)
        except Exception:
            with self._guard:
                self._last_error = "mcp_cleanup_incomplete"
        finally:
            if not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
            thread.join(1)
            if thread.is_alive():
                with self._guard:
                    self._last_error = "mcp_cleanup_incomplete"

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
