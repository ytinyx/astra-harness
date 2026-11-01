"""可持久化的单步状态机。存储与网页通过状态和事件接入，不进入内核。"""

import asyncio
import hashlib
import inspect
import json
import math
import queue
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Callable

from .contracts import Message, ModelRequest, ModelResponse, ToolCall, ToolResult
from .tools import Tool, ToolExecutor, ToolRegistry


TERMINAL = {"completed", "failed", "cancelled", "unknown"}
DEFAULT_LIMITS = {
    "max_model_calls": 12, "max_tool_calls": 24,
    "model_timeout": 60.0, "tool_timeout": 10.0, "run_timeout": 180.0,
}


def normalize_limits(overrides=None):
    """启动和创建运行共用校验；返回独立预算，继续运行不能重新领额度。"""
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("run limits must be an object")
    effective = dict(DEFAULT_LIMITS)
    overrides = {} if overrides is None else overrides
    if set(overrides) - effective.keys():
        raise ValueError("unknown run limit")
    effective.update(overrides)
    for key, value in effective.items():
        if key.startswith("max_"):
            if type(value) is not int or value < (0 if key == "max_tool_calls" else 1):
                raise ValueError("invalid call limit")
        else:
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("invalid time limit")
    return effective


async def _bounded_call(function, args, kwargs, timeout, cancelled, poll=None):
    """隔离等待而非隔离权限；超时的同步函数仍可能在后台执行。

    独立 daemon 线程避免 asyncio 默认线程池在退出时无限等待。异步处理器在
    该线程的事件循环内运行，取消尽力通知；副作用是否完成由上层报告 unknown。
    """
    if cancelled():
        return "cancelled_before_start", None
    mailbox = queue.Queue(maxsize=1)
    async_target = []
    stop_requested = threading.Event()

    async def await_value(value):
        async_target.append((asyncio.get_running_loop(), asyncio.current_task()))
        # 超时可能早于后台事件循环启动；保留通知，不能让迟到协程发起新请求。
        if stop_requested.is_set():
            if inspect.iscoroutine(value):
                value.close()
            raise asyncio.CancelledError
        return await value

    def work():
        try:
            value = function(*args, **kwargs)
            if inspect.isawaitable(value):
                value = asyncio.run(await_value(value))
            mailbox.put(("ok", value))
        except BaseException:
            mailbox.put(("error", None))

    threading.Thread(target=work, daemon=True, name="astra-call").start()
    deadline = time.monotonic() + timeout

    def notify_cancel():
        stop_requested.set()
        if async_target:
            loop, task = async_target[0]
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    try:
        while True:
            if cancelled():
                notify_cancel()
                return "cancelled", None
            if time.monotonic() >= deadline:
                notify_cancel()
                return "timeout", None
            if poll is not None:
                poll()
            try:
                return mailbox.get_nowait()
            except queue.Empty:
                await asyncio.sleep(min(0.01, max(0.001, deadline - time.monotonic())))
    except asyncio.CancelledError:
        notify_cancel()
        return "cancelled", None


class _StreamBuffer:
    """后台模型只入队；事件由 Engine 线程发出，结束后丢弃迟到增量。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._open = True
        self.bytes = 0
        self.count = 0
        self.error = False
        self.first_at = None

    def push(self, text):
        with self._lock:
            if not self._open:
                return False
            try:
                if not isinstance(text, str):
                    raise ValueError("invalid model delta")
                size = len(text.encode("utf-8"))
                if not size:
                    return True
                if self.bytes + size > 262144 or self.count >= 8192:
                    raise ValueError("model delta limit")
            except (ValueError, UnicodeError):
                self.error = True
                raise
            self.bytes += size
            self.count += 1
            if self.first_at is None:
                self.first_at = time.monotonic()
            self._queue.put_nowait(text)
            return True

    def drain(self, emit, limit=256):
        pieces = []
        for _ in range(limit):
            try:
                pieces.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if pieces:
            # 合并同一轮已到达的片段，避免每个 token 都触发一次 SQLite 提交。
            emit("".join(pieces))

    def close(self):
        with self._lock:
            self._open = False


class Engine:
    def __init__(self, model, tools: tuple[Tool, ...], *, system_instruction: str = "", emit=None):
        self.model = model
        # handler 可能绑定含锁/网络连接的对象，只复制定义，不能深拷贝业务资源。
        self.registry = ToolRegistry(replace(tool, input_schema=deepcopy(tool.input_schema)) for tool in tools)
        self.registry.freeze()
        self.executor = ToolExecutor(self.registry)
        self.system_instruction = system_instruction
        self.emit = emit

    def snapshot(self):
        return [
            {"name": tool.name, "description": tool.description,
             "schema": tool.spec().schema(), "permissions": sorted(tool.permissions),
               "side_effect": tool.side_effect, "requires_approval": tool.requires_approval,
               **({"recovery_identity": tool.recovery_identity} if tool.recovery_identity is not None else {})}
            for tool in sorted(self.registry.tools(), key=lambda item: item.name)
        ]

    @staticmethod
    def approval_key(call: dict) -> str:
        payload = {key: call[key] for key in ("call_id", "name", "arguments")}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def create(self, prompt: str, *, history=None, permissions=None, limits=None) -> dict:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty text")
        effective = normalize_limits(limits)
        permissions = permissions or []
        if not isinstance(permissions, (list, tuple, set, frozenset)) or any(not isinstance(p, str) for p in permissions):
            raise ValueError("permissions must contain strings")
        messages = []
        if self.system_instruction:
            messages.append(asdict(Message("system", text=self.system_instruction)))
        for item in history or []:
            if item.get("role") not in {"user", "assistant", "tool"}:
                raise ValueError("invalid history role")
            # 正规化且验证历史，避免把任意对象保存到检查点。
            message = self.message(item)
            messages.append(asdict(message))
        turn_start = len(messages)
        messages.append(asdict(Message("user", text=prompt)))
        state = {
            "id": uuid.uuid4().hex, "status": "pending", "phase": "model",
            "messages": messages, "turn_start": turn_start, "pending": [], "index": 0,
            "model_calls": 0, "tool_calls": 0, "elapsed": 0.0, "limits": effective,
            "permissions": sorted(set(permissions)), "tool_snapshot": self.snapshot(),
            "output": "", "error": None, "approval": None, "approved": [], "seen_ids": [],
            "pause_requested": False, "resume_status": None,
            "usage": {"reported_calls": 0, "input_tokens": 0, "output_tokens": 0,
                      "total_tokens": 0, "cached_input_tokens": 0, "cached_reported_calls": 0},
            "metrics": {"model_duration_ms": 0.0, "last_model": None},
        }
        json.dumps(state, allow_nan=False)
        return state

    def create_tool(self, name: str, arguments: dict, *, permissions=None, limits=None) -> dict:
        """Create an explicit host-requested tool run, without a model phase.

        The caller selects the operation, not its authority. The ordinary tool
        phase still checks the registered contract, permissions and approval.
        """
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or type(arguments) is not dict:
            raise ValueError("invalid tool operation")
        name.encode("utf-8")

        def json_keys(value, depth=0):
            if depth > 16:
                raise ValueError("tool arguments too deep")
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("tool argument keys must be strings")
                for item in value.values():
                    json_keys(item, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    json_keys(item, depth + 1)

        json_keys(arguments)
        encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 262144:
            raise ValueError("tool arguments too large")
        arguments = json.loads(encoded)
        state = self.create("工具操作：" + name, permissions=permissions, limits=limits)
        call = ToolCall(uuid.uuid4().hex, name, arguments)
        state.update(kind="tool", phase="tool", pending=[asdict(call)], seen_ids=[call.call_id],
                     operation={"name": name, "arguments": deepcopy(arguments)}, tool_result=None)
        state["messages"].append(asdict(Message("assistant", tool_calls=(call,))))
        return state

    @staticmethod
    def message(item):
        return Message(
            role=item["role"], text=item.get("text", ""),
            tool_calls=tuple(ToolCall(**call) for call in item.get("tool_calls", ())),
            tool_result=ToolResult(**item["tool_result"]) if item.get("tool_result") is not None else None,
        )

    def _event(self, state, kind, **details):
        if self.emit is not None:
            # 观察者失败不应中断或重试业务操作。
            try:
                self.emit({"type": kind, "run_id": state["id"], **details})
            except Exception:
                pass

    def _finish(self, state, status, error=None):
        state["status"] = status
        state["error"] = error
        state.update(pause_requested=False, resume_status=None)
        self._event(state, "run.end", status=status, error=error)
        return state

    async def advance(self, state: dict, *, cancelled: Callable[[], bool] | None = None) -> dict:
        cancelled = cancelled or (lambda: False)
        if state["status"] in TERMINAL:
            return state
        if cancelled():
            return self._finish(state, "cancelled", "cancelled")
        if state["tool_snapshot"] != self.snapshot():
            return self._finish(state, "failed", "tool_snapshot_changed")
        if state["status"] in {"waiting_approval", "paused"}:
            return state
        if state["elapsed"] >= state["limits"]["run_timeout"]:
            return self._finish(state, "failed", "run_timeout")
        state["status"] = "running"
        started = time.monotonic()
        try:
            if state["phase"] == "model":
                return await self._model(state, cancelled)
            if state["phase"] == "tool":
                return await self._tool(state, cancelled)
            return self._finish(state, "failed", "invalid_phase")
        finally:
            state["elapsed"] += time.monotonic() - started

    async def _invoke(self, state, kind, function, args, kwargs, cancelled, poll=None):
        remaining = state["limits"]["run_timeout"] - state["elapsed"]
        phase_budget = state["limits"][kind + "_timeout"]
        result, value = await _bounded_call(function, args, kwargs, min(remaining, phase_budget), cancelled, poll)
        if result == "timeout":
            result = "run_timeout" if remaining <= phase_budget else kind + "_timeout"
        return result, value

    async def _model(self, state, cancelled):
        if state["model_calls"] >= state["limits"]["max_model_calls"]:
            return self._finish(state, "failed", "model_limit")
        state["model_calls"] += 1
        request = ModelRequest(tuple(self.message(item) for item in deepcopy(state["messages"])), self.registry.specs())
        self._event(state, "model.start", step=state["model_calls"])
        started = time.monotonic()
        stream = getattr(self.model, "stream", None)
        buffer = _StreamBuffer()
        def emit_delta(text):
            if not cancelled():
                self._event(state, "model.delta", step=state["model_calls"], text=text)
        try:
            if callable(stream):
                outcome, response = await self._invoke(
                    state, "model", stream, (request, buffer.push), {}, cancelled,
                    poll=lambda: buffer.drain(emit_delta),
                )
            else:
                outcome, response = await self._invoke(state, "model", self.model.complete, (request,), {}, cancelled)
        finally:
            buffer.close()
        duration_ms = (time.monotonic() - started) * 1000
        if outcome == "ok" and not cancelled():
            buffer.drain(emit_delta, limit=8192)
        if outcome == "cancelled_before_start":
            return self._finish(state, "cancelled", "cancelled")
        if outcome != "ok":
            return self._finish(state, "cancelled" if outcome == "cancelled" else "failed",
                                "model_error" if outcome == "error" else outcome)
        # 非有限数值无法进入JSON检查点。整个参数对象变成必定被object规则拒绝
        # 的哨兵，保留call_id以便回填invalid_arguments，绝不替换单个值后执行。
        if isinstance(response, ModelResponse) and isinstance(response.tool_calls, tuple):
            normalized = []
            for call in response.tool_calls:
                if isinstance(call, ToolCall):
                    try:
                        json.dumps(call.arguments, allow_nan=False)
                    except (TypeError, ValueError, RecursionError):
                        call = replace(call, arguments=None)
                normalized.append(call)
            response = replace(response, tool_calls=tuple(normalized))
        if buffer.error or not self._valid_response(response, state["seen_ids"]):
            return self._finish(state, "failed", "protocol_error")
        if cancelled():
            return self._finish(state, "cancelled", "cancelled")
        calls = [asdict(call) for call in response.tool_calls]
        state["messages"].append(asdict(Message("assistant", response.text, response.tool_calls)))
        metrics = {"duration_ms": duration_ms,
                   "first_delta_ms": (buffer.first_at - started) * 1000 if buffer.first_at is not None else None,
                   "usage": deepcopy(response.usage)}
        totals = state.setdefault("usage", {"reported_calls": 0, "input_tokens": 0, "output_tokens": 0,
                                            "total_tokens": 0, "cached_input_tokens": 0, "cached_reported_calls": 0})
        if response.usage is not None:
            totals["reported_calls"] += 1
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                totals[key] += response.usage[key]
            if "cached_input_tokens" in response.usage:
                totals["cached_reported_calls"] += 1
                totals["cached_input_tokens"] += response.usage["cached_input_tokens"]
        state_metrics = state.setdefault("metrics", {"model_duration_ms": 0.0, "last_model": None})
        state_metrics["model_duration_ms"] += duration_ms
        state_metrics["last_model"] = metrics
        self._event(state, "model.end", step=state["model_calls"], text=response.text, tool_calls=calls, metrics=metrics)
        if cancelled():
            return self._finish(state, "cancelled", "cancelled")
        if not calls:
            state["output"] = response.text
            return self._finish(state, "completed")
        if state["tool_calls"] + len(calls) > state["limits"]["max_tool_calls"]:
            return self._finish(state, "failed", "tool_limit")
        state["seen_ids"].extend(call["call_id"] for call in calls)
        state["pending"] = calls
        state["index"] = 0
        state["phase"] = "tool"
        return state

    @staticmethod
    def _valid_response(response, seen_ids):
        if not isinstance(response, ModelResponse) or not isinstance(response.text, str):
            return False
        if not isinstance(response.tool_calls, tuple):
            return False
        if response.usage is not None:
            required = {"input_tokens", "output_tokens", "total_tokens"}
            if (not isinstance(response.usage, dict) or not required.issubset(response.usage)
                    or set(response.usage) - required - {"cached_input_tokens"}
                    or any(type(value) is not int or value < 0 for value in response.usage.values())):
                return False
            if (response.usage["total_tokens"] != response.usage["input_tokens"] + response.usage["output_tokens"]
                    or response.usage.get("cached_input_tokens", 0) > response.usage["input_tokens"]):
                return False
        seen = set(seen_ids)
        for call in response.tool_calls:
            if not isinstance(call, ToolCall) or not isinstance(call.call_id, str) or not call.call_id.strip():
                return False
            if not isinstance(call.name, str) or not call.name.strip() or call.call_id in seen:
                return False
            seen.add(call.call_id)
        try:
            encoded = json.dumps(asdict(response), ensure_ascii=False, allow_nan=False).encode("utf-8")
            return len(encoded) <= 262144
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return False

    async def _tool(self, state, cancelled):
        if state["index"] >= len(state["pending"]):
            state["phase"] = "model"
            return state
        data = state["pending"][state["index"]]
        call = ToolCall(**deepcopy(data))
        permissions = frozenset(state["permissions"])
        key = self.approval_key(data)
        approved = key in state["approved"]
        rejected = self.executor.check(call, permissions, approved=approved)
        if rejected is not None and rejected.error_code == "approval_required":
            state["status"] = "waiting_approval"
            state["approval"] = {**deepcopy(data), "key": key}
            self._event(state, "run.waiting", approval=state["approval"])
            return state
        if state["tool_calls"] >= state["limits"]["max_tool_calls"]:
            return self._finish(state, "failed", "tool_limit")
        state["tool_calls"] += 1
        tool = self.registry.get(call.name)
        self._event(state, "tool.start", call_id=call.call_id, name=call.name)
        if rejected is not None:
            result = rejected
        else:
            outcome, result = await self._invoke(
                state, "tool", self.executor.aexecute, (call, permissions), {"approved": approved}, cancelled,
            )
            if outcome == "cancelled_before_start":
                return self._finish(state, "cancelled", "cancelled")
            if outcome != "ok":
                reason = "tool_error" if outcome == "error" else outcome
                status = "unknown" if tool.side_effect else ("cancelled" if outcome == "cancelled" else "failed")
                return self._finish(state, status, reason)
            if result.error_code == "execution_unknown" or (
                tool.side_effect and result.error_code in {"tool_error", "invalid_tool_result"}
            ):
                # Keep observations for inspection while leaving the pending
                # call and cursor intact. Uncertainty is not a completed tool
                # message and must not invite a model continuation or replay.
                state["tool_result"] = asdict(result)
                self._event(state, "tool.end", result=asdict(result))
                return self._finish(state, "unknown", result.error_code)
        state["messages"].append(asdict(Message("tool", tool_result=result)))
        state["approval"] = None
        state["index"] += 1
        self._event(state, "tool.end", result=asdict(result))
        if state.get("kind") == "tool":
            # Preserve the actual outcome even if cancellation arrives just
            # after execution. A direct operation must never fall into _model.
            state["tool_result"] = asdict(result)
            state["output"] = json.dumps(result.value, ensure_ascii=False, allow_nan=False)
            state["pending"] = []
            state["index"] = 0
            return self._finish(state, "failed" if result.error_code else "completed", result.error_code)
        if cancelled():
            return self._finish(state, "cancelled", "cancelled")
        if state["index"] == len(state["pending"]):
            state["phase"] = "model"
            state["pending"] = []
            state["index"] = 0
        return state
