"""Build-time ownership of independent, trusted local MCP services.

The catalog is frozen after activation. A failed service supplies no tools; it
does not authorize retries, hot replacement, or replay of uncertain effects.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
import re
import stat
import threading

from .mcp import MCPError, StdioMCPClient
from ..tools import Tool, ToolUnavailable


MAX_CONFIG_BYTES = 65536
MAX_SERVICES = 8
MAX_TOOLS = 200
MAX_MESSAGE_BYTES = 1_000_000
PROTOCOL_VERSION = "2025-06-18"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_ERROR_CODES = frozenset({
    "mcp_initialization_failed", "mcp_unsupported_capabilities", "mcp_invalid_tools",
    "mcp_invalid_schema", "mcp_invalid_cursor", "mcp_page_limit", "mcp_tool_limit",
    "mcp_transport_closed", "mcp_transport_error", "mcp_invalid_message",
    "mcp_message_limit", "mcp_closed", "mcp_send_timeout", "mcp_send_failed",
    "mcp_response_mismatch", "mcp_remote_error", "mcp_timeout", "mcp_tool_error",
    "mcp_execution_unknown", "mcp_cancelled", "mcp_cleanup_failed", "mcp_cleanup_incomplete",
    "mcp_service_failed", "mcp_http_status", "mcp_invalid_response", "mcp_invalid_session",
    "mcp_session_expired", "mcp_unsupported_encoding", "mcp_invalid_result",
})


@dataclass(frozen=True)
class MCPServiceConfig:
    name: str
    command: tuple[str, ...] = field(default=(), repr=False)
    timeout: float = 10.0
    revision: str = "1"
    legacy: bool = False
    transport: str = "stdio"
    endpoint: str | None = field(default=None, repr=False)
    # Capture once: an identical relative command in another directory can name
    # a different executable or data set. This is internal, never a JSON option.
    cwd: str = field(default_factory=lambda: str(Path.cwd().resolve()), init=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError("invalid_mcp_service_name")
        if self.transport not in ("stdio", "http"):
            raise ValueError("invalid_mcp_transport")
        if self.transport == "stdio":
            if self.endpoint is not None:
                raise ValueError("invalid_mcp_endpoint")
            if (not isinstance(self.command, tuple) or not 1 <= len(self.command) <= 64
                    or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in self.command)):
                raise ValueError("invalid_mcp_command")
        else:
            if self.command != () or not isinstance(self.command, tuple):
                raise ValueError("invalid_mcp_command")
            # Static validation only: configuring or diagnosing an endpoint
            # must not connect to it or allocate transport resources.
            from .mcp_http import validate_http_endpoint
            object.__setattr__(self, "endpoint", validate_http_endpoint(self.endpoint))
        if (type(self.timeout) not in (int, float) or not 0 < self.timeout <= 300
                or not math.isfinite(self.timeout)):
            raise ValueError("invalid_mcp_timeout")
        if (not isinstance(self.revision, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.revision)):
            raise ValueError("invalid_mcp_revision")
        if (type(self.legacy) is not bool
                or (self.legacy and (self.name != "legacy" or self.transport != "stdio"))):
            raise ValueError("invalid_mcp_legacy_config")
        try:
            target = self.command if self.transport == "stdio" else self.endpoint
            size = len(json.dumps(target, ensure_ascii=False).encode("utf-8"))
        except UnicodeError:
            raise ValueError("invalid_mcp_command") from None
        if size > MAX_CONFIG_BYTES:
            raise ValueError("mcp_config_limit")
        object.__setattr__(self, "timeout", float(self.timeout))

    @property
    def prefix(self):
        return "mcp" if self.legacy else f"mcp.{self.name}"

    @property
    def permission(self):
        return "mcp.call" if self.legacy else f"mcp.{self.name}.call"

    def recovery_identity(self):
        # Binds host configuration, not executable/server contents. Operators
        # must bump revision when a service changes behind the same target.
        # Preserve the existing STDIO payload so adding HTTP does not invalidate
        # unchanged STDIO approvals or saved runs.
        payload = {
            "name": self.name, "prefix": self.prefix, "transport": self.transport,
            "protocol": PROTOCOL_VERSION,
            "timeout": self.timeout, "max_message_bytes": MAX_MESSAGE_BYTES,
            "max_tools": MAX_TOOLS, "revision": self.revision,
            "policy": {"permissions": [self.permission], "side_effect": True,
                       "requires_approval": True},
        }
        if self.transport == "stdio":
            payload.update(command=list(self.command), cwd=self.cwd)
        else:
            payload["endpoint"] = self.endpoint
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_mcp_config(value):
    """Validate all services before any process or HTTP connection is started."""
    if type(value) is not dict:
        raise ValueError("invalid_mcp_config")
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid_mcp_config") from None
    if size > MAX_CONFIG_BYTES:
        raise ValueError("mcp_config_limit")
    if set(value) == {"command"}:
        if type(value["command"]) is not list:
            raise ValueError("invalid_mcp_command")
        return (MCPServiceConfig("legacy", tuple(value["command"]), legacy=True),)
    if set(value) != {"servers"} or type(value["servers"]) is not list or not 1 <= len(value["servers"]) <= MAX_SERVICES:
        raise ValueError("invalid_mcp_servers")
    configs = []
    names = set()
    for item in value["servers"]:
        if type(item) is not dict or "name" not in item:
            raise ValueError("invalid_mcp_server")
        transport = item.get("transport", "stdio")
        target = "endpoint" if transport == "http" else "command"
        if (target not in item or set(item) - {"name", target, "transport", "timeout", "revision"}
                or (target == "command" and type(item[target]) is not list)):
            raise ValueError("invalid_mcp_server")
        config = MCPServiceConfig(item["name"], tuple(item["command"]) if target == "command" else (),
                                  item.get("timeout", 10), item.get("revision", "1"),
                                  transport=transport, endpoint=item.get("endpoint"))
        if config.name in names:
            raise ValueError("duplicate_mcp_service")
        names.add(config.name)
        configs.append(config)
    return tuple(sorted(configs, key=lambda config: config.name))


def load_mcp_config(path):
    """Read bounded UTF-8 JSON, rejecting duplicate keys and non-JSON numbers."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_mcp_config_key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("invalid_mcp_config_number")

    try:
        source = Path(path)
        if not stat.S_ISREG(source.stat().st_mode):
            raise ValueError("mcp_config_not_readable")
        with source.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
    except (OSError, ValueError, TypeError):
        raise ValueError("mcp_config_not_readable") from None
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("mcp_config_limit")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=invalid_constant)
        return parse_mcp_config(value)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        # JSONDecodeError includes the source document; keep host configuration
        # and possible arguments out of public diagnostics.
        raise ValueError("invalid_mcp_config") from None


@dataclass
class _Service:
    config: MCPServiceConfig
    client: object = None
    state: str = "new"
    error_code: str | None = None
    tools: tuple[Tool, ...] = ()


def _safe_error(error):
    code = str(error) if isinstance(error, (MCPError, str)) else ""
    return code if code in _ERROR_CODES else "mcp_service_failed"


class MCPServiceManager:
    name = "mcp-services"

    def __init__(self, configs, *, client_factory=StdioMCPClient, http_client_factory=None):
        configs = tuple(configs)
        if (not 1 <= len(configs) <= MAX_SERVICES
                or any(not isinstance(config, MCPServiceConfig) for config in configs)
                or len({config.name for config in configs}) != len(configs)
                or (any(config.legacy for config in configs) and len(configs) != 1)):
            raise ValueError("invalid_mcp_services")
        self._services = tuple(_Service(config) for config in sorted(configs, key=lambda c: c.name))
        self._client_factory = client_factory
        self._http_client_factory = http_client_factory
        self._lock = threading.RLock()
        self._activated = False
        self._closed = False
        self.permissions = frozenset(service.config.permission for service in self._services)

    def _tools(self, service):
        remote_tools = service.client.discover_tools()
        tools = []
        for remote in remote_tools:
            if not remote.name.startswith("mcp."):
                raise MCPError("mcp_invalid_tools")
            remote_name = remote.name[len("mcp."):]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", remote_name):
                raise MCPError("mcp_invalid_tools")

            def make_handler(client, name):
                async def handler(**arguments):
                    if self._closed or client.state != "ready":
                        raise ToolUnavailable("mcp_service_unavailable")
                    return await client.acall_tool(name, arguments)
                return handler

            config = service.config
            identity = None if config.legacy else config.recovery_identity()
            tools.append(Tool(
                f"{config.prefix}.{remote_name}", remote.description, (),
                make_handler(service.client, remote_name), input_schema=remote.spec().schema(),
                permissions=frozenset({config.permission}), side_effect=True, requires_approval=True,
                recovery_identity=identity,
                implementation_version="mcp-services-v1" if config.transport == "stdio" else "mcp-http-services-v1",
                implementation_constants={"recovery_identity": identity},
            ))
        if len({tool.name for tool in tools}) != len(tools):
            raise MCPError("mcp_invalid_tools")
        return tuple(sorted(tools, key=lambda tool: tool.name))

    def activate(self, registry):
        with self._lock:
            if self._activated or self._closed:
                raise RuntimeError("mcp_services_already_activated_or_closed")
            self._activated = True
        registered = []
        try:
            count = 0
            for service in self._services:
                with self._lock:
                    if self._closed:
                        raise RuntimeError("mcp_services_closed")
                    service.state = "starting"
                try:
                    with self._lock:
                        if self._closed:
                            raise RuntimeError("mcp_services_closed")
                        config = service.config
                        if config.transport == "http":
                            factory = self._http_client_factory
                            if factory is None:
                                from .mcp_http import StreamableHTTPMCPClient
                                factory = StreamableHTTPMCPClient
                            service.client = factory(config.endpoint, timeout=config.timeout,
                                                     max_message_bytes=MAX_MESSAGE_BYTES)
                        else:
                            service.client = self._client_factory(list(config.command),
                                timeout=config.timeout, max_message_bytes=MAX_MESSAGE_BYTES, cwd=config.cwd)
                    service.client.start()
                    candidates = self._tools(service)
                    if count + len(candidates) > MAX_TOOLS:
                        raise MCPError("mcp_tool_limit")
                except Exception as error:
                    service.error_code = _safe_error(error)
                    service.state = "failed"
                    self._close_service(service)
                    if service.config.legacy:
                        raise MCPError(service.error_code) from None
                    if self._closed:
                        raise RuntimeError("mcp_services_closed") from None
                    continue
                with self._lock:
                    if self._closed:
                        raise RuntimeError("mcp_services_closed")
                    # Registration failures are host errors: roll back this whole
                    # activation, while a remote discovery failure is isolated.
                    for tool in candidates:
                        registry.register(tool)
                        registered.append(tool)
                    service.tools = candidates
                    service.state = "ready"
                    count += len(candidates)
            return self.close
        except BaseException:
            try:
                for tool in reversed(registered):
                    if registry.get(tool.name) is tool:
                        registry.unregister(tool.name)
            finally:
                try:
                    self.close()
                except Exception:
                    pass
            raise

    def status(self):
        with self._lock:
            result = []
            for service in self._services:
                state, error = service.state, service.error_code
                if self._closed:
                    state = "closed"
                elif state == "ready":
                    observed = service.client.state
                    if observed in {"new", "starting", "ready", "failed", "closed"}:
                        state = observed
                    else:
                        state, error = "failed", "mcp_service_failed"
                    if service.client.last_error:
                        error = _safe_error(service.client.last_error)
                if service.client is not None and service.client.last_error == "mcp_cleanup_incomplete":
                    error = "mcp_cleanup_incomplete"
                result.append({"name": service.config.name, "state": state,
                               "error_code": error, "transport": service.config.transport,
                               "tools": [tool.name for tool in service.tools]})
            return result

    @staticmethod
    def _close_service(service):
        if service.client is None:
            return False
        try:
            service.client.close()
        except Exception:
            service.error_code = "mcp_cleanup_failed"
            return True
        # Transport cleanup is bounded and may report an incomplete shutdown
        # without raising. Preserve that fact even when the manager is closed.
        if service.client.last_error == "mcp_cleanup_incomplete":
            service.error_code = "mcp_cleanup_incomplete"
            return True
        return False

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            services = tuple(reversed(self._services))
        failed = False
        for service in services:
            if self._close_service(service):
                failed = True
        if failed:
            raise MCPError("mcp_cleanup_failed")
