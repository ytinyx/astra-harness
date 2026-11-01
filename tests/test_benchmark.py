import json
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.benchmark import benchmark, compare_reports, default_tool_factory, load_cases, score_case
from astra_harness.application import Application
from astra_harness.engine import normalize_limits
from astra_harness.tool_identity import build_tool_identity, presentation_fingerprint


class BenchmarkTests(unittest.TestCase):
    def test_dataset_runs_actual_tools_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps({"id": "sum", "prompt": "计算 -2 + 7",
                                        "required_tools": ["add"], "answer_contains": ["5"]}), encoding="utf-8")
            report = benchmark(path, Path(tmp) / "out")
            self.assertTrue(report["passed"])
            self.assertEqual(report["cases"][0]["tool_calls"], 1)
            self.assertFalse(report["real_model_verified"])
            self.assertTrue(Path(report["report"]).is_file())
            second = benchmark(path, Path(tmp) / "out", baseline=report["report"])
            self.assertEqual(second["comparison"]["regressions"], [])

    def test_incorrect_expectation_fails_and_reports_specific_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps({"id": "bad", "prompt": "计算 2 + 3", "answer_contains": ["999"]}), encoding="utf-8")
            report = benchmark(path, Path(tmp) / "out")
            self.assertFalse(report["passed"])
            self.assertFalse(report["cases"][0]["checks"]["answer:999"])

    def test_invalid_dataset_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            for case in ({"id": "x", "prompt": "x", "artifact_json": {"../outside": []}},
                         {"id": "x", "prompt": "x", "approval": "automatic"},
                         {"id": "x", "prompt": "x", "max_tool_calls": True}):
                path.write_text(json.dumps(case), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_cases(path)
            path.write_text('{"id":"x","prompt":"a"}\n{"id":"x","prompt":"b"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_comparison_requires_identical_cases_and_detects_regression(self):
        before = {"schema_version": 3, "mode": "demo", "dataset_sha256": "a" * 64,
                  "tool_identity": build_tool_identity(default_tool_factory()),
                  "tool_presentation_sha256": presentation_fingerprint(default_tool_factory()),
                  "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                                  "system": platform.system()},
                  "runtime_config": {"limits": normalize_limits(), "context_chars": 48000},
                  "model": {"adapter": "DemoModel", "name": None}, "passed": True,
                  "cases": [{"id": "a", "passed": True, "duration_ms": 1}],
                  "summary": {"total": 1, "passed": 1, "failed": 0, "pass_rate": 1, "mean_duration_ms": 1}}
        after = {**before, "passed": False, "cases": [{"id": "a", "passed": False, "duration_ms": 2}],
                 "summary": {"total": 1, "passed": 0, "failed": 1, "pass_rate": 0, "mean_duration_ms": 2}}
        self.assertEqual(compare_reports(after, before)["regressions"], ["a"])
        with self.assertRaises(ValueError):
            compare_reports({**after, "mode": "live"}, before)

    def test_forbidden_attempt_is_not_hidden_by_missing_result(self):
        state = {"status": "unknown", "tool_calls": 1, "messages": [
            {"tool_calls": [{"name": "dangerous"}]}]}
        checks = score_case({"status": "unknown", "forbidden_tools": ["dangerous"]}, state, Path("."))
        self.assertFalse(checks["forbidden:dangerous"])

    def test_tool_value_and_failure_reason_are_verified(self):
        state = {"status": "failed", "error": "model_timeout", "tool_calls": 1, "output": "15", "messages": [
            {"tool_result": {"name": "add", "value": 15, "error_code": None}}]}
        checks = score_case({"status": "failed", "expected_error": "approval_denied",
                             "tool_values": {"add": 5}}, state, Path("."))
        self.assertFalse(checks["tool_value:add"])
        self.assertFalse(checks["error"])

    def test_live_label_cannot_use_default_demo(self):
        with self.assertRaises(ValueError):
            benchmark(mode="live")

    def test_custom_budget_is_enforced_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps({"id": "limited", "prompt": "计算 2 + 3",
                                        "status": "failed", "expected_error": "model_limit",
                                        "required_tools": ["add"]}), encoding="utf-8")
            report = benchmark(path, Path(tmp) / "out", limits={"max_model_calls": 1},
                               max_context_chars=12000)
            self.assertTrue(report["passed"])
            self.assertEqual(report["cases"][0]["model_calls"], 1)
            self.assertEqual(report["runtime_config"]["limits"]["max_model_calls"], 1)
            self.assertEqual(report["runtime_config"]["context_chars"], 12000)
            # Reject an incomparable baseline before making directories or a model.
            output = Path(tmp) / "not-created"
            with self.assertRaisesRegex(ValueError, "same runtime configuration"):
                benchmark(path, output, baseline=report["report"])
            self.assertFalse(output.exists())

    def test_invalid_runtime_configuration_creates_no_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "not-created"
            for kwargs in ({"limits": {"max_model_calls": True}}, {"max_context_chars": True}):
                with self.assertRaises(ValueError):
                    benchmark(output_dir=output, **kwargs)
                self.assertFalse(output.exists())

    def test_outer_wait_respects_long_runtime_budget(self):
        waits = []
        original = Application.wait
        def record_wait(app, identifier, timeout=30):
            waits.append(timeout)
            return original(app, identifier, timeout=5)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Application, "wait", record_wait):
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps({"id": "chat", "prompt": "你好"}), encoding="utf-8")
            self.assertTrue(benchmark(path, Path(tmp) / "out", limits={"run_timeout": 600})["passed"])
        self.assertEqual(waits, [630])
