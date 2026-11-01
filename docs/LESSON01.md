# 第一课：一次真实的加法工具往返

[项目首页](../README.md) · [文档地图](README.md) · [验证状态](../HANDOFF.md)

先按 [新手教程](GETTING_STARTED.md) 安装项目依赖，再在项目根目录运行本课命令。本课缩小到一个加法工具，完整运行时见 [源码导读](WALKTHROUGH.md)。

目标：定位谁提出调用、谁检查、谁计算，以及计算结果怎样成为第二轮模型的输入。

## 先运行

先按 [新手入门](GETTING_STARTED.md) 完成项目安装，再在项目根目录打开终端：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m examples.lesson01
```

关键轨迹：

```text
[模型第 1 轮] 收到 1 条消息
[提出调用] id=call-1, tool=add
[Python 函数] add(a=2, b=3) → 5
[工具结果] id=call-1, value=5, error=None
[模型第 2 轮] 收到 3 条消息
[结束] reason=completed, model_calls=2, tool_calls=1
最终答复：2 + 3 = 5
```

实际输出还包含模型可见的参数规则。这里的模型是 `AdditionModel` 教学替身，固定提出 `2` 和 `3`，不会理解任意问题；Python 加法和结果回传是真实执行的。更改问题文本不会自动改变这两个参数。

## 从示例入口读起

打开 [lesson01.py](../examples/lesson01.py)，先看 `main()`：

```python
harness = Harness(
    model=AdditionModel(),
    tools=(Tool("add", "计算两个数的和", ("a", "b"), add),),
)
result = harness.run("请计算 2 + 3", trace=print)
```

`model=` 传入有 `complete()` 方法的模型对象。`tools=` 传入可用工具；`Tool` 中最后的 `add` 是函数对象，没有括号，此处只登记函数，还未计算。`trace=print` 让循环把过程输出到终端。

这就是显式注入：Harness 接收实现，核心无需导入某个计算器或供应商。

本课保留同步模型替身与同步 Harness 门面。网页现在使用 FastAPI/Uvicorn，CLI live 使用 httpx 异步适配器，但它们仍接入同一 Engine；本例无需启动 HTTP 服务或调用付费模型。

## 第一轮只提出请求

继续看 `AdditionModel.complete()`：

```python
return ModelResponse(tool_calls=(ToolCall("call-1", "add", {"a": 2, "b": 3}),))
```

`ToolCall` 是一份数据：编号是 `call-1`，工具名是 `add`，参数是 `a=2, b=3`。这行本身没有执行 `add()`。末尾逗号表示包含一个元素的 tuple。

模型收到的是 `ToolSpec`，包括名称、说明和参数规则，不含 Python 函数。Schema 供模型描述参数，执行器仍独立检查真实参数，不能仅依赖模型遵守说明。

## 找到真正计算的位置

[runtime.py](../astra_harness/runtime.py) 的同步 Runner 现在驱动同一个 [Engine.advance](../astra_harness/engine.py)。Engine 的工具阶段经有界调用进入 `ToolExecutor.aexecute()`。

在 [tools.py](../astra_harness/tools.py) 中，真正的执行点是：

```python
value = tool.handler(**deepcopy(call.arguments))
```

`handler` 就是登记时传入的 `add`；复制参数后用 `**` 展开成关键字参数，相当于 `add(a=2, b=3)`。在它之前，程序已检查工具名、参数、权限和审批；模型只提出请求。

## 第二轮多了哪两条消息

Runner 保留模型提出的调用，再把对应的结果加入消息：

| 顺序 | 消息 | 内容 |
| --- | --- | --- |
| 1 | user | 请计算 2 + 3 |
| 2 | assistant | 提出编号 call-1 的 add 调用 |
| 3 | tool | 编号 call-1 的执行结果 5 |

第二次 `complete()` 从最后一条消息读取 `tool_result.value`，据此生成答案。`call_id` 用来把结果和原调用配对，尤其在同一轮提出多个调用时，不能只靠工具名区分。

模型不再提出工具调用，Runner 返回 `reason="completed"`。模型和工具分别计数；`RunLimits(2, 1)` 恰好容纳本例。若模型预算只有一轮，加法仍可完成，但无法请求第二轮总结，运行以 `model_limit` 结束，不能视为正常完成。

## 验证与本课练习

[test_two_rounds_preserve_call_and_real_result](../tests/test_loop.py) 同时检查实际函数执行次数、第二轮消息、调用编号、结果和计数。其余测试检查未知工具、非法参数、异常及超限之后没有新调用。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

先用自己的话回答：为什么模型第一轮返回 `ToolCall` 后，还需要程序执行 `handler(**arguments)`？

理解这条主线后再做一个小练习：增加减法工具，把模型替身的调用改为 `subtract(a=7, b=2)`，并相应调整最终展示文本和配对检查，验证真实结果仍是 5。练习尚未预先实现，本课示例只实现加法。
