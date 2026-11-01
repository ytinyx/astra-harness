"""独立核验实际文件；使用显式预期值，不复用模型替身的抽取规则。"""

import json
from pathlib import Path
import tempfile

from .application import Application

EXPECTED = [
    {"task": "整理首版需求清单", "owner": "林舟", "due": "2026-11-05"},
    {"task": "完成交互原型", "owner": "陈澄", "due": "2026-11-07"},
    {"task": "补充接口错误码", "owner": "周宁", "due": "2026-11-06"},
    {"task": "验证任务取消行为", "owner": "吴桐", "due": "2026-11-08"},
    {"task": "编写安装说明", "owner": "许安", "due": "2026-11-09"},
    {"task": "检查演示数据", "owner": "沈禾", "due": "2026-11-10"},
]


def evaluate(output_dir=".local/evaluations"):
    from .builtin.files import file_tools
    from .builtin.models import DemoModel
    from .cli import seed_workspace

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    workspace = seed_workspace(directory / "workspace")
    database = directory / "runs.sqlite3"
    with Application(DemoModel(), file_tools(workspace), database=database,
                     workspace=workspace, permissions=["files.read", "files.write"]) as app:
        chat = app.wait(app.submit("你好")["id"])
        submitted = app.submit("整理会议待办并保存 todos.json", request_id="acceptance-file-task")
        waiting = app.wait(submitted["id"])
        no_write_before_approval = not (workspace / "todos.json").exists()
        if waiting["status"] != "waiting_approval":
            raise RuntimeError("acceptance expected approval")
    # 重开应用验证持久审批及剩余预算继承。
    with Application(DemoModel(), file_tools(workspace), database=database,
                     workspace=workspace, permissions=["files.read", "files.write"]) as app:
        before = app.get_run(submitted["id"])
        app.approve(submitted["id"])
        finished = app.wait(submitted["id"])
        actual = json.loads((workspace / "todos.json").read_text(encoding="utf-8"))
        artifact_matches = actual == EXPECTED
        replay = app.submit("整理会议待办并保存 todos.json", request_id="acceptance-file-task")
        report = {
            "passed": all([chat["status"] == "completed", chat["tool_calls"] == 0,
                           no_write_before_approval, before["status"] == "waiting_approval",
                           before["model_calls"] == waiting["model_calls"],
                           artifact_matches, finished["status"] == "completed",
                           replay["id"] == submitted["id"]]),
            "model": "deterministic-demo", "real_model_verified": False,
            "chat": {"model_calls": chat["model_calls"], "tool_calls": chat["tool_calls"]},
            "file_task": {"model_calls": finished["model_calls"], "tool_calls": finished["tool_calls"]},
            "no_write_before_approval": no_write_before_approval,
            "approval_survived_restart": before["status"] == "waiting_approval",
            "six_todos_match": artifact_matches, "idempotent_submission": replay["id"] == submitted["id"],
            "event_count": len(app.events(finished["id"])),
        }
    report["report"] = (directory / "report.json").relative_to(Path.cwd()).as_posix()
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
