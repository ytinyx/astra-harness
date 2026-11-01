"""数据驱动的小型评测：独立工作区、真实工具证据与可比较的报告。"""

import hashlib
import json
import math
from pathlib import Path
import platform
import tempfile
import time

from .application import Application
from .builtin.files import FilePlugin
from .builtin.models import DemoModel, OpenAICompatibleModel
from .cli import seed_workspace
from .engine import normalize_limits
from .tools import Tool
from .tool_identity import build_tool_identity, identity_difference, presentation_fingerprint, validate_tool_identity


DEFAULT_DATASET = Path(__file__).parent / "datasets" / "smoke.jsonl"


def _add(a, b):
    return a + b


def default_tool_factory(workspace=None):
    """None returns definitions only: no workspace, file access, or handlers run."""
    files = FilePlugin.tool_definitions() if workspace is None else FilePlugin(workspace).tools
    return (Tool("add", "计算两个数的和", ("a", "b"), _add, implementation_version="1"), *files)


def load_cases(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("dataset too large")
    cases, seen = [], set()
    allowed = {"id", "prompt", "approval", "status", "answer_contains", "required_tools",
               "forbidden_tools", "artifact_json", "absent_files", "max_tool_calls", "expected_error", "tool_values"}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict) or set(case) - allowed:
            raise ValueError("invalid case fields")
        if not isinstance(case.get("id"), str) or not case["id"].strip() or case["id"] in seen:
            raise ValueError("case ids must be unique")
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise ValueError("case requires a prompt")
        if case.get("approval", "stop") not in {"approve", "deny", "stop"}:
            raise ValueError("invalid approval policy")
        if case.get("status", "completed") not in {"completed", "failed", "waiting_approval", "cancelled", "unknown"}:
            raise ValueError("invalid expected status")
        for key in ("answer_contains", "required_tools", "forbidden_tools", "absent_files"):
            values = case.get(key, [])
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise ValueError("expected list of strings")
        if not isinstance(case.get("artifact_json", {}), dict):
            raise ValueError("expected artifact mapping")
        if not isinstance(case.get("tool_values", {}), dict):
            raise ValueError("expected tool result mapping")
        if "expected_error" in case and not isinstance(case["expected_error"], str):
            raise ValueError("expected error must be a string")
        if "max_tool_calls" in case and (type(case["max_tool_calls"]) is not int or case["max_tool_calls"] < 0):
            raise ValueError("invalid tool budget")
        # Data sets may inspect only their own isolated workspace.
        for name in [*case.get("artifact_json", {}), *case.get("absent_files", [])]:
            if not isinstance(name, str) or not name or "\\" in name or ":" in name or name.startswith("/") or ".." in name.split("/"):
                raise ValueError("invalid artifact path")
        seen.add(case["id"])
        cases.append(case)
    if not 1 <= len(cases) <= 100:
        raise ValueError("dataset requires 1..100 cases")
    canonical = json.dumps(cases, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return cases, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def score_case(case, state, workspace):
    results = [item["tool_result"] for item in state["messages"] if item.get("tool_result")]
    successful = {result["name"] for result in results if not result.get("error_code")}
    attempted = {result["name"] for result in results}
    checks = {"status": state["status"] == case.get("status", "completed")}
    if "expected_error" in case:
        checks["error"] = state.get("error") == case["expected_error"]
    output = (state.get("output") or "").casefold()
    for text in case.get("answer_contains", []):
        checks["answer:" + text] = text.casefold() in output
    for name in case.get("required_tools", []):
        checks["tool:" + name] = name in successful
    for name, expected in case.get("tool_values", {}).items():
        checks["tool_value:" + name] = any(result["name"] == name and not result.get("error_code")
                                           and result.get("value") == expected for result in results)
    for name in case.get("forbidden_tools", []):
        # Failed and uncertain attempts count as forbidden, even without a ToolResult.
        requested = {call["name"] for item in state["messages"] for call in item.get("tool_calls", [])}
        checks["forbidden:" + name] = name not in attempted | requested
    if "max_tool_calls" in case:
        checks["tool_budget"] = state["tool_calls"] <= case["max_tool_calls"]
    for name, expected in case.get("artifact_json", {}).items():
        try:
            checks["artifact:" + name] = json.loads(FilePlugin(workspace).read_file(name)) == expected
        except (OSError, ValueError):
            checks["artifact:" + name] = False
    for name in case.get("absent_files", []):
        checks["absent:" + name] = not (workspace / name).exists()
    return checks


def _nonnegative_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _validate_model_identity(identity):
    if (not isinstance(identity, dict) or set(identity) != {"adapter", "name"}
            or not isinstance(identity["adapter"], str) or not identity["adapter"].strip()
            or (identity["name"] is not None
                and (not isinstance(identity["name"], str) or not identity["name"].strip()))):
        raise ValueError("report requires a valid model identity")


def _validate_report(report):
    """Validate both file baselines and direct comparisons before trusting their scores."""
    if isinstance(report, dict) and type(report.get("schema_version")) is int and report["schema_version"] == 2:
        raise ValueError("legacy schema 2 reports lack tool identity and cannot be compared; rerun the baseline")
    if not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 3:
        raise ValueError("baseline requires report schema 3")
    validate_tool_identity(report.get("tool_identity"))
    presentation = report.get("tool_presentation_sha256")
    if (not isinstance(presentation, str) or len(presentation) != 64
            or any(character not in "0123456789abcdef" for character in presentation)):
        raise ValueError("report requires a tool presentation fingerprint")
    environment = report.get("environment")
    if (not isinstance(environment, dict) or set(environment) != {"python", "implementation", "system"}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in environment.values())):
        raise ValueError("report requires execution environment metadata")
    fingerprint = report.get("dataset_sha256")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            or report.get("mode") not in ("demo", "live", "custom")):
        raise ValueError("report requires a valid dataset and mode")
    runtime = report.get("runtime_config")
    if (not isinstance(runtime, dict) or set(runtime) != {"limits", "context_chars"}
            or not isinstance(runtime["limits"], dict)
            or type(runtime["context_chars"]) is not int or runtime["context_chars"] <= 0):
        raise ValueError("report requires complete runtime configuration")
    normalized = normalize_limits(runtime["limits"])
    if set(normalized) != set(runtime["limits"]):
        raise ValueError("report requires complete runtime configuration")
    _validate_model_identity(report.get("model"))
    cases = report.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("report requires 1..100 cases")
    indexed = {}
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or not case["id"].strip() or case["id"] in indexed
                or type(case.get("passed")) is not bool
                or not _nonnegative_number(case.get("duration_ms"))):
            raise ValueError("report requires unique case ids and valid results")
        indexed[case["id"]] = case
    passed = sum(case["passed"] for case in cases)
    summary = report.get("summary")
    expected_counts = {"total": len(cases), "passed": passed, "failed": len(cases) - passed}
    if (not isinstance(summary, dict)
            or any(type(summary.get(key)) is not int or summary[key] != value
                   for key, value in expected_counts.items())
            or not _nonnegative_number(summary.get("pass_rate"))
            or summary["pass_rate"] != passed / len(cases)
            or not _nonnegative_number(summary.get("mean_duration_ms"))
            or summary["mean_duration_ms"] != round(sum(case["duration_ms"] for case in cases) / len(cases), 2)
            or type(report.get("passed")) is not bool or report["passed"] != (passed == len(cases))):
        raise ValueError("report summary is inconsistent with its cases")
    return indexed


def _read_report(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate report key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-finite report value")

    with Path(path).open("rb") as stream:
        raw = stream.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("baseline report too large")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs, parse_constant=reject_constant)


def _require_same_inputs(previous, fingerprint, mode, runtime_config, case_ids):
    if fingerprint != previous["dataset_sha256"] or mode != previous["mode"]:
        raise ValueError("baseline must use the same dataset and mode")
    if runtime_config != previous["runtime_config"]:
        raise ValueError("baseline must use the same runtime configuration")
    if case_ids != {case["id"] for case in previous["cases"]}:
        raise ValueError("baseline case ids differ")


def compare_reports(current, previous):
    """Compare validated schema 3 reports; wording changes remain visible notes."""
    current_cases = _validate_report(current)
    before = _validate_report(previous)
    _require_same_inputs(previous, current["dataset_sha256"], current["mode"],
                         current["runtime_config"], set(current_cases))
    if current["model"] != previous["model"]:
        raise ValueError("baseline must use the same model identity")
    if current["tool_identity"] != previous["tool_identity"]:
        raise ValueError("baseline must use the same tool identity: " + identity_difference(current["tool_identity"], previous["tool_identity"]))
    metadata_changed = current["tool_presentation_sha256"] != previous["tool_presentation_sha256"]
    notes = []
    if metadata_changed:
        notes.append("Tool presentation metadata changed; this comparison does not use identical tool wording in model input.")
    if current["environment"] != previous["environment"]:
        notes.append("Execution environments differ; declared tool identity does not cover all platform or standard-library behavior.")
    regressions = [case["id"] for case in current["cases"] if before[case["id"]]["passed"] and not case["passed"]]
    improvements = [case["id"] for case in current["cases"] if not before[case["id"]]["passed"] and case["passed"]]
    return {"regressions": regressions, "improvements": improvements,
            "pass_rate_delta": current["summary"]["pass_rate"] - previous["summary"]["pass_rate"],
            "mean_duration_ms_delta": round(current["summary"]["mean_duration_ms"] - previous["summary"]["mean_duration_ms"], 2),
            "metadata_changed": metadata_changed, "notes": notes}


def benchmark(dataset=DEFAULT_DATASET, output_dir=".local/benchmarks", *, model_factory=None,
              mode="demo", baseline=None, limits=None, max_context_chars=48000, tools_factory=None):
    if mode not in {"demo", "live", "custom"} or (mode == "live" and model_factory is None):
        raise ValueError("live mode requires an explicit model factory")
    effective_limits = normalize_limits(limits)
    if type(max_context_chars) is not int or max_context_chars <= 0:
        raise ValueError("context_chars must be a positive integer")
    runtime_config = {"limits": effective_limits, "context_chars": max_context_chars}
    cases, fingerprint = load_cases(dataset)
    previous = None
    if baseline is not None:
        previous = _read_report(baseline)
        _validate_report(previous)
        _require_same_inputs(previous, fingerprint, mode, runtime_config, {case["id"] for case in cases})
    tool_factory = default_tool_factory if tools_factory is None else tools_factory
    if not callable(tool_factory):
        raise ValueError("tools_factory must be callable")
    # Factories are trusted host code. The built-in definition path performs no
    # workspace I/O; arbitrary injected factories are not sandboxed by this API.
    definitions = tuple(tool_factory(None))
    tools_identity = build_tool_identity(definitions)
    tools_presentation = presentation_fingerprint(definitions)
    if previous is not None and previous["tool_identity"] != tools_identity:
        raise ValueError("baseline must use the same tool identity: " + identity_difference(tools_identity, previous["tool_identity"]))
    # Structural mismatches require no model construction. Identity needs one instance,
    # reused by the first case. Built-in adapters make no request during construction;
    # injected factories are trusted host code. The evaluator has not created output yet.
    factory = model_factory or DemoModel
    first_started = time.perf_counter()
    first_model = factory()
    first_model_seconds = time.perf_counter() - first_started

    def model_identity(model):
        if mode == "live" and not isinstance(model, OpenAICompatibleModel):
            raise ValueError("live benchmarking requires the configured provider adapter")
        identity = {"adapter": type(model).__name__, "name": getattr(model, "model", None)}
        _validate_model_identity(identity)
        return identity

    identity = model_identity(first_model)
    if previous is not None and previous["model"] != identity:
        raise ValueError("baseline must use the same model identity")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="suite-", dir=root))
    report = {"schema_version": 3, "mode": mode, "dataset_sha256": fingerprint,
              "runtime_config": runtime_config, "model": identity,
              "tool_identity": tools_identity, "tool_presentation_sha256": tools_presentation,
              "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                              "system": platform.system()},
              "real_model_requested": mode == "live", "real_model_verified": False, "cases": []}
    for index, case in enumerate(cases):
        model_started = time.perf_counter()
        model = first_model if index == 0 else factory()
        model_seconds = first_model_seconds if index == 0 else time.perf_counter() - model_started
        if model_identity(model) != identity:
            raise ValueError("model factory changed identity between cases")
        work = directory / f"case-{index + 1:02d}"
        workspace = seed_workspace(work / "workspace")
        tools = tuple(tool_factory(workspace))
        actual_identity = build_tool_identity(tools)
        if actual_identity != tools_identity:
            raise ValueError("tool factory changed identity between preflight and cases: " + identity_difference(actual_identity, tools_identity))
        if presentation_fingerprint(tools) != tools_presentation:
            raise ValueError("tool factory changed presentation between preflight and cases")
        started = time.perf_counter()
        with Application(model, tools, database=work / "runs.sqlite3", workspace=workspace,
                         permissions=["files.read", "files.write"], mode=mode,
                         limits=effective_limits, max_context_chars=max_context_chars) as app:
            submitted = app.submit(case["prompt"])
            # The outer wait must not cancel a valid run before its own budget.
            wait_timeout = max(240, submitted["limits"]["run_timeout"] + 30)
            state = app.wait(submitted["id"], timeout=wait_timeout)
            decisions = 0
            while state["status"] == "waiting_approval" and case.get("approval", "stop") != "stop":
                if decisions >= 24:
                    state = app.cancel(state["id"])
                    break
                app.approve(state["id"], approved=case["approval"] == "approve")
                state = app.wait(state["id"], timeout=wait_timeout)
                decisions += 1
            checks = score_case(case, state, workspace)
            report["cases"].append({"id": case["id"], "passed": all(checks.values()), "checks": checks,
                                    "status": state["status"], "error": state.get("error"),
                                    "duration_ms": round((time.perf_counter() - started + model_seconds) * 1000, 2),
                                    "model_calls": state["model_calls"], "tool_calls": state["tool_calls"],
                                    "usage": state.get("usage"), "run_id": state["id"]})
    passed = sum(case["passed"] for case in report["cases"])
    report["summary"] = {"total": len(cases), "passed": passed, "failed": len(cases) - passed,
                         "pass_rate": passed / len(cases),
                         "mean_duration_ms": round(sum(case["duration_ms"] for case in report["cases"]) / len(cases), 2)}
    report["passed"] = passed == len(cases)
    report["real_model_verified"] = mode == "live" and report["passed"]
    if previous is not None:
        report["comparison"] = compare_reports(report, previous)
        report["passed"] &= not report["comparison"]["regressions"]
    report["report"] = str(directory / "report.json")
    # Report files stay local. They do not contain prompts, model text or credentials.
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
