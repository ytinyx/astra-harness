"""Host configuration stays inert, bounded and separate from active adapters."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from astra_harness.builtin.embeddings import EmbeddingBatch, OpenAICompatibleEmbeddingProvider
from astra_harness.builtin.knowledge_collections import KnowledgeCollectionManager, load_knowledge_config
from astra_harness.config import ConfigurationError
from astra_harness.config_file import ConfigurationConflict, JSONConfigurationFile
from astra_harness.tools import ToolRegistry


EMBEDDING = {"enabled": True, "endpoint": "http://127.0.0.1:8912/v1/embeddings",
             "model": "synthetic-v1", "key_env": "ASTRA_EMBEDDING_TEST_API_KEY",
             "dimensions": 2, "revision": "1", "timeout": 2}


class RecordingProvider:
    """No HTTP: host/approval fixture, not evidence of semantic model quality."""
    def __init__(self, config):
        self.config, self.calls = config, []

    def status(self):
        return {"state": "ready", "key_present": True}

    async def embed(self, texts):
        self.calls.append(list(texts))
        return EmbeddingBatch(tuple((1.0, 0.0) for _ in texts), self.config.model, None)


class HostFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "materials"
        for name in ("alpha", "beta"):
            directory = self.workspace / name
            directory.mkdir(parents=True)
            (directory / "guide.md").write_text(name + " synthetic guide", encoding="utf-8")
        self.database = self.root / "runs.sqlite3"
        self.path = self.root / "knowledge.json"
        self.value = {"collections": [{"id": name, "label": name.title(), "path": name,
                                        "index": name + ".sqlite3"} for name in ("alpha", "beta")]}
        self.write_config(self.value)

    def write_config(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def manager(self, *, activate=True, editable=True, factory=None):
        configs = load_knowledge_config(self.path, workspace=self.workspace, database=self.database)
        manager = KnowledgeCollectionManager(configs, config_path=self.path if editable else None,
            workspace=self.workspace, database=self.database, provider_factory=factory)
        self.addCleanup(manager.close)
        registry = ToolRegistry()
        if activate:
            manager.activate(registry)
        return manager, registry


class EmbeddingHostTests(HostFixture, unittest.TestCase):
    def test_disabled_startup_and_save_do_not_construct_provider_or_sidecar(self):
        factory = Mock(side_effect=AssertionError("save activated a provider"))
        manager, registry = self.manager(factory=factory)
        before = manager.embedding_configuration("alpha")
        self.assertFalse(before["active"]["enabled"])
        self.assertFalse(before["restart_required"])
        saved = manager.save_embedding_configuration("alpha", EMBEDDING, before["config_revision"])
        self.assertTrue(saved["saved"]["enabled"])
        self.assertFalse(saved["active"]["enabled"])
        self.assertTrue(saved["restart_required"])
        factory.assert_not_called()
        self.assertFalse(list(self.root.glob("*.vectors.sqlite3*")))
        self.assertIsNone(registry.get("knowledge.alpha.embedding_test"))
        self.assertEqual(json.loads(self.path.read_text())["collections"][1], self.value["collections"][1])
        self.assertEqual(manager.get("alpha").search("synthetic")["hits"], [])

    def test_enabled_missing_key_keeps_lexical_host_and_status_usable(self):
        self.value["collections"][0]["embedding"] = EMBEDDING
        self.write_config(self.value)
        factory = lambda config: OpenAICompatibleEmbeddingProvider(config, environ={})
        manager, registry = self.manager(factory=factory)
        self.assertEqual(manager._semantic["alpha"].status()["provider_status"], "missing_key")
        self.assertFalse(manager.embedding_configuration("alpha")["active_key_present"])
        self.assertTrue(manager.get("alpha").catalog()["ok"])
        self.assertTrue(registry.get("knowledge.alpha.embedding_test").requires_approval)
        self.assertIn("knowledge.alpha.external", manager.permissions)
        self.assertNotIn("knowledge.beta.external", manager.permissions)

    def test_save_freezes_active_adapter_until_restart_and_never_returns_secret(self):
        self.value["collections"][0]["embedding"] = EMBEDDING
        self.write_config(self.value)
        made = []
        def factory(config):
            provider = RecordingProvider(config)
            made.append(provider)
            return provider
        manager, registry = self.manager(factory=factory)
        original = registry.get("knowledge.alpha.embedding_test").recovery_identity
        secret = "synthetic-only-private-marker"
        with patch.dict(os.environ, {EMBEDDING["key_env"]: secret}):
            before = manager.embedding_configuration("alpha")
            changed = dict(EMBEDDING, model="synthetic-v2")
            after = manager.save_embedding_configuration("alpha", changed, before["config_revision"])
        self.assertNotIn(secret, json.dumps(after))
        self.assertNotIn(secret, self.path.read_text())
        self.assertTrue(after["saved_key_present"])
        self.assertEqual(after["active"]["model"], "synthetic-v1")
        self.assertEqual(made[0].config.model, "synthetic-v1")
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].calls, [])
        self.assertEqual(registry.get("knowledge.alpha.embedding_test").recovery_identity, original)
        manager.close()
        reloaded, current = self.manager(factory=factory)
        self.assertFalse(reloaded.embedding_configuration("alpha")["restart_required"])
        self.assertNotEqual(current.get("knowledge.alpha.embedding_test").recovery_identity, original)

    def test_two_concurrent_editors_have_one_winner_and_no_partial_file(self):
        manager, _ = self.manager(activate=False)
        revision = manager.embedding_configuration("alpha")["config_revision"]
        def save(model):
            try:
                manager.save_embedding_configuration("alpha", dict(EMBEDDING, model=model), revision)
                return "saved"
            except ConfigurationConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(save, ("candidate-a", "candidate-b"))), ["conflict", "saved"])
        self.assertIn(json.loads(self.path.read_text())["collections"][0]["embedding"]["model"],
                      {"candidate-a", "candidate-b"})
        self.assertFalse(list(self.root.glob(".astra-config-*")))

    def test_invalid_secret_fields_and_external_edit_leave_bytes_unchanged(self):
        manager, _ = self.manager(activate=False)
        revision = manager.embedding_configuration("alpha")["config_revision"]
        before = self.path.read_bytes()
        invalid = [dict(EMBEDDING, api_key="synthetic-secret"), dict(EMBEDDING, headers={}),
                   dict(EMBEDDING, key_env="DEEPSEEK_API_KEY"), dict(EMBEDDING, endpoint="http://example.com/embeddings")]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                manager.save_embedding_configuration("alpha", value, revision)
            self.assertEqual(self.path.read_bytes(), before)
        external = deepcopy(self.value)
        external["collections"][1]["label"] = "User edit"
        self.write_config(external)
        with self.assertRaises(ConfigurationConflict):
            manager.save_embedding_configuration("alpha", EMBEDDING, revision)
        self.assertEqual(json.loads(self.path.read_text()), external)

    def test_malformed_current_config_is_not_repaired_by_save(self):
        manager, _ = self.manager(activate=False)
        for data in (b'{"collections":[],"collections":[]}', b'{"collections":NaN}', b' ' * 65537):
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):
                manager.save_embedding_configuration("alpha", EMBEDDING, hashlib.sha256(data).hexdigest())
            self.assertEqual(self.path.read_bytes(), data)

    def test_hardlinked_configuration_cannot_be_read_or_replaced(self):
        alias = self.root / "alias.json"
        os.link(self.path, alias)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            JSONConfigurationFile(self.path).read()
        with self.assertRaises(ValueError):
            load_knowledge_config(self.path, workspace=self.workspace, database=self.database)
        self.assertEqual(alias.read_bytes(), before)

    def test_vector_database_conflicts_are_rejected_before_creation(self):
        for conflicting in ("alpha.sqlite3.vectors.sqlite3", "alpha.sqlite3.vectors.sqlite3-wal",
                            "alpha.sqlite3.vectors.sqlite3-journal"):
            value = deepcopy(self.value)
            value["collections"][0]["embedding"] = EMBEDDING
            value["collections"][1]["index"] = conflicting
            self.write_config(value)
            with self.subTest(conflicting=conflicting), self.assertRaises(ConfigurationError):
                load_knowledge_config(self.path, workspace=self.workspace, database=self.database)
        self.value["collections"][0]["embedding"] = EMBEDDING
        self.write_config(self.value)
        with self.assertRaises(ConfigurationError):
            load_knowledge_config(self.path, workspace=self.workspace,
                                  database=self.root / "alpha.sqlite3.vectors.sqlite3")
        self.assertFalse(list(self.root.glob("*.sqlite3")))

    def test_configuration_cannot_occupy_lexical_or_vector_database_journals(self):
        self.value["collections"][0]["embedding"] = EMBEDDING
        for name in ("alpha.sqlite3-journal", "alpha.sqlite3-wal", "alpha.sqlite3.vectors.sqlite3.lock",
                     "alpha.sqlite3.vectors.sqlite3-shm", "runs.sqlite3", "runs.sqlite3-journal"):
            path = self.root / name
            path.write_text(json.dumps(self.value), encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                load_knowledge_config(path, workspace=self.workspace, database=self.database)

    def test_legacy_manager_reports_noneditable_and_never_creates_configuration(self):
        manager, _ = self.manager(editable=False)
        result = manager.embedding_configuration("alpha")
        self.assertFalse(result["editable"])
        self.assertIsNone(result["config_revision"])
        with self.assertRaises(RuntimeError):
            manager.save_embedding_configuration("alpha", EMBEDDING, "a" * 64)
        self.assertNotIn("embedding", json.loads(self.path.read_text())["collections"][0])


if __name__ == "__main__":
    unittest.main()
