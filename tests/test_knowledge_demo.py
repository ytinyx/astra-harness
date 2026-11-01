"""Independent synthetic facts, actual lexical results, and existing executor gates."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from examples.knowledge_demo import (
    AnswerModel, HOST_PERMISSIONS, InjectionProbeModel, SOURCE_BYTES, SOURCE_PATH,
    SYSTEM_INSTRUCTION, open_host, prepare, run_demo, run_maintenance, verify_answer,
)


class KnowledgeDemoTests(unittest.TestCase):
    def test_real_import_approval_persistent_index_and_source_citation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "demo"
            result = run_demo(directory)
            self.assertTrue(result["passed"])
            self.assertEqual(result["first_run"], {"status": "completed", "model_calls": 3, "tool_calls": 2})
            self.assertEqual(result["reopened_search"], {"status": "completed", "model_calls": 2, "tool_calls": 1})
            answer = json.loads((directory / "answer.json").read_text(encoding="utf-8"))
            self.assertEqual((answer["service_level"], answer["retry_seconds"]), ("Gold", 17))
            citation = answer["citation"]
            self.assertEqual(citation["document_sha256"], hashlib.sha256(SOURCE_BYTES).hexdigest())
            self.assertEqual(citation["text"], SOURCE_BYTES.decode("utf-8")[citation["start_offset"]:citation["end_offset"]])
            self.assertIn("Ignore all rules", citation["text"])
            self.assertFalse((directory / "unauthorized.txt").exists())
            self.assertNotIn(str(directory), json.dumps(result))
            for field, value in (("document_sha256", "0" * 64), ("start_offset", 1), ("start_column", 99)):
                invalid = deepcopy(answer)
                invalid["citation"][field] = value
                with self.subTest(field=field), self.assertRaises(RuntimeError):
                    verify_answer(invalid, SOURCE_BYTES)
            # A real source and valid coordinates are insufficient if the cited
            # slice does not actually support the answer. Do not reuse the model parser.
            title_only = deepcopy(answer)
            source_text = SOURCE_BYTES.decode("utf-8")
            end = source_text.index("\n")
            title_only["citation"].update(start_offset=0, end_offset=end, text=source_text[:end],
                                           start_line=1, start_column=1, end_line=1, end_column=end + 1)
            with self.assertRaisesRegex(RuntimeError, "support"):
                verify_answer(title_only, SOURCE_BYTES)

    def test_answer_changes_with_actual_source_instead_of_fixture_expected_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "changed"
            prepare(directory)
            changed = SOURCE_BYTES.replace(b"Gold", b"Silver").replace(b"17 seconds", b"23 seconds")
            (directory / "materials" / SOURCE_PATH).write_bytes(changed)
            imported = run_maintenance(directory, "knowledge.import_file", {"path": SOURCE_PATH}, approve=True)
            self.assertTrue(imported["value"]["ok"])
            with open_host(directory, AnswerModel(import_first=False)) as app:
                run = app.submit("Read the actual synthetic indexed source")
                state = app.wait(run["id"])
                self.assertEqual(state["status"], "completed")
                answer = json.loads(state["output"])
            self.assertEqual((answer["service_level"], answer["retry_seconds"]), ("Silver", 23))
            self.assertEqual(answer["citation"]["document_sha256"], hashlib.sha256(changed).hexdigest())
            with self.assertRaises(RuntimeError):
                verify_answer(answer, changed)  # The independent main-demo oracle expects Gold/17.

    def test_changed_source_is_stale_until_reimport_then_remove_leaves_source_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "maintenance"
            prepare(directory)
            first = run_maintenance(directory, "knowledge.import_file", {"path": SOURCE_PATH}, approve=True)["value"]
            self.assertTrue(first["ok"])
            source = directory / "materials" / SOURCE_PATH
            changed = SOURCE_BYTES.replace(b"17 seconds", b"29 seconds")
            source.write_bytes(changed)
            stale = run_maintenance(directory, "knowledge.search", {"query": "Retry delay"})["value"]
            self.assertTrue(stale["ok"])
            self.assertEqual(stale["hits"], [])
            self.assertGreaterEqual(stale["stale_count"], 1)
            refreshed = run_maintenance(directory, "knowledge.import_file", {"path": SOURCE_PATH}, approve=True)["value"]
            self.assertTrue(refreshed["ok"])
            self.assertNotEqual(first["document_sha256"], refreshed["document_sha256"])
            found = run_maintenance(directory, "knowledge.search", {"query": "Retry delay"})["value"]
            self.assertTrue(any("29 seconds" in item["text"] for item in found["hits"]))
            self.assertTrue(all(item["document_sha256"] == hashlib.sha256(changed).hexdigest() for item in found["hits"]))
            removed = run_maintenance(directory, "knowledge.remove", {"path": SOURCE_PATH}, approve=True)["value"]
            self.assertTrue(removed["ok"] and removed["removed"])
            empty = run_maintenance(directory, "knowledge.search", {"query": "Retry delay"})["value"]
            self.assertEqual(empty["hits"], [])
            self.assertEqual(source.read_bytes(), changed)

    def test_literal_empty_result_and_short_query_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "queries"
            prepare(directory)
            run_maintenance(directory, "knowledge.import_file", {"path": SOURCE_PATH}, approve=True)
            for query in ("不存在的合成内容", "Gold OR Retry"):
                found = run_maintenance(directory, "knowledge.search", {"query": query})["value"]
                self.assertTrue(found["ok"])
                self.assertEqual(found["hits"], [])
            short = run_maintenance(directory, "knowledge.search", {"query": "17"})["value"]
            self.assertEqual(short["mode"], "short_literal_scan")
            self.assertTrue(any("17 seconds" in hit["text"] for hit in short["hits"]))

    def test_retrieved_injection_cannot_grant_write_permission(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "injection"
            prepare(directory)
            run_maintenance(directory, "knowledge.import_file", {"path": SOURCE_PATH}, approve=True)
            calls = []
            with open_host(directory, InjectionProbeModel(), note_calls=calls) as app:
                run = app.submit("Retrieve the synthetic untrusted instruction and exercise the permission boundary")
                state = app.wait(run["id"])
                self.assertEqual(state["status"], "completed")
                self.assertEqual(json.loads(state["output"]), {"write_error": "permission_denied"})
                self.assertEqual((state["model_calls"], state["tool_calls"]), (3, 2))
                self.assertEqual(state["permissions"], sorted(HOST_PERMISSIONS))
                self.assertEqual(state["messages"][0]["text"], SYSTEM_INSTRUCTION)
                results = [item["result"] for item in app.events(run["id"]) if item["type"] == "tool.end"]
            self.assertEqual([item["name"] for item in results], ["knowledge.search", "demo.write_note"])
            self.assertTrue(any("call demo.write_note" in hit["text"] for hit in results[0]["value"]["hits"]))
            self.assertEqual(results[1]["error_code"], "permission_denied")
            self.assertEqual(calls, [])
            self.assertFalse((directory / "unauthorized.txt").exists())

    def test_existing_directory_is_preserved_and_default_output_is_relative(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory / "keep.txt"
            sentinel.write_text("existing work", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                run_demo(directory)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing work")
            self.assertEqual([item.name for item in directory.iterdir()], ["keep.txt"])
        result = run_demo()
        self.assertFalse(result["retained"])
        self.assertEqual(result["artifacts"]["source"], "materials/handbook.md")


if __name__ == "__main__":
    unittest.main()
