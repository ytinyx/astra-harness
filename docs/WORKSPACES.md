# 选择工作区与保持任务目录

[项目首页](../README.md) · [文档地图](README.md) · [验证状态](../HANDOFF.md)

工作区是后端允许文件工具访问的目录。在文件面板选择新的目录后，新对话使用新目录；已经提交的运行、会话历史和待审批操作仍绑定原目录。工具继续经过统一执行器、权限、审批和运行限制。

这是本地单用户功能。输入的是**运行 Astra 后端的电脑上已存在的绝对目录路径**，不是上传文件，也不是浏览器原生文件夹选择器。基础安装见 [新手入门](GETTING_STARTED.md)，运行与恢复边界见 [架构](ARCHITECTURE.md) 和 [安全说明](../SECURITY.md)。

## 先运行无密钥示例

在已安装依赖的项目根目录打开 PowerShell，执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m examples.workspace_demo
```

[示例源码](../examples/workspace_demo.py) 自建两个合成目录，通过真实 Application、文件工具和审批完成验证。预期 `passed: true`、`task_runs: 4`、`tool_calls: 3`、`model_calls: 0`、`provider_requests: 0`：三次获准写入完成，另一项因目录被替换而拒绝继续。默认退出后清理示例自己的临时目录，不读取用户材料或调用真实模型。

需要保留产物时，指定源码树以外的**全新目录**：

```powershell
$ProofDirectory = Join-Path $env:TEMP ("astra-workspace-proof-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -X utf8 -m examples.workspace_demo --output-dir "$ProofDirectory"
Get-Content -LiteralPath (Join-Path $ProofDirectory "summary.json") -Encoding UTF8
```

`summary.json` 记录实际文件文本与 SHA；`runs.json` 和 `trace.json` 保留运行及事件。可核对 A 的 `value.txt` 为 `A approved`、`after-restart.txt` 为 `Still A`，保留的原 B 的 `value.txt` 为 `B approved`；替换后的 B 没有 `must-not-exist.txt`。这验证普通目录替换与任务绑定，不是恶意并发换链实验。输出目录已存在时拒绝执行，不覆盖旧证据。运行记录含本地目录信息，分享前应脱敏。

## 准备网页练习目录

以下另建一组临时目录，避免复用自动示例中被故意替换的 B。仍在项目根目录的同一个 PowerShell 窗口执行：

```powershell
$PracticeDirectory = Join-Path $env:TEMP ("astra-workspaces-" + [guid]::NewGuid().ToString("N"))
$WorkspaceA = Join-Path $PracticeDirectory "workspace-A"
$WorkspaceB = Join-Path $PracticeDirectory "workspace-B"
$StateDirectory = Join-Path $PracticeDirectory "state"
$RunDatabase = Join-Path $StateDirectory "runs.sqlite3"
New-Item -ItemType Directory -Path $PracticeDirectory | Out-Null
New-Item -ItemType Directory -Path $WorkspaceA, $WorkspaceB, $StateDirectory | Out-Null
Set-Content -LiteralPath (Join-Path $WorkspaceA "value.txt") -Value "A original" -Encoding UTF8
Set-Content -LiteralPath (Join-Path $WorkspaceB "value.txt") -Value "B original" -Encoding UTF8
$WorkspaceB
.\.venv\Scripts\python.exe -X utf8 -m astra_harness serve `
  --workspace "$WorkspaceA" --database "$RunDatabase" --port 18770
```

打开终端显示的本地地址。A 是这份运行数据库的**启动工作区**，`state` 是独立的运行数据目录。命令没有 `--live`，使用固定规则 DemoModel；它支持这里的搜索练习，不代表任意任务理解能力。先在 A 提交“搜索 original”，应从 `value.txt` 读到 `A original`。

## 在页面选择 B

1. 找到文件面板顶部的当前目录，点击 **选择目录**。
2. 把刚才终端输出的 `$WorkspaceB` 路径粘贴到 **后端目录路径**，点击 **校验目录**。校验只读取路径和目录元数据，不枚举材料、读取正文、创建目录或调用模型。
3. 核对返回的规范化路径，再点击 **确认切换**。确认时会重新检查目录身份和当前选择版本；校验通过不等于已经切换。
4. 确认文件面板显示 B，开启新对话，提交“搜索 original”。结果应来自 B 的 `value.txt`，内容为 `B original`。

**取消** 关闭尚未提交的选择，不更改工作区；切换请求已经发出时不能用取消保证回滚。**最近已选目录** 是已登记目录的快捷入口，点击只填写路径，仍须重新校验和确认；它不是按最近访问时间排序的文件管理器。最多登记 32 个目录，当前没有删除登记记录的入口。

**刷新工作区** 从后端重新读取当前选择。如果切换响应丢失，按钮显示 **确认当前工作区**：先读取实际状态，不自动重发切换请求。多个页面发生选择冲突时，也要刷新后重新校验。

切换不会取消后台任务。页面清理旧文件与知识视图，按当前目录加载会话；运行详情和审批仍显示其绑定目录。若提交任务的响应尚未确认，页面暂时禁止切换、新建对话和修改输入；使用 **确认上次提交** 核对原请求，不把同一次提交改投另一个目录。

## 重开、旧任务与命令行

| 操作 | 使用的目录 |
| --- | --- |
| 网页选 B 后新建对话 | 当前选定的 B |
| A 中已提交运行的审批、预览、暂停后继续 | 原来的 A；切换不修改其参数、权限或预算 |
| 在 B 尝试继续 A 的会话 | 拒绝跨目录续聊；切回 A，或在 B 新建对话 |
| 用原启动参数重开 `serve` | 恢复数据库保存的网页选择 B |
| 同数据库运行 CLI `run` | 命令中原启动工作区 A，不跟随网页选择 B |
| CLI `inspect`／`approve`／`resume` 指定已有运行 | 按该运行保存的目录绑定处理 |

在服务终端按 Ctrl+C 停止，再执行原来的 `serve --workspace "$WorkspaceA" --database "$RunDatabase" --port 18770`，即可练习选择持久化。重启始终沿用**原启动目录、数据库和集成配置**，不要为了恢复 B 而把启动参数改成 B：同一数据库固定启动目录及身份，换启动目录会被拒绝。需要另一个独立启动入口时，使用新的运行数据库。

先停止网页服务，再运行 CLI；不要让两个进程争用同一运行数据库。例如，同一个 PowerShell 窗口中：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m astra_harness run "搜索 original" `
  --workspace "$WorkspaceA" --database "$RunDatabase"
```

这里应搜索 A，不会把已保存的网页选择改成 A。随后用原命令重开 `serve`，仍恢复 B。

## 目录变化与旧版本记录

路径相同不代表目录还是原来的目录。系统保存规范化路径和文件系统目录身份；目录被替换、丢失或暂时不可访问时，保留原选择并明确报错，**不会自动退回启动目录**。可以显式选择另一个有效目录继续新任务。旧目录上的非终态任务会按实际检查结果拒绝继续，不把原审批转给替换后的目录；同一路径的替代目录也不能继承旧登记身份。

升级前应在旧版本完成或取消尚未结束的任务。没有物理目录身份、也没有在途调用标记的旧非终态记录，会以 `workspace_identity_unavailable` 失败并保留证据；已有在途标记时优先沿用中断处理，副作用结果不明仍记为 `unknown`。已有终态记录不据此重写结果。旧会话没有目录绑定时可查看，但不能直接续聊，需要显式新建对话。程序不猜测旧目录身份、不自动重放任务，也不通过改数据库继承旧批准。

`unknown` 仍是整个 Application 的核查门禁。切换到另一个目录不会绕过未知副作用；核查流程见 [完整演示路线](DEMO.md)。

## 可选目录与扩展边界

选择器只接受已存在、可访问的本地目录。网络共享、映射网络盘、设备路径、相对路径、文件系统根目录、用户主目录、受保护的 Astra 源码／运行数据／配置／索引位置，以及与已有工作区互相嵌套的目录会被拒绝。默认启动专用目录仍可使用项目内的 `.local/workspace`，不表示可以把整个项目或数据父目录开放给选择器。

文件工具排除隐藏文件和常见凭据类名称，拒绝符号链接、Windows 重解析点及多硬链接普通文件；目录祖先链也检查链接。名称规则不能识别所有秘密内容，请只准备明确允许处理的材料。目录身份与路径检查适用于可信本机环境，不是 OS 沙箱，也不保证抵御恶意进程并发替换文件。

启动时配置的 [MCP](MCP.md)、[知识集合](KNOWLEDGE.md) 和 [embedding](EMBEDDING.md) 只装配在启动工作区 A。选择 B 后装配 `add`、普通文件和编辑／恢复工具，并按启动白名单装配可选命令；不会把 A 的材料、索引或外部服务自动迁到 B。编辑历史按工作区隔离，命令 cwd 和旧审批仍绑定原目录；切回 A 才显示其启动集成。已提交的 A 任务仍按 A 的工具集合执行。这是单用户运行范围绑定，不是多租户访问控制。

上述文件路径检查不限制已批准子进程的全部能力：允许的程序仍可按服务账号权限访问工作区外文件或网络。命令默认关闭、每次需要审批，详细启用方式及编辑恢复边界见 [基本工具指南](CORE_TOOLS.md)。普通目录选择不会提前生成备份或启动命令。

`--no-files` 禁止目录选择和编辑工具，也不能与命令白名单同用；选择操作不会擅自增加宿主未配置的权限。任务模型另有 [配置档案](MODEL_PROFILES.md)，模型选择与工作区绑定是两项独立约束，旧会话不能因任一选择变化而跨身份续聊。[MCP 档案](MCP_PROFILES.md) 的管理与动态工具也仅属于启动工作区，切换目录不迁移连接。本地 [Skill](SKILLS.md) 按当前工作区分别发现和批准，同名文件不共享启用身份，旧运行保留原快照。当前不提供文件上传或任意目录自动发现；范围见 [路线图](ROADMAP.md)。

## 常见提示

| 提示 | 处理方式 |
| --- | --- |
| `invalid_workspace` | 确认输入的是后端电脑上已存在的绝对本地目录，且没有链接或特殊路径语法。 |
| `protected_workspace`／`workspace_overlap` | 准备独立的材料子目录，与程序、运行数据及其他工作区分开。 |
| `workspace_conflict` | 其他页面已经改变选择；刷新当前工作区后重新校验。 |
| `workspace_changed`／`workspace_unavailable` | 核对原目录是否被替换或不可访问；保留旧记录，显式选择有效目录，不自动重试旧写入。 |
| `workspace_session_mismatch` | 会话属于另一个目录；切回原目录或新建对话。 |
| `workspace_identity_unavailable` | 无法确认目录身份，或旧记录缺少身份；使用有效目录新建任务，不手改检查点。 |
| `workspace_not_configured` | 当前目录没有该启动集成；切回启动工作区核对配置。 |
| `workspace_disabled` | 当前启动未启用文件能力；选择器不能提升权限。 |
