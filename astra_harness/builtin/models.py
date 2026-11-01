"""离线场景模型与 Chat Completions 协议适配；不在模块导入时读取凭据。"""

import json
import hashlib
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from ..contracts import ModelRequest, ModelResponse, ToolCall


class ModelAdapterError(RuntimeError):
    """错误只含固定代码，不含响应正文、请求头或供应商异常。"""


def _reject_constant(value):
    raise ValueError("non_finite_json")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class DemoModel:
    """固定规则演示，不是能理解任意任务的语言模型。"""

    def complete(self, request: ModelRequest) -> ModelResponse:
        # 仅使用最后一次用户提交之后的轨迹，兼容多轮会话和持久恢复。
        start = max(index for index, item in enumerate(request.messages) if item.role == "user")
        messages = request.messages[start:]
        prompt = messages[0].text
        results = [m.tool_result for m in messages if m.tool_result is not None]
        if results and results[-1].error_code:
            return ModelResponse(text=f"离线演示遇到工具错误：{results[-1].error_code}")
        names = {tool.name for tool in request.tools}
        search = re.fullmatch(r"搜索\s+(.+)", prompt.strip())
        if search:
            if "files.search" not in names:
                return ModelResponse(text="离线搜索演示需要开启文件工具。")
            query = search[1].strip()
            if not results:
                return ModelResponse(tool_calls=(ToolCall("demo-search", "files.search", {"query": query}),))
            result = results[-1].value
            matches = result["matches"]
            if not matches:
                return ModelResponse(text=f"关键词搜索没有找到包含“{query}”的文本；已检查 {result['scanned_files']} 个文件。")
            lines = [f"- {item['path']}:{item['line']} — {item['text']}" for item in matches]
            if result["truncated"]:
                lines.append("结果或扫描范围已达到限制，可以缩小关键词后再搜索。")
            return ModelResponse(text="关键词搜索结果（来自实际文件）：\n" + "\n".join(lines))
        if "会议" in prompt or "todo" in prompt.lower():
            if not {"files.list", "files.read", "files.write"}.issubset(names):
                return ModelResponse(text="离线会议演示需要开启文件工具。")
            listings = [r for r in results if r.name == "files.list"]
            if not listings:
                return ModelResponse(tool_calls=(ToolCall("demo-list", "files.list", {}),))
            paths = [p for p in listings[-1].value if p.endswith(".md")]
            reads = [r for r in results if r.name == "files.read"]
            if len(reads) < len(paths):
                index = len(reads)
                return ModelResponse(tool_calls=(ToolCall(f"demo-read-{index}", "files.read", {"path": paths[index]}),))
            if not any(r.name == "files.write" for r in results):
                todos = []
                for result in reads:
                    for task, owner, due in re.findall(
                        r"^- 事项：(.*?)；负责人：(.*?)；截止：(\d{4}-\d{2}-\d{2})\s*$", result.value, re.M
                    ):
                        todos.append({"task": task, "owner": owner, "due": due})
                if not todos:
                    return ModelResponse(text="离线演示未找到符合示例格式的待办。")
                return ModelResponse(tool_calls=(ToolCall("demo-write", "files.write", {
                    "path": "todos.json", "content": json.dumps(todos, ensure_ascii=False, indent=2) + "\n"
                }),))
            return ModelResponse(text="离线演示已读取会议记录，并按审批结果保存 todos.json。")
        if "add" in names and ("计算" in prompt or "+" in prompt):
            if results:
                return ModelResponse(text=f"离线计算结果：{results[-1].value}")
            numbers = re.findall(r"-?\d+(?:\.\d+)?", prompt)
            if len(numbers) == 2:
                return ModelResponse(tool_calls=(ToolCall("demo-add", "add", {
                    "a": float(numbers[0]), "b": float(numbers[1])
                }),))
        return ModelResponse(text="这是离线模型替身。可演示计算、“搜索 关键词”或“整理会议待办”；任意对话请配置真实模型。")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenAICompatibleModel:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.deepseek.com",
                 timeout: float = 60, max_response_bytes: int = 2_000_000, *,
                 max_output_tokens: int | None = None) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid_model_base_url")
        if not api_key or not model or not math.isfinite(timeout) or timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("invalid_model_configuration")
        if max_output_tokens is not None and (type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32768):
            raise ValueError("invalid_model_configuration")
        self.api_key, self.model = api_key, model
        # 恢复配置指纹读取同一规范化端点；以前只存 url 会漏掉端点变化。
        self.base_url = urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                                                parsed.path.rstrip("/"), "", ""))
        self.url = self.base_url + "/chat/completions"
        self.timeout, self.max_response_bytes = timeout, max_response_bytes
        self.max_output_tokens = max_output_tokens
        self.opener = urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("ASTRA_API_KEY", ""), os.environ.get("ASTRA_MODEL", ""),
                   os.environ.get("ASTRA_BASE_URL", "https://api.deepseek.com"))

    def _payload(self, request: ModelRequest, *, stream=False):
        # Chat Completions 工具名通常只允许字母、数字、下划线和横线。
        # 内部 files.read 等名称在传输边界映射，恢复时由同一工具快照重新生成。
        names = {
            tool.name: tool.name if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name)
            else "t_" + hashlib.sha256(tool.name.encode("utf-8")).hexdigest()[:40]
            for tool in request.tools
        }
        inverse_names = {value: key for key, value in names.items()}
        if len(inverse_names) != len(names):
            raise ModelAdapterError("tool_name_collision")
        messages = []
        for message in request.messages:
            if message.role == "tool":
                result = message.tool_result
                if result is None:
                    raise ModelAdapterError("invalid_model_request")
                messages.append({"role": "tool", "tool_call_id": result.call_id,
                                 "content": json.dumps({"value": result.value, "error_code": result.error_code}, ensure_ascii=False, allow_nan=False)})
            else:
                item = {"role": message.role, "content": message.text}
                if message.tool_calls:
                    item["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {
                        "name": names.get(call.name, call.name), "arguments": json.dumps(dict(call.arguments), allow_nan=False)
                    }} for call in message.tool_calls]
                messages.append(item)
        payload = {"model": self.model, "messages": messages, "stream": stream}
        if self.max_output_tokens is not None:
            payload["max_tokens"] = self.max_output_tokens
        if urllib.parse.urlsplit(self.base_url).hostname == "api.deepseek.com":
            # 当前契约仅保存可见文本和工具调用，不回传 reasoning_content；
            # DeepSeek 工具续轮因此显式使用非思考模式，其他兼容端点不附加此扩展。
            payload["thinking"] = {"type": "disabled"}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if request.tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": names[tool.name], "description": f"{tool.name}: {tool.description}", "parameters": tool.schema()
            }} for tool in request.tools]
        return payload, inverse_names

    def _request(self, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return urllib.request.Request(self.url, data=body, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"
        })

    @staticmethod
    def _usage(data):
        if data is None:
            return None
        usage = {"input_tokens": data["prompt_tokens"], "output_tokens": data["completion_tokens"],
                 "total_tokens": data["total_tokens"]}
        details = data.get("prompt_tokens_details") or {}
        cached = data.get("prompt_cache_hit_tokens", details.get("cached_tokens"))
        if cached is not None:
            usage["cached_input_tokens"] = cached
        if any(type(value) is not int or value < 0 for value in usage.values()):
            raise ValueError("invalid usage")
        if (usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
                or usage.get("cached_input_tokens", 0) > usage["input_tokens"]):
            raise ValueError("invalid usage")
        return usage

    @staticmethod
    def _calls(items, inverse_names):
        calls = []
        for call in items:
            arguments = json.loads(call["function"]["arguments"], parse_constant=_reject_constant,
                                   object_pairs_hook=_unique_pairs)
            name, identifier = call["function"]["name"], call["id"]
            if not isinstance(arguments, dict) or not isinstance(identifier, str) or not identifier.strip():
                raise ValueError("invalid call")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("invalid call")
            calls.append(ToolCall(identifier, inverse_names.get(name, name), arguments))
        if len({call.call_id for call in calls}) != len(calls):
            raise ValueError("duplicate call")
        return tuple(calls)

    def complete(self, request: ModelRequest) -> ModelResponse:
        """保留非流式公开入口，Engine 优先选择下方 stream。"""
        payload, inverse_names = self._payload(request)
        try:
            with self.opener.open(self._request(payload), timeout=self.timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise ModelAdapterError("model_response_too_large")
        except ModelAdapterError:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            raise ModelAdapterError("model_transport_error") from None
        except Exception:
            raise ModelAdapterError("model_transport_error") from None
        try:
            data = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
            choice = data["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ValueError("incomplete_response")
            message = choice["message"]
            text = message.get("content") or ""
            if not isinstance(text, str):
                raise ValueError("invalid_content")
            calls = self._calls(message.get("tool_calls") or [], inverse_names)
            return ModelResponse(text=text, tool_calls=calls, usage=self._usage(data.get("usage")))
        except Exception:
            raise ModelAdapterError("model_protocol_error") from None

    def _events(self, response):
        """逐行解帧，限制整个响应和单行；半条 SSE 不能当完整响应。"""
        consumed, fields = 0, []
        while True:
            line = response.readline(min(262145, self.max_response_bytes - consumed + 1))
            if not line:
                if fields:
                    raise ModelAdapterError("model_protocol_error")
                return
            consumed += len(line)
            if consumed > self.max_response_bytes or len(line) > 262144:
                raise ModelAdapterError("model_response_too_large")
            text = line.decode("utf-8").rstrip("\r\n")
            if not text:
                if fields:
                    yield "\n".join(fields)
                    fields = []
            elif text.startswith("data:"):
                value = text[5:]
                fields.append(value[1:] if value.startswith(" ") else value)
            # SSE 心跳、id 和其他字段不属于模型内容。

    def stream(self, request: ModelRequest, on_delta) -> ModelResponse:
        """只把文本片段交给展示；工具参数拼完并验证后才返回 ToolCall。"""
        payload, inverse_names = self._payload(request, stream=True)
        text_parts, pending = [], {}
        finish, usage, done = None, None, False
        try:
            with self.opener.open(self._request(payload), timeout=self.timeout) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise ValueError("expected SSE")
                for event in self._events(response):
                    if event == "[DONE]":
                        done = True
                        break
                    chunk = json.loads(event, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise ValueError("invalid chunk")
                    if chunk.get("usage") is not None:
                        usage = self._usage(chunk["usage"])
                    choices = chunk["choices"]
                    if not isinstance(choices, list) or len(choices) > 1:
                        raise ValueError("invalid choices")
                    if not choices:
                        if chunk.get("usage") is None:
                            raise ValueError("empty chunk")
                        continue
                    choice = choices[0]
                    if finish is not None or choice.get("index") != 0:
                        raise ValueError("invalid choice")
                    delta = choice["delta"]
                    if not isinstance(delta, dict):
                        raise ValueError("invalid delta")
                    text = delta.get("content")
                    if text is not None:
                        if not isinstance(text, str):
                            raise ValueError("invalid content")
                        text.encode("utf-8")
                        if text:
                            text_parts.append(text)
                            if on_delta(text) is False:
                                raise ModelAdapterError("model_stream_closed")
                    for item in delta.get("tool_calls") or []:
                        index = item["index"]
                        if type(index) is not int or not 0 <= index < 128 or item.get("type", "function") != "function":
                            raise ValueError("invalid tool index")
                        call = pending.setdefault(index, {"id": "", "function": {"name": "", "arguments": ""}})
                        for source, target, key in ((item, call, "id"), (item.get("function") or {}, call["function"], "name"),
                                                    (item.get("function") or {}, call["function"], "arguments")):
                            piece = source.get(key)
                            if piece is not None:
                                if not isinstance(piece, str):
                                    raise ValueError("invalid tool fragment")
                                target[key] += piece
                                if len(target[key]) > (262144 if key == "arguments" else 1024):
                                    raise ValueError("tool fragment limit")
                    finish = choice.get("finish_reason")
                    if finish is not None and finish not in {"stop", "tool_calls"}:
                        raise ValueError("incomplete completion")
            if not done or finish is None or (finish == "tool_calls") != bool(pending):
                raise ValueError("incomplete stream")
            if sorted(pending) != list(range(len(pending))):
                raise ValueError("missing tool fragment")
            calls = self._calls([pending[index] for index in sorted(pending)], inverse_names)
            return ModelResponse(text="".join(text_parts), tool_calls=calls, usage=usage)
        except ModelAdapterError:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            raise ModelAdapterError("model_transport_error") from None
        except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
            raise ModelAdapterError("model_protocol_error") from None
        except Exception:
            raise ModelAdapterError("model_transport_error") from None
