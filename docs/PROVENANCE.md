# 来源与许可状态

[项目首页](../README.md) · [文档地图](README.md) · [副本范围](../HANDOFF.md)

本页说明公开源码的材料来源与许可边界。代码和文档含 AI 辅助工作；个人贡献应按实际修改、设计理解和实验分析表述，不将辅助产出全部说成独立完成。

## 材料范围

| 材料 | 当前说明 | 分发时的边界 |
| --- | --- | --- |
| Astra Harness 源码、文档与合成样例 | 来源于原项目本轮工作区整理，内部记为 S27；没有继承原项目 Git 历史或原始验收报告 | S27 是工作区来源标记，不是本仓库已有标签；初始提交从本次公开整理开始 |
| 设计参考 | [参考表](EVALUATION.md#设计参考与归属) 保留 LangGraph、OpenAI Agents 和 mini-swe-agent 的设计问题与取舍 | 项目记录声明借鉴思路，未复制所列项目的源码或图像；若以后实际复用材料，应记录文件、revision、修改和适用声明 |
| 模型与 MCP 协议 | 官方协议入口由 [架构文档](ARCHITECTURE.md#插件与适配) 维护 | 协议参考不等于获得其文档或素材的任意再分发许可 |
| 项目配图与界面截图 | 文档配图说明职责与流程；界面截图使用合成任务材料 | 截图只说明当时界面，不是模型成功率或最新全部路径验收的证明；本副本不附历史实验数据图 |
| 字体 | CSS／SVG 使用系统字体名和回退，不分发字体文件 | 后续引入字体或第三方素材时，保留其适用声明 |
| 软件依赖 | FastAPI、Pydantic、Uvicorn、Starlette、HTTPX 及传递依赖见锁文件 | 依赖保持各自发行包的许可；它们的许可证不能替代项目自身许可证 |

**项目所有者已选择 MIT 许可证**，完整条款与版权声明见 [LICENSE](../LICENSE)。这项选择来自所有者决定，不是从参考项目自动继承；第三方依赖及以后实际复用的材料仍需遵守各自适用的声明。

文件中未检出特定版权标记或 vendor 目录，并不能独立证明所有材料从未借用他人内容；现有项目说明也不替代完整的逐文件来源审查。

## 保留的参考入口

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 与 [上游 LICENSE](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)。
- [OpenAI Agents 审批设计](https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md) 与 [上游 LICENSE](https://github.com/openai/openai-agents-python/blob/main/LICENSE)。
- [mini-swe-agent README](https://github.com/SWE-agent/mini-swe-agent/blob/main/README.md) 与 [上游 LICENSE](https://github.com/SWE-agent/mini-swe-agent/blob/main/LICENSE.md)。

这些是已有项目记录中的出处；本次副本文档整理没有重新联网核验上游文件。后续复用第三方材料时，按实际内容核实许可与保留要求，不因只参考抽象设计思想而增加不相关的代码归属声明。

## 首次公开整理

0.4.0 源码已采用 MIT 公开至 [ytinyx/astra-harness](https://github.com/ytinyx/astra-harness)。仓库使用独立 Git 历史，初始提交从本次公开整理开始，未导入原项目的提交或内部阶段标签。

首次推送已完成；首轮 CI 失败及测试修正见 [当前交接](../HANDOFF.md)，后续状态以对应提交的 Actions 记录为准。源码公开不等于安装包发行。后续变更仍需核对文件清单、凭据排除、构建与材料范围；原项目的历史记录独立保留，没有因公开仓库不附带而改写或销毁。
