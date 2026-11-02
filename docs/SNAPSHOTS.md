# 0.5.0 运行快照

[项目首页](../README.md) · [文档地图](README.md) · [验证状态](../HANDOFF.md)

以下图片于 2026-11-02 从 0.5.0 的本地工作台重新截取，使用独立数据库、固定规则模型与合成材料。11 张既有快照全部更新，另补充 3 张新功能快照。桌面截图使用浏览器实际视口，窄屏使用 390 × 844；工具结果截取页面局部，避开本机目录。

| 场景 | 图片 | 可核对的状态 |
| --- | --- | --- |
| 工作台总览 | [工作台](images/workbench-current.jpg) | 离线演示、新任务入口、知识库与配置导航、退出入口 |
| 文件写入审批 | [待审批](images/task-approval-current.jpg) | 拟写入 `todos.json` 的内容与批准／拒绝入口，尚未执行写入 |
| 工具返回 | [写入结果](images/tool-result-current.jpg) | 实际 `files.write` 返回 `todos.json`、558 字节 |
| 创建知识库 | [创建入口](images/knowledge-create-current.jpg) | 无启动参数时可填写名称并新建集合 |
| 上传材料 | [上传后](images/knowledge-upload-current.jpg) | 合成 `guide.md` 已保存，仍需提交导入审批 |
| 词法检索 | [桌面](images/knowledge-desktop-current.jpg) · [窄屏](images/knowledge-mobile-current.jpg) | 经审批导入后，`Silver` 返回原文片段与来源位置 |
| 模型配置 | [密钥入口](images/model-profiles-current.jpg) | 合成配置已保存，密钥不回显，任务仍使用离线启动配置 |
| MCP 配置 | [桌面](images/mcp-profiles-current.jpg) · [窄屏](images/mcp-services-mobile-current.jpg) | 本机 HTTP 示例已停用、0 个注册工具，没有建立连接 |
| 本地 Skill | [正文与启用](images/local-skills-current.jpg) | 仓库自带 `report-check` 当前文本版本已启用，未选给任务 |
| 可选 Embedding | [桌面](images/embedding-desktop-current.jpg) · [窄屏](images/embedding-mobile-current.jpg) | 未配置，不执行向量生成、连接测试或语义查询 |
| 退出服务 | [确认框](images/server-shutdown-current.jpg) | 说明后台退出与保留数据的边界；本轮随后实际确认并核对进程退出 |

这些图片只说明对应界面的实际状态，不证明真实模型任务成功率、服务连通性或语义检索质量。完整复现步骤见 [演示路线](DEMO.md)、[知识库](KNOWLEDGE.md) 和各配置指南；实际执行与未执行项见 [验证状态](../HANDOFF.md)。
