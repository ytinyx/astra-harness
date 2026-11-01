# 精确编辑、恢复与受控命令

[项目首页](../README.md) · [文档地图](README.md) · [当前交接](../HANDOFF.md)

这组工具接在原有 Harness 执行路径上：`files.inspect` 检查文件摘要，`files.edit` 按条件修改，`files.restore` 按条件恢复；可选的 `commands.run` 执行宿主明确允许的程序。它们都注册为普通 Tool，使用同一执行器、权限、审批、运行预算和事件记录，不另建执行循环。

先完成 [新手安装](GETTING_STARTED.md)。目录选择与旧任务绑定见 [工作区指南](WORKSPACES.md)；这里的子进程不是文件系统或网络沙箱。

## 先跑完整离线示例

在已安装依赖的项目根目录，用 PowerShell 执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m examples.core_tools_demo
.\.venv\Scripts\python.exe -X utf8 -m examples.core_tools_demo --deny
```

[示例](../examples/core_tools_demo.py) 自建带中文和 CRLF 换行的 `settings.txt`，把 `retries=2` 精确改成 `retries=3`，再执行自编的本地 Python 验证程序，最后通过另一项审批恢复原始字节。固定脚本模型从真实工具结果取得 SHA，并检查命令返回；宿主只批准这组明确的合成操作，不访问用户材料或真实模型。

正常示例预期 `passed: true`、`task_model_calls: 4`、`task_tool_calls: 3`、`restore_tool_calls: 1`、`subprocess_invocations: 1`、`provider_requests: 0`。其中四次模型调用来自本地脚本替身，不是 API 请求。最终 `exact_original_restored: true`，`final_sha256` 等于 `initial_sha256`；审批在正常关闭重开后仍有效。`--deny` 拒绝编辑，原文不变，也不启动验证子进程；已经执行的只读 inspect 仍计为一次工具调用。

默认退出后清理示例临时目录。要保留证据，用全新目录：

```powershell
$CoreToolsProof = Join-Path $env:TEMP ("astra-core-tools-proof-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -X utf8 -m examples.core_tools_demo --output-dir "$CoreToolsProof"
Get-Content -LiteralPath (Join-Path $CoreToolsProof "summary.json") -Encoding UTF8
```

重复使用已有输出目录会被拒绝，不覆盖旧文件。`workspace` 包含合成输入和验证程序，`summary.json` 核对前后字节 SHA，`runs.json`／`trace.json` 保存真实运行，`state/file-history` 保存示例的恢复记录。示例属于源码和 sdist，不是安装 wheel 后自带的示例入口；打包范围见 [安装验收](PACKAGING.md)。

## 如何精确修改文件

启用文件能力的 CLI／网页宿主默认装配编辑工具，仍需明确批准每次写入。普通规则 DemoModel 的会议／搜索行为没有扩成任意编辑助手；完整离线入口是上面的专用示例。

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `files.inspect` | `path` | 读取已有普通 UTF-8 文件，返回原始字节 `sha256`、`bytes`、编码和相对路径，不返回正文；需要 `files.read`。 |
| `files.edit` | `path`、`old_text`、`new_text`、`expected_sha256` | SHA 必须匹配当前原始字节，非空旧文本必须恰好出现一次；需要文件读写权限及审批。 |
| `files.restore` | `backup_id`、`expected_sha256` | 从本工作区备份确定原路径，核对当前文件 SHA 后恢复；需要文件读写权限及另一项审批。 |

先用 `files.read`／`files.read_range` 取得需要修改的原文，再用 inspect 的实际 SHA 构造 edit。旧文本采用精确匹配，不是正则、模糊匹配或“全部替换”；重叠出现也算多处匹配。`new_text` 可以为空以删除匹配片段。替换部分以外的字节保持原样，包括 CRLF 和 UTF-8 BOM；新文本中的换行由调用者明确提供。文件和修改后结果均不超过 128,000 字节，拒绝无效 UTF-8 和 NUL。

审批卡展示当前内容与拟修改内容，但预览不是文件锁。执行时重新读取并核对 SHA；保存备份后、替换目标前还会再次检查。成功返回 `ok: true`、`applied: true`、`backup_id`、前后 SHA 和实际字节数。发现 SHA 过期、旧文本缺失、多处匹配或没有变化时拒绝，不擅自猜测目标位置。重新检查文件后，应提交新的调用及审批，不修改旧批准参数。

`files.write` 仍保留整文件写入行为，没有自动获得精确编辑的条件 SHA 或备份语义。需要条件修改与可追溯恢复时，明确使用 edit／restore。

## 备份保留与恢复

默认主机把备份放在运行数据库父目录下的 `file-history/<workspace_id>/`，与材料工作区分离。创建插件、检查文件和选择目录不会创建备份；只有批准后的 edit／restore 才先新增恢复记录，再替换目标。

每个工作区最多 100 条记录，每条保存不超过 128,000 字节的原内容。JSON 使用 Base64，磁盘占用还有编码和元数据开销，不能把原文总字节数当成完整磁盘上限。程序以新 ID 排他创建、不覆盖旧记录；这不是操作系统不可修改存储。备份含原文，Base64 不是加密，不应公开分享整份历史目录。

`backup_id` 是 64 位十六进制 SHA256，绑定含随机 nonce 的完整规范记录正文，包括原路径、工作区、操作类型与恢复字节。恢复时重新核对它，避免批准同一个 ID 后记录被改指向另一个文件。这不是签名或恶意本机用户防护，也不能把“备份存在”当作目标编辑已经成功的证据。

恢复要求原路径的文件**仍然存在**，且当前 SHA 与 `expected_sha256` 一致；它不重新创建已删除文件，也不能把 A 的备份恢复到 B。恢复前再备份当前内容，因此一次成功恢复也消耗一条记录。备份内容、工作区身份或记录结构不一致时拒绝恢复；没有自动撤销或后台回滚。

没有自动裁剪历史。遇到 `history_full` 时，先停止相关任务和服务，保留运行记录并人工检查、归档恢复记录，再决定哪些旧记录可移出当前历史目录。移出的 `backup_id` 不再能直接使用；不要改写 JSON 或把删除历史当作修复未知操作。损坏或不完整记录保留供检查，可能占用容量或阻止后续写入。

若保存备份后目标又发生变化，edit 可拒绝而保留该备份。`edit_rejected` 的 `details.reason` 表示具体原因，`applied: false` 表示未应用目标修改；有 `backup_id`／`backup_confirmed` 时应一并检查，不能据此声称整个操作没有产生任何文件。目标替换已发起而最终状态不明时进入 `unknown`，不自动恢复或重试。

## 显式启用本地命令

默认没有命令工具。只有可信宿主在启动时传入一个或多个 `--allow-command ABS_EXE`，才注册 `commands.run` 并授予 `commands.execute`；每次调用仍需审批。Windows 要求原生 `.exe` 的绝对路径，不按 PATH 查找程序。

以下仅演示把当前 Python 的基础解释器加入白名单。允许解释器就允许经批准的代码使用该账号的能力，先核对程序与每次 argv；不要将它理解成“只能处理工作区文件”。

```powershell
$AllowedPython = & .\.venv\Scripts\python.exe -X utf8 -c "import pathlib,sys; print(pathlib.Path(sys._base_executable).resolve())"
.\.venv\Scripts\python.exe -X utf8 -m astra_harness doctor --allow-command "$AllowedPython"
.\.venv\Scripts\python.exe -X utf8 -m astra_harness serve --allow-command "$AllowedPython"
```

`doctor` 只做静态配置和程序元数据检查，不运行该程序。全新数据库且未选择外部模型档案时，最后一行使用规则 DemoModel；已有数据库的 `serve` 会恢复保存的模型选择，提交前应核对当前模型。允许命令不让规则模型自动学会任意命令任务。需要复现 inspect → edit → command → restore 时运行专用示例，不必配置真实模型。不能与 `--no-files` 同用；`eval`／`benchmark` 明确拒绝 `--allow-command`，避免把隔离任务集误说成已覆盖自定义命令。恢复已有任务须沿用原启动参数和白名单。

工具 Schema 不会自动向模型列出白名单。实际任务需要明确提供允许使用的完整程序路径，不应让模型猜测或寻找系统程序；示例的脚本模型由宿主显式传入解释器路径，不能据此宣称真实模型已具备程序发现能力。

调用参数只有 `argv` 数组和可选 `cwd`。`argv[0]` 必须匹配白名单规范路径与登记的程序元数据；`cwd` 默认为 `.`，其他值必须是绑定工作区内已有普通目录的相对路径。没有 shell 字符串、用户自定义环境字段或自动插值；工具不代用户解释管道和重定向。若用户显式允许某个解释器或 shell，其自身仍可解释传入的代码，这不受文件工具路径检查约束。

| 默认 CLI 限制 | 行为 |
| --- | --- |
| 5 秒命令等待 | 超时进入结果不明处理并清理拥有的进程；外层 Engine 预算也可先到期。 |
| stdout 与 stderr 合计 16,384 字节 | 输出超限停止等待、触发清理并保留有界诊断；不是无限输出截断后继续执行。 |
| 最多 64 个 argv 元素，合计 UTF-8 大小不超过 8,192 字节（含元素分隔计数） | 超限在启动前拒绝。 |
| 最多 16 项启动白名单 | 只接受宿主明确提供的程序，不动态发现或按模型请求增加。 |

子进程不继承 PATH、用户目录、临时目录或模型密钥环境变量；Windows 仅保留必要的 `SystemRoot`／`WINDIR`。程序若依赖其他环境可能失败。stdout／stderr 按 UTF-8 展示，无效字节用替代字符；输出是程序数据，可能含敏感内容，不会因为环境已收窄就自动脱敏。超限时 `output_bytes_lower_bound` 表示计数只是下界，不是完整输出大小。

## 如何判断结果与取消

命令正常退出会返回 `exit_code`、`ok`、stdout／stderr、输出计数及 `cleanup_complete`。**直接工具运行的 `completed` 只说明调用有确定结果**：非零退出码对应 `ok: false`，页面会明确显示命令未成功；不能据此算任务成功，也不能推定没有副作用。还需检查实际产物，示例就另外核对了输出 JSON、文件字节和 SHA。

启动前拒绝返回 `command_rejected` 与固定原因，不启动目标命令。派发后超时、取消、输出超限、仍有后台进程或清理未确认，都可能成为 `unknown`；不自动重试。Windows 使用自有可信启动器，先加入 kill-on-close Job 再释放 argv；POSIX 使用自有进程组。平台行为需要分别验证，不能由 Windows 示例推断 Linux 兼容性。清理只针对本次拥有的资源，不撤销已经写入的文件或远端效果。

外层 Engine 取消可能先返回 unknown，异步清理随后完成；主机关闭还会显式清理其命令资源。看到取消或超时不能立刻推断进程已全部退出，更不能跳过原 unknown 人工核查门禁。具备相应能力的程序仍可能访问工作区外文件或网络；这些措施不是 OS 沙箱，也不承诺抵御恶意程序或恶意并发换链。

Windows 启动器保留退出码 `253` 表示无法确认派发结果。目标程序自身返回同一个码时也保守记为 `unknown`，不会猜测为普通失败或自动重试；这是当前简化启动协议的明确限制。

## 工作区与其他能力

文件编辑和可选命令按宿主策略为每个工作区装配，备份与命令 cwd 绑定原目录；切换到 B 不改变 A 的旧审批、工具和权限。选择、inspect 和审批预览不提前创建备份或启动命令。MCP、知识库和 embedding 仍只在启动工作区装配。

需要用真实模型提出这些调用，见 [模型档案](MODEL_PROFILES.md)；接入外部工具见 [MCP 档案](MCP_PROFILES.md)；复用任务步骤见 [本地 Skill](SKILLS.md)。这些能力都保留同一执行器的权限与审批。源码和设计取舍见 [架构](ARCHITECTURE.md)、[决策](DECISIONS.md)，验证方法见 [评测说明](EVALUATION.md)。
