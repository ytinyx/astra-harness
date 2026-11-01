# 本地打包与安装验收

[项目首页](../README.md) · [文档地图](README.md) · [验证状态](../HANDOFF.md)

第一次体验项目，请从 [GitHub Releases](https://github.com/ytinyx/astra-harness/releases) 获取源码，按 [新手入门](GETTING_STARTED.md) 运行。应用需要 Python 3.10+ 和锁定的后端依赖。新手使用 `uv sync --locked --inexact`；打包是另一项开发验证，需要本机已有构建工具及预先下载的依赖 wheelhouse。

| 形式 | 包含内容 | 使用边界 |
| --- | --- | --- |
| 项目源码／sdist | 运行时代码、公开文档、样例、测试和实验脚本 | 在源码根目录运行 `python -m scripts...` 等开发入口 |
| wheel | `astra_harness` 运行时代码、网页静态资源、会议样例和默认任务集 | 安装后提供 `astra-harness` 与 `python -m astra_harness` |

`scripts.recovery_lab`、`scripts.task_recovery_lab`、`scripts.file_range_lab`、`scripts.tool_identity_lab` 和打包脚本不在 wheel 内。复现实验需使用源码目录或解包后的 sdist，不能把安装 wheel 当成安装了整套源码工具。

GitHub 自动生成的源码归档与本指南构建的 sdist 是两种文件：前者对应标签提交，后者按 Python 打包清单生成。归档不包含 `.git`，`git clone` 则保留公开提交历史。实验脚本在本地生成新报告；复现方法见 [评测说明](EVALUATION.md)。

## 在已有构建环境中执行

以下 PowerShell 命令均在项目根目录运行。`python` 应指向本机已具备 `build`、`setuptools`、`wheel`、`pip` 的解释器，构建驱动使用 Python 3.11+（读取标准 TOML）；可另指定 Python 3.10 验证安装兼容性。可先检查：

```powershell
python -X utf8 -c "from importlib.metadata import version; print({name: version(name) for name in ('build', 'setuptools', 'wheel', 'pip')})"
```

若缺少工具，先停止；本入口不会联网下载或自动补装构建依赖。解释器还需能够用自带的 `venv`／`ensurepip` 创建测试环境。

先单独准备依赖。下面只从官方 PyPI 下载锁定且经过 SHA-256 核验的 wheel；这一步需要联网，不安装到全局环境：

```powershell
python -m pip download --require-hashes --only-binary=:all: --index-url https://pypi.org/simple --dest .local/wheelhouse --requirement requirements.lock
```

wheel 与解释器、系统平台相关。若追加 Python 3.10 检查，也用该解释器执行同一下载命令，补齐所需 wheel。确认依赖准备完成后，运行不联网的构建与安装验收：

```powershell
python -X utf8 -m scripts.package_release --wheelhouse .local/wheelhouse --requirements-lock requirements.lock
```

每次创建新的 `.local/milestone5-package/package-*/` 目录，保留之前的结果，不覆盖已有 `dist/`。如需选择其他输出位置，可添加 `--output-dir`。退出码为 0 且输出 `"passed": true` 才表示本次流水线完成。

如果本机已经安装 Python 3.10 且 Windows `py` 启动器可用，可解析其实际解释器路径，并在同一轮追加基本安装检查：

```powershell
$AstraPython310 = & py -3.10 -c "import sys; print(sys.executable)"
if ($LASTEXITCODE -ne 0 -or -not $AstraPython310) { throw "未找到可用的 Python 3.10；不自动安装。" }
$AstraPython310 = $AstraPython310.Trim()
& $AstraPython310 -m pip download --require-hashes --only-binary=:all: --index-url https://pypi.org/simple --dest .local/wheelhouse --requirement requirements.lock
python -X utf8 -m scripts.package_release --wheelhouse .local/wheelhouse --requirements-lock requirements.lock --additional-python $AstraPython310
```

主解释器执行完整验收；附加解释器执行安装、导入、控制台入口、诊断、加法和错误提示检查。附加检查不等于又跑了一遍完整任务与网页验收。

## 流水线实际检查什么

[打包驱动](../scripts/package_release.py) 按 [MANIFEST.in](../MANIFEST.in) 的明确清单复制公开文件，在独立副本中执行 `python -m build --no-isolation`，生成 wheel 和真正的 `.tar.gz` sdist。它拒绝环境文件、数据库、本地产物和缓存；公开的 `.env.example` 模板除外。源码包经过成员与散列检查后才解包，再从解包结果构建 wheel，并实际运行其中的文件片段与工具身份实验，验证源码专用入口确实可用。

安装使用全新虚拟环境，先从核验后的本地 wheelhouse 按 `requirements.lock` 安装完整运行依赖，再安装项目 wheel。依赖安装启用 `--no-index --no-deps --require-hashes --only-binary=:all:`，随后执行 `pip check`，核对安装来源、版本与实际导入路径。`--no-deps` 在这里用于禁止递归解析或直接 URL 下载，不是省略锁定依赖。`--no-isolation` 使用本机已有构建工具；整个构建验收阶段不访问包索引。

[安装检查](../scripts/package_smoke.py) 在独立工作目录使用 `python -I`，核对实际导入位置及安装文件，另以子进程调用控制台入口和模块入口。完整验收包含：只读诊断、真实加法工具、会议任务审批、跨进程暂停／继续／批准、六项待办产物、九例 benchmark、离线 eval、网页资源和脱敏错误提示。网页检查只启动临时回环服务器，结束后关闭；不请求模型供应商。

## 查看本次证据

在命令输出的 `package-*` 目录中查看：

| 路径 | 证据 |
| --- | --- |
| `package-report.json` | 成功或失败阶段、源码文件 SHA-256、归档 SHA-256、工具版本与安装结果索引 |
| `artifacts/` | 本次 wheel 与 sdist；版本号相同也用散列区分具体文件 |
| `rebuilt/` | 从 sdist 重建的 wheel |
| `source/`、`sdist-source/` | 构建快照与经过检查的源码包内容 |
| `installed-0.json`、`installed-0.commands.json` | 主解释器安装检查及原始 CLI 输出；附加解释器使用后续编号 |
| `*.log`、`installed-run-*/` | 构建／安装日志及独立验收工作目录；失败记录也保留 |

`runtime_bytes_identical` 表示源码快照、wheel 和从 sdist 重建的 wheel 中运行时文件逐字节一致；安装检查还核对实际安装文件，并将每个安装环境计算的工具语义／说明摘要与源码比较；指定附加解释器后，才会形成跨解释器验证。它不表示两个归档文件必定位元相同：时间戳等构建元数据可能改变归档 SHA-256。不要把运行字节一致写成已经实现可重复构建。

本脚本只构建和验证本地包，不创建 GitHub Release，也不上传 PyPI。是否通过，以本次生成的 `package-report.json` 和日志为准；CI 的 wheel 构建通过不代表已执行这里完整的 sdist 重建与独立安装流程。当前验证范围见 [验证状态](../HANDOFF.md)，正式源码版本见 [GitHub Releases](https://github.com/ytinyx/astra-harness/releases)。
