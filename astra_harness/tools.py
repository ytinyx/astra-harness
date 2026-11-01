"""工具定义、Schema 子集验证及统一准入；处理函数按可信本地代码运行。"""

import inspect
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .contracts import ToolCall, ToolResult, ToolSpec


def _diagnostic_details(details):
    """Detach bounded JSON supplied explicitly by a trusted handler, never str(error).

    This is a transport/type bound, not secret detection. Handlers must select
    non-secret fields; in particular they must not pass arbitrary exception text.
    """
    if details is None:
        return None
    nodes = 0

    def valid(value, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 256 or depth > 8:
            return False
        if value is None or type(value) is bool:
            return True
        if type(value) in (int, float):
            return is_number(value) and (type(value) is float or value.bit_length() <= 256)
        if type(value) is str:
            return len(value) <= 65536
        if type(value) is list:
            return len(value) <= 128 and all(valid(item, depth + 1) for item in value)
        if type(value) is dict:
            return len(value) <= 64 and all(type(key) is str and len(key) <= 64
                and valid(item, depth + 1) for key, item in value.items())
        return False

    try:
        if type(details) is not dict or not valid(details):
            return {"diagnostics_omitted": True}
        encoded = json.dumps(details, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 131072:
            return {"diagnostics_omitted": True}
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return {"diagnostics_omitted": True}


class ToolExecutionUnknown(RuntimeError):
    """请求已送出或副作用可能发生，调用方无法确认最终结果。"""

    def __init__(self, message="Tool execution unknown", *, details=None):
        super().__init__(message)
        self.details = _diagnostic_details(details)


class ToolUnavailable(RuntimeError):
    """The requested primary operation was rejected, with a known outcome.

    A preparatory artifact (for example an edit backup) may remain only when
    explicitly described in details; this is not a claim of zero filesystem IO.
    """

    def __init__(self, message="Tool unavailable", *, code="tool_unavailable", details=None):
        super().__init__(message)
        if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid tool rejection code")
        self.code = code
        self.details = _diagnostic_details(details)


def is_number(value: object) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def check_schema(schema: dict, depth: int = 0) -> None:
    """显式拒绝未实现的规则，避免以为验证了实际上忽略的约束。"""
    allowed = {
        "type", "properties", "required", "additionalProperties", "items", "enum",
        "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
        "description", "title", "default",
    }
    if depth > 16 or not isinstance(schema, dict) or set(schema) - allowed:
        raise ValueError("unsupported tool schema")
    kind = schema.get("type")
    if kind not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise ValueError("schema requires a supported type")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError("invalid schema bound")
    for key in ("minimum", "maximum"):
        if key in schema and not is_number(schema[key]):
            raise ValueError("invalid numeric schema bound")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("invalid enum")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not all(isinstance(k, str) for k in properties):
            raise ValueError("invalid properties")
        if not isinstance(required, list) or any(not isinstance(k, str) for k in required):
            raise ValueError("invalid required fields")
        if set(required) - properties.keys() or len(set(required)) != len(required):
            raise ValueError("required field is not declared")
        if type(schema.get("additionalProperties", False)) is not bool:
            raise ValueError("additionalProperties must be boolean")
        for child in properties.values():
            check_schema(child, depth + 1)
    if kind == "array":
        check_schema(schema.get("items"), depth + 1)
    json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8")


def validate(value: Any, schema: dict, depth: int = 0) -> bool:
    if depth > 16:
        return False
    kind = schema["type"]
    if kind == "object":
        if type(value) is not dict or not all(isinstance(k, str) for k in value):
            return False
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            return False
        if not schema.get("additionalProperties", False) and set(value) - properties.keys():
            return False
        if not all(validate(v, properties[k], depth + 1) for k, v in value.items() if k in properties):
            return False
    elif kind == "array":
        if type(value) is not list or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            return False
        if not all(validate(item, schema["items"], depth + 1) for item in value):
            return False
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 262144):
            return False
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
    elif kind == "number":
        if not is_number(value):
            return False
    elif kind == "integer":
        if type(value) is not int:
            return False
    elif kind == "boolean":
        if type(value) is not bool:
            return False
    elif kind == "null" and value is not None:
        return False
    if kind in {"number", "integer"}:
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            return False
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        return False
    return True


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: tuple[str, ...]
    handler: Callable[..., Any]
    input_schema: dict | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    side_effect: bool = False
    requires_approval: bool = False
    # Optional benchmark declarations; they do not alter model-visible schemas,
    # permissions, approval decisions, or the Engine's recovery snapshot.
    implementation_version: str | None = None
    implementation_dependencies: tuple[Callable[..., Any], ...] = ()
    implementation_constants: dict[str, Any] = field(default_factory=dict)
    # Optional host binding for an external resource/configuration. It is part
    # of recovery checks, not model input or proof of the remote implementation.
    recovery_identity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool name must not be empty")
        if not isinstance(self.description, str) or not callable(self.handler):
            raise ValueError("tool requires description and callable handler")
        self.name.encode("utf-8")
        self.description.encode("utf-8")
        if not isinstance(self.parameters, tuple) or any(
            not isinstance(name, str) or not name.isidentifier() for name in self.parameters
        ) or len(set(self.parameters)) != len(self.parameters):
            raise ValueError("invalid parameters")
        if not isinstance(self.permissions, frozenset) or any(not isinstance(p, str) for p in self.permissions):
            raise ValueError("permissions must be a frozenset of strings")
        if type(self.side_effect) is not bool or type(self.requires_approval) is not bool:
            raise ValueError("tool policy flags must be boolean")
        if self.recovery_identity is not None:
            if (not isinstance(self.recovery_identity, str) or not self.recovery_identity.strip()
                    or len(self.recovery_identity) > 128):
                raise ValueError("invalid tool recovery identity")
            self.recovery_identity.encode("utf-8")
        if self.implementation_version is not None and (
                not isinstance(self.implementation_version, str) or not self.implementation_version.strip()
                or len(self.implementation_version) > 128):
            raise ValueError("invalid implementation version")
        if (not isinstance(self.implementation_dependencies, tuple)
                or not all(callable(item) for item in self.implementation_dependencies)):
            raise ValueError("implementation dependencies must be a tuple of callables")
        if not isinstance(self.implementation_constants, dict):
            raise ValueError("implementation constants must be an object")
        json.dumps(self.implementation_constants, ensure_ascii=False, allow_nan=False).encode("utf-8")
        object.__setattr__(self, "implementation_constants", deepcopy(self.implementation_constants))
        if self.input_schema is not None:
            check_schema(self.input_schema)
            if self.input_schema["type"] != "object":
                raise ValueError("tool input must be an object")
            object.__setattr__(self, "input_schema", deepcopy(self.input_schema))

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters, deepcopy(self.input_schema))


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self._frozen = False
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if self._frozen:
            raise ValueError("registry is frozen")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        if self._frozen:
            raise ValueError("registry is frozen")
        self._tools.pop(name, None)

    def freeze(self) -> None:
        self._frozen = True

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec() for tool in self._tools.values())


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def check(self, call: ToolCall, permissions: frozenset[str] = frozenset(), *, approved: bool = False) -> ToolResult | None:
        tool = self.registry.get(call.name)
        if tool is None:
            return ToolResult(call.call_id, call.name, error_code="unknown_tool")
        try:
            encoded = json.dumps(call.arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
            valid = len(encoded) <= 262144 and validate(call.arguments, tool.spec().schema())
        except (TypeError, ValueError, UnicodeError, RecursionError):
            valid = False
        if not valid:
            return ToolResult(call.call_id, call.name, error_code="invalid_arguments")
        if tool.permissions - permissions:
            return ToolResult(call.call_id, call.name, error_code="permission_denied")
        if tool.requires_approval and not approved:
            return ToolResult(call.call_id, call.name, error_code="approval_required")
        return None

    @staticmethod
    def result(call: ToolCall, tool: Tool, value: Any) -> ToolResult:
        try:
            if tool.input_schema is None and not is_number(value):
                raise ValueError("numeric result required")
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 262144:
                raise ValueError("result too large")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return ToolResult(call.call_id, call.name, error_code="invalid_tool_result")
        return ToolResult(call.call_id, call.name, value=json.loads(encoded))

    def execute(self, call: ToolCall, permissions: frozenset[str] = frozenset(), *, approved: bool = False) -> ToolResult:
        rejected = self.check(call, permissions, approved=approved)
        if rejected is not None:
            return rejected
        tool = self.registry.get(call.name)
        try:
            value = tool.handler(**deepcopy(call.arguments))
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                return ToolResult(call.call_id, call.name, error_code="async_tool_requires_engine")
            return self.result(call, tool, value)
        except ToolExecutionUnknown as error:
            return ToolResult(call.call_id, call.name, value=_diagnostic_details(error.details), error_code="execution_unknown")
        except ToolUnavailable as error:
            return ToolResult(call.call_id, call.name, value=_diagnostic_details(error.details), error_code=error.code)
        except Exception:
            return ToolResult(call.call_id, call.name, error_code="tool_error")

    async def aexecute(self, call: ToolCall, permissions: frozenset[str] = frozenset(), *, approved: bool = False) -> ToolResult:
        rejected = self.check(call, permissions, approved=approved)
        if rejected is not None:
            return rejected
        tool = self.registry.get(call.name)
        try:
            value = tool.handler(**deepcopy(call.arguments))
            if inspect.isawaitable(value):
                value = await value
            return self.result(call, tool, value)
        except ToolExecutionUnknown as error:
            return ToolResult(call.call_id, call.name, value=_diagnostic_details(error.details), error_code="execution_unknown")
        except ToolUnavailable as error:
            return ToolResult(call.call_id, call.name, value=_diagnostic_details(error.details), error_code=error.code)
        except Exception:
            return ToolResult(call.call_id, call.name, error_code="tool_error")
