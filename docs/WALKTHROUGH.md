# 源码阅读路线

[项目首页](../README.md) · [文档地图](README.md) · [验证状态](../HANDOFF.md)

面向想理解或修改 Astra Harness 的开发者。先跟踪一个任务，再查看可选适配器；无需从所有模块或历史变更开始读。首次安装见 [新手入门](GETTING_STARTED.md)，运行语义见 [架构](ARCHITECTURE.md)，讲解顺序见 [演示路线](DEMO.md)。

## 从一条请求读起

以“计算 2 + 3”为起点：固定模型提出加法调用，执行器运行函数，真实结果回到模型，再保存任务终态。

| 顺序 | 先读哪个入口 | 需要找到的事实 |
| --- | --- | --- |
| 1. 装配 | [cli.py](../astra_harness/cli.py) `build_application` | 配置校验后选择模型、工具、工作区，注册资源清理，再启动 Application。 |
| 2. 接收目标 | [web.py](../astra_harness/web.py) 与 [application.py](../astra_harness/application.py) `submit` | HTTP 请求是入口；会话、请求去重和运行记录由 Application 管理。 |
| 3. 准备上下文 | [context.py](../astra_harness/context.py) | 裁剪完整旧轮次，保留当前任务与工具配对；当前内容仍超限时在请求模型前结束。 |
| 4. 推进一个阶段 | [engine.py](../astra_harness/engine.py) `advance` | 一次推进处理模型阶段或一个工具阶段；模型返回调用批次后不会绕过执行器。 |
| 5. 检查与执行 | [tools.py](../astra_harness/tools.py) `ToolExecutor` | Schema、权限和审批先检查；真正的业务操作发生在 handler。 |
| 6. 保存与展示 | [store.py](../astra_harness/store.py)、Application `_worker`、Web SSE | 检查点保存执行位置，事件展示过程；SSE 读取事件不执行工具。 |

再沿同一路径看“整理会议待办”，比较它为什么会停在 `waiting_approval`，以及批准后从哪里继续。阅读模型流式分支时留意：界面已有文本片段，不等于模型响应已经完整；工具参数必须完整解析、校验后才能执行。

### Runner、Worker 与 Engine 各做什么

`runtime.py` 中的 `Runner / Harness` 是同步兼容入口，复用同一 Engine，适合最小示例。持久任务由 `Application._worker` 的单个后台线程顺序推进；它管理检查点和生命周期，没有分布式任务队列。

FastAPI/Uvicorn 负责 HTTP 服务。Engine 在有界调用线程里等待同步处理函数，或运行异步协程；真实模型适配器使用 [HTTPX 异步实现](../astra_harness/builtin/async_models.py)。这里的异步模型 I/O、HTTP 并发和任务调度是三件事，同步工具也不会因此获得可强制终止能力。

## 可以演示的任务

使用 [快速开始](../README.md#快速开始) 中的全新离线工作台。已有数据库可能恢复保存的模型选择；提交前确认页面显示离线演示。需要单独的演示数据时，按 [DEMO](DEMO.md) 创建隔离目录，避免改动日常资料。

| 操作 | 观察什么 | 能说明什么 |
| --- | --- | --- |
| 发送“计算 2 + 3” | 一次加法工具调用、返回值 `5`、模型完成响应 | 模型提议与工具执行分开。 |
| 发送“搜索 取消” | 文件名、行号和命中文本 | `files.search` 做有界字面搜索，结果可回到原文核对。 |
| 发送“整理会议待办” | 读文件、拟写入内容、等待批准、`todos.json` | 多步骤执行和文件写入审批。 |
| 在审批处暂停，刷新后继续 | 同一运行 ID、原计数、原审批 | 暂停保留位置；继续不等于批准。 |
| 拒绝写入 | 任务结束、没有这次写入产生的文件 | 已拒绝调用没有执行；不是模型回答“我没有写”就算验证。 |

演示模型只识别固定任务模式。文件搜索不理解同义改写；本地知识索引和可选语义检索是另外的适配能力，分别见 [知识检索](KNOWLEDGE.md) 与 [Embedding](EMBEDDING.md)。

需要一个直接检查真实产物的源码示例，可运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m examples.extension_demo
.\.venv\Scripts\python.exe -X utf8 -m examples.extension_demo --deny
```

前者正常关闭并重开待审批任务后生成合成报告，后者验证拒绝。它们使用临时目录，不调用外部模型；输出判据见 [工具扩展指南](EXTENDING.md#再接一个需要审批的写工具)。

## 再读审批、暂停与恢复

按下面三个场景对照源码和测试，比只看状态名称更容易理解恢复语义。

| 场景 | 阅读位置 | 核对要点 |
| --- | --- | --- |
| 等待审批时重开 | `Application._recover / approve`；[审批测试](../tests/test_application.py) | 原调用参数、工具快照和配置仍匹配；批准不能迁移到另一调用。 |
| 调用中请求暂停 | `Application.pause / _worker / resume`；[暂停测试](../tests/test_pause_resume.py) | 当前调用结束并保存结果后停在边界；继续沿用原预算，不重新批准。 |
| 副作用结果不确定 | `Application._recover / resolve_unknown`；[恢复实验](EVALUATION.md) | 保留 `unknown` 和证据；人工核查只记录结论与解除阻断，不改写成成功或重放调用。 |

`paused` 是等待状态，会占用原会话；其他会话仍可执行。`cancelled` 表示运行停止，`unknown` 表示主机不能确认副作用结果。不要把“进程结束”“等待超时”“工具没有返回”当作没有发生写入的证明。

CLI 的 `pause RUN_ID`、`resume RUN_ID` 直接打开指定数据库，不能与已持有该库的网页服务同时使用。网页任务用页面按钮；跨重启继续需要原运行绑定仍可验证。`inspect` 和 SSE 回放用于查看记录，不启动执行。

## 按需走进扩展模块

| 能力 | 源码入口 | 重点 |
| --- | --- | --- |
| 函数工具 | [tooling.py](../astra_harness/tooling.py)、[plugins.py](../astra_harness/plugins.py) | 普通函数转换成 `Tool`，工具组注册与资源清理分开。 |
| 任务模型档案 | [model_profiles.py](../astra_harness/model_profiles.py)、[model_binding.py](../astra_harness/model_binding.py) | 选择影响新任务；既有会话和运行冻结模型配置。 |
| MCP | [mcp_profiles.py](../astra_harness/mcp_profiles.py)、[builtin/mcp_services.py](../astra_harness/builtin/mcp_services.py) | 配置保存不启动；发现的工具仍进入统一注册和执行路径。 |
| 工作区 | [workspaces.py](../astra_harness/workspaces.py)、[workspace_binding.py](../astra_harness/workspace_binding.py) | 选择目录不迁移旧任务；路径、目录身份和工具绑定一起检查。 |
| Skill | [skill_catalog.py](../astra_harness/skill_catalog.py)、[task_context.py](../astra_harness/task_context.py) | 按任务选择文本指导、冻结内容；不能增加权限或自动执行脚本。 |
| 检索与向量 | [builtin/knowledge.py](../astra_harness/builtin/knowledge.py)、[builtin/embeddings.py](../astra_harness/builtin/embeddings.py) | 默认词法检索；可选服务生成向量，来源与索引身份仍需核对。 |

修改前先查 [架构](ARCHITECTURE.md) 与对应使用指南。Engine 处理执行契约；供应商参数、目录装配和外部连接生命周期留在各自适配器，避免每接入一个服务就改循环分支。

## 用检查区分配置、运行与效果

`doctor` 只检查静态配置和基本路径条件，不发模型请求、不验证认证、数据库锁或 MCP 连通性。配置放在 [astra.example.json](../astra.example.json) 的同类 JSON 中；`.env` 仅在显式指定 `--env-file` 时读取。模型档案另用后端环境变量引用密钥，完整操作见 [模型配置](MODEL_PROFILES.md)。

在独立运行目录中，将配置的 `max_tool_calls` 设为 `0`，提交加法任务并观察 `tool_limit` 与零工具执行；将 `context_chars` 调小，观察模型请求前的 `context_limit`。这类练习核对运行限制，不证明真实模型的质量。

`eval` 使用固定产物判据检查文件与恢复流程；`benchmark` 使用任务集和独立预期。基线比较同时核对任务、模式、模型适配器、预算、上下文和工具身份，不能只比较一个通过率数字。真实供应商验收、离线故障实验与协议夹具分别记录，入口和可复现命令集中在 [评测指南](EVALUATION.md)。

## 阅读时要能解释的边界

- `ToolSpec` 是模型可见说明，`Tool` 含处理函数和宿主策略，`ToolCall` 是待校验数据；三者不是同一个对象。
- `implementation_version` 等 benchmark 声明与 `recovery_identity` 的用途不同。更换实现后不能仅凭相同函数名认定旧审批安全。
- 上下文预算按字符估计，不是供应商 tokenizer 的精确计费。没有 usage 表示未报告，不代表免费。
- 同步调用超时只结束等待；命令工具有自己的进程清理机制，但允许的程序仍拥有宿主账号能力，不是 OS 沙箱。
- SQLite 检查点和事件支持本机恢复，不提供多节点调度或任意工具的 exactly-once 保证。

当前执行证据看 [HANDOFF](../HANDOFF.md)，软件版本看 [版本说明](VERSION_TIMELINE.md)，实现来源与协作方式看 [PROVENANCE](PROVENANCE.md)。本页是阅读路线，不另复制测试成绩或供应商成功率。
