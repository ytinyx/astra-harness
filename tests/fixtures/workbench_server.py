"""浏览器验收专用本地模型替身：明确假用量，不连接供应商。

在项目根目录使用 python tests/fixtures/workbench_server.py，默认端口 8767。
"""

import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astra_harness.application import Application
from astra_harness.builtin.files import file_tools
from astra_harness.builtin.models import DemoModel
from astra_harness.cli import seed_workspace
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.web import serve


class BrowserFixtureModel(DemoModel):
    def stream(self, request, on_delta):
        prompt = next(item.text for item in reversed(request.messages) if item.role == "user")
        if "暂停验收" in prompt:
            if request.messages[-1].role == "tool":
                return ModelResponse(text="暂停验收完成：目录工具只执行了一次。")
            on_delta("正在准备读取目录；这段延迟用于检查运行中的暂停请求。")
            time.sleep(5)
            return ModelResponse(tool_calls=(ToolCall("pause-list", "files.list", {}),))
        if "界面验收" not in prompt:
            return self.complete(request)
        parts = ["## 本地流式验收\n\n", "这些文字来自**测试替身**，不代表真实模型。\n\n",
                 "- 增量一到达\n", "- 增量二到达\n\n",
                 "| 检查 | 结果 |\n| --- | --- |\n| Markdown | 可读 |\n\n",
                 "```python\nprint('Astra')\n```\n\n",
                 "<img src=x onerror=alert(1)>\n\n", "完成。"]
        for part in parts:
            if on_delta(part) is False:
                return ModelResponse(text="取消")
            time.sleep(0.5)
        # 用量字段也是fixture数据，服务运行在demo模式且文案明确标识。
        return ModelResponse(text="".join(parts), usage={"input_tokens": 100, "output_tokens": 80, "total_tokens": 180})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--root", default=".local/browser-fixture")
    args = parser.parse_args()
    root = Path(args.root)
    workspace = seed_workspace(root / "workspace")
    with Application(BrowserFixtureModel(), file_tools(workspace), database=root / "runs.sqlite3",
                     workspace=workspace, mode="demo", permissions=["files.read", "files.write"]) as app:
        serve(app, port=args.port)
