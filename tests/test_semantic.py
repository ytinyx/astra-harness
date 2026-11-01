"""Adapter invariants using hand-written vectors, never semantic-quality claims."""
import asyncio
from contextlib import closing
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
import unittest

from astra_harness.builtin.embeddings import EmbeddingBatch, EmbeddingConfig, EmbeddingError
from astra_harness.builtin.knowledge import KnowledgePlugin
from astra_harness.builtin.semantic import SemanticKnowledgePlugin, TEST_TEXT
from astra_harness.contracts import ToolCall
from astra_harness.tools import ToolExecutionUnknown, ToolExecutor, ToolRegistry


class FixedProvider:
    """Exact-input table, not a substitute embedding model or quality oracle."""
    def __init__(self, config, *, identity="fixture-credential"):
        self.config = config
        self.identity = identity
        self.calls = []
        self.vectors = {}
        self.before_response = None
        self.failure = None
        self.state = "ready"

    def status(self):
        return {"state": self.state, "key_present": self.state == "ready"}

    def recovery_identity(self):
        return hashlib.sha256(self.identity.encode()).hexdigest()

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        if self.before_response:
            await self.before_response()
        if self.failure:
            raise self.failure
        return EmbeddingBatch(tuple(tuple(self.vectors.get(text, (1, 0, 0))) for text in texts),
                              self.config.model, {"prompt_tokens": len(texts), "total_tokens": len(texts)})


class SemanticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-semantic-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "materials"
        self.root.mkdir()
        self.knowledge = KnowledgePlugin(self.root, self.directory / "lexical.sqlite3")
        self.addCleanup(self.knowledge.close)
        self.config = EmbeddingConfig(enabled=True, endpoint="http://127.0.0.1:43210/v1/embeddings",
                                      model="fixture", dimensions=3)
        self.provider = FixedProvider(self.config)
        self.plugin = self.make_plugin()

    def make_plugin(self, provider=None, config=None, index_path=None):
        plugin = SemanticKnowledgePlugin(self.knowledge, provider or self.provider, config or self.config, index_path)
        self.addCleanup(plugin.close)
        return plugin

    def document(self, name="guide.md", text="A verified document.\n"):
        (self.root / name).write_bytes(text.encode("utf-8"))
        result = self.knowledge.import_file(name)
        self.assertTrue(result["ok"], result)
        return text

    def digest(self):
        snapshot = self.knowledge.export_snapshot()
        self.assertTrue(snapshot["ok"], snapshot)
        return snapshot["corpus_sha256"]

    async def built(self, text="A verified document.\n"):
        self.document(text=text)
        digest = self.digest()
        result = await self.plugin.embedding_rebuild(digest)
        self.assertTrue(result["ok"], result)
        return digest

    def stored(self):
        return (list(map(tuple, self.plugin._db.execute("SELECT * FROM metadata ORDER BY key"))),
                list(map(tuple, self.plugin._db.execute("SELECT * FROM vectors ORDER BY chunk_id"))))

    async def test_manual_vector_ranking_persistence_and_independent_citation_oracle(self):
        first = self.document("alpha.md", "first\r\n独立材料🙂 has no query terms.\n")
        self.document("beta.md", "second separate material.")
        self.provider.vectors = {first: (2, 0, 0), "second separate material.": (0, 2, 0), "unrelated question": (0.9, 0.1, 0)}
        digest = self.digest()
        self.assertEqual(self.plugin.status()["state"], "not_built")
        result = await self.plugin.embedding_rebuild(digest)
        self.assertEqual((result["chunk_count"], result["dimensions"], result["request_count"]), (2, 3, 1))
        self.assertEqual(self.plugin.status()["state"], "ready")
        self.plugin.close()
        self.plugin = self.make_plugin()
        found = await self.plugin.semantic_search("unrelated question", digest)
        self.assertTrue(found["ok"])
        hit = found["hits"][0]
        self.assertEqual(hit["path"], "alpha.md")
        self.assertAlmostEqual(hit["score"], 0.9 / math.sqrt(0.9 ** 2 + 0.1 ** 2))
        self.assertEqual(self.knowledge.search("unrelated question")["hits"], [])
        self.assertEqual(hit["document_sha256"], hashlib.sha256(first.encode()).hexdigest())
        self.assertEqual(hit["text"], first[hit["start_offset"]:hit["end_offset"]])
        starts = [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", first)]
        for prefix in ("start", "end"):
            offset = hit[prefix + "_offset"]
            line = max(i for i, start in enumerate(starts) if start <= offset)
            self.assertEqual((hit[prefix+"_line"], hit[prefix+"_column"]), (line+1, offset-starts[line]+1))

    async def test_snapshot_rechecks_cached_text_coordinates_and_search_data(self):
        self.document()
        for field, value in (("text", "forged content"), ("start_column", 50), ("search_text", "poison"),
                             ("chunk_id", "a" * 64), ("ordinal", 2)):
            with self.subTest(field=field):
                digest = self.digest()
                self.knowledge._db.execute("UPDATE chunks SET " + field + "=?", (value,))
                self.knowledge._db.commit()
                result = await self.plugin.embedding_rebuild(digest)
                self.assertEqual(result["error_code"], "index_inconsistent")
                self.assertEqual(self.provider.calls, [])
                self.knowledge.remove("guide.md")
                self.knowledge.import_file("guide.md")

    async def test_snapshot_digest_stable_across_reimport_and_bounds_are_checked(self):
        self.document()
        digest = self.digest()
        self.knowledge.import_file("guide.md")
        self.assertEqual(self.digest(), digest)
        for value in (True, 0, 4097, "128"):
            self.assertEqual(self.knowledge.export_snapshot(value)["error_code"], "invalid_snapshot_limit")
        for value in (None, "x", "A" * 64):
            self.assertEqual(self.knowledge.verify_snapshot(value)["error_code"], "invalid_digest")

    async def test_no_raw_source_query_or_credentials_in_sidecar(self):
        secret_marker = "SYNTHETIC-CONTENT-NOT-REAL-SECRET-5aef83"
        digest = await self.built(secret_marker)
        await self.plugin.semantic_search("SYNTHETIC-QUERY-MARKER-264b", digest)
        raw = self.plugin.index_path.read_bytes()
        for value in (secret_marker, "SYNTHETIC-QUERY-MARKER-264b", "fixture-credential", "guide.md"):
            self.assertNotIn(value.encode(), raw)
        self.assertEqual([row[1] for row in self.plugin._db.execute("PRAGMA table_info(vectors)")],
                         ["chunk_id", "vector", "sha256"])

    async def test_source_change_and_removal_prevent_query_before_request(self):
        digest = await self.built()
        for removed in (False, True):
            with self.subTest(removed=removed):
                self.provider.calls.clear()
                if removed:
                    (self.root / "guide.md").unlink()
                else:
                    (self.root / "guide.md").write_text("changed", encoding="utf-8")
                result = await self.plugin.semantic_search("question", digest)
                self.assertEqual(result["error_code"], "source_changed")
                self.assertEqual(self.provider.calls, [])

    async def test_old_corpus_approval_cannot_read_reimported_material(self):
        previous = await self.built()
        self.document(text="New selected content.")
        self.provider.calls.clear()
        for operation in (self.plugin.embedding_rebuild(previous), self.plugin.semantic_search("question", previous)):
            result = await operation
            self.assertEqual(result["error_code"], "corpus_changed")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.plugin.status()["state"], "rebuild_required")

    async def test_profile_change_requires_rebuild_but_key_rotation_only_changes_approval(self):
        digest = await self.built()
        self.plugin.close()
        rotated = FixedProvider(self.config, identity="second-fixture-credential")
        same_index = self.make_plugin(rotated)
        self.assertNotEqual(same_index.recovery_identity, self.plugin.recovery_identity)
        self.assertEqual(same_index.index_identity, self.plugin.index_identity)
        self.assertEqual(same_index.status()["state"], "ready")
        same_index.close()
        for changes in ({"model": "other"}, {"dimensions": 2}, {"revision": "2"},
                        {"endpoint": "http://127.0.0.1:43211/v1/embeddings"}):
            with self.subTest(changes=changes):
                config = replace(self.config, **changes)
                provider = FixedProvider(config)
                plugin = self.make_plugin(provider, config)
                self.assertEqual(plugin.status()["state"], "rebuild_required")
                self.assertEqual((await plugin.semantic_search("question", digest))["error_code"], "embedding_rebuild_required")
                self.assertEqual(provider.calls, [])
                plugin.close()

    async def test_later_batch_failure_keeps_previous_vectors_and_is_unknown(self):
        await self.built()
        before = self.stored()
        self.document(text="x" * 10000)
        async def fail_second():
            if len(self.provider.calls) == 3:  # one old build, then two new batches
                raise EmbeddingError("provider_error", request_issued=True)
        self.provider.before_response = fail_second
        with self.assertRaises(ToolExecutionUnknown):
            await self.plugin.embedding_rebuild(self.digest())
        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.plugin.status()["state"], "rebuild_required")

    async def test_cancellation_stops_later_batches_and_retains_previous_vectors(self):
        await self.built()
        before = self.stored()
        self.document(text="x" * 10000)
        entered = asyncio.Event()
        async def suspended():
            entered.set()
            await asyncio.Event().wait()
        self.provider.before_response = suspended
        task = asyncio.create_task(self.plugin.embedding_rebuild(self.digest()))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.stored(), before)
        self.assertFalse(self.plugin._operation.locked())

    async def test_source_change_while_rebuilding_retains_previous_vectors(self):
        digest = await self.built()
        before = self.stored()
        async def change():
            (self.root / "guide.md").write_text("changed during request", encoding="utf-8")
        self.provider.before_response = change
        result = await self.plugin.embedding_rebuild(digest)
        self.assertEqual((result["error_code"], result["request_count"]), ("source_changed", 1))
        self.assertEqual(self.stored(), before)

    async def test_source_change_during_query_never_returns_old_citation(self):
        digest = await self.built()
        async def change():
            (self.root / "guide.md").write_text("changed during query", encoding="utf-8")
        self.provider.before_response = change
        result = await self.plugin.semantic_search("question", digest)
        self.assertEqual((result["error_code"], result["request_count"]), ("source_changed", 1))
        self.assertNotIn("hits", result)

    async def test_concurrent_generation_change_prevents_overwriting_winner(self):
        digest = await self.built()
        async def change():
            with closing(sqlite3.connect(self.plugin.index_path)) as other:
                other.execute("UPDATE metadata SET value=? WHERE key='generation'", ("b" * 32,))
                other.commit()
        self.provider.before_response = change
        result = await self.plugin.embedding_rebuild(digest)
        self.assertEqual(result["error_code"], "embedding_index_changed")
        self.assertEqual(dict(self.stored()[0])["generation"], "b" * 32)

    async def test_partial_sqlite_replace_failure_rolls_back_old_index(self):
        digest = await self.built()
        before = self.stored()
        self.plugin._db.execute("CREATE TRIGGER fixture_failure BEFORE INSERT ON vectors BEGIN SELECT RAISE(FAIL,'fixture'); END")
        self.plugin._db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.plugin.embedding_rebuild(digest)
        self.assertEqual(self.stored(), before)

    async def test_too_many_chunks_refused_without_provider_request(self):
        self.document("one.md", "a" * 60000)
        self.document("two.md", "b" * 60000)
        snapshot = self.knowledge.export_snapshot(4096)
        self.assertGreater(snapshot["chunk_count"], 128)
        result = await self.plugin.embedding_rebuild(snapshot["corpus_sha256"])
        self.assertEqual(result["error_code"], "embedding_corpus_limit")
        self.assertEqual(self.provider.calls, [])

    async def test_empty_corpus_clears_numeric_cache_without_key_or_request(self):
        digest = await self.built()
        before = self.stored()
        self.knowledge.remove("guide.md")
        self.assertEqual(self.plugin.status()["state"], "rebuild_required")
        self.assertEqual(self.stored(), before)  # logical invalidation does not rewrite the sidecar
        self.provider.state = "missing_key"
        self.provider.calls.clear()
        cleared = await self.plugin.embedding_rebuild(self.digest())
        self.assertEqual((cleared["ok"], cleared["chunk_count"], cleared["request_count"]), (True, 0, 0))
        self.assertEqual(self.stored()[1], [])
        self.assertEqual(self.provider.calls, [])
        self.provider.state = "ready"
        result = await self.plugin.semantic_search("question", self.digest())
        self.assertEqual((result["hits"], result["request_count"]), ([], 0))
        self.assertNotEqual(digest, self.digest())

    async def test_missing_key_disabled_and_closed_have_no_requests(self):
        self.document()
        self.provider.state = "missing_key"
        self.assertEqual(self.plugin.status()["state"], "missing_key")
        self.assertEqual((await self.plugin.embedding_rebuild(self.digest()))["error_code"], "embedding_missing_key")
        disabled_config = EmbeddingConfig()
        disabled = self.make_plugin(FixedProvider(disabled_config), disabled_config, self.directory / "never.sqlite3")
        self.assertEqual(disabled.tools, ())
        self.assertFalse(disabled.index_path.exists())
        self.assertEqual((await disabled.embedding_test())["error_code"], "embedding_not_configured")
        self.plugin.close()
        self.assertEqual((await self.plugin.embedding_test())["error_code"], "embedding_index_closed")
        self.assertEqual(self.provider.calls, [])

    async def test_vector_and_metadata_corruption_blocks_query_before_request(self):
        digest = await self.built()
        original = self.stored()
        changes = [
            ("UPDATE metadata SET value='2' WHERE key='vector_count'", ()),
            ("UPDATE metadata SET value='4097' WHERE key='dimension'", ()),
            ("UPDATE vectors SET sha256=?", ("0" * 64,)),
            ("UPDATE vectors SET chunk_id=NULL", ()),
            ("UPDATE metadata SET key=NULL WHERE key='dimension'", ()),
        ]
        for vector in ((math.nan, 0, 0), (0, 0, 0), (2, 0, 0)):
            raw = struct.pack("<ddd", *vector)
            changes.append(("UPDATE vectors SET vector=?,sha256=?", (raw, hashlib.sha256(raw).hexdigest())))
        for sql, parameters in changes:
            with self.subTest(sql=sql, parameters=parameters):
                self.plugin._db.execute(sql, parameters)
                self.plugin._db.commit()
                self.provider.calls.clear()
                result = await self.plugin.semantic_search("question", digest)
                self.assertEqual(result["error_code"], "embedding_index_inconsistent")
                self.assertEqual(self.provider.calls, [])
                self.plugin._db.execute("DELETE FROM metadata")
                self.plugin._db.executemany("INSERT INTO metadata VALUES (?,?)", original[0])
                self.plugin._db.execute("DELETE FROM vectors")
                self.plugin._db.executemany("INSERT INTO vectors VALUES (?,?,?)", original[1])
                self.plugin._db.commit()

    async def test_invalid_provider_vectors_are_unknown_and_never_written(self):
        self.document()
        before = self.stored()
        for vector in ((0, 0, 0), (math.inf, 1, 0), (True, 0, 0), (1, 0), (10 ** 1000, 1, 0)):
            with self.subTest(vector=vector):
                self.provider.vectors = {"A verified document.\n": vector}
                with self.assertRaises(ToolExecutionUnknown):
                    await self.plugin.embedding_rebuild(self.digest())
                self.assertEqual(self.stored(), before)

    async def test_invalid_query_and_digest_fail_before_custom_provider(self):
        digest = await self.built()
        self.provider.calls.clear()
        for query, limit in (("", 1), ("q" * 201, 1), ("\ud800", 1), ("a\0b", 1), ("q", True), ("q", 6)):
            self.assertEqual((await self.plugin.semantic_search(query, digest, limit))["error_code"], "invalid_query")
        self.assertEqual((await self.plugin.semantic_search("q", "bad"))["error_code"], "invalid_digest")
        self.assertEqual(self.provider.calls, [])

    async def test_result_bound_applies_to_actual_text_and_top_k(self):
        digest = await self.built("a" * 10000)
        found = await self.plugin.semantic_search("question", digest, limit=5)
        self.assertTrue(found["ok"])
        self.assertLessEqual(len(found["hits"]), 5)
        self.assertLessEqual(sum(len(hit["text"]) for hit in found["hits"]), 4000)
        self.assertTrue(found["truncated"])

    async def test_executor_permissions_approval_and_fixed_test_text(self):
        self.document()
        digest = self.digest()
        registry = ToolRegistry(self.plugin.tools)
        executor = ToolExecutor(registry)
        all_permissions = frozenset(self.plugin.permissions)
        for suffix, arguments in (("embedding_test", {}), ("embedding_rebuild", {"expected_corpus_sha256": digest}),
                                  ("semantic_search", {"query": "question", "expected_corpus_sha256": digest})):
            call = ToolCall("call", self.knowledge.namespace + "." + suffix, arguments)
            self.assertEqual((await executor.aexecute(call, all_permissions - {self.knowledge.namespace + ".external"}, approved=True)).error_code,
                             "permission_denied")
            self.assertEqual((await executor.aexecute(call, all_permissions)).error_code, "approval_required")
        self.assertEqual(self.provider.calls, [])
        status = executor.execute(ToolCall("status", self.knowledge.namespace + ".embedding_status", {}),
                                  frozenset({"files.read", self.knowledge.namespace + ".read"}))
        self.assertEqual(status.value["state"], "not_built")
        tested = await executor.aexecute(ToolCall("test", self.knowledge.namespace + ".embedding_test", {}), all_permissions, approved=True)
        self.assertTrue(tested.value["ok"])
        self.assertEqual(self.provider.calls, [(TEST_TEXT,)])
        self.provider.failure = EmbeddingError("authentication_failed", request_issued=True)
        result = await executor.aexecute(ToolCall("test2", self.knowledge.namespace + ".embedding_test", {}), all_permissions, approved=True)
        self.assertEqual(result.error_code, "execution_unknown")

    async def test_sidecar_cannot_alias_lexical_or_live_in_workspace_or_open_other_db(self):
        for path in (self.knowledge.index_path, self.root / "vectors.sqlite3", Path(str(self.knowledge.index_path) + "-wal")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.make_plugin(index_path=path)
        wrong = self.directory / "other.sqlite3"
        with closing(sqlite3.connect(wrong)) as database:
            database.execute("CREATE TABLE other(value TEXT)")
            database.commit()
        with self.assertRaisesRegex(ValueError, "embedding_index_incompatible"):
            self.make_plugin(index_path=wrong)
        broken = self.directory / "broken.sqlite3"
        broken.write_bytes(b"not a database")
        with self.assertRaisesRegex(ValueError, "embedding_index_incompatible"):
            self.make_plugin(index_path=broken)
        self.assertEqual(self.knowledge._db.execute("PRAGMA user_version").fetchone()[0], 1)

    async def test_busy_does_not_start_a_second_provider_request(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def suspended():
            entered.set()
            await release.wait()
        self.provider.before_response = suspended
        task = asyncio.create_task(self.plugin.embedding_test())
        await asyncio.wait_for(entered.wait(), timeout=2)
        self.assertEqual((await self.plugin.embedding_test())["error_code"], "embedding_busy")
        release.set()
        self.assertTrue((await task)["ok"])
        self.assertEqual(len(self.provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
