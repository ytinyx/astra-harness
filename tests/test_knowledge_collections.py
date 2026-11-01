"""Static collections, content-bound edits and bounded local browsing."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.builtin import knowledge
from astra_harness.builtin.knowledge import KnowledgePlugin
from astra_harness.builtin.knowledge_collections import (
    KnowledgeCollectionConfig, KnowledgeCollectionManager, load_knowledge_config,
)
from astra_harness.config import ConfigurationError
from astra_harness.contracts import ToolCall
from astra_harness.tools import Tool, ToolExecutor, ToolRegistry


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="astra-collections-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        for name in ("product", "notes"):
            (self.workspace / name).mkdir()
        self.config_path = self.base / "collections.json"
        self.database = self.base / "runtime.sqlite3"
        self.items = [{"id": name, "label": name.title(), "path": name,
                       "index": "indexes/" + name + ".sqlite3"} for name in ("product", "notes")]

    def load(self, items=None):
        self.config_path.write_text(json.dumps({"collections": self.items if items is None else items}), encoding="utf-8")
        return load_knowledge_config(self.config_path, workspace=self.workspace, database=self.database)

    def manager(self):
        manager = KnowledgeCollectionManager(self.load())
        self.addCleanup(manager.close)
        registry = ToolRegistry()
        manager.activate(registry)
        return manager, registry

    def plugin(self):
        plugin = KnowledgePlugin(self.workspace / "product", self.base / "index.sqlite3")
        self.addCleanup(plugin.close)
        return plugin

    def write(self, name="guide.md", text="Gold recovery budget 17.\n", collection="product"):
        target = self.workspace / collection / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = text.encode("utf-8")
        target.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def snapshot(self, plugin):
        return tuple(plugin._db.iterdump())

    def test_two_roots_same_path_isolate_index_queries_and_removal(self):
        self.write(text="Gold recovery budget 17.")
        self.write(text="Silver recovery budget 42.", collection="notes")
        manager, _ = self.manager()
        product, notes = manager.get("product"), manager.get("notes")
        product.import_file("guide.md")
        notes.import_file("guide.md")
        self.assertEqual(product.search("Silver")["hits"], [])
        self.assertEqual(notes.search("Gold")["hits"], [])
        self.assertIn("17", product.search("Gold")["hits"][0]["text"])
        self.assertIn("42", notes.search("Silver")["hits"][0]["text"])
        self.assertTrue(product.remove("guide.md")["removed"])
        self.assertTrue(notes.search("Silver")["hits"])
        self.assertTrue((self.workspace / "product/guide.md").is_file())
        self.assertIsNone(manager.get("missing"))

    def test_namespaced_executor_permissions_and_existing_approval_gate(self):
        self.write()
        manager, registry = self.manager()
        executor = ToolExecutor(registry)
        permissions = frozenset({"files.read", "knowledge.product.read", "knowledge.product.write"})
        for suffix, arguments in (("catalog", {}), ("inspect", {"path": "guide.md"}),
                                  ("search", {"query": "Gold"})):
            denied = executor.execute(ToolCall("deny-" + suffix, "knowledge.notes." + suffix, arguments), permissions)
            self.assertEqual(denied.error_code, "permission_denied")
        call = ToolCall("import", "knowledge.product.import_file", {"path": "guide.md"})
        self.assertEqual(executor.execute(call, permissions).error_code, "approval_required")
        self.assertTrue(executor.execute(call, permissions, approved=True).value["ok"])
        self.assertTrue(executor.execute(ToolCall("read", "knowledge.product.catalog", {}), permissions).value["documents"])
        public = json.dumps(manager.collections())
        self.assertNotIn(str(self.base), public)
        self.assertEqual(len(manager.collections()[0]["tools"]), 5)
        self.assertEqual(set(manager.permissions), {"files.read", "knowledge.product.read", "knowledge.product.write",
                                                   "knowledge.notes.read", "knowledge.notes.write"})

    def test_default_manual_config_keeps_legacy_names_and_path_calls(self):
        self.write()
        config = KnowledgeCollectionConfig("default", "Default", ".", self.workspace, self.base / "legacy.sqlite3")
        manager = KnowledgeCollectionManager((config,))
        self.addCleanup(manager.close)
        registry = ToolRegistry()
        manager.activate(registry)
        plugin = manager.get("default")
        self.assertIsNotNone(registry.get("knowledge.import_file"))
        self.assertIsNotNone(registry.get("knowledge.search"))
        self.assertIsNotNone(registry.get("knowledge.remove"))
        self.assertTrue(plugin.import_file("product/guide.md")["ok"])
        self.assertTrue(plugin.remove("product/guide.md")["removed"])

    def test_import_binds_source_and_absent_index_without_mutating_on_conflict(self):
        first = self.write()
        plugin = self.plugin()
        inspected = plugin.inspect("guide.md")
        self.assertEqual(inspected["source_sha256"], first)
        self.assertEqual(inspected["indexed_sha256"], "")
        self.assertEqual(inspected["status"], "unindexed")
        before = self.snapshot(plugin)
        second = self.write(text="Gold recovery budget 42.")
        self.assertEqual(plugin.import_file("guide.md", first, ""), {"ok": False, "error_code": "source_changed"})
        self.assertEqual(self.snapshot(plugin), before)
        self.assertTrue(plugin.import_file("guide.md", second, "")["ok"])
        before = self.snapshot(plugin)
        self.assertEqual(plugin.import_file("guide.md", second, "")["error_code"], "index_changed")
        self.assertEqual(self.snapshot(plugin), before)
        self.assertTrue(plugin.import_file("guide.md", second, second)["unchanged"])

    def test_other_connection_update_invalidates_both_import_and_remove_guards(self):
        first = self.write()
        plugin = self.plugin()
        plugin.import_file("guide.md")
        second_plugin = KnowledgePlugin(self.workspace / "product", self.base / "index.sqlite3")
        self.addCleanup(second_plugin.close)
        second = self.write(text="Gold recovery budget 42.")
        second_plugin.import_file("guide.md")
        before = self.snapshot(plugin)
        self.assertEqual(plugin.import_file("guide.md", second, first)["error_code"], "index_changed")
        self.assertEqual(plugin.remove("guide.md", first)["error_code"], "index_changed")
        self.assertEqual(self.snapshot(plugin), before)
        self.assertTrue(plugin.remove("guide.md", second)["removed"])
        self.assertFalse(plugin.remove("guide.md", "")["removed"])

    def test_source_binding_uses_exact_read_bytes_not_a_second_read(self):
        original_digest = self.write()
        plugin = self.plugin()
        original_source = plugin._source
        def read_then_change(path):
            result = original_source(path)
            self.write(text="Gold different after authorized read.")
            return result
        with patch.object(plugin, "_source", side_effect=read_then_change) as read:
            result = plugin.import_file("guide.md", original_digest, "")
        self.assertTrue(result["ok"])
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result["document_sha256"], original_digest)
        self.assertEqual(plugin.inspect("guide.md")["status"], "stale")
        self.assertEqual(plugin.search("Gold")["hits"], [])

    def test_index_change_after_source_read_is_checked_in_write_transaction(self):
        first = self.write()
        plugin = self.plugin()
        plugin.import_file("guide.md")
        other = KnowledgePlugin(self.workspace / "product", self.base / "index.sqlite3")
        self.addCleanup(other.close)
        second = self.write(text="Gold updated source")
        original_source = plugin._source
        def read_then_remove(path):
            value = original_source(path)
            self.assertTrue(other.remove(path, first)["removed"])
            return value
        with patch.object(plugin, "_source", side_effect=read_then_remove):
            result = plugin.import_file("guide.md", second, first)
        self.assertEqual(result, {"ok": False, "error_code": "index_changed"})
        self.assertEqual(plugin._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_invalid_digest_never_changes_index(self):
        self.write()
        plugin = self.plugin()
        before = self.snapshot(plugin)
        for digest in ("x", "F" * 64, "g" * 64, 12, False, []):
            with self.subTest(digest=digest):
                self.assertEqual(plugin.import_file("guide.md", digest)["error_code"], "invalid_digest")
                self.assertEqual(plugin.import_file("guide.md", expected_index_sha256=digest)["error_code"], "invalid_digest")
                self.assertEqual(plugin.remove("guide.md", digest)["error_code"], "invalid_digest")
        self.assertEqual(plugin.import_file("guide.md", "")["error_code"], "invalid_digest")
        self.assertEqual(self.snapshot(plugin), before)

    def test_catalog_only_stats_unimported_candidates_and_never_auto_imports(self):
        self.write()
        self.write("bad.txt", "x\0y")
        plugin = self.plugin()
        before = self.snapshot(plugin)
        with patch.object(plugin, "_source", side_effect=AssertionError("candidate body must not be read")):
            result = plugin.catalog()
        self.assertEqual({row["path"] for row in result["available_files"]}, {"guide.md", "bad.txt"})
        self.assertEqual(result["documents"], [])
        self.assertEqual(self.snapshot(plugin), before)
        self.assertEqual(plugin.inspect("bad.txt")["status"], "unreadable")
        self.assertEqual(plugin.inspect("bad.txt")["error_code"], "binary_file")

    def test_catalog_exact_indexed_states_and_inspect_unicode(self):
        digest = self.write("材料.md", "兰花计划 🌸\r\nGold 17\n")
        plugin = self.plugin()
        plugin.import_file("材料.md")
        row = plugin.catalog()["documents"][0]
        self.assertEqual(row["status"], "current")
        self.assertEqual(row["source_sha256"], digest)
        self.assertEqual(row["source_bytes"], len("兰花计划 🌸\r\nGold 17\n".encode()))
        self.write("材料.md", "Gold updated")
        self.assertEqual(plugin.catalog()["documents"][0]["status"], "stale")
        with patch.object(plugin.files, "_read_bytes", side_effect=PermissionError):
            self.assertEqual(plugin.catalog()["documents"][0]["status"], "unreadable")
        (self.workspace / "product/材料.md").unlink()
        row = plugin.catalog()["documents"][0]
        self.assertEqual(row["status"], "missing")
        self.assertEqual(row["indexed_sha256"], digest)
        self.assertEqual(row["source_sha256"], "")
        self.assertEqual(plugin.inspect("材料.md")["status"], "missing")

    def test_catalog_limits_metadata_entries_files_and_depth(self):
        for index in range(205):
            self.write(f"{index:03}.md", "Gold")
        plugin = self.plugin()
        result = plugin.catalog()
        self.assertEqual(len(result["available_files"]), 100)
        self.assertEqual(result["scanned_entries"], 200)
        self.assertTrue(result["truncated"])
        # An independent empty collection tests depth rather than entry pressure.
        other = KnowledgePlugin(self.workspace / "notes", self.base / "notes.sqlite3")
        self.addCleanup(other.close)
        self.write("/".join(["nested"] * 9) + "/guide.md", collection="notes")
        result = other.catalog()
        self.assertEqual(result["available_files"], [])
        self.assertTrue(result["truncated"])

    def test_forbidden_names_are_neither_candidates_nor_readable(self):
        plugin = self.plugin()
        for path in (".env", ".env.md", ".private/note.md", "credentials/note.md", "secrets.txt", "id_rsa.txt"):
            self.write(path)
            self.assertEqual(plugin.import_file(path)["error_code"], "forbidden_path")
            self.assertEqual(plugin.inspect(path)["error_code"], "forbidden_path")
        self.write("public.md")
        self.assertEqual(plugin.catalog()["available_files"], [{"path": "public.md", "bytes": 25}])

    @unittest.skipUnless(os.name == "nt", "Windows hidden attribute")
    def test_windows_hidden_attribute_blocks_catalog_inspect_and_import(self):
        import ctypes
        self.write()
        plugin = self.plugin()
        target = self.workspace / "product/guide.md"
        original = target.stat().st_file_attributes
        set_attributes = ctypes.windll.kernel32.SetFileAttributesW
        self.assertTrue(set_attributes(str(target), original | 0x2))
        try:
            self.assertEqual(plugin.catalog()["available_files"], [])
            self.assertEqual(plugin.inspect("guide.md")["status"], "unreadable")
            self.assertEqual(plugin.inspect("guide.md")["error_code"], "forbidden_path")
            self.assertEqual(plugin.import_file("guide.md")["error_code"], "forbidden_path")
        finally:
            self.assertTrue(set_attributes(str(target), original))

    def test_empty_and_closed_read_tools(self):
        plugin = self.plugin()
        self.assertEqual(plugin.catalog(), {"ok": True, "namespace": "knowledge", "available_files": [],
                                          "documents": [], "truncated": False, "scanned_entries": 0})
        plugin.close()
        for value in (plugin.catalog(), plugin.inspect("guide.md")):
            self.assertEqual(value, {"ok": False, "error_code": "index_closed"})

    def test_loader_is_static_and_index_paths_are_config_relative(self):
        configs = self.load()
        self.assertEqual([item.id for item in configs], ["notes", "product"])
        self.assertEqual(configs[1].index_path, self.base / "indexes/product.sqlite3")
        self.assertEqual(configs[1].root, self.workspace / "product")
        self.assertFalse((self.base / "indexes").exists())
        self.assertFalse(self.database.exists())

    def test_loader_rejects_unsafe_roots_and_overlaps_without_creating_indexes(self):
        (self.workspace / "product/nested").mkdir()
        (self.workspace / ".hidden").mkdir()
        (self.workspace / "credentials").mkdir()
        for path in (".", "../product", "product/../notes", str(self.workspace / "product"), ".hidden", "credentials", "missing"):
            with self.subTest(path=path):
                item = dict(self.items[0], path=path)
                with self.assertRaises(ConfigurationError):
                    self.load([item])
        with self.assertRaises(ConfigurationError):
            self.load([self.items[0], dict(self.items[1], path="product/nested")])
        self.assertFalse((self.base / "indexes").exists())

    def test_loader_rejects_duplicate_id_bad_fields_and_oversized_config(self):
        cases = [[], [dict(self.items[0], id="../oops")], [dict(self.items[0], label="")],
                 [dict(self.items[0], label="x" * 65)], [dict(self.items[0], surprise="x")],
                 [self.items[0], dict(self.items[1], id="product")], self.items * 5]
        for items in cases:
            with self.subTest(items=items), self.assertRaises(ConfigurationError):
                self.load(items)
        for data in ('{"collections":[],"collections":[]}', '{"collections":NaN}', " " * 65537):
            self.config_path.write_text(data, encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "invalid_knowledge_config"):
                load_knowledge_config(self.config_path, workspace=self.workspace, database=self.database)
        with self.assertRaisesRegex(ConfigurationError, "knowledge_requires_files"):
            load_knowledge_config(self.config_path, workspace=self.workspace, database=self.database, no_files=True)

    def test_loader_rejects_workspace_runtime_and_cross_index_sidecars(self):
        for index in ("workspace/product.sqlite3", "runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3.lock",
                      str(self.base / "absolute.sqlite3")):
            with self.subTest(index=index), self.assertRaises(ConfigurationError):
                self.load([dict(self.items[0], index=index)])
        for index in ("indexes/product.sqlite3", "indexes/product.sqlite3-wal", "indexes/product.sqlite3-journal"):
            with self.subTest(index=index), self.assertRaises(ConfigurationError):
                self.load([self.items[0], dict(self.items[1], index=index)])
        self.assertFalse((self.base / "indexes").exists())

    def test_loader_rejects_link_alias_of_index(self):
        indexes = self.base / "indexes"
        indexes.mkdir()
        first, second = indexes / "product.sqlite3", indexes / "notes.sqlite3"
        first.write_bytes(b"synthetic")
        os.link(first, second)
        with self.assertRaises(ConfigurationError):
            self.load()

    def test_activation_conflict_rolls_back_all_tools_and_closes_indexes(self):
        configs = self.load()
        manager = KnowledgeCollectionManager(configs)
        self.addCleanup(manager.close)
        existing = Tool("knowledge.product.inspect", "existing", (), lambda: 1)
        registry = ToolRegistry((existing,))
        with self.assertRaises(ValueError):
            manager.activate(registry)
        self.assertIs(registry.get(existing.name), existing)
        self.assertIsNone(registry.get("knowledge.notes.catalog"))
        self.assertEqual(manager.get("notes").catalog()["error_code"], "index_closed")
        for config in configs:
            replacement = KnowledgePlugin(config.root, config.index_path, namespace=config.namespace)
            replacement.close()
        manager.close()

    def test_second_index_initialization_failure_preserves_wrong_db_and_closes_first(self):
        configs = self.load()
        (self.base / "indexes").mkdir()
        wrong = self.base / "indexes/product.sqlite3"
        connection = sqlite3.connect(wrong)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
        connection.close()
        before = wrong.read_bytes()
        manager = KnowledgeCollectionManager(configs)
        self.addCleanup(manager.close)
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            manager.activate(registry)
        self.assertEqual(wrong.read_bytes(), before)
        self.assertIsNone(registry.get("knowledge.notes.search"))
        self.assertEqual(manager.get("notes").catalog()["error_code"], "index_closed")


if __name__ == "__main__":
    unittest.main()
