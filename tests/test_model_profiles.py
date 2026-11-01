"""Synthetic catalog fixtures; these tests never contact a model service."""

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from astra_harness.model_binding import ModelProfileError
from astra_harness.model_profiles import ModelProfiles, CATALOG_KEY, SEAL_KEY, profile_identity
from astra_harness.store import Store


class ObservedEnvironment(Mapping):
    def __init__(self):
        self.values = {"ASTRA_MODEL_TEST_API_KEY": "synthetic-credential-one"}
        self.reads = []

    def __getitem__(self, key):
        self.reads.append(key)
        return self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)


class ModelProfilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-model-profiles-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "runs.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.environment = ObservedEnvironment()
        self.environment_calls = 0

        def environment():
            self.environment_calls += 1
            return self.environment

        self.catalog = ModelProfiles(self.store, environment=environment)
        self.profile = {"name": "Local fixture", "base_url": "http://127.0.0.1:12345/v1/",
                        "model": "synthetic-model", "key_env": "ASTRA_MODEL_TEST_API_KEY"}

    def create(self):
        value = self.catalog.save(self.profile, self.catalog.list()["revision"])
        return value["items"][-1]

    @staticmethod
    def snapshot(item):
        return {key: value for key, value in item.items() if key != "identity"}

    def test_crud_selection_has_zero_environment_and_adapter_activity(self):
        with patch("astra_harness.model_profiles.build_profile_model", side_effect=AssertionError("no adapter")):
            empty = self.catalog.list()
            self.assertEqual((empty["revision"], empty["selected_identity"]), (0, "startup"))
            self.assertIsNone(self.catalog.selected_snapshot())
            item = self.create()
            self.assertEqual(item["base_url"], "http://127.0.0.1:12345/v1")
            selected = self.catalog.select(item["id"], 1)
            self.assertEqual(selected["selected_identity"], item["identity"])
            snapshot = self.catalog.selected_snapshot()
            snapshot["model"] = "local-mutation"
            self.assertEqual(self.catalog.selected_snapshot()["model"], "synthetic-model")
            result = self.catalog.delete(item["id"], 2)
            self.assertEqual((result["revision"], result["selected_identity"], result["items"]), (3, "startup", []))
        self.assertEqual(self.environment_calls, 0)
        self.assertEqual(self.environment.reads, [])
        self.assertIsNone(self.store.host_setting(SEAL_KEY))

    def test_profile_revision_and_collection_cas_have_distinct_meanings(self):
        item = self.create()
        same = self.catalog.save({**self.profile, "id": item["id"]}, 1)
        self.assertEqual(same["revision"], 2)
        self.assertEqual(same["items"][0]["identity"], item["identity"])
        changed = self.catalog.save({**self.profile, "id": item["id"], "model": "other"}, 2)
        self.assertEqual(changed["items"][0]["revision"], 2)
        self.assertNotEqual(changed["items"][0]["identity"], item["identity"])
        for operation in (lambda: self.catalog.save(self.profile, 2), lambda: self.catalog.select(None, 2),
                          lambda: self.catalog.delete(item["id"], 2)):
            with self.assertRaisesRegex(ModelProfileError, "^model_profile_conflict$"):
                operation()
        self.assertEqual(self.catalog.list(), changed)

    def test_two_catalog_writers_cannot_lose_an_update(self):
        other = ModelProfiles(self.store, environment=lambda: self.environment)
        barrier = threading.Barrier(2)
        original = self.store.compare_and_swap_host_setting

        def competing(key, expected, value):
            barrier.wait(timeout=5)
            return original(key, expected, value)

        def save(catalog):
            try:
                return catalog.save(self.profile, 0)["revision"]
            except ModelProfileError as error:
                return error.code

        with patch.object(self.store, "compare_and_swap_host_setting", competing), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(save, catalog) for catalog in (self.catalog, other)]
            results = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(results, [1, "model_profile_conflict"])
        self.assertEqual(len(self.catalog.list()["items"]), 1)

    def test_max_profiles_and_unknown_edit_id_are_not_silent_creates(self):
        for index in range(16):
            self.catalog.save({**self.profile, "name": "fixture " + str(index)}, index)
        before = self.catalog.list()
        with self.assertRaisesRegex(ModelProfileError, "model_profile_conflict"):
            self.catalog.save(self.profile, 16)
        with self.assertRaisesRegex(ModelProfileError, "model_profile_not_found"):
            self.catalog.save({**self.profile, "id": "f" * 32}, 16)
        self.assertEqual(self.catalog.list(), before)

    def test_invalid_profile_fields_and_types_are_fixed_errors(self):
        cases = [{**self.profile, "api_key": "do-not-echo"}, {**self.profile, "password": "do-not-echo"},
                 {**self.profile, "timeout": True}, {**self.profile, "timeout": float("nan")},
                 {**self.profile, "max_output_tokens": 32769}, {**self.profile, "name": "\x00bad"},
                 {**self.profile, "name": "\ud800"}, {**self.profile, "base_url": "http://private.invalid/v1"},
                 {**self.profile, "key_env": "ASTRA_EMBEDDING_API_KEY"}, {**self.profile, "revision": 99}]
        for profile in cases:
            with self.subTest(field=set(profile) - set(self.profile)):
                with self.assertRaisesRegex(ModelProfileError, "^invalid_model_profile$"):
                    self.catalog.save(profile, 0)
        for revision in (-1, True, 1.0, 9007199254740992):
            with self.assertRaisesRegex(ModelProfileError, "^invalid_model_profile$"):
                self.catalog.save(self.profile, revision)
        self.assertEqual(self.catalog.list()["revision"], 0)
        self.assertEqual(self.environment_calls, 0)

    def test_bind_uses_frozen_snapshot_after_edit_and_delete(self):
        original = self.snapshot(self.create())
        first = self.catalog.bind(original)
        self.catalog.save({**self.profile, "id": original["id"], "model": "new-model"}, 1)
        self.catalog.delete(original["id"], 2)
        recovered = self.catalog.bind(original)
        self.assertEqual(first.identity, recovered.identity)
        self.assertEqual(first.credential_seal, recovered.credential_seal)
        self.assertEqual(recovered.model.model, "synthetic-model")
        self.assertEqual(self.environment.reads, ["ASTRA_MODEL_TEST_API_KEY"] * 2)
        altered = {**original, "adapter_revision": "2"}
        with self.assertRaisesRegex(ModelProfileError, "invalid_model_profile"):
            self.catalog.bind(altered)

    def test_missing_credentials_do_not_prevent_save_or_select(self):
        self.environment.values.clear()
        item = self.create()
        self.catalog.select(item["id"], 1)
        with self.assertRaisesRegex(ModelProfileError, "^model_credentials_missing$"):
            self.catalog.bind(self.catalog.selected_snapshot())
        self.assertIsNone(self.store.host_setting(SEAL_KEY))

    def test_restart_retains_seal_but_key_rotation_and_other_database_change_it(self):
        snapshot = self.snapshot(self.create())
        first = self.catalog.bind(snapshot)
        self.store.close()
        self.store = Store(self.path)
        catalog = ModelProfiles(self.store, environment=lambda: self.environment)
        second = catalog.bind(snapshot)
        self.assertEqual(first.credential_seal, second.credential_seal)
        self.environment.values["ASTRA_MODEL_TEST_API_KEY"] = "synthetic-credential-two"
        rotated = catalog.bind(snapshot)
        self.assertEqual(first.identity, rotated.identity)
        self.assertNotEqual(first.credential_seal, rotated.credential_seal)
        self.assertEqual(first.model.api_key, "synthetic-credential-one")
        with tempfile.TemporaryDirectory(prefix="astra-second-catalog-") as directory:
            other = Store(Path(directory) / "runs.sqlite3")
            try:
                foreign = ModelProfiles(other, environment=lambda: self.environment).bind(snapshot)
                self.assertNotEqual(rotated.credential_seal, foreign.credential_seal)
            finally:
                other.close()
        rows = json.dumps([tuple(row) for row in self.store.db.execute("SELECT key,value FROM host_settings")])
        public = json.dumps(catalog.list())
        for secret in ("synthetic-credential-one", "synthetic-credential-two", first.credential_seal):
            self.assertNotIn(secret, rows + public)

    def test_corrupted_catalog_and_seal_fail_closed_without_overwrite(self):
        snapshot = self.snapshot(self.create())
        with self.store.db:
            self.store.db.execute("INSERT INTO host_settings VALUES(?,?)", (SEAL_KEY, "invalid-private-state"))
        with self.assertRaisesRegex(ModelProfileError, "model_profile_unavailable"):
            self.catalog.bind(snapshot)
        self.assertEqual(self.store.host_setting(SEAL_KEY), "invalid-private-state")
        corruptions = ['{"format":1,"format":1,"revision":0,"selected_id":null,"items":[]}',
                       '{"format":1,"revision":0,"selected_id":"missing","items":[]}',
                       json.dumps({"format": 1, "revision": 0, "selected_id": None,
                                   "items": [snapshot, snapshot]}), "x" * 65537]
        for raw in corruptions:
            with self.store.db:
                self.store.db.execute("UPDATE host_settings SET value=? WHERE key=?", (raw, CATALOG_KEY))
            with self.assertRaisesRegex(ModelProfileError, "^model_profile_unavailable$"):
                self.catalog.list()
            self.assertEqual(self.store.db.execute("SELECT value FROM host_settings WHERE key=?", (CATALOG_KEY,)).fetchone()[0], raw)

    def test_host_settings_cas_is_bounded_and_does_not_overwrite_mismatch(self):
        self.assertTrue(self.store.compare_and_swap_host_setting("test.setting", None, "value"))
        self.assertFalse(self.store.compare_and_swap_host_setting("test.setting", None, "other"))
        self.assertFalse(self.store.compare_and_swap_host_setting("test.setting", "stale", "other"))
        self.assertEqual(self.store.host_setting("test.setting"), "value")
        self.assertTrue(self.store.compare_and_swap_host_setting("test.setting", "value", "new"))
        for bad in ("x" * 65537, "\ud800", 1):
            with self.assertRaisesRegex(ValueError, "^invalid_host_setting$"):
                self.store.compare_and_swap_host_setting("test.setting", "new", bad)
        self.assertEqual(self.store.host_setting("test.setting"), "new")

    def test_identity_is_order_stable_and_rejects_noncanonical_snapshot(self):
        snapshot = self.snapshot(self.create())
        self.assertEqual(profile_identity(snapshot), profile_identity(dict(reversed(list(snapshot.items())))))
        with self.assertRaisesRegex(ModelProfileError, "invalid_model_profile"):
            profile_identity({**snapshot, "base_url": snapshot["base_url"] + "/"})

    def test_explicit_probe_freezes_identity_sanitizes_output_and_releases_lock(self):
        async def scenario():
            item = self.create()
            entered, release = asyncio.Event(), asyncio.Event()

            async def probe(model):
                self.assertEqual(model.model, "synthetic-model")
                entered.set()
                await release.wait()
                return {"ok": True, "request_issued": True, "usage": None,
                        "text": "do-not-return", "api_key": "do-not-return"}

            with patch("astra_harness.model_profiles.model_profile_adapter.probe_model", probe):
                task = asyncio.create_task(self.catalog.test(item["id"], 1))
                await asyncio.wait_for(entered.wait(), 2)
                with self.assertRaisesRegex(ModelProfileError, "model_profile_in_use"):
                    await self.catalog.test(item["id"], 1)
                self.catalog.save({**self.profile, "id": item["id"], "model": "changed"}, 1)
                release.set()
                result = await task
            self.assertEqual((result["identity"], result["revision"], result["profile_revision"]), (item["identity"], 1, 1))
            self.assertNotIn("do-not-return", json.dumps(result))
            self.assertEqual(self.environment_calls, 1)
            self.assertFalse(self.catalog._test_lock.locked())
        asyncio.run(scenario())

    def test_probe_cancel_and_stale_revision_never_trigger_second_request(self):
        async def scenario():
            item = self.create()
            with self.assertRaisesRegex(ModelProfileError, "model_profile_conflict"):
                await self.catalog.test(item["id"], 0)
            self.assertEqual(self.environment_calls, 0)
            calls = []
            entered = asyncio.Event()

            async def probe(model):
                calls.append(model)
                entered.set()
                await asyncio.Event().wait()

            with patch("astra_harness.model_profiles.model_profile_adapter.probe_model", probe):
                task = asyncio.create_task(self.catalog.test(item["id"], 1))
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(len(calls), 1)
            self.assertFalse(self.catalog._test_lock.locked())
        asyncio.run(scenario())

    def test_probe_error_code_and_usage_are_bounded_without_generated_text(self):
        item = self.create()

        async def probe(model):
            return {"ok": False, "request_issued": True, "error_code": "secret-raw-provider-error"}

        with patch("astra_harness.model_profiles.model_profile_adapter.probe_model", probe):
            result = asyncio.run(self.catalog.test(item["id"], 1))
        self.assertEqual(result["error_code"], "model_transport_error")
        self.assertNotIn("secret-raw", json.dumps(result))

        async def invalid_usage(model):
            return {"ok": True, "request_issued": True, "usage": {"input_tokens": "raw-error"}}

        with patch("astra_harness.model_profiles.model_profile_adapter.probe_model", invalid_usage):
            with self.assertRaisesRegex(ModelProfileError, "^model_profile_unavailable$"):
                asyncio.run(self.catalog.test(item["id"], 1))
        self.assertFalse(self.catalog._test_lock.locked())


if __name__ == "__main__":
    unittest.main()
