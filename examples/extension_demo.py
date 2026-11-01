"""Typed function → existing executor → persisted approval → real JSON artifact.

No provider, credentials, network, or user files are needed. The scripted model
only proposes a fixed synthetic task; the ordinary Python function does the work.
"""
import argparse
import json
from pathlib import Path
import tempfile

from astra_harness import tool_from_function
from astra_harness.application import Application
from astra_harness.contracts import ModelResponse, ToolCall


PERMISSION = "reports.write"


def report_writer(workspace: Path, observed_writes: list):
    """Bind the output location in trusted host code, outside model arguments."""
    def create_report(daily_counts: list[int], team: str = "合成支持组") -> dict:
        """Create a small support-work report from synthetic daily ticket counts."""
        if not team.strip() or len(team) > 80:
            raise ValueError("team must contain 1..80 visible characters")
        if not 1 <= len(daily_counts) <= 7 or any(type(n) is not int or not 0 <= n <= 10_000 for n in daily_counts):
            raise ValueError("provide 1..7 nonnegative daily counts, each at most 10000")
        data = {"team": team, "daily_counts": daily_counts,
                "days": len(daily_counts), "resolved_total": sum(daily_counts)}
        # The model cannot choose a path, and a repeated invocation cannot
        # silently overwrite an existing report. This is not an OS sandbox.
        with (workspace / "report.json").open("x", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        observed_writes.append("report.json")
        return {"path": "report.json", "resolved_total": data["resolved_total"]}

    return create_report


def make_report_tool(workspace: Path, observed_writes: list):
    return tool_from_function(
        report_writer(workspace, observed_writes),
        name="reports.write",
        permissions=frozenset({PERMISSION}),
        side_effect=True,
        requires_approval=True,
        implementation_version="extension-report-v1",
    )


class ReportModel:
    """Deterministic fixture: request a real write, then use its actual result."""
    def complete(self, request):
        result = request.messages[-1].tool_result
        if result is None:
            # Omit team deliberately: the function's scalar default must work.
            return ModelResponse(tool_calls=(ToolCall("report-1", "reports.write", {"daily_counts": [3, 5, 4]}),))
        if result.error_code:
            return ModelResponse(text="报告未生成：" + result.error_code)
        return ModelResponse(text=f"{result.value['path']}：共处理 {result.value['resolved_total']} 项。")


def open_application(directory: Path, observed_writes: list, *, autostart=True):
    """Both openings supply the same model, tool policy, workspace and limits."""
    workspace = directory / "workspace"
    return Application(
        ReportModel(), (make_report_tool(workspace, observed_writes),),
        database=directory / "runs.sqlite3", workspace=workspace,
        permissions=(PERMISSION,), mode="extension-demo", autostart=autostart,
        limits={"max_model_calls": 2, "max_tool_calls": 1, "run_timeout": 15},
    )


def _require(value, message):
    if not value:
        raise RuntimeError(message)


def _execute(directory: Path, *, approve: bool, retained: bool):
    workspace = directory / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.json"
    observed_writes = []
    with open_application(directory, observed_writes) as app:
        created = app.submit("请把合成支持组最近三天的处理量 3、5、4 汇总成报告。")
        waiting = app.wait(created["id"], timeout=10)
        _require(waiting["status"] == "waiting_approval", "expected approval before writing")
        _require(not artifact.exists() and not observed_writes, "write occurred before approval")
        schema = app.tools[0].spec().schema()
        approval_key = waiting["approval"]["key"]

    # This is an Application close/reopen, not a simulated crash or a claim of
    # arbitrary exactly-once writes. The SQLite record carries the approval.
    with open_application(directory, observed_writes) as app:
        restored = app.get_run(created["id"])
        _require(restored["status"] == "waiting_approval" and restored["approval"]["key"] == approval_key,
                 "approval did not survive reopen")
        _require(not artifact.exists() and not observed_writes, "reopen unexpectedly executed the write")
        # The demo host explicitly makes this decision for its own synthetic
        # task. Real integrations must supply a trusted approval decision.
        app.approve(created["id"], approved=approve)
        final = app.wait(created["id"], timeout=10)
        events = app.events(created["id"])

    # The oracle is a literal expected business artifact, independent of the
    # function's sum calculation and the scripted model's final sentence.
    expected = {"team": "合成支持组", "daily_counts": [3, 5, 4], "days": 3, "resolved_total": 12}
    actual = json.loads(artifact.read_text(encoding="utf-8")) if artifact.exists() else None
    if approve:
        _require(final["status"] == "completed" and actual == expected, "report artifact mismatch")
        _require(observed_writes == ["report.json"], "expected exactly one observed write in this case")
        _require(final["model_calls"] == 2 and final["tool_calls"] == 1, "unexpected execution counts")
    else:
        _require(final["status"] == "failed" and final["error"] == "approval_denied", "rejection was not respected")
        _require(actual is None and not observed_writes and final["tool_calls"] == 0, "rejected write executed")

    # Store a portable teaching trace rather than a host configuration dump.
    trace = []
    for event in events:
        item = {key: event[key] for key in ("seq", "type", "status", "approved") if key in event}
        if event["type"] == "tool.end":
            item["result"] = event["result"]
        trace.append(item)
    (directory / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "passed": True, "model_kind": "scripted_offline", "approval_decision": "approved" if approve else "denied",
        "states": [waiting["status"], "application_closed", restored["status"], final["status"]],
        "write_before_approval": False, "observed_writes": len(observed_writes),
        "status": final["status"], "error": final["error"],
        "model_calls": final["model_calls"], "tool_calls": final["tool_calls"],
        "output": final["output"], "schema": schema,
        "artifact": "workspace/report.json" if approve else None, "artifact_content": actual,
        "trace": "trace.json", "database": "runs.sqlite3", "retained": retained,
    }
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def run_demo(output_dir=None, *, approve=True):
    """Use our temporary directory, or exclusively create a new caller-chosen one."""
    if type(approve) is not bool:
        raise ValueError("approve must be boolean")
    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix="astra-extension-") as temporary:
            return _execute(Path(temporary), approve=approve, retained=False)
    directory = Path(output_dir)
    # Never clear/reuse a directory, database, or report belonging to the caller.
    directory.mkdir(parents=True, exist_ok=False)
    return _execute(directory.resolve(), approve=approve, retained=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Create a new directory and retain its synthetic artifacts.")
    parser.add_argument("--deny", action="store_true", help="Demonstrate an explicit approval rejection instead.")
    args = parser.parse_args(argv)
    try:
        summary = run_demo(args.output_dir, approve=not args.deny)
    except FileExistsError:
        parser.exit(2, "目标已存在；请选择新的 --output-dir，不会覆盖原文件。\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
