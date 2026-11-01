"""运行：python -m examples.lesson01。固定模型决策，真实执行加法。"""

from astra_harness import Harness, ModelRequest, ModelResponse, Tool, ToolCall


def add(a: float, b: float) -> float:
    """真正的计算只发生在这里。"""
    print(f"[Python 函数] add(a={a}, b={b}) → {a + b}")
    return a + b


class AdditionModel:
    """教学替身：不理解自然语言，也不连接任何模型供应商。"""

    def complete(self, request: ModelRequest) -> ModelResponse:
        last_message = request.messages[-1]
        if last_message.role == "user":
            spec = request.tools[0]
            print(f"[模型可见的参数规则] {spec.schema()}")
            return ModelResponse(tool_calls=(ToolCall("call-1", "add", {"a": 2, "b": 3}),))

        # 第二轮读取程序回传的真实结果，而不是硬编码最终答案。
        result = last_message.tool_result
        if result is None or result.call_id != "call-1":
            raise ValueError("missing paired tool result")
        if result.error_code:
            return ModelResponse(text=f"工具执行失败：{result.error_code}")
        return ModelResponse(text=f"2 + 3 = {result.value}")


def main() -> None:
    harness = Harness(
        model=AdditionModel(),
        tools=(Tool("add", "计算两个数的和", ("a", "b"), add),),
    )
    result = harness.run("请计算 2 + 3", trace=print)
    print(f"最终答复：{result.output}")


if __name__ == "__main__":
    main()
