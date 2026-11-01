"""Offline benchmark-identity acceptance and refusal experiments.

The baseline is produced by an actual tool run. Changed tools are real factory
variants; only the explicitly labelled legacy-schema file is derived data.
"""

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys
import tempfile

from astra_harness.benchmark import benchmark
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.tools import Tool


ACCEPTED_VARIANTS = ("same", "normalization", "description")
REJECTED_VARIANTS = ("tool_added", "tool_removed", "schema", "permission", "approval",
                     "implementation_body", "implementation_version", "implementation_constants", "legacy_schema2")
VARIANTS = ACCEPTED_VARIANTS + REJECTED_VARIANTS
LIMITS = {"max_model_calls": 4, "max_tool_calls": 2,
          "model_timeout": 10, "tool_timeout": 10, "run_timeout": 30}
SOURCE_FILES = ("scripts/tool_identity_lab.py", "astra_harness/benchmark.py", "astra_harness/tool_identity.py",
                "astra_harness/tools.py", "astra_harness/contracts.py", "astra_harness/application.py",
                "astra_harness/engine.py", "astra_harness/store.py", "astra_harness/context.py",
                "astra_harness/cli.py", "astra_harness/builtin/files.py", "astra_harness/builtin/models.py",
                "astra_harness/process_state.py")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write_json(path, value):
    Path(path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def _offset_sum(a, b, offset):
    return a + b + offset


class ProbeModel:
    """A fixed plan parses task operands and returns the actual tool value."""

    def __init__(self, observations):
        self.observations = observations

    def complete(self, request):
        self.observations["model_complete"] += 1
        results = [message.tool_result for message in request.messages if message.tool_result is not None]
        if not results:
            prompt = next(message.text for message in reversed(request.messages) if message.role == "user")
            operands = re.search(r"(-?\d+)\s*\+\s*(-?\d+)", prompt)
            if operands is None:
                raise ValueError("fixture requires an addition task")
            return ModelResponse(tool_calls=(ToolCall("sum-1", "lab.sum", {
                "a": int(operands[1]), "b": int(operands[2])}),))
        result = results[-1]
        if result.name != "lab.sum" or result.error_code:
            raise ValueError("fixture sum tool failed")
        return ModelResponse(text=json.dumps({"sum": result.value}, separators=(",", ":"), allow_nan=False))


class Probe:
    """Counters observe execution; all behavior constants are declared on Tool."""

    def __init__(self, variant):
        if variant not in ("baseline", *VARIANTS):
            raise ValueError("invalid fixture variant")
        self.variant = variant
        self.constants = {"offset": 1 if variant == "implementation_constants" else 0}
        self.counts = {"model_factory": 0, "model_complete": 0, "tool_handler": 0,
                       "tool_factory_preflight": 0, "tool_factory_bound": 0}
        self.prototypes = None

    def model_factory(self):
        self.counts["model_factory"] += 1
        return ProbeModel(self.counts)

    def sum(self, a, b):
        self.counts["tool_handler"] += 1
        return _offset_sum(a, b, self.constants["offset"])

    def revised_sum(self, a, b):
        self.counts["tool_handler"] += 1
        return _offset_sum(a, b, self.constants["offset"]) + 1

    def marker(self):
        self.counts["tool_handler"] += 1
        return "unused fixture marker"

    def tools_factory(self, workspace=None):
        # This factory performs no I/O, including in prototype mode. Capturing
        # references lets the driver inspect the exact definitions after refusal.
        kind = "tool_factory_preflight" if workspace is None else "tool_factory_bound"
        self.counts[kind] += 1
        schema = {"type": "object", "title": "Addition input", "properties": {
            "a": {"type": "number", "enum": [-2, 2, 7], "title": "Left operand"},
            "b": {"type": "number", "enum": [3, 7], "description": "Right operand"}},
            "required": ["a", "b"], "additionalProperties": False}
        sum_tool = Tool("lab.sum", "Add the two operands", (), self.sum, input_schema=schema,
                        permissions=frozenset({"files.read", "files.write"}),
                        implementation_version="lab-v1", implementation_dependencies=(_offset_sum,),
                        implementation_constants=self.constants)
        marker = Tool("lab.marker", "Unused marker capability", (), self.marker,
                      input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                      implementation_version="lab-v1")
        if self.variant == "normalization":
            schema["properties"] = dict(reversed(list(schema["properties"].items())))
            schema["required"].reverse()
            for child in schema["properties"].values():
                child["enum"].reverse()
            sum_tool = replace(sum_tool, input_schema=dict(reversed(list(schema.items()))))
        elif self.variant == "description":
            schema["title"] = "Reworded addition input"
            schema["properties"]["a"]["title"] = "First value"
            schema["properties"]["b"]["description"] = "Second value"
            sum_tool = replace(sum_tool, description="Return the sum of the provided values", input_schema=schema)
        elif self.variant == "schema":
            schema["properties"]["a"]["minimum"] = 0
            sum_tool = replace(sum_tool, input_schema=schema)
        elif self.variant == "permission":
            sum_tool = replace(sum_tool, permissions=frozenset({"lab.restricted"}))
        elif self.variant == "approval":
            sum_tool = replace(sum_tool, requires_approval=True)
        elif self.variant == "implementation_body":
            sum_tool = replace(sum_tool, handler=self.revised_sum)
        elif self.variant == "implementation_version":
            sum_tool = replace(sum_tool, implementation_version="lab-v2")
        tools = [sum_tool, marker]
        if self.variant == "tool_added":
            tools.append(replace(marker, name="lab.extra"))
        elif self.variant == "tool_removed":
            tools.remove(marker)
        elif self.variant == "normalization":
            tools.reverse()
        if workspace is None:
            self.prototypes = tuple(tools)
        return tools


def _definitions(tools):
    return [{"name": tool.name, "description": tool.description, "schema": tool.spec().schema(),
             "permissions": sorted(tool.permissions), "side_effect": tool.side_effect,
             "requires_approval": tool.requires_approval, "handler": tool.handler.__func__.__qualname__,
             "implementation_version": tool.implementation_version,
             "implementation_dependencies": [function.__qualname__ for function in tool.implementation_dependencies],
             "implementation_constants": deepcopy(tool.implementation_constants)} for tool in tools]


def _entries(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) if root.exists() else []


def _portable_error(error, suite):
    message = str(error)
    for root, replacement in ((suite.resolve(), "<suite>"), (Path(__file__).resolve().parents[1], "<project>")):
        message = message.replace(str(root), replacement).replace(root.as_posix(), replacement)
    return {"type": type(error).__name__, "message": message}


def _report_evidence(report, suite):
    source = Path(report["report"])
    return {"origin": "actual_benchmark_run", "path": source.relative_to(suite.resolve()).as_posix(),
            "sha256": _sha(source.read_bytes()), "schema_version": report["schema_version"],
            "tool_identity": report["tool_identity"],
            "tool_presentation_sha256": report["tool_presentation_sha256"],
            "dataset_sha256": report["dataset_sha256"], "runtime_config": report["runtime_config"],
            "model": report["model"], "passed": report["passed"],
            "cases": [{key: case.get(key) for key in ("id", "status", "passed", "model_calls", "tool_calls")}
                      for case in report["cases"]]}


def _attempt(variant, dataset, baseline, outputs, suite):
    from astra_harness.tool_identity import build_tool_identity, presentation_fingerprint

    probe = Probe(variant)
    output = outputs / variant
    before = _entries(outputs)
    baseline_hash = _sha(baseline.read_bytes())
    row = {"id": variant, "origin": "derived_fixture" if variant == "legacy_schema2" else "actual_tools_factory",
           "expected": "accepted" if variant in ACCEPTED_VARIANTS else "rejected",
           "outcome": None, "passed": False, "baseline_path": baseline.relative_to(suite).as_posix(),
           "baseline_sha256": baseline_hash, "output_path": output.relative_to(suite).as_posix(),
           "output_existed_before": output.exists(), "entries_before": before}
    try:
        result = benchmark(dataset, output, mode="custom", baseline=baseline, model_factory=probe.model_factory,
                           tools_factory=probe.tools_factory, limits=LIMITS)
        row.update(outcome="accepted", report=_report_evidence(result, suite), comparison=result["comparison"])
    except ValueError as error:
        row.update(outcome="rejected", exception=_portable_error(error, suite))
    except Exception as error:
        row.update(outcome="driver_error", exception=_portable_error(error, suite))
    row.update(counts=dict(probe.counts), output_exists_after=output.exists(), entries_after=_entries(outputs),
               baseline_unchanged=baseline_hash == _sha(baseline.read_bytes()))
    if probe.prototypes is not None:
        row["current_definition"] = _definitions(probe.prototypes)
        row["current_tool_identity"] = build_tool_identity(probe.prototypes)
        row["current_tool_presentation_sha256"] = presentation_fingerprint(probe.prototypes)
    if row["expected"] == "accepted":
        row["checks"] = {
            "accepted": row["outcome"] == "accepted",
            "actual_task_passed": row.get("report", {}).get("passed") is True,
            "executed_model_and_tool": probe.counts["model_complete"] == 2 and probe.counts["tool_handler"] == 1,
            "new_output": not row["output_existed_before"] and row["output_exists_after"],
            "description_note": variant != "description" or (row.get("comparison", {}).get("metadata_changed") is True
                                                              and bool(row.get("comparison", {}).get("notes"))),
            "baseline_unchanged": row["baseline_unchanged"],
        }
    else:
        message = row.get("exception", {}).get("message", "").casefold()
        reason = ("legacy schema 2" in message and "lack tool identity" in message
                  if variant == "legacy_schema2" else "same tool identity" in message)
        row["checks"] = {
            "rejected": row["outcome"] == "rejected", "reason_matches_contract": reason,
            "no_model_construction": probe.counts["model_factory"] == 0,
            "no_model_or_tool_execution": probe.counts["model_complete"] == 0 and probe.counts["tool_handler"] == 0,
            "no_bound_tool_factory": probe.counts["tool_factory_bound"] == 0,
            "no_output_directory": not row["output_existed_before"] and not row["output_exists_after"],
            "no_new_benchmark_entries": row["entries_before"] == row["entries_after"],
            "baseline_unchanged": row["baseline_unchanged"],
        }
    row["passed"] = all(row["checks"].values())
    return row


def _summary(report):
    lines = ["# 工具身份前置校验实验", "",
             "先用真实本地模型替身和加法工具运行一个任务生成基线，再执行相同条件与实际工具工厂变体。",
             "固定模型从实际 ToolResult 生成回答，数据集独立核对结果 5。每个变体只试一次；不代表 LLM 能力或性能提升。", "",
             "| 变体 | 数据来源 | 预期／实际 | 模型构造／调用 | 工具执行 | 新评测目录 | 判据 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["rows"]:
        counts = row["counts"]
        lines.append(f"| {row['id']} | {row['origin']} | {row['expected']} / {row['outcome']} | "
                     f"{counts['model_factory']} / {counts['model_complete']} | {counts['tool_handler']} | "
                     f"{row['output_exists_after']} | {row['passed']} |")
    lines += ["", "旧 schema 2 项是从本次真实基线显式派生的拒绝夹具，未冒充历史运行，也未为旧报告补造身份。"
              "JSON 保存父报告散列、派生步骤及实际工厂定义。", "",
              "description/title 修改按契约可比较，并在 comparison.notes 中提示展示元数据变化；"
              "顺序规范化可比不等于模型输入逐字相同。", "",
              "拒绝行的零目录指无新增 benchmark 工作目录；实验驱动仍会保存本报告。"
              "工具工厂是可信主机代码，预检的无 I/O 契约不等于对任意工厂的系统级隔离。", "",
              "实现身份包括源码 AST、显式依赖、版本与常量；它不证明任意闭包、外部服务或机器环境完全相同。"
              "本实验不改变生产恢复语义，不报告时间提升或统计显著性。", "",
              "[JSON 报告](report.json) 包含逐项判据、输入与运行源码指纹。", ""]
    return "\n".join(lines)


def run_suite(output_dir=".local/tool-identity"):
    started_at = datetime.now(timezone.utc).isoformat()
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    suite = Path(tempfile.mkdtemp(prefix="suite-", dir=root))
    outputs = suite / "benchmarks"
    dataset = suite / "task.jsonl"
    dataset.write_bytes((json.dumps({"id": "actual-sum", "prompt": "Compute -2 + 7", "required_tools": ["lab.sum"],
                                    "tool_values": {"lab.sum": 5}, "answer_contains": ['"sum":5']},
                                   ensure_ascii=False) + "\n").encode("utf-8"))
    baseline_probe = Probe("baseline")
    baseline = benchmark(dataset, outputs / "baseline", mode="custom", model_factory=baseline_probe.model_factory,
                         tools_factory=baseline_probe.tools_factory, limits=LIMITS)
    baseline_path = Path(baseline["report"])
    baseline_evidence = _report_evidence(baseline, suite)
    baseline_evidence["counts"] = dict(baseline_probe.counts)
    baseline_evidence["definition"] = _definitions(baseline_probe.prototypes)
    # This is a labelled transformation of the actual baseline, not an asserted
    # historical execution. The original bytes remain untouched.
    legacy = deepcopy(baseline)
    legacy["schema_version"] = 2
    removed = [name for name in ("tool_identity", "tool_presentation_sha256") if name in legacy]
    for name in removed:
        del legacy[name]
    legacy_path = suite / "derived-schema2.json"
    _write_json(legacy_path, legacy)
    derivation = {"origin": "derived_fixture", "path": legacy_path.relative_to(suite).as_posix(),
                  "sha256": _sha(legacy_path.read_bytes()), "parent_path": baseline_evidence["path"],
                  "parent_sha256": baseline_evidence["sha256"],
                  "operations": {"set": {"schema_version": 2}, "remove": removed},
                  "historical_run": False}
    source_root = Path(__file__).resolve().parents[1]
    report = {"schema_version": 1, "suite": "tool-identity-preflight", "mode": "deterministic_local",
              "provider_requests": 0, "directory": suite.name, "report": "report.json", "summary_report": "summary.md",
              "started_at_utc": started_at,
              "environment": {"python": platform.python_version(), "platform": platform.system()},
              "independent_tasks": 1, "attempts_per_variant": 1,
              "dataset": {"path": "task.jsonl", "bytes_sha256": _sha(dataset.read_bytes()),
                          "canonical_sha256": baseline["dataset_sha256"]},
              "source_files_sha256": {name: _sha((source_root / name).read_bytes()) for name in SOURCE_FILES},
              "baseline": baseline_evidence, "derived_fixture": derivation, "rows": []}
    for variant in VARIANTS:
        chosen = legacy_path if variant == "legacy_schema2" else baseline_path
        report["rows"].append(_attempt(variant, dataset, chosen, outputs, suite))
    report["summary"] = {"attempts": len(report["rows"]), "expectations_met": sum(row["passed"] for row in report["rows"]),
                         "accepted_comparisons": sum(row["outcome"] == "accepted" for row in report["rows"]),
                         "rejected_comparisons": sum(row["outcome"] == "rejected" for row in report["rows"])}
    report["passed"] = baseline_evidence["passed"] and all(row["passed"] for row in report["rows"])
    _write_json(suite / "report.json", report)
    (suite / "summary.md").write_bytes(_summary(report).encode("utf-8"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=".local/tool-identity")
    args = parser.parse_args(argv)
    report = run_suite(args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
