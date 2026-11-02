# 开发与验收

[项目首页](README.md) · [文档地图](docs/README.md) · [验证状态](HANDOFF.md)

面向准备修改 0.5.0 源码、示例或文档的开发者。第一次使用先完成 [新手入门](docs/GETTING_STARTED.md)，理解执行主线后再看 [源码导读](docs/WALKTHROUGH.md) 和 [扩展契约](docs/EXTENDING.md)。

## 首次编码准备

从项目根目录安装锁定依赖，直接使用项目解释器：

```powershell
uv sync --locked --inexact
$AstraPython = ".\.venv\Scripts\python.exe"
& $AstraPython -X utf8 -m examples.lesson01
```

运行要求 Python 3.10+。Node 24 用于前端测试，应用启动不需要 Node 或前端打包器。后端接口采用 FastAPI／Pydantic／Uvicorn，模型 I/O 使用 HTTPX；核心执行模块不直接依赖这些适配器。默认回归使用合成数据和模型替身，可能启动自编本机服务或子进程，不调用真实供应商。

修改依赖声明后同步两个锁文件，并核对变化来源：

```powershell
uv lock
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.lock
```

只在确需修改依赖时执行锁文件更新；日常安装沿用 `--locked`，不把锁文件漂移作为环境问题的默认解决方法。

## 单次任务流程

1. 阅读 [验证状态](HANDOFF.md)，执行 `git status` 检查已有修改。使用下载的源码归档时，没有 `.git` 属于正常情况；需要贡献代码时请克隆仓库并创建分支。
2. 写清本次输入、行为、失败方式和验收标准，围绕一个可运行目标推进。
3. 阅读实际调用路径，修改正常流程与相关错误边界；工具仍通过注册表和执行器。
4. 按影响选择下表中的检查，核对实际调用或产物，不只看状态字样。
5. 更新受影响文档，在 HANDOFF 中区分本次执行、未执行项和下一步。
6. 按当次授权处理 Git、模型费用和发布；路线图本身不授权外部操作。

## 测试策略

以下命令在项目根目录的 PowerShell 中执行，沿用前面定义的 `$AstraPython`。选择与改动相称的检查；涉及核心执行语义或依赖的变更再运行完整回归。

| 修改内容 | 检查入口 | 重点观察 |
| --- | --- | --- |
| Python 核心或适配器 | 对应 `tests/test_*.py`，必要时完整 unittest | 处理函数是否执行、参数及错误、取消与预算边界。 |
| 前端状态或交互 | 全部 `tests/frontend/*.test.mjs`，再做浏览器走查 | 重复点击、迟到结果、错误、取消、焦点及窄屏。 |
| 文件任务与评测 | `eval`、`benchmark` 和对应实验脚本 | 原始产物及独立评分规则，不依赖模型自评。 |
| 包结构、资源或依赖 | [本地打包验收](docs/PACKAGING.md) | wheel／sdist 内容、独立安装、资源和入口。 |
| Markdown 文档 | `scripts.check_docs`，配图另做实际渲染检查 | 文件链接、标题锚点、措辞和实现状态。 |

```powershell
# 例：只验证一次工具循环相关改动
& $AstraPython -X utf8 -m unittest tests.test_engine tests.test_tooling -v

# 核心变化完成后的完整 Python 回归
& $AstraPython -X utf8 -m unittest discover -s tests -v

# 全部前端模块，不只运行早期的几个测试文件
$FrontendTests = (Get-ChildItem -LiteralPath tests/frontend -Filter *.test.mjs).FullName
node --test @FrontendTests

# Python 编译检查与文档检查
& $AstraPython -X utf8 -m compileall -q astra_harness examples tests scripts
& $AstraPython -X utf8 -m scripts.check_docs
```

编译检查不等同类型或风格检查，Node 测试也不能替代真实浏览器交互。回归的实际环境、数量和失败记录见 [HANDOFF](HANDOFF.md)，不要将旧结果计为本次已执行。

需要复现具体行为时，从这些源码入口中选用：

| 目标 | 命令入口 |
| --- | --- |
| 普通函数接入统一执行器 | `python -m examples.extension_demo` |
| 多服务发现、审批与恢复 | `python -m examples.mcp_services_demo` |
| 本地检索与来源核对 | `python -m examples.knowledge_demo` |
| 正常文件任务 | `python -m astra_harness eval` |
| 固定任务集评分与基线 | `python -m astra_harness benchmark` |
| 真实子进程退出窗口 | `python -m scripts.recovery_lab` |
| 任务级恢复策略对照 | `python -m scripts.task_recovery_lab` |
| 文件片段读取 | `python -m scripts.file_range_lab` |
| 工具身份与可比性 | `python -m scripts.tool_identity_lab` |

表中的 `python` 使用同一个项目解释器；PowerShell 可写为 `& $AstraPython -X utf8 -m ...`。实验在独立样例目录中进行，不导入个人工作区。实验协议、分母与局限见 [评测文档](docs/EVALUATION.md)。

| 验证方式 | 可以支持的结论 | 不能据此推断 |
| --- | --- | --- |
| 确定性模型替身 | 输入输出配对、循环、错误与预算行为 | 真实模型能理解并完成任意任务。 |
| 实际工具和产物检查 | 指定处理函数和文件效果符合预期 | 所有操作系统或外部服务均兼容。 |
| 真实模型联调 | 特定模型、配置和任务下的观测 | 普遍成功率、供应商可用性或公网安全。 |

例如，未知工具测试应确认处理函数未执行；预算测试应确认不再派发下一次请求；文件任务应读取实际 JSON 并核对字段。暂停、流式输出和取消测试优先使用同步信号控制阶段，不用打字动画或短暂等待代替协议证据。

修改工具实现时，还要核对 benchmark 的函数依赖、常量与显式版本声明。Schema 3 同时比较任务集、模式、配置和工具契约；模型部署及主机环境仍需另行记录，指纹匹配不意味着所有实验条件完全相同。

真实模型命令需要对应授权。`doctor --live --env-file .env` 仅做静态检查，不联网；live 任务与评测才会发送上下文并可能计费。密钥不进入源码、日志、报告或公开包。包构建的环境准备和离线验收分开，具体步骤见 [打包指南](docs/PACKAGING.md)。

仓库的 [CI 配置](.github/workflows/ci.yml) 执行测试、离线验收、文档检查与 wheel 构建，不上传包或部署。查看 [GitHub Actions](https://github.com/ytinyx/astra-harness/actions/workflows/ci.yml) 中与待验证提交对应的结果；本地测试和其他提交的成功不能替代它。

## 学习与代码讲解

每次修改应能解释：数据从哪里进入；模型、运行时和工具分别负责什么；失败怎样结束；哪个测试或产物支持结论。代码和证据对不上的能力，不写入项目介绍或面试材料。

## 文档检查

- 文件链接和标题锚点可定位；相同内容优先链接到负责该主题的指南。
- 当前实现、历史阶段和计划明确分开，规划中的文件名只写为代码文本。
- 检查重复标题、代码围栏、行尾空白和不应公开的路径或信息。
- SVG／流程图对应真实职责与状态；检查实际渲染、窄屏文字和浅深背景。
- 文档检查只证明文档通过，不能记为应用回归。

## 证据记录格式

公开 HANDOFF 按软件版本记录验证范围、结果、已知限制和后续检查。测试记录绑定实际提交、环境与执行时间；预期结果和历史结果不能替代本次执行。

公开报告使用相对路径，排除凭据、敏感请求正文、私人聊天及无关日志。发布前核对文件清单、版本元数据、标签与目标提交；已发布标签不移动，后续修订使用新的版本号。
