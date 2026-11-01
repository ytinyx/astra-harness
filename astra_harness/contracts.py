"""模型与程序交换的数据；这里不执行模型请求或业务函数。"""

from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    value: Any = None
    error_code: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    """模型只能看到说明和参数规则，看不到 Python 处理函数。"""

    name: str
    description: str
    parameters: tuple[str, ...]
    input_schema: dict[str, Any] | None = None

    def schema(self) -> dict[str, Any]:
        if self.input_schema is not None:
            return deepcopy(self.input_schema)
        # 保留第一课的数值参数简写；扩展工具使用显式 Schema 子集。
        return {
            "type": "object",
            "properties": {name: {"type": "number"} for name in self.parameters},
            "required": list(self.parameters),
            "additionalProperties": False,
        }


@dataclass(frozen=True)
class Message:
    role: str
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_result: ToolResult | None = None


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    # None 表示供应商没有报告；不能把缺失用量当作免费或零 token。
    usage: dict[str, int] | None = None


class Model(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True)
class RunLimits:
    max_model_calls: int = 4
    max_tool_calls: int = 4

    def __post_init__(self) -> None:
        for name, minimum in (("max_model_calls", 1), ("max_tool_calls", 0)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class RunResult:
    output: str
    reason: str
    model_calls: int
    tool_calls: int
    messages: tuple[Message, ...]


Trace = Callable[[str], None]
