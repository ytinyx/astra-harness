# 来源与许可

[项目首页](../README.md) · [文档地图](README.md) · [验证状态](../HANDOFF.md)

项目采用 [MIT License](../LICENSE)，版权及完整条款以该文件为准。代码和文档含 AI 辅助工作；设计、修改与实验贡献应按实际参与情况表述。

## 材料范围

| 材料 | 来源与处理方式 |
| --- | --- |
| 项目源码、文档与合成样例 | 本项目维护；公开版本范围见 [变更日志](../CHANGELOG.md) |
| 设计参考 | 参考下列项目讨论的持久化、审批、追踪和分层问题，独立实现；项目记录未声明复制这些项目的源码或图像 |
| 模型与 MCP 协议 | 官方协议入口见 [架构文档](ARCHITECTURE.md#插件与适配)；协议支持范围按实际实现说明 |
| 配图与截图 | 项目机制图和使用合成任务材料的真实界面截图；截图不作为模型成功率证据 |
| 字体 | CSS／SVG 仅指定系统字体与回退，不分发字体文件 |
| 软件依赖 | FastAPI、Pydantic、Uvicorn、Starlette、HTTPX 及传递依赖见锁文件，适用各自发行包的许可证 |

## 设计参考入口

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 与 [上游 LICENSE](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)。
- [OpenAI Agents 审批设计](https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md) 与 [上游 LICENSE](https://github.com/openai/openai-agents-python/blob/main/LICENSE)。
- [mini-swe-agent README](https://github.com/SWE-agent/mini-swe-agent/blob/main/README.md) 与 [上游 LICENSE](https://github.com/SWE-agent/mini-swe-agent/blob/main/LICENSE.md)。

参考问题和本项目取舍见 [评测中的参考表](EVALUATION.md#设计参考与归属)。这些链接不表示项目依赖其运行框架，也不将上游许可自动套用于本项目。

## 后续复用材料

实际复用第三方代码、图片、字体或数据时，应记录材料、版本、修改和适用声明，并保留必要署名。参考抽象设计不等于复制材料；项目 MIT 也不能替代第三方材料自身的使用条件。公开目录检查不是完整的逐文件来源证明。
