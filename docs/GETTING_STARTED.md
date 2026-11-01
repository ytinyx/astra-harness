# 新手上手：完成第一个文件任务

[项目首页](../README.md) · [文档地图](README.md) · [当前交接](../HANDOFF.md)

本教程面向第一次运行项目的人。你将启动本地工作台，让 Agent 读取三份虚构会议记录，在写入前亲自审批，再查看产物和执行过程。**使用全新演示数据库和固定规则模型，不需要 API Key，不调用外部模型。**

命令按 Windows PowerShell 编写，需要 Python 3.10+；前端直接由浏览器运行，不需要安装 Node。项目定位见 [README](../README.md)，完整命令行演示见 [演示路线](DEMO.md)。

## 1. 打开项目文件夹和终端

在文件资源管理器中打开项目文件夹，空白处右键选择“在终端中打开”。如果终端不是 PowerShell，在标签页菜单中选择 PowerShell。输入：

```powershell
Get-Location
Test-Path -LiteralPath .\README.md
Test-Path -LiteralPath .\astra_harness\__main__.py
python --version
```

两个 `Test-Path` 都应返回 `True`，Python 版本应为 3.10 或更高。若 `python` 不可用但 `py --version` 可用，安装工具时可用 `py` 替代；若两者都没有，先安装 Python 并重新打开终端。

如果当前目录不对，复制项目文件夹地址，执行 `Set-Location -LiteralPath "这里替换为项目文件夹地址"`；不要原样运行占位文字。下文命令始终在项目根目录执行。

## 2. 安装到项目自己的环境

先检查依赖管理工具：

```powershell
uv --version
```

若没有 uv，按 [官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装；已有 Python 和 pip 时也可运行 `python -m pip install uv`，然后重新打开终端。

执行以下命令，等待安装完成：

```powershell
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
uv sync --locked --inexact
$AstraPython = ".\.venv\Scripts\python.exe"
& $AstraPython --version
uv pip list --python $AstraPython
```

`uv sync` 根据 `uv.lock` 安装项目和依赖到 `.venv`，首次安装需要访问包索引。包列表应包含 astra-harness、fastapi、pydantic、uvicorn、starlette 和 httpx；`--inexact` 保留这个环境中已有的其他包。安装结束后，下面的演示可以离线运行。

直接调用 `.venv` 的解释器即可，**不需要激活环境或修改 PowerShell 执行策略**。`&` 用于执行变量里的程序路径；UTF-8 设置和变量只在当前终端有效，重新开终端后需要再次设置。

## 3. 启动一个独立的离线工作台

以下命令每次生成一组新的演示路径，避免混用已有材料、模型选择和运行记录：

```powershell
$AstraDemoRoot = Join-Path ".local/first-run" ([guid]::NewGuid().ToString("N"))
$AstraOptions = @(
    "--workspace", (Join-Path $AstraDemoRoot "workspace"),
    "--database", (Join-Path $AstraDemoRoot "runs.sqlite3")
)
& $AstraPython -X utf8 -m astra_harness doctor @AstraOptions
& $AstraPython -X utf8 -m astra_harness serve --demo-files @AstraOptions --port 18765
```

`doctor` 应输出 `"ok": true` 和 `"mode": "demo"`。`database_lock: not_checked` 是正常结果：它只检查配置和基本路径，不测试模型认证或数据库占用。

保持终端运行，在浏览器打开 **http://127.0.0.1:18765**。页面应显示 **离线演示**；`--demo-files` 会复制缺少的虚构会议记录，不覆盖已有文件。`$AstraDemoRoot` 保存了本次演示目录的位置，后续检查会继续使用它。

**为什么要新建数据库？** 工作台会恢复已经保存的模型档案选择。旧数据库即使启动时没有 `--live`，也可能仍选着外部模型。第一次练习使用上面生成的新路径；后续打开自己的数据库时，提交前先核对页面模型。

## 4. 让 Agent 整理待办，由你决定是否写入

输入：

> 整理会议待办并保存 todos.json

1. 查看右侧 **执行过程**。系统会列出文件、读取三份会议记录，再提出写入 `todos.json`。
2. 在 **执行前，请你确认** 卡片中核对拟写入内容。此时只是等待审批，全新演示目录里还没有 `todos.json`。
3. 可以先点击 **暂停**，再点击 **继续**。系统仍等待原审批，继续不会代你批准。
4. 点击 **批准并继续**。完成后在文件面板打开 `todos.json`，应有 **6 条待办**，每条包含 `task`、`owner`、`due`。

选择 **拒绝此次操作** 会结束这次运行，不执行该写入。想重新练习时新建对话；已有文件不会因为你练习而自动清除。

这里真正执行了文件工具、审批和持久化，模型本身是固定规则演示模型，只支持会议整理、两数加法和关键词搜索。它用于看清 Harness 的运行方式，不代表真实大语言模型的任意任务能力。

再提交“搜索 取消”，应看到来源文件、行号与文字。写入生成的 `todos.json` 也可能成为搜索命中，这属于当前工作区的实际内容。

## 5. 核对产物，重开后查看记录

回到启动服务的终端，按 **Ctrl+C**，等命令提示符回来。然后在同一个窗口读取刚才的实际文件：

```powershell
$TodoPath = Join-Path $AstraDemoRoot "workspace/todos.json"
Get-Content -LiteralPath $TodoPath -Encoding UTF8
$Todos = Get-Content -LiteralPath $TodoPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Todos.Count
```

如果批准了写入，最后一行应为 `6`。拒绝写入后找不到文件是预期结果。

重开同一工作台，检查之前的会话、工具结果和运行事件仍在：

```powershell
& $AstraPython -X utf8 -m astra_harness serve @AstraOptions --port 18765
```

这次不要重新生成 `$AstraDemoRoot`，否则会进入另一份数据库。网页服务运行时通过页面控制任务，不要另开 CLI 访问同一数据库。需要 CLI 的 `inspect`、`pause`、`resume`、`approve` 时先停止服务；完整步骤和独立产物核对见 [演示路线](DEMO.md)。

## 接下来按需要选择

先跑通上面一个闭环，再添加所需能力；不必一次配置全部集成。

| 想做什么 | 阅读入口 | 会发生什么 |
| --- | --- | --- |
| 理解一次工具调用 | [第一课](LESSON01.md) | 用加法示例看调用与结果配对。 |
| 讲解中断、恢复与 unknown | [演示路线](DEMO.md) | 用合成任务核对检查点和实际效果。 |
| 换自己的材料目录 | [工作区](WORKSPACES.md) | 显式校验并选择目录；旧任务仍绑定原目录。 |
| 精确编辑文件、运行获准程序 | [基本工具](CORE_TOOLS.md) | 编辑检查旧内容；命令默认关闭，每次仍需审批。 |
| 查询材料并核对来源 | [本地知识检索](KNOWLEDGE.md) | 默认词法检索；准备、导入和检索分别操作。 |
| 添加真实任务模型 | [模型档案](MODEL_PROFILES.md) | 保存配置不调用服务；测试或任务可能发送数据并计费。 |
| 添加 MCP 工具 | [MCP 档案](MCP_PROFILES.md) | 保存不启动；发现和启用必须明确确认。 |
| 使用语义检索 | [Embedding](EMBEDDING.md) | 可选服务，默认关闭；外传内容和费用需审批。 |
| 复用任务步骤或输出要求 | [本地 Skill](SKILLS.md) | 写文本指导，按任务选择；不会增加工具权限。 |
| 扩展 Python 工具或阅读实现 | [扩展指南](EXTENDING.md) · [源码导读](WALKTHROUGH.md) | 保持工具经过统一注册和执行器。 |

真实模型配置只支持已实现的协议。密钥留在后端环境，不填入网页字段、截图、聊天或 Git。原启动方式可显式使用 `--env-file .env`；不传该选项就不会自动读取文件，已有 `.env` 不要覆盖。配置、费用与兼容限制统一见 [模型档案](MODEL_PROFILES.md) 和 [安全边界](../SECURITY.md)。

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 找不到 `python` 或版本低于 3.10 | 检查 `py --version`；准备合适的 Python 后重开终端。 |
| `No module named astra_harness`、`fastapi` 或 `httpx` | 回到项目根目录运行 `uv sync --locked --inexact`，确认使用 `.venv` 的 Python。 |
| `Activate.ps1` 被阻止 | 不必激活，按本文直接调用 `Scripts/python.exe`。 |
| 新终端找不到变量 | 重新设置 `$AstraPython`；若要重开旧练习，请使用原工作区和数据库路径，不重新生成练习编号。 |
| 中文显示异常 | 先设置终端 UTF-8；命令保留 `-X utf8`，读取文件加 `-Encoding UTF8`。 |
| 页面打不开或端口占用 | 查看启动终端是否报错；可将命令中的端口和浏览器地址同时改为另一个本机端口。 |
| 数据库被占用 | 停止占用同一数据库的旧服务或 CLI；不要删除锁文件绕过检查。 |
| 暂停后继续仍等待批准 | 这是预期行为；继续恢复原审批，不代替批准。 |
| `configuration_changed`、`tool_snapshot_changed` | 核对原数据库、启动工作区、模型、工具和上下文配置，不修改检查点强行恢复。 |
| `workspace_changed`、`workspace_identity_unavailable` | 核对原目录身份；保留旧证据，用有效目录显式新建任务。详见 [工作区指南](WORKSPACES.md)。 |
| 运行变成 `unknown` | 先停止原服务并核查实际效果，重开后记录核查说明；不要盲目重试写入。详见 [演示路线](DEMO.md)。 |
| 任意提问只得到规则演示说明 | 当前模型只支持预设示例；真实模型需要另行配置和验证。 |

回归命令、实验协议与实际验证范围分别见 [开发指南](../CONTRIBUTING.md)、[评测文档](EVALUATION.md) 和 [交接记录](../HANDOFF.md)。本文讲使用步骤，历史测试通过不代表你当前环境已经完成验收。
