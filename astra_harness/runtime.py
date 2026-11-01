"""同步兼容门面：第一课和旧调用方复用同一个 Engine 状态机。"""

import asyncio

from .contracts import Model, RunLimits, RunResult, Trace
from .engine import Engine, TERMINAL
from .tools import Tool, ToolExecutor, ToolRegistry


class Runner:
    def __init__(self, model: Model, registry: ToolRegistry, executor: ToolExecutor) -> None:
        self.model, self.registry, self.executor = model, registry, executor
        self.engine = Engine(model, registry.tools())
        # 保留原构造接口的执行器注入，同时统一其注册集合并在运行前冻结。
        if executor.registry is not registry:
            raise ValueError("executor must use the same registry")
        registry.freeze()
        self.engine.registry, self.engine.executor = registry, executor

    def run(self, prompt: str, limits: RunLimits, trace: Trace) -> RunResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("sync run cannot execute inside an event loop; await Engine.advance() instead")

        state = self.engine.create(prompt, limits={
            "max_model_calls": limits.max_model_calls,
            "max_tool_calls": limits.max_tool_calls,
        })

        def emit(event):
            kind = event["type"]
            if kind == "model.start":
                trace(f"[模型第 {event['step']} 轮] 收到 {len(state['messages'])} 条消息")
            elif kind == "tool.start":
                trace(f"[提出调用] id={event['call_id']}, tool={event['name']}")
            elif kind == "tool.end":
                result = event["result"]
                trace(f"[工具结果] id={result['call_id']}, value={result['value']}, error={result['error_code']}")

        self.engine.emit = emit

        async def drive():
            while state["status"] not in TERMINAL | {"waiting_approval"}:
                await self.engine.advance(state)

        asyncio.run(drive())
        reason = state["error"] or state["status"]
        if state["status"] == "waiting_approval":
            reason = "approval_required"
        elif state["status"] == "unknown":
            reason = "execution_unknown"
        trace(f"[结束] reason={reason}, model_calls={state['model_calls']}, tool_calls={state['tool_calls']}")
        return RunResult(state["output"], reason, state["model_calls"], state["tool_calls"],
                         tuple(Engine.message(item) for item in state["messages"]))


class Harness:
    """小型同步调用入口。需要审批恢复、取消或异步集成时使用 Engine / Application。"""

    def __init__(self, model: Model, tools: tuple[Tool, ...] = ()) -> None:
        registry = ToolRegistry(tools)
        self.runner = Runner(model, registry, ToolExecutor(registry))

    def run(self, prompt: str, *, limits: RunLimits | None = None, trace: Trace | None = None) -> RunResult:
        return self.runner.run(prompt, limits or RunLimits(), trace or (lambda event: None))
