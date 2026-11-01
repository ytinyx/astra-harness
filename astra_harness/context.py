"""按完整历史轮次裁剪上下文；字符预算是估算，不是供应商 token 计数。"""

import json


def fit_state(state: dict, tools: list[dict], max_chars: int = 48_000) -> None:
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("invalid context budget")
    messages = state["messages"]
    # 最后一个 user 是当前任务；整个任务内的工具配对都必须保留。
    user_indices = [i for i, message in enumerate(messages) if message["role"] == "user"]
    if not user_indices:
        raise ValueError("missing user message")

    def size():
        return len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False))

    dropped = 0
    while size() > max_chars and len(user_indices) > 1:
        start, end = user_indices[:2]
        del messages[start:end]
        dropped += 1
        user_indices = [i for i, message in enumerate(messages) if message["role"] == "user"]
    state["context"] = {"estimated_chars": size(), "max_chars": max_chars,
                        "dropped_turns": state.get("context", {}).get("dropped_turns", 0) + dropped}
    state["turn_start"] = user_indices[-1]
    if size() > max_chars:
        # 当前问题、system 和当前工具结果无法安全删减时，模型请求不能发生。
        raise ValueError("context_limit")
