"""Offline model-binding lifecycle probes, using separate execution oracles."""
import asyncio
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import threading
import unittest

from astra_harness.application import Application
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.model_binding import ModelBinding, ModelProfileError
from astra_harness.tools import Tool
from astra_harness.builtin.files import FilePlugin, inspect_workspace_directory
from astra_harness.workspace_binding import WorkspaceBinding


def identity(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


class RecordingModel:
    def __init__(self, name, writes=False, entered=None, release=None):
        self.name, self.writes = name, writes
        self.requests = []
        self.entered, self.release = entered, release

    def complete(self, request):
        self.requests.append(request)
        if self.entered:
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("fixture release missing")
        if self.writes and request.messages[-1].role != "tool":
            return ModelResponse(tool_calls=(ToolCall("fixture-write", "write", {"value": self.name}),))
        return ModelResponse(text=self.name)


class Catalog:
    """Tiny test host; no network, environment or credential file access."""
    def __init__(self, store, keys, models, bindings):
        self.store, self.keys, self.models, self.bindings = store, keys, models, bindings
        self.test_entered, self.test_release = threading.Event(), threading.Event()
        with store._lock, store.db:
            store.db.execute("INSERT OR IGNORE INTO host_settings VALUES('fixture_models',?)",
                             (json.dumps({"revision": 0, "selected_id": None, "items": []}),))

    def state(self):
        with self.store._lock:
            return json.loads(self.store.db.execute("SELECT value FROM host_settings WHERE key='fixture_models'").fetchone()[0])

    def list(self):
        state = self.state()
        for item in state["items"]:
            item["identity"] = identity(item)
        state["selected_identity"] = next((item["identity"] for item in state["items"]
                                          if item["id"] == state["selected_id"]), "startup")
        return state

    def selected_snapshot(self):
        state = self.state()
        return next((item for item in state["items"] if item["id"] == state["selected_id"]), None)

    def bind(self, snapshot):
        key = self.keys.get(snapshot["key_env"])
        if key is None:
            raise ModelProfileError("model_credentials_missing")
        self.bindings.append(deepcopy(snapshot))
        tag = (snapshot["id"], snapshot["revision"])
        model = self.models.setdefault(tag, RecordingModel(snapshot["model"]))
        seal = hmac.new(b"fixture-private-db-key", (key + identity(snapshot)).encode(), hashlib.sha256).hexdigest()
        return ModelBinding(deepcopy(snapshot), identity(snapshot), seal, model)

    def _change(self, expected_revision, action):
        state = self.state()
        if state["revision"] != expected_revision:
            raise ModelProfileError("model_profile_conflict")
        action(state)
        state["revision"] += 1
        with self.store._lock, self.store.db:
            self.store.db.execute("UPDATE host_settings SET value=? WHERE key='fixture_models'", (json.dumps(state),))
        return self.list()

    def save(self, profile, expected_revision):
        def action(state):
            old = next((row for row in state["items"] if row["id"] == profile["id"]), None)
            saved = {**profile, "revision": old["revision"] + 1 if old else 1}
            state["items"] = [row for row in state["items"] if row["id"] != profile["id"]] + [saved]
        return self._change(expected_revision, action)

    def select(self, identifier, expected_revision):
        def action(state):
            if identifier is not None and not any(row["id"] == identifier for row in state["items"]):
                raise ModelProfileError("model_profile_not_found")
            state["selected_id"] = identifier
        return self._change(expected_revision, action)

    def delete(self, identifier, expected_revision):
        def action(state):
            state["items"] = [row for row in state["items"] if row["id"] != identifier]
            if state["selected_id"] == identifier:
                state["selected_id"] = None
        return self._change(expected_revision, action)

    async def test(self, identifier, expected_revision):
        self.test_entered.set()
        while not self.test_release.is_set():
            await asyncio.sleep(0.005)
        return {"ok": True, "id": identifier}


class ModelProfilesRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-profile-runtime-")
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "runs.sqlite3"
        self.keys = {"KEY_A": "private-A", "KEY_B": "private-B"}
        self.models, self.bindings, self.effects = {}, [], []
        self.startup = RecordingModel("startup")
        self.tool = Tool("write", "synthetic effect", ("value",), lambda value: self.effects.append(value),
                         input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                                       "required": ["value"], "additionalProperties": False},
                         side_effect=True, requires_approval=True)

    def app(self, **kwargs):
        app = Application(self.startup, (self.tool,), database=self.database,
                          model_catalog_factory=lambda store: Catalog(store, self.keys, self.models, self.bindings),
                          **kwargs)
        self.addCleanup(app.close)
        return app

    def save(self, app, identifier, model=None):
        profile = {"id": identifier, "model": model or identifier, "key_env": "KEY_" + identifier.upper(),
                   "protocol": "fixture"}
        return app.save_model_profile(profile, app.model_profiles()["revision"])

    def select(self, app, identifier):
        return app.select_model_profile(identifier, app.model_profiles()["revision"])

    def configured(self, app, identifier="a"):
        self.save(app, identifier)
        return self.select(app, identifier)

    def assert_private(self, app, run_id, *responses):
        private = app.store.get(run_id)["_model_credential_seal"]
        text = json.dumps(responses, ensure_ascii=False)
        self.assertNotIn("_model_credential_seal", text)
        self.assertNotIn(private, text)
        self.assertNotIn("private-A", text)

    def test_listing_saving_selecting_do_not_bind_or_request(self):
        app = self.app()
        self.configured(app)
        self.assertEqual(self.bindings, [])
        self.assertEqual(self.models, {})
        self.assertEqual(self.startup.requests, [])

    def test_switch_during_call_keeps_one_worker_and_original_model(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.models[("a", 1)] = RecordingModel("A", entered=entered, release=release)
        app = self.app()
        self.configured(app)
        worker = app._thread
        first = app.submit("original A")
        self.assertTrue(entered.wait(2))
        self.configured(app, "b")
        second = app.submit("new B")
        self.assertIs(app._thread, worker)
        self.assertEqual(app.get_run(second["id"])["model_calls"], 0)
        release.set()
        self.assertEqual(app.wait(first["id"])["output"], "A")
        self.assertEqual(app.wait(second["id"])["output"], "b")
        self.assertEqual(self.startup.requests, [])

    def test_edit_and_reopen_approval_use_saved_revision_not_current_profile(self):
        self.models[("a", 1)] = RecordingModel("original A", writes=True)
        app = self.app()
        self.configured(app)
        pending = app.wait(app.submit("write")['id'])
        self.assertEqual(pending["status"], "waiting_approval")
        self.save(app, "a", "changed revision")
        self.configured(app, "b")
        app.close()
        app = self.app()
        self.assertEqual(app.get_run(pending["id"])["status"], "waiting_approval")
        app.approve(pending["id"])
        result = app.wait(pending["id"])
        self.assertEqual(result["output"], "original A")
        self.assertEqual(self.effects, ["original A"])
        self.assertEqual(result["model_snapshot"]["revision"], 1)
        self.assertNotIn(("a", 2), self.models)

    def test_credential_drift_restart_fails_before_approved_effect(self):
        self.models[("a", 1)] = RecordingModel("old A", writes=True)
        app = self.app()
        self.configured(app)
        pending = app.wait(app.submit("write")["id"])
        app.close()
        self.keys["KEY_A"] = "rotated-A"
        app = self.app()
        result = app.get_run(pending["id"])
        self.assertEqual((result["status"], result["error"]), ("failed", "model_credentials_changed"))
        self.assertEqual(self.effects, [])
        self.assertEqual(len(self.models[("a", 1)].requests), 1)

    def test_missing_credentials_restart_is_explicit_without_startup_fallback(self):
        app = self.app(autostart=False)
        self.configured(app)
        pending = app.submit("pending")
        app.close()
        self.keys.clear()
        app = self.app()
        result = app.get_run(pending["id"])
        self.assertEqual(result["error"], "model_credentials_missing")
        self.assertEqual(self.startup.requests, [])

    def test_in_process_pending_keeps_original_binding_after_key_change(self):
        app = self.app(autostart=False)
        self.configured(app)
        pending = app.submit("queued")
        self.keys.clear()
        app.start()
        self.assertEqual(app.wait(pending["id"])["output"], "a")
        self.assertEqual(len(self.bindings), 1)

    def test_existing_session_cannot_send_history_to_changed_profile(self):
        app = self.app()
        self.configured(app)
        first = app.wait(app.submit("secret fixture A history")["id"])
        self.configured(app, "b")
        before = len(self.bindings)
        with self.assertRaisesRegex(ModelProfileError, "model_session_mismatch"):
            app.submit("continue elsewhere", session_id=first["session_id"])
        self.assertEqual(len(self.bindings), before)
        self.assertEqual(app.get_session(first["session_id"])["model_identity"], first["model_identity"])
        second = app.wait(app.submit("new B conversation")["id"])
        self.assertNotIn("secret fixture A history", [m.text for m in self.models[("b", 1)].requests[0].messages])
        self.assertNotEqual(first["session_id"], second["session_id"])

    def test_old_request_confirmation_is_read_only_after_selection_changes(self):
        app = self.app()
        metadata = self.configured(app)
        old_identity = metadata["selected_identity"]
        run = app.wait(app.submit("confirm", request_id="same", expected_model_identity=old_identity)["id"])
        self.configured(app, "b")
        confirmed = app.submit("confirm", request_id="same", expected_model_identity=old_identity)
        self.assertEqual(confirmed["id"], run["id"])
        with self.assertRaisesRegex(ModelProfileError, "model_profile_not_selected"):
            app.submit("stale page", expected_model_identity=old_identity)
        with self.assertRaises(ValueError):
            app.submit("confirm", request_id="same", expected_model_identity=app.model_profiles()["selected_identity"])
        self.assertEqual(len(app.list_runs()), 1)

    def test_delete_nonterminal_reference_rejects_but_terminal_keeps_snapshot(self):
        app = self.app(autostart=False)
        self.configured(app)
        pending = app.submit("queued")
        with self.assertRaisesRegex(ModelProfileError, "model_profile_in_use"):
            app.delete_model_profile("a", app.model_profiles()["revision"])
        app.cancel(pending["id"])
        original = app.store.get(pending["id"])
        app.delete_model_profile("a", app.model_profiles()["revision"])
        self.assertEqual(app.store.get(pending["id"]), original)
        self.assertEqual(app.get_run(pending["id"])["model_snapshot"]["id"], "a")

    def test_direct_tool_bypasses_selected_profile_and_missing_key(self):
        app = self.app()
        self.configured(app)
        self.keys.clear()
        pending = app.wait(app.submit_tool("write", {"value": "direct"})["id"])
        app.approve(pending["id"])
        self.assertEqual(app.wait(pending["id"])["status"], "completed")
        self.assertEqual(self.effects, ["direct"])
        self.assertEqual(self.bindings, [])
        self.assertNotIn("model_snapshot", pending)

    def test_startup_pending_recovery_ignores_new_selected_profile(self):
        app = self.app(autostart=False)
        pending = app.submit("startup pending")
        self.configured(app)
        app.close()
        app = self.app()
        self.assertEqual(app.wait(pending["id"])["output"], "startup")
        self.assertEqual(self.bindings, [])

    def test_cli_selection_override_does_not_modify_saved_web_selection(self):
        app = self.app()
        self.configured(app)
        before = app.model_profiles()
        app.close()
        app = self.app(restore_model_selection=False)
        result = app.wait(app.submit("CLI startup")["id"])
        self.assertEqual(result["output"], "startup")
        self.assertEqual(app.model_profiles(), before)

    def test_all_public_run_responses_strip_private_credential_seal(self):
        self.models[("a", 1)] = RecordingModel("write A", writes=True)
        app = self.app(autostart=False)
        self.configured(app)
        submitted = app.submit("write", request_id="private-check")
        paused = app.pause(submitted["id"])
        resumed = app.resume(submitted["id"])
        app.start()
        pending = app.wait(submitted["id"])
        approved = app.approve(submitted["id"])
        result = app.wait(submitted["id"])
        self.assert_private(app, submitted["id"], submitted, paused, resumed, pending, approved, result,
                            app.get_run(submitted["id"]), app.list_runs(), app.get_session(submitted["session_id"]),
                            app.cancel(submitted["id"]), app.submit("write", request_id="private-check"),
                            app.events(submitted["id"]))

    def test_unknown_inflight_effect_wins_over_missing_credentials_and_blocks(self):
        app = self.app(autostart=False)
        self.configured(app)
        pending = app.submit("inflight")
        raw = app.store.get(pending["id"])
        raw.update(status="running", phase="tool", inflight={"phase": "tool", "side_effect": True})
        app.store.save(raw)
        app.close()
        self.keys.clear()
        app = self.app()
        result = app.get_run(pending["id"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "interrupted_effect_unknown")
        self.assertEqual(self.effects, [])
        with self.assertRaises(RuntimeError):
            app.submit("must not run")

    def test_async_probe_does_not_hold_application_lock(self):
        app = self.app(autostart=False)
        self.configured(app)
        catalog = app._model_catalog
        self.addCleanup(catalog.test_release.set)
        responses = []
        thread = threading.Thread(target=lambda: responses.append(asyncio.run(app.test_model_profile("a", 2))))
        thread.start()
        self.assertTrue(catalog.test_entered.wait(1))
        # Taking the lock from this thread while the coroutine waits would hang
        # if the public test wrapper held the application lock across await.
        self.assertTrue(app._lock.acquire(timeout=0.5))
        app._lock.release()
        catalog.test_release.set()
        thread.join(2)
        self.assertEqual(responses, [{"ok": True, "id": "a"}])

    def test_catalog_disabled_preserves_legacy_api(self):
        app = Application(self.startup, database=self.database)
        self.addCleanup(app.close)
        self.assertEqual(app.wait(app.submit("legacy")["id"])["output"], "startup")
        with self.assertRaisesRegex(ModelProfileError, "model_profiles_disabled"):
            app.model_profiles()

    def test_workspace_and_profile_are_independently_frozen_across_restart(self):
        roots = {name: self.database.parent / name for name in ("A", "B")}
        for path in roots.values():
            path.mkdir()
        specs = {name: {"id": name, **inspect_workspace_directory(path)} for name, path in roots.items()}
        def factory(spec):
            files = FilePlugin(spec["path"])
            write = Tool("write", "fixture effect", ("value",), lambda value: files.write_file("result.txt", value),
                         input_schema=self.tool.input_schema, side_effect=True, requires_approval=True)
            return WorkspaceBinding(spec["id"], spec["path"], spec["identity"], files.tools + (write,),
                                    ("files.read", "files.write"), files)
        options = {"workspace": roots["A"], "workspace_spec": specs["A"], "workspace_factory": factory}
        self.models[("a", 1)] = RecordingModel("A model writes", writes=True)
        app = self.app(**options)
        self.configured(app)
        pending = app.wait(app.submit("write in original A")["id"])
        app.select_workspace(specs["B"], app.workspaces()["revision"])
        self.configured(app, "b")
        app.close()
        app = self.app(**options)
        self.assertEqual(app.selected_workspace_id, "B")
        app.approve(pending["id"])
        self.assertEqual(app.wait(pending["id"])["status"], "completed")
        self.assertEqual((roots["A"] / "result.txt").read_text(), "A model writes")
        self.assertFalse((roots["B"] / "result.txt").exists())

    def test_profile_recovery_does_not_depend_on_unrelated_startup_model(self):
        app = self.app(autostart=False)
        self.configured(app)
        pending = app.submit("old selected profile")
        app.close()
        class DifferentStartup(RecordingModel):
            pass
        self.startup = DifferentStartup("unrelated startup")
        app = self.app()
        self.assertEqual(app.wait(pending["id"])["output"], "a")
        self.assertEqual(self.startup.requests, [])

    def test_invalid_private_binding_fails_closed_without_provider_call(self):
        app = self.app(autostart=False)
        self.configured(app)
        pending = app.submit("corrupted private binding")
        state = app.store.get(pending["id"])
        state["_model_credential_seal"] = ["not a seal"]
        app.store.save(state)
        app.close()
        before = len(self.bindings)
        app = self.app()
        self.assertEqual(app.get_run(pending["id"])["error"], "model_profile_changed")
        self.assertEqual(len(self.bindings), before)

    def test_metadata_and_cli_selection_are_visible_without_resolving_credentials(self):
        app = self.app()
        self.assertEqual(app.info()["model_selection"]["source"], "startup")
        metadata = self.configured(app)
        self.assertEqual(app.info()["model_selection"]["identity"], metadata["selected_identity"])
        self.keys.clear()
        self.assertEqual(app.info()["model_selection"]["source"], "profile")
        self.assertEqual(self.bindings, [])


if __name__ == "__main__":
    unittest.main()
