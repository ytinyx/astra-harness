"""Idle runtime publication with synthetic tools; no service processes or network."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from astra_harness.application import Application, RuntimeUpdateError
from astra_harness.builtin.files import FilePlugin, inspect_workspace_directory
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.model_binding import ModelBinding
from astra_harness.tools import Tool
from astra_harness.workspace_binding import WorkspaceBinding


class Model:
    def complete(self, request):
        if request.messages[-1].role == 'tool':
            return ModelResponse(text=str(request.messages[-1].tool_result.value))
        return ModelResponse(tool_calls=(ToolCall('read-value', 'value', {}),))


class ProfileCatalog:
    snapshot = {'id': 'fixture', 'name': 'fixture', 'provider': 'fixture', 'revision': 1}
    def selected_snapshot(self): return dict(self.snapshot)
    def list(self):
        return {'selected_id': 'fixture', 'selected_identity': 'a' * 64,
                'items': [{**self.snapshot, 'identity': 'a' * 64}]}
    def bind(self, snapshot): return ModelBinding(dict(snapshot), 'a' * 64, 'b' * 64, Model())


class MCPProfilesRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='astra-runtime-refresh-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.database = self.base / 'runs.sqlite3'
        self.effects = []
        self.value = Tool('value', 'synthetic value', (), lambda: 1,
                          permissions=frozenset({'value'}), recovery_identity='value-v1')
        self.write = Tool('write', 'synthetic approved effect', (), self.write_effect,
                          permissions=frozenset({'write'}), side_effect=True, requires_approval=True)

    def write_effect(self):
        self.effects.append('written')
        return 1

    def app(self, **kwargs):
        options = {'tools': (self.value, self.write), 'permissions': ('value', 'write')}
        options.update(kwargs)
        app = Application(Model(), database=self.database, **options)
        self.addCleanup(app.close)
        return app

    def replacement(self):
        return Tool('value', 'synthetic value', (), lambda: 2,
                    permissions=frozenset({'value'}), recovery_identity='value-v2')

    def publish(self, app, tools=None, **kwargs):
        token = app.reserve_tool_update()
        try:
            app.publish_tool_update(token, tools=tools or (self.replacement(),), permissions=('value',), **kwargs)
        finally:
            app.release_tool_update(token)

    def workspace_options(self):
        self.specs, self.files = {}, {}
        for name in ('A', 'B'):
            root = self.base / name
            root.mkdir()
            self.specs[name] = {'id': name, **inspect_workspace_directory(root)}
            self.files[name] = FilePlugin(root)
        return {'workspace': self.base / 'A', 'workspace_spec': self.specs['A'],
                'workspace_factory': self.factory((self.value, self.write))}

    def factory(self, startup_extra):
        def build(spec):
            files = self.files[spec['id']]
            extra = startup_extra if spec['id'] == 'A' else (self.write,)
            tools = files.tools + tuple(extra)
            permissions = tuple(sorted({'files.read', 'files.write'} | {p for t in extra for p in t.permissions}))
            return WorkspaceBinding(**spec, tools=tools, permissions=permissions, file_plugin=files)
        return build

    def test_idle_publication_rebuilds_frozen_registry_and_permissions(self):
        app = self.app()
        self.assertEqual(app.wait(app.submit_tool('value', {})['id'])['tool_result']['value'], 1)
        self.publish(app)
        self.assertEqual(app.info()['tools'], ['value'])
        self.assertEqual(app.wait(app.submit_tool('value', {})['id'])['tool_result']['value'], 2)
        with self.assertRaisesRegex(ValueError, 'frozen'):
            app.engine.registry.register(self.write)
        denied = app.wait(app.submit_tool('write', {})['id'])
        self.assertEqual(denied['error'], 'unknown_tool')

    def test_pending_approval_blocks_change_until_explicitly_cancelled(self):
        app = self.app()
        pending = app.wait(app.submit_tool('write', {})['id'])
        before = app.store.get(pending['id'])
        with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'):
            app.reserve_tool_update()
        self.assertEqual(app.store.get(pending['id']), before)
        self.assertEqual(self.effects, [])
        app.cancel(pending['id'])
        self.publish(app)
        with self.assertRaises(RuntimeError): app.approve(pending['id'])
        self.assertEqual(self.effects, [])

    def test_paused_startup_run_blocks_publication(self):
        app = self.app(autostart=False)
        run = app.submit('queued')
        app.pause(run['id'])
        with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'):
            app.reserve_tool_update()
        self.assertEqual(app.get_run(run['id'])['status'], 'paused')

    def test_active_call_cannot_be_closed_or_replaced_by_update(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow():
            entered.set()
            if not release.wait(3): raise RuntimeError('fixture release missing')
            return 4
        app = self.app(tools=(Tool('slow', 'synthetic slow read', (), slow),))
        run = app.submit_tool('slow', {})
        self.assertTrue(entered.wait(2))
        with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.reserve_tool_update()
        release.set()
        self.assertEqual(app.wait(run['id'])['tool_result']['value'], 4)

    def test_unknown_blocks_update_without_retrying_or_changing_evidence(self):
        app = self.app(autostart=False)
        run = app.submit_tool('write', {})
        raw = app.store.get(run['id'])
        raw.update(status='unknown', error='fixture_unknown', unknown_process='legacy')
        app.store.save(raw)
        before = app.store.get(run['id'])
        with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.reserve_tool_update()
        self.assertEqual(app.store.get(run['id']), before)
        self.assertEqual(self.effects, [])

    def test_reservation_blocks_new_work_but_known_confirmation_is_read_only(self):
        app = self.app()
        old = app.wait(app.submit_tool('value', {}, request_id='known')['id'])
        token = app.reserve_tool_update()
        try:
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.submit('new agent')
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.submit_tool('value', {})
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.reserve_tool_update()
            confirmed = app.submit_tool('value', {}, request_id='known')
            self.assertEqual(confirmed['id'], old['id'])
            self.assertEqual(len(app.list_runs()), 1)
        finally:
            app.release_tool_update(token)

    def test_invalid_token_cannot_publish_or_release_another_reservation(self):
        app = self.app()
        token = app.reserve_tool_update()
        try:
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_invalid'):
                app.publish_tool_update('wrong', tools=(self.replacement(),), permissions=('value',))
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_invalid'): app.release_tool_update('wrong')
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.submit('still reserved')
        finally:
            app.release_tool_update(token)
        with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_invalid'): app.release_tool_update(token)

    def test_failed_candidate_keeps_old_runtime_and_reservation(self):
        app = self.app()
        before = app.engine.snapshot()
        token = app.reserve_tool_update()
        try:
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_invalid'):
                app.publish_tool_update(token, tools=(self.value, self.value), permissions=('value',))
            self.assertEqual(app.engine.snapshot(), before)
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.submit('not released')
        finally:
            app.release_tool_update(token)
        self.assertEqual(app.wait(app.submit_tool('value', {})['id'])['tool_result']['value'], 1)

    def test_same_permissions_refresh_clears_profile_engine_cache(self):
        app = self.app(tools=(self.value,), permissions=('value',), model_catalog_factory=lambda store: ProfileCatalog())
        first = app.wait(app.submit('old model binding')['id'])
        self.assertEqual(first['output'], '1')
        fingerprint = app.fingerprint
        self.assertTrue(app._profile_executions)
        self.publish(app)
        self.assertEqual(app.fingerprint, fingerprint)
        self.assertEqual(app._profile_executions, {})
        second = app.wait(app.submit('new tool composition')['id'])
        self.assertEqual(second['model_identity'], first['model_identity'])
        self.assertEqual(second['output'], '2')
        self.assertEqual(second['tool_snapshot'][0]['recovery_identity'], 'value-v2')

    def test_startup_factory_replacement_does_not_grant_tools_to_other_workspace(self):
        app = self.app(**self.workspace_options())
        extra = (self.replacement(),)
        factory = self.factory(extra)
        binding = factory(self.specs['A'])
        token = app.reserve_tool_update()
        try:
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'):
                app.select_workspace(self.specs['B'], app.workspaces()['revision'])
            app.publish_tool_update(token, tools=binding.tools, permissions=binding.permissions,
                                    workspace_factory=factory, service_status=lambda: [{'state': 'ready'}])
        finally:
            app.release_tool_update(token)
        app.select_workspace(self.specs['B'], app.workspaces()['revision'])
        self.assertNotIn('value', app.info()['tools'])
        app.select_workspace(self.specs['A'], app.workspaces()['revision'])
        self.assertEqual(app.wait(app.submit_tool('value', {})['id'])['tool_result']['value'], 2)
        self.assertEqual(app.info()['services'], [{'state': 'ready'}])

    def test_other_workspace_queue_waits_during_reservation_and_then_resumes(self):
        app = self.app(autostart=False, **self.workspace_options())
        app.select_workspace(self.specs['B'], app.workspaces()['revision'])
        pending = app.submit_tool('write', {})
        token = app.reserve_tool_update()
        try:
            app.start()
            time.sleep(0.12)
            self.assertEqual(app.get_run(pending['id'])['status'], 'pending')
            self.assertEqual(app.get_run(pending['id'])['tool_calls'], 0)
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.resume(pending['id'])
        finally:
            app.release_tool_update(token)
        self.assertEqual(app.wait(pending['id'])['status'], 'waiting_approval')
        token = app.reserve_tool_update()
        try:
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.approve(pending['id'])
            denied = app.approve(pending['id'], approved=False)
            self.assertEqual(denied['error'], 'approval_denied')
        finally:
            app.release_tool_update(token)
        self.assertEqual(self.effects, [])

    def test_same_reservation_can_restore_before_release_and_close_waits_for_cleanup(self):
        app = self.app()
        token = app.reserve_tool_update()
        try:
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.close()
            app.publish_tool_update(token, tools=(self.replacement(),), permissions=('value',))
            with self.assertRaisesRegex(RuntimeUpdateError, 'runtime_update_busy'): app.submit_tool('value', {})
            # A host-side configuration CAS failure can restore the exact old
            # composition before any task is admitted to the candidate runtime.
            app.publish_tool_update(token, tools=(self.value, self.write), permissions=('value', 'write'))
        finally:
            app.release_tool_update(token)
        self.assertEqual(app.wait(app.submit_tool('value', {})['id'])['tool_result']['value'], 1)
        app.close()


if __name__ == '__main__': unittest.main()
