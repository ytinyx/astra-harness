"""One offline file task comparing full reads with bounded, cited fragments.

The fixed model consumes real tool results. It never calls the independent
answer oracle; this is a runtime mechanism demonstration, not an LLM benchmark.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile

from astra_harness.application import Application
from astra_harness.builtin.files import FilePlugin
from astra_harness.contracts import ModelResponse, ToolCall


STRATEGIES = ("full_read", "range_read")
INPUT_PATH = "records/project-notes.txt"
QUERY = "ASTRA-RANGE-TARGET-2027"
PROMPT = f"Find {QUERY}; extract project, owner, due and integer quota, with source path and line numbers."
CONTEXT_CHARS = 48_000
LIMITS = {"max_model_calls": 4, "max_tool_calls": 3,
          "model_timeout": 10, "tool_timeout": 10, "run_timeout": 30}
SOURCE_FILES = ("scripts/file_range_lab.py", "astra_harness/builtin/files.py",
                "astra_harness/application.py", "astra_harness/engine.py", "astra_harness/store.py",
                "astra_harness/tools.py", "astra_harness/contracts.py", "astra_harness/context.py",
                "astra_harness/process_state.py")


def _serialized(value):
    # Same JSON character convention as context.fit_state, including spaces.
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write_json(path, value):
    Path(path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def prepare_workspace(workspace):
    """Create a synthetic UTF-8 source; never replace an existing workspace."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / "records").mkdir()
    background = [f"Row {number:04d}: " + "routine background content; " * 2 + "\n"
                  for number in range(1, 901)]
    # ASCII filler deliberately makes character count exceed 48,000 too:
    # a 60 KB Chinese file alone need not exceed a 48,000-character budget.
    target = [QUERY + "\n", "project: Astra field extraction\n", "owner: 林舟\n",
              "due: 2027-05-20\n", "quota: 17\n"]
    data = "".join(background[:450] + target + background[450:]).encode("utf-8")
    (workspace / INPUT_PATH).write_bytes(data)
    return data


class FileTaskModel:
    """Both strategies share this fixed plan; only the selected read differs."""

    def __init__(self, strategy):
        if strategy not in STRATEGIES:
            raise ValueError("invalid strategy")
        self.strategy = strategy
        self.observed_requests = []

    def complete(self, request):
        results = [message.tool_result for message in request.messages if message.tool_result is not None]
        self.observed_requests.append([result.name for result in results])
        if not results:
            return ModelResponse(tool_calls=(ToolCall("search-1", "files.search", {"query": QUERY}),))
        if any(result.error_code for result in results):
            raise ValueError("tool execution failed")
        search = results[0]
        if search.name != "files.search" or search.value.get("truncated"):
            raise ValueError("incomplete search")
        matches = search.value["matches"]
        if len(matches) != 1:
            raise ValueError("expected one located section")
        hit = matches[0]
        if len(results) == 1:
            name = "files.read" if self.strategy == "full_read" else "files.read_range"
            arguments = {"path": hit["path"]}
            if name == "files.read_range":
                arguments.update(start_line=hit["line"], start_column=1, line_count=5)
            return ModelResponse(tool_calls=(ToolCall("read-1", name, arguments),))
        if len(results) != 2:
            raise ValueError("unexpected tool result sequence")
        read = results[1]
        if read.name == "files.read":
            lines = list(enumerate(read.value.splitlines(), 1))
        elif read.name == "files.read_range":
            value = read.value
            if value.get("ok") is not True or value.get("path") != hit["path"]:
                raise ValueError("range read failed")
            fragments = value["fragments"]
            if any(part["partial"] or part["start_column"] != 1 for part in fragments):
                raise ValueError("task fields require complete lines")
            lines = [(part["line"], part["text"]) for part in fragments]
        else:
            raise ValueError("unexpected read tool")
        source = {number: text for number, text in lines}
        if source.get(hit["line"]) != QUERY:
            raise ValueError("source anchor missing")
        fields = {}
        for number in range(hit["line"] + 1, hit["line"] + 5):
            key, separator, value = source[number].partition(": ")
            if not separator or key in fields or key not in {"project", "owner", "due", "quota"}:
                raise ValueError("invalid source field")
            fields[key] = {"value": int(value) if key == "quota" else value, "line": number}
        answer = {"source": {"path": hit["path"], "anchor_line": hit["line"]}, "fields": fields}
        return ModelResponse(text=_serialized(answer))


def score_output(output):
    """Independent literal answer, including JSON types and exact citations."""
    expected = {
        "source": {"path": "records/project-notes.txt", "anchor_line": 451},
        "fields": {
            "project": {"value": "Astra field extraction", "line": 452},
            "owner": {"value": "林舟", "line": 453},
            "due": {"value": "2027-05-20", "line": 454},
            "quota": {"value": 17, "line": 455},
        },
    }
    try:
        parsed = json.loads(output)
        # Canonical JSON distinguishes true/1 and 17/17.0, unlike Python ==.
        correct = json.dumps(parsed, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
            expected, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        parsed, correct = None, False
    return {"output_json": parsed, "output_correct": correct}


def run_strategy(strategy, workspace, row_dir, suite_dir):
    """Run one real Application with retained database, state and tool results."""
    row_dir, suite_dir, workspace = Path(row_dir), Path(suite_dir), Path(workspace)
    row_dir.mkdir(parents=True, exist_ok=False)
    model = FileTaskModel(strategy)
    before = (workspace / INPUT_PATH).read_bytes()
    row = {"strategy": strategy, "model_class": type(model).__qualname__,
           "status": None, "error": None, "experiment_passed": False,
           "output_correct": False, "task_success": False, "paths": {}}
    relative = lambda path: Path(path).relative_to(suite_dir).as_posix()
    database = row_dir / "runs.sqlite3"
    row["paths"]["database"] = relative(database)
    try:
        # Both receive the complete, identical tool registry, not a smaller
        # schema budget selected to make the range strategy look better.
        with Application(model, FilePlugin(workspace).tools, database=database,
                         workspace=workspace, permissions=("files.read",),
                         max_context_chars=CONTEXT_CHARS, limits=LIMITS) as app:
            initial = app.submit(PROMPT)
            state = app.wait(initial["id"], timeout=15)
            events = app.events(initial["id"])
        _write_json(row_dir / "checkpoint.json", state)
        _write_json(row_dir / "events.json", events)
        row["paths"].update(checkpoint=relative(row_dir / "checkpoint.json"),
                            events=relative(row_dir / "events.json"))
        results = [message["tool_result"] for message in state["messages"] if message.get("tool_result")]
        result_metrics = []
        for index, result in enumerate(results, 1):
            data = _serialized(result).encode("utf-8")
            path = row_dir / f"tool-result-{index:02}.json"
            path.write_bytes(data)
            result_metrics.append({"name": result["name"], "call_id": result["call_id"],
                                   "error_code": result.get("error_code"),
                                   "serialized_chars": len(data.decode("utf-8")),
                                   "serialized_utf8_bytes": len(data), "sha256": _sha(data),
                                   "path": relative(path)})
        row.update(status=state["status"], error=state.get("error"),
                   model_calls=state["model_calls"], tool_calls=state["tool_calls"],
                   actual_model_invocations=len(model.observed_requests),
                   model_request_tool_results=model.observed_requests,
                   context=state["context"], limits=state["limits"],
                   permissions=state["permissions"], tool_snapshot=state["tool_snapshot"],
                   output=state.get("output", ""), tool_results=result_metrics,
                   tool_result_chars=sum(item["serialized_chars"] for item in result_metrics),
                   tool_result_utf8_bytes=sum(item["serialized_utf8_bytes"] for item in result_metrics))
        row.update(score_output(row["output"]))
        row["task_success"] = row["status"] == "completed" and row["output_correct"]
        row["checks"] = {
            "two_successful_tool_results": len(results) == 2 and all(
                not item.get("error_code") and (item["name"] != "files.read_range"
                                                or item["value"].get("ok") is True) for item in results),
            "search_first": bool(results) and results[0]["name"] == "files.search",
            "expected_read_tool": len(results) == 2 and results[1]["name"] == (
                "files.read" if strategy == "full_read" else "files.read_range"),
            "input_unchanged": before == (workspace / INPUT_PATH).read_bytes(),
            "expected_outcome": (row["status"] == "failed" and row["error"] == "context_limit"
                                 and not row["output_correct"]) if strategy == "full_read" else row["task_success"],
        }
        row["experiment_passed"] = all(row["checks"].values())
    except Exception as error:
        # Keep failed trials and their evidence. Exception text can include
        # private absolute paths, so the portable report records only its type.
        row["driver_error_type"] = type(error).__name__
    row["paths"]["result"] = relative(row_dir / "result.json")
    _write_json(row_dir / "result.json", row)
    return row


def _summary_markdown(report):
    lines = ["# 单文件读取策略对照", "",
             "同一合成 UTF-8 文件、同一固定模型类、工具列表、权限与 48,000 字符预算；每种策略仅运行一次。",
             "模型先搜索真实路径与行号，再读取并解析真实工具返回。预期答案由独立字面量判据核对。", "",
             f"输入：{report['input']['utf8_bytes']} UTF-8 字节，{report['input']['characters']} 字符；"
             f"SHA-256 `{report['input']['sha256']}`。", "",
             "| 策略 | 终态 | 错误 | 模型／工具调用 | 工具结果字符／UTF-8 字节 | 独立答案正确 | 实验判据 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["rows"]:
        lines.append(f"| {row['strategy']} | {row['status']} | {row.get('error') or row.get('driver_error_type', '—')} | "
                     f"{row.get('model_calls', '—')} / {row.get('tool_calls', '—')} | "
                     f"{row.get('tool_result_chars', '—')} / {row.get('tool_result_utf8_bytes', '—')} | "
                     f"{row['output_correct']} | {row['experiment_passed']} |")
    lines += ["", "工具结果大小是持久 ToolResult 按 `json.dumps(ensure_ascii=False)` 默认分隔符序列化后的实测值，"
              "不等于完整上下文大小或 token 数。报告同时保存每次结果、真实检查点和完整上下文字符估计。", "",
              "实验判据通过包含旧读取按预期触发 context_limit，不能解释为两个任务都完成。"
              "这只是一个输入的一次成对机制演示，没有 LLM 请求、泛化成功率、延迟提升或统计显著性结论。", "",
              "片段工具仍受文件总大小和路径边界约束；本例不检验分页、跨文件汇总、并发修改或网络故障。", "",
              "[原始 JSON](report.json) 保存运行源码指纹、限制与逐项实际结果。", ""]
    return "\n".join(lines)


def run_suite(output_dir=".local/file-range"):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    suite = Path(tempfile.mkdtemp(prefix="suite-", dir=output))
    workspace = suite / "workspace"
    raw = prepare_workspace(workspace)
    source_root = Path(__file__).resolve().parents[1]
    report = {
        "schema_version": 1, "suite": "file-range-context", "mode": "deterministic_local",
        "provider_requests": 0, "directory": suite.name, "report": "report.json", "summary_report": "summary.md",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.system()},
        "independent_inputs": 1, "runs_per_strategy": 1, "context_budget_chars": CONTEXT_CHARS,
        "limits": dict(LIMITS), "prompt": PROMPT,
        "serialization": "json.dumps(ToolResult, ensure_ascii=False, allow_nan=False), default separators",
        "input": {"path": INPUT_PATH, "fixture_path": (workspace / INPUT_PATH).relative_to(suite).as_posix(),
                  "sha256": _sha(raw), "characters": len(raw.decode("utf-8")),
                  "utf8_bytes": len(raw), "total_lines": len(raw.decode("utf-8").splitlines())},
        "source_files_sha256": {name: _sha((source_root / name).read_bytes()) for name in SOURCE_FILES},
        "rows": [],
    }
    for strategy in STRATEGIES:
        report["rows"].append(run_strategy(strategy, workspace, suite / strategy, suite))
    report["summary"] = {"strategy_runs": len(report["rows"]),
                         "experiment_passed": sum(row["experiment_passed"] for row in report["rows"]),
                         "task_successes": sum(row["task_success"] for row in report["rows"])}
    report["passed"] = all(row["experiment_passed"] for row in report["rows"])
    _write_json(suite / "report.json", report)
    (suite / "summary.md").write_bytes(_summary_markdown(report).encode("utf-8"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=".local/file-range")
    args = parser.parse_args(argv)
    report = run_suite(args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
