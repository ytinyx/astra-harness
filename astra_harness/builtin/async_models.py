"""HTTPX 异步 Chat Completions 适配；请求资源限于单次模型调用。"""

import asyncio
from contextlib import asynccontextmanager
import json

import httpx

from ..contracts import ModelRequest, ModelResponse
from .models import ModelAdapterError, OpenAICompatibleModel, _reject_constant, _unique_pairs


_MAX_LINE_BYTES = 262_144


class AsyncOpenAICompatibleModel(OpenAICompatibleModel):
    """保留已有模型身份接口；Engine 会等待这两个 async 方法。

    当前 Engine 在不同调用中创建不同事件循环，因此不跨调用共享连接池。
    类名与同步适配器不同，旧非终态运行仍由主机配置指纹拒绝误续接。
    """

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.deepseek.com",
                 timeout: float = 60, max_response_bytes: int = 2_000_000, *,
                 max_output_tokens: int | None = None) -> None:
        # NaN / infinity would make the byte comparison fail open.
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("invalid_model_configuration")
        super().__init__(api_key, model, base_url, timeout, max_response_bytes,
                         max_output_tokens=max_output_tokens)

    def _client(self):
        # 不隐式重试、跳转或使用环境代理；超时仍不代表供应商停止计费。
        transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        return httpx.AsyncClient(transport=transport, timeout=self.timeout,
                                 follow_redirects=False, trust_env=False)

    @asynccontextmanager
    async def _response(self, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "Accept-Encoding": "identity"}
        async with self._client() as client:
            async with client.stream("POST", self.url, content=body, headers=headers) as response:
                if not 200 <= response.status_code < 300:
                    raise ModelAdapterError("model_transport_error")
                # aiter_raw 避免解压发生在大小检查之前；不接收压缩响应。
                if response.headers.get("content-encoding", "").strip().lower() not in ("", "identity"):
                    raise ModelAdapterError("model_protocol_error")
                yield response

    async def _raw(self, response):
        consumed = 0
        # 不指定 chunk_size；小片段需要立即呈现，不能等待凑满固定块。
        async for chunk in response.aiter_raw():
            consumed += len(chunk)
            if consumed > self.max_response_bytes:
                raise ModelAdapterError("model_response_too_large")
            yield chunk

    async def _events(self, response):
        """按原始字节限额拆 SSE，支持跨块 UTF-8、LF、CRLF 与 CR。"""
        line, fields = bytearray(), []
        skip_lf, first_line = False, True
        async for chunk in self._raw(response):
            start = 0
            for index, value in enumerate(chunk):
                if skip_lf:
                    skip_lf = False
                    if value == 10:
                        start = index + 1
                        continue
                if value not in (10, 13):
                    continue
                if len(line) + index - start > _MAX_LINE_BYTES:
                    raise ModelAdapterError("model_response_too_large")
                line.extend(chunk[start:index])
                text = line.decode("utf-8")
                line.clear()
                if first_line:
                    text = text.removeprefix("\ufeff")
                    first_line = False
                start, skip_lf = index + 1, value == 13
                if not text:
                    if fields:
                        yield "\n".join(fields)
                        fields.clear()
                elif text.startswith("data:"):
                    value = text[5:]
                    fields.append(value[1:] if value.startswith(" ") else value)
                # SSE 注释、id 和其他字段不会成为模型内容。
            if len(line) + len(chunk) - start > _MAX_LINE_BYTES:
                raise ModelAdapterError("model_response_too_large")
            line.extend(chunk[start:])
        if line or fields:
            raise ModelAdapterError("model_protocol_error")

    @staticmethod
    def _json(raw):
        return json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """非流式入口同样有界读取；取消会退出响应和客户端上下文。"""
        try:
            payload, inverse = self._payload(request)
            raw = bytearray()
            async with self._response(payload) as response:
                async for chunk in self._raw(response):
                    raw.extend(chunk)
            data = self._json(raw)
            if not isinstance(data, dict) or "error" in data:
                raise ValueError("invalid completion")
            choices = data.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("invalid choices")
            choice = choices[0]
            finish, message = choice.get("finish_reason"), choice.get("message")
            if finish not in ("stop", "tool_calls") or not isinstance(message, dict):
                raise ValueError("incomplete completion")
            text = message.get("content")
            if text is None:
                text = ""
            if not isinstance(text, str):
                raise ValueError("invalid content")
            text.encode("utf-8")
            items = message.get("tool_calls")
            if items is None:
                items = []
            if not isinstance(items, list) or len(items) > 128:
                raise ValueError("invalid tool calls")
            if any(not isinstance(item, dict) or item.get("type", "function") != "function" for item in items):
                raise ValueError("invalid tool type")
            calls = self._calls(items, inverse)
            if (finish == "tool_calls") != bool(calls):
                raise ValueError("inconsistent completion")
            return ModelResponse(text=text, tool_calls=calls, usage=self._usage(data.get("usage")))
        except asyncio.CancelledError:
            raise
        except ModelAdapterError:
            raise
        except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
            raise ModelAdapterError("model_protocol_error") from None
        except Exception:
            raise ModelAdapterError("model_transport_error") from None

    async def stream(self, request: ModelRequest, on_delta) -> ModelResponse:
        """回调只接收完整文本片段；完整工具参数仅在正常结束后返回。"""
        text_parts, pending = [], {}
        finish, usage, done, usage_seen = None, None, False, False
        try:
            payload, inverse = self._payload(request, stream=True)
            async with self._response(payload) as response:
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "text/event-stream":
                    raise ValueError("expected SSE")
                async for event in self._events(response):
                    if event == "[DONE]":
                        done = True
                        break
                    chunk = self._json(event)
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise ValueError("invalid chunk")
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or len(choices) > 1:
                        raise ValueError("invalid choices")
                    if choices:
                        choice = choices[0]
                        if finish is not None or type(choice.get("index")) is not int or choice["index"] != 0:
                            raise ValueError("invalid choice")
                        delta = choice.get("delta")
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
                        items = delta.get("tool_calls")
                        if items is None:
                            items = []
                        if not isinstance(items, list):
                            raise ValueError("invalid tool fragments")
                        for item in items:
                            index = item["index"]
                            if type(index) is not int or not 0 <= index < 128 or item.get("type", "function") != "function":
                                raise ValueError("invalid tool index")
                            function = item.get("function")
                            if function is None:
                                function = {}
                            if not isinstance(function, dict):
                                raise ValueError("invalid tool function")
                            call = pending.setdefault(index, {"id": "", "function": {"name": "", "arguments": ""}})
                            for source, target, key in ((item, call, "id"), (function, call["function"], "name"),
                                                        (function, call["function"], "arguments")):
                                piece = source.get(key)
                                if piece is not None:
                                    if not isinstance(piece, str):
                                        raise ValueError("invalid tool fragment")
                                    target[key] += piece
                                    if len(target[key]) > (262144 if key == "arguments" else 1024):
                                        raise ValueError("tool fragment limit")
                        finish = choice.get("finish_reason")
                        if finish is not None and finish not in ("stop", "tool_calls"):
                            raise ValueError("incomplete completion")
                    elif chunk.get("usage") is None:
                        raise ValueError("empty chunk")
                    if chunk.get("usage") is not None:
                        # 缺失用量仍为 None；早期或重复统计不能冒充最终用量。
                        if finish is None or usage_seen:
                            raise ValueError("nonfinal usage")
                        usage, usage_seen = self._usage(chunk["usage"]), True
            if not done or finish is None or (finish == "tool_calls") != bool(pending):
                raise ValueError("incomplete stream")
            if sorted(pending) != list(range(len(pending))):
                raise ValueError("missing tool fragment")
            calls = self._calls([pending[index] for index in sorted(pending)], inverse)
            return ModelResponse(text="".join(text_parts), tool_calls=calls, usage=usage)
        except asyncio.CancelledError:
            raise
        except ModelAdapterError:
            raise
        except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
            raise ModelAdapterError("model_protocol_error") from None
        except Exception:
            raise ModelAdapterError("model_transport_error") from None
