"""Local corpus/index oracles; no provider or user documents."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.builtin import knowledge
from astra_harness.builtin.knowledge import KnowledgePlugin
from astra_harness.contracts import ToolCall
from astra_harness.plugins import PluginManager
from astra_harness.tool_identity import build_tool_identity, validate_tool_identity
from astra_harness.tools import Tool, ToolExecutor, ToolRegistry


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="astra-knowledge-")
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "docs"
        self.root.mkdir()
        self.database = self.directory / "index.sqlite3"
        self.plugin = KnowledgePlugin(self.root, self.database)

    def tearDown(self):
        self.plugin.close()
        self.temporary.cleanup()

    def document(self, name="guide.md", text="服务等级 Gold\r\n恢复等待 17 秒。\n"):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        return text

    def test_persistent_idempotent_revision_and_no_source_mutation(self):
        text = self.document()
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        first = self.plugin.import_file("guide.md")
        self.assertEqual(first, {"ok": True, "path": "guide.md", "document_sha256": expected,
                                 "chunk_count": 1, "unchanged": False})
        second = self.plugin.import_file("guide.md")
        self.assertTrue(second["unchanged"])
        before = self.plugin.search("Gold")
        self.plugin.close()
        self.plugin = KnowledgePlugin(self.root, self.database)
        self.assertEqual(self.plugin.search("Gold"), before)
        self.assertEqual((self.root / "guide.md").read_bytes(), text.encode("utf-8"))
        self.assertEqual(self.plugin._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)

    def test_actual_unicode_crlf_and_long_line_offsets(self):
        text = self.document(text="标题🙂\r\n" + "兰" * 988 + "Orchid" + "花" * 1300 + "\r末行\n")
        self.plugin.import_file("guide.md")
        result = self.plugin.search("Orchid", limit=5)
        self.assertTrue(result["hits"])
        starts = [0] + [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]
        for hit in result["hits"]:
            self.assertEqual(hit["text"], text[hit["start_offset"]:hit["end_offset"]])
            self.assertIn("Orchid", hit["text"])
            self.assertLessEqual(len(hit["text"]), 1000)
            for prefix in ("start", "end"):
                offset = hit[prefix + "_offset"]
                line = max(i for i, value in enumerate(starts) if value <= offset)
                self.assertEqual((hit[prefix+"_line"], hit[prefix+"_column"]), (line+1, offset-starts[line]+1))
        self.assertEqual(result["hits"][0]["document_sha256"], hashlib.sha256(text.encode()).hexdigest())

    def test_overlap_preserves_phrase_across_first_chunk_boundary(self):
        self.document(text="x" * 997 + "恢复等待" + "y" * 30)
        self.plugin.import_file("guide.md")
        result = self.plugin.search("恢复等待")
        self.assertTrue(any("恢复等待" in hit["text"] for hit in result["hits"]))

    def test_crlf_at_exact_chunk_edge_is_not_split(self):
        text = self.document(text="x" * 999 + "\r\nGold 17")
        self.plugin.import_file("guide.md")
        chunks = list(self.plugin._db.execute("SELECT * FROM chunks ORDER BY ordinal"))
        self.assertEqual(chunks[0]["end_offset"], 999)
        self.assertEqual((chunks[0]["end_line"], chunks[0]["end_column"]), (1, 1000))
        for row in chunks:
            self.assertEqual(row["text"], text[row["start_offset"]:row["end_offset"]])
            self.assertFalse(row["text"].endswith("\r"))
            self.assertFalse(row["text"].startswith("\n"))
        self.assertTrue(self.plugin.search("Gold")["hits"])

    def test_corrupt_cached_text_coordinates_or_id_never_become_citations(self):
        self.document(text="Verified budget is 42.\n")
        for field, value in (("text", "Invented budget is 9000."), ("start_column", 99), ("end_line", 20)):
            with self.subTest(field=field):
                self.plugin.remove("guide.md")
                self.plugin.import_file("guide.md")
                self.plugin._db.execute("UPDATE chunks SET " + field + "=?", (value,))
                self.plugin._db.commit()
                self.assertEqual(self.plugin.search("budget"), {"ok": False, "error_code": "index_inconsistent"})
        self.plugin.remove("guide.md")
        self.plugin.import_file("guide.md")
        self.plugin._db.execute("UPDATE chunks SET chunk_id='synthetic-corruption'")
        self.plugin._db.execute("UPDATE chunks_fts SET chunk_id='synthetic-corruption'")
        self.plugin._db.commit()
        self.assertEqual(self.plugin.search("budget")["error_code"], "index_inconsistent")
        self.plugin.remove("guide.md")
        self.plugin.import_file("guide.md")
        self.assertEqual(self.plugin.search("budget")["hits"][0]["text"], "Verified budget is 42.\n")

    def test_plain_query_does_not_enable_boolean_or_wildcard_syntax(self):
        self.document(text='Alpha\nBeta\nQuoted "OR" phrase\nLiteral a*b\n')
        self.plugin.import_file("guide.md")
        for query in ("Alpha OR Beta", "Alpha*", '" OR "'):
            self.assertEqual(self.plugin.search(query)["hits"], [])
        for query in ('"OR"', 'a*b', 'alpha'):
            self.assertTrue(self.plugin.search(query)["hits"])

    def test_short_chinese_and_casefold_are_literal_not_semantic(self):
        self.document(text="恢复预算 17 秒\nStraße\n")
        self.plugin.import_file("guide.md")
        self.assertEqual(self.plugin.search("恢复")["mode"], "short_literal_scan")
        self.assertTrue(self.plugin.search("预")["hits"])
        self.assertTrue(self.plugin.search("STRASSE")["hits"])
        self.assertEqual(self.plugin.search("重新开始的时间")["hits"], [])

    def test_empty_document_and_empty_corpus(self):
        self.assertEqual(self.plugin.search("missing")["hits"], [])
        self.document(text="")
        self.assertEqual(self.plugin.import_file("guide.md")["chunk_count"], 0)
        self.assertEqual(self.plugin.search("anything")["hits"], [])
        self.assertEqual(self.plugin.remove("guide.md"), {"ok": True, "path": "guide.md", "removed": True})
        self.assertFalse(self.plugin.remove("guide.md")["removed"])

    def test_changed_deleted_and_unreadable_sources_are_not_served_from_cache(self):
        self.document(text="Original Gold 17\n")
        self.plugin.import_file("guide.md")
        self.document(text="Updated Gold 42\n")
        result = self.plugin.search("Gold")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["stale_count"], 1)
        self.assertNotIn("Original", json.dumps(result))
        self.assertFalse(self.plugin.import_file("guide.md")["unchanged"])
        self.assertIn("42", self.plugin.search("Gold")["hits"][0]["text"])
        with patch.object(self.plugin.files, "_read_bytes", side_effect=PermissionError):
            result = self.plugin.search("Gold")
            self.assertEqual(result["hits"], [])
            self.assertEqual(result["stale_count"], 1)
        (self.root / "guide.md").unlink()
        self.assertEqual(self.plugin.search("Gold")["hits"], [])
        self.assertTrue(self.plugin.remove("guide.md")["removed"])
        self.assertEqual(self.plugin.search("Gold")["stale_count"], 0)

    def test_remove_survives_deleted_parent_and_never_deletes_source(self):
        self.document("nested/guide.md", "Gold")
        self.plugin.import_file("nested/guide.md")
        self.assertTrue(self.plugin.remove("nested/guide.md")["removed"])
        self.assertEqual((self.root / "nested/guide.md").read_text(), "Gold")
        self.plugin.import_file("nested/guide.md")
        (self.root / "nested/guide.md").unlink()
        (self.root / "nested").rmdir()
        self.assertTrue(self.plugin.remove("nested/guide.md")["removed"])

    def test_update_rolls_back_old_complete_index_on_sql_failure(self):
        self.document(text="Gold 17")
        self.plugin.import_file("guide.md")
        rows_before = [tuple(row) for row in self.plugin._db.execute("SELECT * FROM chunks")]
        self.document(text="Gold 42")
        original = self.plugin._delete_document
        def fail_after_delete(path):
            original(path)
            raise sqlite3.OperationalError("synthetic write failure")
        with patch.object(self.plugin, "_delete_document", fail_after_delete):
            with self.assertRaises(sqlite3.OperationalError):
                self.plugin.import_file("guide.md")
        self.assertEqual([tuple(row) for row in self.plugin._db.execute("SELECT * FROM chunks")], rows_before)
        self.assertEqual(self.plugin.search("Gold")["hits"], [])  # preserved old index is stale
        self.assertTrue(self.plugin.import_file("guide.md")["ok"])
        self.assertIn("42", self.plugin.search("Gold")["hits"][0]["text"])

    def test_document_byte_and_chunk_caps_leave_previous_index_intact(self):
        self.document(text="Gold")
        self.plugin.import_file("guide.md")
        for setting, value in (("MAX_DOCUMENTS", 1), ("MAX_TOTAL_BYTES", 5), ("MAX_CHUNKS", 1)):
            self.document("extra.md", "Silver")
            with self.subTest(setting=setting), patch.object(knowledge, setting, value):
                self.assertEqual(self.plugin.import_file("extra.md"), {"ok": False, "error_code": "index_limit"})
            self.assertTrue(self.plugin.search("Gold")["hits"])
            self.assertEqual(self.plugin.search("Silver")["hits"], [])

    def test_return_chars_hits_and_candidate_scan_are_bounded(self):
        for i in range(7):
            self.document(f"{i}.md", "Gold " + "x" * 995)
            self.plugin.import_file(f"{i}.md")
        result = self.plugin.search("Gold", limit=5)
        self.assertLessEqual(len(result["hits"]), 5)
        self.assertLessEqual(sum(len(hit["text"]) for hit in result["hits"]), 4000)
        self.assertTrue(result["truncated"])
        with patch.object(knowledge, "MAX_CANDIDATES", 2):
            result = self.plugin.search("G", limit=5)
            self.assertLessEqual(len(result["hits"]), 2)
            self.assertTrue(result["truncated"])

    def test_stable_result_order_and_ids(self):
        for name in ("z.md", "b.md", "a.md"):
            self.document(name, "Gold 17")
            self.plugin.import_file(name)
        expected = self.plugin.search("Gold")
        self.assertEqual([hit["path"] for hit in expected["hits"]], ["a.md", "b.md", "z.md"])
        self.plugin.close()
        self.plugin = KnowledgePlugin(self.root, self.database)
        self.assertEqual(self.plugin.search("Gold"), expected)

    def test_path_format_and_file_limits(self):
        self.document()
        for path in ("../guide.md", "C:/guide.md", "nested/../guide.md", "guide.md:secret", "./guide.md", "guide.json"):
            with self.subTest(path=path):
                self.assertFalse(self.plugin.import_file(path)["ok"])
                self.assertFalse(self.plugin.remove(path)["ok"])
        self.assertEqual(self.plugin.import_file("missing.md")["error_code"], "path_missing")
        for content, code in ((b"x" * 128001, "file_limit"), (b"\xff", "invalid_utf8"), (b"a\0b", "binary_file")):
            (self.root / "guide.md").write_bytes(content)
            self.assertEqual(self.plugin.import_file("guide.md")["error_code"], code)

    def test_closed_plugin_and_invalid_queries(self):
        for query, limit in (("", 1), ("   ", 1), ("x" * 201, 1), ("x\0", 1), ("\ud800", 1), ("x", True), ("x", 0), ("x", 6)):
            with self.subTest(query=repr(query), limit=limit):
                self.assertEqual(self.plugin.search(query, limit), {"ok": False, "error_code": "invalid_query"})
        self.plugin.close()
        self.plugin.close()
        for result in (self.plugin.import_file("guide.md"), self.plugin.remove("guide.md"), self.plugin.search("Gold")):
            self.assertEqual(result, {"ok": False, "error_code": "index_closed"})

    def test_wrong_database_is_not_mutated(self):
        other = self.directory / "other.sqlite3"
        connection = sqlite3.connect(other)
        try:
            with connection:
                connection.execute("CREATE TABLE private_data (value TEXT)")
                connection.execute("INSERT INTO private_data VALUES ('synthetic')")
        finally:
            # A sqlite3 transaction context manager does not close its handle.
            connection.close()
        before = other.read_bytes()
        with self.assertRaisesRegex(ValueError, "knowledge_index_incompatible"):
            KnowledgePlugin(self.root, other)
        self.assertEqual(other.read_bytes(), before)

    def test_workspace_binding_rejects_reuse_and_changes_identity(self):
        other = self.directory / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "workspace_changed"):
            KnowledgePlugin(other, self.database)
        alternate = KnowledgePlugin(other, self.directory / "other.sqlite3")
        try:
            self.assertNotEqual(self.plugin.tools[0].recovery_identity, alternate.tools[0].recovery_identity)
            self.assertNotIn(str(self.root), json.dumps(self.plugin.tools[0].spec().schema()))
        finally:
            alternate.close()

    def test_tool_identity_is_complete_stable_and_does_not_reveal_host_paths(self):
        identity = build_tool_identity(self.plugin.tools)
        validate_tool_identity(identity)
        self.assertEqual(identity, build_tool_identity(reversed(self.plugin.tools)))
        serialized = json.dumps(identity)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.database), serialized)

    def test_index_inside_documents_and_index_hardlink_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside_workspace"):
            KnowledgePlugin(self.root, self.root / "index.sqlite3")
        alias = self.directory / "alias.sqlite3"
        os.link(self.database, alias)
        with self.assertRaisesRegex(ValueError, "not_regular"):
            KnowledgePlugin(self.root, alias)
        alias.unlink()

    def test_source_hardlink_and_reparse_are_rejected(self):
        self.document()
        alias = self.root / "alias.md"
        os.link(self.root / "guide.md", alias)
        self.assertEqual(self.plugin.import_file("alias.md")["error_code"], "links_not_allowed")
        alias.unlink()
        # Controlled reparse metadata exercises the Windows junction guard even
        # on systems where creating directory links requires extra privileges.
        target = self.root / "guide.md"
        info = target.lstat()
        class Reparse:
            st_mode = info.st_mode
            st_file_attributes = 0x400
        original = Path.lstat
        with patch.object(Path, "lstat", lambda value: Reparse() if value == target else original(value)):
            self.assertEqual(self.plugin.import_file("guide.md")["error_code"], "links_not_allowed")

    @unittest.skipUnless(os.name == "nt", "Windows path alias rule")
    def test_windows_case_and_separator_aliases_share_one_document(self):
        self.document("Sub/Guide.MD", "Gold")
        first = self.plugin.import_file("Sub/Guide.MD")
        self.assertEqual(first["path"], "sub/guide.md")
        self.assertTrue(self.plugin.import_file("sub\\GUIDE.md")["unchanged"])
        self.assertEqual(self.plugin._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        self.assertTrue(self.plugin.remove("SUB/guide.MD")["removed"])

    def test_permissions_schema_and_approval_share_existing_executor(self):
        self.document()
        registry = ToolRegistry()
        manager = PluginManager(registry)
        manager.activate(self.plugin)
        registry.freeze()
        executor = ToolExecutor(registry)
        call = ToolCall("import-1", "knowledge.import_file", {"path": "guide.md"})
        permissions = frozenset(self.plugin.permissions)
        self.assertEqual(executor.execute(call).error_code, "permission_denied")
        self.assertEqual(executor.execute(call, permissions).error_code, "approval_required")
        self.assertEqual(self.plugin.search("Gold")["hits"], [])
        self.assertTrue(executor.execute(call, permissions, approved=True).value["ok"])
        denied = executor.execute(ToolCall("search-1", "knowledge.search", {"query": "Gold"}), frozenset({"knowledge.read"}))
        self.assertEqual(denied.error_code, "permission_denied")
        invalid = executor.execute(ToolCall("search-2", "knowledge.search", {"query": "Gold", "limit": True}), permissions)
        self.assertEqual(invalid.error_code, "invalid_arguments")
        self.assertTrue(executor.execute(ToolCall("search-3", "knowledge.search", {"query": "Gold"}), permissions).value["hits"])
        manager.close()

    def test_registration_failure_closes_index_and_preserves_existing_tools(self):
        existing = Tool("knowledge.search", "existing", (), lambda: 1)
        registry = ToolRegistry((existing,))
        manager = PluginManager(registry)
        with self.assertRaises(ValueError):
            manager.activate(self.plugin)
        self.assertIs(registry.get("knowledge.search"), existing)
        self.assertIsNone(registry.get("knowledge.import_file"))
        self.assertEqual(self.plugin.search("Gold")["error_code"], "index_closed")
        replacement = KnowledgePlugin(self.root, self.database)
        replacement.close()


if __name__ == "__main__":
    unittest.main()
