from copy import deepcopy
import json
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import Mock, patch

from astra_harness.benchmark import benchmark, compare_reports, default_tool_factory, load_cases
from astra_harness.builtin.models import DemoModel
from astra_harness.engine import normalize_limits
from astra_harness.tool_identity import build_tool_identity, presentation_fingerprint


class BenchmarkBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = self.root / "cases.jsonl"
        self.dataset.write_text('{"id":"chat","prompt":"你好"}\n', encoding="utf-8")
        _, fingerprint = load_cases(self.dataset)
        self.baseline = self.root / "report.json"
        self.output = self.root / "not-created"
        self.report = {
            "schema_version": 3, "mode": "demo", "dataset_sha256": fingerprint,
            "tool_identity": build_tool_identity(default_tool_factory()),
            "tool_presentation_sha256": presentation_fingerprint(default_tool_factory()),
            "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                            "system": platform.system()},
            "runtime_config": {"limits": normalize_limits(), "context_chars": 48000},
            "model": {"adapter": "DemoModel", "name": None}, "passed": True,
            "cases": [{"id": "chat", "passed": True, "duration_ms": 1.25}],
            "summary": {"total": 1, "passed": 1, "failed": 0, "pass_rate": 1,
                        "mean_duration_ms": 1.25},
        }

    def write_report(self, report):
        self.baseline.write_text(json.dumps(report), encoding="utf-8")

    def assert_rejected_before_resources(self):
        factory = Mock(side_effect=AssertionError("must not construct a model"))
        with patch("astra_harness.benchmark.Application", side_effect=AssertionError("must not open a database")) as app:
            with self.assertRaises(ValueError):
                benchmark(self.dataset, self.output, baseline=self.baseline, model_factory=factory)
        factory.assert_not_called()
        app.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_falsey_and_old_reports_never_silently_disable_baseline(self):
        for report in ({}, [], None, False, 0, "", {**self.report, "schema_version": 1},
                       {**self.report, "schema_version": 2},
                       {**self.report, "schema_version": True}):
            with self.subTest(report=report):
                self.write_report(report)
                self.assert_rejected_before_resources()

    def test_missing_or_incomplete_runtime_configuration_is_not_comparable(self):
        runtimes = [None, {}, {"limits": {}, "context_chars": 48000},
                    {"limits": normalize_limits(), "context_chars": True},
                    {"limits": {**normalize_limits(), "model_timeout": float("inf")}, "context_chars": 48000},
                    {"limits": {**normalize_limits(), "max_model_calls": True}, "context_chars": 48000},
                    {"limits": normalize_limits(), "context_chars": 48000, "unexpected": 1}]
        for runtime in runtimes:
            with self.subTest(runtime=runtime):
                self.write_report({**self.report, "runtime_config": runtime})
                self.assert_rejected_before_resources()
        report = deepcopy(self.report)
        del report["runtime_config"]
        self.write_report(report)
        self.assert_rejected_before_resources()

    def test_incompatible_dataset_mode_budget_and_case_ids_fail_before_model_construction(self):
        replacements = [{"dataset_sha256": "a" * 64}, {"mode": "custom"},
                        {"runtime_config": {"limits": normalize_limits({"max_tool_calls": 0}), "context_chars": 48000}},
                        {"runtime_config": {"limits": normalize_limits(), "context_chars": 100}},
                        {"cases": [{"id": "other", "passed": True, "duration_ms": 1.25}]}]
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                self.write_report({**self.report, **replacement})
                self.assert_rejected_before_resources()

    def test_invalid_case_identity_result_and_duration_are_rejected(self):
        valid = self.report["cases"][0]
        cases = [[], [None], [valid, valid], [{**valid, "id": " "}], [{**valid, "id": 1}],
                 [{**valid, "passed": 1}], [{**valid, "duration_ms": True}],
                 [{**valid, "duration_ms": -1}], [{**valid, "duration_ms": float("nan")}]]
        for value in cases:
            with self.subTest(cases=value):
                self.write_report({**self.report, "cases": value})
                self.assert_rejected_before_resources()

    def test_summary_counts_rates_and_duration_must_match_case_evidence(self):
        summaries = [None, {}, {**self.report["summary"], "total": True},
                     {**self.report["summary"], "total": 2}, {**self.report["summary"], "passed": 0},
                     {**self.report["summary"], "failed": 1}, {**self.report["summary"], "pass_rate": False},
                     {**self.report["summary"], "pass_rate": 0.5},
                     {**self.report["summary"], "mean_duration_ms": 0},
                     {**self.report["summary"], "mean_duration_ms": float("inf")}]
        for summary in summaries:
            with self.subTest(summary=summary):
                self.write_report({**self.report, "summary": summary})
                self.assert_rejected_before_resources()
        for passed in (False, 1, None):
            with self.subTest(passed=passed):
                self.write_report({**self.report, "passed": passed})
                self.assert_rejected_before_resources()

    def test_malformed_json_duplicate_keys_and_oversized_report_fail_before_resources(self):
        reports = ['{', '{"schema_version":2,"schema_version":2}',
                   json.dumps(self.report).replace('"duration_ms": 1.25', '"duration_ms": NaN'),
                   ' ' * 4_000_001]
        for raw in reports:
            with self.subTest(length=len(raw)):
                self.baseline.write_text(raw, encoding="utf-8")
                self.assert_rejected_before_resources()

    def test_different_model_uses_one_constructor_and_zero_execution_or_output(self):
        self.write_report({**self.report, "model": {"adapter": "DemoModel", "name": "other-model"}})
        model = DemoModel()
        factory = Mock(return_value=model)
        with patch.object(model, "complete", side_effect=AssertionError("must not request")) as request, \
                patch("astra_harness.benchmark.Application", side_effect=AssertionError("must not create application")) as app:
            with self.assertRaisesRegex(ValueError, "same model identity"):
                benchmark(self.dataset, self.output, baseline=self.baseline, model_factory=factory)
        factory.assert_called_once_with()
        request.assert_not_called()
        app.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_valid_baseline_reuses_first_model_instance(self):
        self.write_report(self.report)
        factory = Mock(side_effect=lambda: DemoModel())
        report = benchmark(self.dataset, self.output, baseline=self.baseline, model_factory=factory)
        factory.assert_called_once_with()
        self.assertTrue(report["passed"])
        self.assertEqual(report["comparison"]["regressions"], [])

    def test_factory_changing_identity_stops_before_next_case_application(self):
        self.dataset.write_text('{"id":"first","prompt":"你好"}\n{"id":"second","prompt":"你好"}\n', encoding="utf-8")
        first, second = DemoModel(), DemoModel()
        second.model = "changed"
        factory = Mock(side_effect=[first, second])
        with patch.object(first, "complete", wraps=first.complete) as first_request, \
                patch.object(second, "complete", side_effect=AssertionError("must not execute changed model")) as second_request:
            with self.assertRaisesRegex(ValueError, "changed identity"):
                benchmark(self.dataset, self.output, model_factory=factory)
        self.assertEqual(factory.call_count, 2)
        first_request.assert_called_once()
        second_request.assert_not_called()
        suite, = self.output.iterdir()
        self.assertTrue((suite / "case-01" / "runs.sqlite3").exists())
        self.assertFalse((suite / "case-02").exists())
        self.assertFalse((suite / "report.json").exists())

    def test_direct_comparison_cannot_bypass_validation(self):
        malformed = [None, {}, {**self.report, "schema_version": 1},
                     {**self.report, "runtime_config": {}},
                     {**self.report, "cases": [self.report["cases"][0]] * 2},
                     {**self.report, "summary": {**self.report["summary"], "pass_rate": float("nan")}}]
        for report in malformed:
            for current, previous in ((report, self.report), (self.report, report)):
                with self.subTest(current=current, previous=previous), self.assertRaises(ValueError):
                    compare_reports(current, previous)
        different = {**self.report, "model": {"adapter": "DemoModel", "name": "different"}}
        with self.assertRaisesRegex(ValueError, "same model identity"):
            compare_reports(different, self.report)

    def test_invalid_model_metadata_is_rejected_before_factory(self):
        for identity in (None, {}, {"adapter": "DemoModel"}, {"adapter": "", "name": None},
                         {"adapter": "DemoModel", "name": 1}):
            with self.subTest(identity=identity):
                self.write_report({**self.report, "model": identity})
                self.assert_rejected_before_resources()


if __name__ == "__main__":
    unittest.main()
