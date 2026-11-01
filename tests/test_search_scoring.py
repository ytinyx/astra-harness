import copy
from pathlib import Path
import unittest

from astra_harness.benchmark import DEFAULT_DATASET, load_cases, score_case


class SearchScoringTests(unittest.TestCase):
    def setUp(self):
        cases, _ = load_cases(DEFAULT_DATASET)
        self.cases = {case["id"]: case for case in cases}
        # Independent expectations from the three fixed sample files, not search().
        self.match = {
            "matches": [{
                "path": "02-engineering.md",
                "line": 4,
                "text": "- 事项：验证任务取消行为；负责人：吴桐；截止：2026-11-08",
            }],
            "scanned_files": 3,
            "truncated": False,
        }
        self.empty = {"matches": [], "scanned_files": 3, "truncated": False}
        self.answer = "在 `02-engineering.md` 的第 4 行找到验证任务取消行为的待办。"

    def score(self, case_id, value, output=None):
        state = {
            "status": "completed",
            "tool_calls": 1,
            "output": self.answer if output is None else output,
            "messages": [{"tool_result": {
                "name": "files.search", "value": value, "error_code": None,
            }}],
        }
        return score_case(self.cases[case_id], state, Path("."))

    def test_expected_match_accepts_a_naturally_worded_citation(self):
        # Citation punctuation and prose need not match the demo model's template.
        self.assertTrue(all(self.score("search-match", self.match).values()))

    def test_empty_search_cannot_pass_with_a_fabricated_matching_answer(self):
        checks = self.score("search-match", self.empty)
        self.assertTrue(checks["answer:取消"])
        self.assertTrue(checks["answer:02-engineering.md"])
        self.assertFalse(checks["tool_value:files.search"])
        self.assertFalse(all(checks.values()))

    def test_wrong_path_line_or_text_cannot_pass_despite_a_correct_answer(self):
        for key, incorrect in (
            ("path", "01-product.md"),
            ("line", 3),
            ("text", "- 事项：伪造取消事项；负责人：未知；截止：2026-11-08"),
        ):
            with self.subTest(field=key):
                value = copy.deepcopy(self.match)
                value["matches"][0][key] = incorrect
                checks = self.score("search-match", value)
                self.assertFalse(checks["tool_value:files.search"])
                self.assertFalse(all(checks.values()))

    def test_unrelated_nonempty_result_cannot_pass_the_empty_case(self):
        checks = self.score("search-empty", self.match, "未找到指定关键词。")
        self.assertFalse(checks["tool_value:files.search"])
        self.assertFalse(all(checks.values()))

    def test_empty_case_requires_complete_search_but_no_fixed_answer_wording(self):
        self.assertTrue(all(self.score("search-empty", self.empty, "No matches found.").values()))
        for value in (
            {**self.empty, "scanned_files": 0},
            {**self.empty, "truncated": True},
        ):
            with self.subTest(value=value):
                self.assertFalse(all(self.score("search-empty", value).values()))

    def test_fabricated_answer_without_a_tool_result_cannot_pass(self):
        state = {
            "status": "completed", "tool_calls": 0,
            "output": self.answer, "messages": [],
        }
        checks = score_case(self.cases["search-match"], state, Path("."))
        self.assertTrue(checks["answer:02-engineering.md"])
        self.assertFalse(checks["tool:files.search"])
        self.assertFalse(checks["tool_value:files.search"])
        self.assertFalse(all(checks.values()))

    def test_correct_tool_result_requires_the_answer_to_identify_its_source(self):
        checks = self.score("search-match", self.match, "在 01-product.md 找到了取消待办。")
        self.assertTrue(checks["tool_value:files.search"])
        self.assertFalse(checks["answer:02-engineering.md"])
        self.assertFalse(all(checks.values()))


if __name__ == "__main__":
    unittest.main()
