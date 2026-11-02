# 文档地图

[项目首页](../README.md) · [验证状态](../HANDOFF.md) · [版本与升级](VERSION_TIMELINE.md)

先完成一个任务，再按需要理解内核或接入能力。所有指南对应 **0.5.0**；下载固定版本见 [GitHub Release](https://github.com/ytinyx/astra-harness/releases/tag/v0.5.0)。

## 首次使用

| 文档 | 解决的问题 |
| --- | --- |
| [新手入门](GETTING_STARTED.md) | 如何安装、打开工作台并检查第一份产物 |
| [完整演示](DEMO.md) | 如何演示工具调用、审批、暂停与恢复 |
| [运行快照](SNAPSHOTS.md) | 0.5.0 桌面／窄屏界面，以及每张截图实际展示的状态 |
| [工作区](WORKSPACES.md) | 如何选择自己的目录，旧任务为什么不跟着切换 |
| [基本工具](CORE_TOOLS.md) | 文件读取、搜索、精确编辑与命令的用法和限制 |

## 理解运行内核

| 文档 | 解决的问题 |
| --- | --- |
| [架构与运行语义](ARCHITECTURE.md) | Application、Engine、执行器和 Store 如何配合 |
| [源码导读](WALKTHROUGH.md) | 从哪个函数跟进一次完整运行 |
| [设计决策](DECISIONS.md) | 为什么这样划分边界，有什么代价 |
| [路线图](ROADMAP.md) | 已交付范围与尚未验收的方向 |
| [第一课](LESSON01.md) | 用较小的教学示例理解工具调用循环 |

## 按需接入能力

| 文档 | 适用对象 |
| --- | --- |
| [扩展工具](EXTENDING.md) | 想用 Python 增加工具或适配器的开发者 |
| [模型档案](MODEL_PROFILES.md) | 想添加自己的 Chat Completions 服务的使用者 |
| [MCP 协议边界](MCP.md) · [MCP 配置](MCP_PROFILES.md) | 先理解支持范围，再显式添加、测试与启停服务 |
| [知识检索](KNOWLEDGE.md) | 准备本地材料与可核对引用 |
| [可选 Embedding](EMBEDDING.md) | 自行配置向量服务，并了解外传与计费边界 |
| [自定义 Skill](SKILLS.md) | 使用文本指导任务，而不增加执行权限 |

## 验证与维护

- [评测与实验复现](EVALUATION.md)：区分回归、故障实验、离线任务与真实模型验收。
- [真实模型验收准备](LIVE_ACCEPTANCE.md)：任务、预算、脱敏与中止条件，不会因阅读文档自动执行。
- [安装包验证](PACKAGING.md)：wheel、sdist 与离线安装检查。
- [贡献约定](../CONTRIBUTING.md) · [安全边界](../SECURITY.md)：修改、验证、凭据与权限。
- [变更日志](../CHANGELOG.md) · [版本与升级](VERSION_TIMELINE.md)：了解固定版本、开发分支和升级注意。
- [项目来源](PROVENANCE.md)：源码、材料与许可证状态。
- [验证状态](../HANDOFF.md)：自动化验收范围、证据入口与未验证的能力。

## 如何读实验记录

按评测入口生成自己的报告，保留输入、源码和工具身份、预算、实际环境、逐项结果及真实执行时间。预期输出只是判据，不是已通过的证据。比较两次结果前先核对条件；不把新执行记录写成过去阶段已经完成的测试。
