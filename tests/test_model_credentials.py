"""Synthetic keys only; exercise persistence, deletion and failure boundaries."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from astra_harness.application import Application
from astra_harness.builtin.models import DemoModel
from astra_harness import model_credentials
from astra_harness.model_binding import ModelProfileError
from astra_harness.model_profiles import CATALOG_KEY, CREDENTIAL_PREFIX, ModelProfiles
from astra_harness.store import Store

KEY = "synthetic-local-key-never-a-real-credential"
OTHER_KEY = "synthetic-replacement-key"
PROFILE = {"name": "Stored fixture", "base_url": "http://127.0.0.1:12345/v1",
           "model": "fixture", "key_env": None}


class CredentialValidationTests(unittest.TestCase):
    def test_invalid_keys_never_echo_the_input(self):
        for value in (None, True, 123, "", "with space", "line\nbreak", "\u5bc6\u94a5", "x" * 4097):
            with self.assertRaisesRegex(ModelProfileError, "^model_credentials_invalid$"):
                model_credentials.validate_key(value)

    def test_unavailable_platform_has_no_plaintext_fallback(self):
        with patch.object(model_credentials, "available", return_value=False):
            with self.assertRaisesRegex(ModelProfileError, "^model_key_storage_unavailable$"):
                model_credentials.protect_key(KEY, "a" * 32)


@unittest.skipUnless(os.name == "nt", "Windows account-bound DPAPI storage")
class StoredCredentialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-stored-keys-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "runs.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.catalog = ModelProfiles(self.store, environment=lambda: self.fail("stored credentials must not read env"))

    def save(self, **changes):
        return self.catalog.save({**PROFILE, **changes}, self.catalog.list()["revision"])["items"][0]

    @staticmethod
    def snapshot(item):
        return {key: value for key, value in item.items() if key not in {"identity", "has_api_key"}}

    def test_dpapi_roundtrip_is_bound_to_profile_and_corruption_fails_closed(self):
        protected = model_credentials.protect_key(KEY, "a" * 32)
        self.assertNotIn(KEY, protected)
        self.assertEqual(model_credentials.unprotect_key(protected, "a" * 32), KEY)
        for value, identifier in ((protected, "b" * 32), ("invalid", "a" * 32),
                                  (model_credentials.PREFIX + "not-base64!", "a" * 32),
                                  (protected[:-12], "a" * 32)):
            with self.assertRaisesRegex(ModelProfileError, "^model_key_storage_unavailable$"):
                model_credentials.unprotect_key(value, identifier)

    def test_saved_key_survives_restart_and_never_enters_catalog_or_db_as_plaintext(self):
        item = self.save(api_key=KEY)
        self.assertTrue(item["has_api_key"])
        self.catalog.select(item["id"], 1)
        before = self.catalog.bind(self.catalog.selected_snapshot())
        self.store.close()
        self.store = Store(self.path)
        self.catalog = ModelProfiles(self.store, environment=lambda: {})
        after = self.catalog.bind(self.catalog.selected_snapshot())
        self.assertEqual(before.credential_seal, after.credential_seal)
        self.assertEqual(after.model.api_key, KEY)
        self.assertNotIn(KEY, json.dumps(self.catalog.list()))
        self.assertNotIn("api_key", self.store.host_setting(CATALOG_KEY))
        for suffix in ("", "-wal", "-journal"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                self.assertNotIn(KEY.encode(), path.read_bytes())

    def test_blank_edit_preserves_key_and_replacement_changes_identity(self):
        item = self.save(api_key=KEY)
        bound = self.catalog.bind(self.snapshot(item))
        unchanged = self.save(id=item["id"], api_key="")
        self.assertEqual(unchanged["identity"], item["identity"])
        self.assertEqual(self.catalog.bind(self.snapshot(unchanged)).model.api_key, KEY)
        replaced = self.save(id=item["id"], api_key=OTHER_KEY)
        self.assertNotEqual(replaced["identity"], item["identity"])
        self.assertEqual(self.catalog.bind(self.snapshot(replaced)).model.api_key, OTHER_KEY)
        self.assertEqual(bound.model.api_key, KEY, "in-flight binding is not silently replaced")

    def test_delete_key_keeps_selected_profile_blocks_binding_and_can_be_replaced(self):
        item = self.save(api_key=KEY)
        self.catalog.select(item["id"], 1)
        deleted = self.catalog.delete_key(item["id"], 2)
        self.assertEqual(deleted["selected_id"], item["id"])
        self.assertFalse(deleted["items"][0]["has_api_key"])
        self.assertNotEqual(deleted["selected_identity"], item["identity"])
        self.assertEqual(deleted["items"][0]["base_url"], PROFILE["base_url"])
        self.assertIsNone(self.store.host_setting(CREDENTIAL_PREFIX + item["id"]))
        with self.assertRaisesRegex(ModelProfileError, "^model_credentials_missing$"):
            self.catalog.bind(self.catalog.selected_snapshot())
        self.assertEqual(self.catalog.delete_key(item["id"], 3), deleted, "duplicate deletion is harmless")
        replaced = self.save(id=item["id"], api_key=OTHER_KEY)
        self.assertEqual(self.catalog.bind(self.snapshot(replaced)).model.api_key, OTHER_KEY)
        self.catalog.delete(item["id"], 4)
        self.assertIsNone(self.store.host_setting(CREDENTIAL_PREFIX + item["id"]))
        self.assertEqual(self.catalog.list()["items"], [])

    def test_stale_save_or_delete_cannot_replace_or_remove_current_key(self):
        item = self.save(api_key=KEY)
        before = self.catalog.list()
        for operation in (lambda: self.catalog.save({**PROFILE, "id": item["id"], "api_key": OTHER_KEY}, 0),
                          lambda: self.catalog.delete_key(item["id"], 0),
                          lambda: self.catalog.delete(item["id"], 0)):
            with self.assertRaisesRegex(ModelProfileError, "^model_profile_conflict$"):
                operation()
        self.assertEqual(self.catalog.list(), before)
        self.assertEqual(self.catalog.bind(self.snapshot(item)).model.api_key, KEY)

    def test_storage_failure_rolls_back_catalog_and_key_together(self):
        item = self.save(api_key=KEY)
        before = self.catalog.list()
        self.store.db.execute("CREATE TRIGGER fail_key BEFORE UPDATE ON host_settings "
                              "WHEN NEW.key LIKE 'model_profiles.credential.%' "
                              "BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END")
        with self.assertRaisesRegex(ModelProfileError, "^model_profile_unavailable$"):
            self.save(id=item["id"], api_key=OTHER_KEY, name="should roll back")
        self.assertEqual(self.catalog.list(), before)
        self.assertEqual(self.catalog.bind(self.snapshot(item)).model.api_key, KEY)

    def test_competing_creates_store_only_the_winning_credential(self):
        other = ModelProfiles(self.store, environment=lambda: {})
        original = self.store.compare_and_swap_host_setting
        barrier = threading.Barrier(2)

        def competing(key, expected, value, **kwargs):
            barrier.wait(timeout=5)
            return original(key, expected, value, **kwargs)

        def save(catalog, key):
            try:
                return catalog.save({**PROFILE, "api_key": key}, 0)["revision"]
            except ModelProfileError as error:
                return error.code

        with patch.object(self.store, "compare_and_swap_host_setting", competing), ThreadPoolExecutor(2) as pool:
            first = pool.submit(save, self.catalog, KEY)
            second = pool.submit(save, other, OTHER_KEY)
            self.assertCountEqual([first.result(timeout=10), second.result(timeout=10)], [1, "model_profile_conflict"])
        rows = self.store.db.execute("SELECT key FROM host_settings WHERE key LIKE 'model_profiles.credential.%'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.catalog.list()["items"]), 1)

    def test_active_or_paused_run_blocks_deletion_and_switch_to_environment(self):
        app = Application(DemoModel(), database=self.path.parent / "runtime.sqlite3", autostart=False,
                          model_catalog_factory=ModelProfiles)
        self.addCleanup(app.close)
        catalog = app.save_model_profile({**PROFILE, "api_key": KEY}, 0)
        item = catalog["items"][0]
        app.select_model_profile(item["id"], 1)
        run = app.submit("synthetic task")
        app.pause(run["id"])
        for operation in (lambda: app.delete_model_profile_key(item["id"], 2),
                          lambda: app.delete_model_profile(item["id"], 2),
                          lambda: app.save_model_profile({**PROFILE, "id": item["id"], "key_env": "ASTRA_MODEL_API_KEY"}, 2),
                          lambda: app.save_model_profile({key: value for key, value in {**PROFILE, "id": item["id"]}.items() if key != "key_env"}, 2)):
            with self.assertRaisesRegex(ModelProfileError, "^model_profile_in_use$"):
                operation()
        app.cancel(run["id"])
        app.delete_model_profile_key(item["id"], 2)
        with self.assertRaisesRegex(ModelProfileError, "^model_credentials_missing$"):
            app.submit("must not reuse cached key")

    def test_switch_to_environment_clears_stored_key_without_fallback(self):
        item = self.save(api_key=KEY)
        updated = self.save(id=item["id"], key_env="ASTRA_MODEL_API_KEY")
        self.assertIsNone(self.store.host_setting(CREDENTIAL_PREFIX + item["id"]))
        catalog = ModelProfiles(self.store, environment=lambda: {})
        with self.assertRaisesRegex(ModelProfileError, "^model_credentials_missing$"):
            catalog.bind(self.snapshot(updated))


if __name__ == "__main__":
    unittest.main()
