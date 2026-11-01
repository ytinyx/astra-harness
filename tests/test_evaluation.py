"""The offline acceptance command must return a usable report on all Python versions."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.evaluation import evaluate, EXPECTED


class EvaluationPathTests(unittest.TestCase):
    def assert_report(self, report, output):
        self.assertTrue(report["passed"])
        self.assertFalse(report["real_model_verified"])
        path = Path(report["report"])
        self.assertTrue(path.resolve().is_relative_to(output.resolve()))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
        self.assertEqual(json.loads((path.parent / "workspace" / "todos.json").read_text(encoding="utf-8")), EXPECTED)
        self.assertTrue(report["no_write_before_approval"])
        self.assertTrue(report["approval_survived_restart"])

    def test_relative_output_with_pre312_mkdtemp_behavior(self):
        actual_mkdtemp = tempfile.mkdtemp
        def legacy_mkdtemp(*, prefix, dir):
            result = actual_mkdtemp(prefix=prefix, dir=dir)
            return os.path.relpath(result) if not Path(dir).is_absolute() else result
        with tempfile.TemporaryDirectory(prefix="astra-eval-", dir=Path.cwd()) as temporary:
            output = Path(temporary).relative_to(Path.cwd()) / "reports"
            with patch("astra_harness.evaluation.tempfile.mkdtemp", side_effect=legacy_mkdtemp):
                report = evaluate(output)
            self.assertFalse(Path(report["report"]).is_absolute())
            self.assert_report(report, output)

    def test_explicit_output_outside_current_directory(self):
        with tempfile.TemporaryDirectory(prefix="astra-eval-external-") as temporary:
            output = Path(temporary).resolve() / "reports"
            current = Path(temporary).resolve() / "current"
            current.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(current)
                report = evaluate(output)
                self.assertTrue(Path(report["report"]).is_absolute())
                self.assert_report(report, output)
            finally:
                os.chdir(previous)
