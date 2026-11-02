"""Web-created collections use real files/indexes and never call providers."""
import base64
from contextlib import ExitStack
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from astra_harness.application import RuntimeUpdateError
from astra_harness.cli import build_application
from astra_harness.knowledge_library import KnowledgeLibraryError, library_directory, upload_material
from astra_harness.web import create_server
from astra_harness.workspace_binding import WorkspaceError
from tests.test_workspace_host import WorkspaceHostFixture


class KnowledgeLibraryTests(WorkspaceHostFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app, self.services, self.resources = self.build()
        self.library = self.services['knowledge']

    def create(self, digit='a', label='产品手册', **changes):
        body = dict(id='kb_' + digit * 24, label=label,
                    expected_revision=self.library.management()['revision'], workspace_id=self.app.startup_workspace_id)
        body.update(changes)
        return self.library.create(**body)

    def server(self):
        server = create_server(self.app, port=0, **self.services)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        self.port = server.server_port

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        fields = {'Host': f'127.0.0.1:{self.port}'}
        if body is not None:
            body = json.dumps(body).encode()
            fields['Content-Type'] = 'application/json'
        fields.update(headers or {})
        try:
            connection.request('POST' if body is not None else 'GET', path, body, fields)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def upload_body(self, id, raw=b'Retry delay: 17 seconds\r\n', filename='guide.md'):
        return dict(filename=filename, content_base64=base64.b64encode(raw).decode(),
                    expected_identity=self.library.get(id).recovery_identity, workspace_id=self.app.startup_workspace_id)

    def test_browser_flow_create_upload_approve_search_and_remove(self):
        self.server()
        status, initial = self.request('/api/knowledge')
        self.assertEqual(status, 200)
        self.assertTrue(initial['management']['can_create'])
        self.assertEqual(initial['collections'], [])
        identifier = 'kb_' + 'a' * 24
        body = dict(id=identifier, label='产品手册', expected_revision=initial['management']['revision'], workspace_id=initial['workspace_id'])
        self.assertEqual(self.request('/api/knowledge/collections', body)[0], 201)
        status, uploaded = self.request(f'/api/knowledge/{identifier}/files', self.upload_body(identifier))
        self.assertEqual(status, 201)
        plugin = self.library.get(identifier)
        self.assertEqual(plugin.catalog()['documents'], [])
        self.assertEqual((plugin.root / uploaded['path']).read_bytes(), b'Retry delay: 17 seconds\r\n')
        inspected = self.request(f'/api/knowledge/{identifier}/inspect?path=guide.md')[1]
        arguments = dict(path='guide.md', expected_source_sha256=inspected['source_sha256'], expected_index_sha256='')
        status, run = self.request(f'/api/knowledge/{identifier}/operations',
                                   dict(action='import_file', arguments=arguments, request_id='import-one', workspace_id=initial['workspace_id']))
        self.assertEqual(status, 201)
        waiting = self.app.wait(run['id'])
        self.assertEqual(waiting['status'], 'waiting_approval')
        self.assertEqual(plugin.catalog()['documents'], [])
        self.request(f"/api/runs/{run['id']}/approve", {'approved': True})
        completed = self.app.wait(run['id'])
        self.assertTrue(completed['tool_result']['value']['ok'])
        self.assertEqual(completed['model_calls'], 0)
        _, searched = self.request(f'/api/knowledge/{identifier}/operations',
                                   dict(action='search', arguments={'query': 'Retry delay'}, request_id='search-one'))
        answer = self.app.wait(searched['id'])['tool_result']['value']
        self.assertIn('17 seconds', answer['hits'][0]['text'])
        _, removal = self.request(f'/api/knowledge/{identifier}/operations', dict(action='remove',
            arguments={'path': 'guide.md', 'expected_index_sha256': inspected['source_sha256']}, request_id='remove-one'))
        self.app.wait(removal['id'])
        self.request(f"/api/runs/{removal['id']}/approve", {'approved': True})
        self.app.wait(removal['id'])
        self.assertEqual(plugin.catalog()['documents'], [])
        self.assertTrue((plugin.root / 'guide.md').exists())

    def test_restart_restores_collection_tools_material_and_pending_approval(self):
        identifier = self.create()['id']
        plugin = self.library.get(identifier)
        upload_material(plugin, 'guide.md', base64.b64encode(b'Gold 17 seconds').decode())
        inspected = plugin.inspect('guide.md')
        run = self.app.submit_tool(f'knowledge.{identifier}.import_file', dict(path='guide.md',
            expected_source_sha256=inspected['source_sha256'], expected_index_sha256=''))
        self.assertEqual(self.app.wait(run['id'])['status'], 'waiting_approval')
        self.resources.close()
        app, services, _ = self.build()
        self.assertEqual(services['knowledge'].collections()[0]['id'], identifier)
        self.assertEqual(app.get_run(run['id'])['status'], 'waiting_approval')
        self.assertEqual(services['knowledge'].get(identifier).inspect('guide.md')['source_sha256'], inspected['source_sha256'])
        self.assertIn(f'knowledge.{identifier}.search', [tool.name for tool in app.tools])

    def test_creation_retry_conflict_and_collection_isolation(self):
        revision = self.library.management()['revision']
        first = self.create(expected_revision=revision)
        self.assertEqual(self.create(expected_revision=revision)['id'], first['id'])
        with self.assertRaises(KnowledgeLibraryError):
            self.create(label='另一个名称', expected_revision=revision)
        with self.assertRaises(KnowledgeLibraryError):
            self.create('b', expected_revision=revision)
        second = self.create('b', '学习笔记')
        self.assertNotEqual(self.library.get(first['id']).root, self.library.get(second['id']).root)
        self.assertNotEqual(self.library.get(first['id']).index_path, self.library.get(second['id']).index_path)

    def test_failed_persistence_restores_runtime_and_can_retry(self):
        old = self.app.tools
        with patch('astra_harness.knowledge_library._new_file', side_effect=OSError('synthetic')):
            with self.assertRaises(KnowledgeLibraryError):
                self.create()
        self.assertEqual(self.app.tools, old)
        self.assertEqual(self.services['mcp_profiles'].base_tools, old)
        self.assertEqual(self.library.collections(), [])
        self.create()
        self.assertEqual(len(self.library.collections()), 1)

    def test_new_collection_preserves_composition_and_mcp_recomposition(self):
        from astra_harness.tools import Tool
        from types import SimpleNamespace
        profiles = self.services['mcp_profiles']
        # A deterministic active connection exercises the same composition used
        # by discovery without starting any external process or network service.
        tool = Tool('mcp.synthetic.echo', 'Synthetic echo', (), lambda: 'ok')
        profiles._active['synthetic'] = SimpleNamespace(tools=(tool,))
        try:
            self.create()
            self.assertIn(tool, self.app.tools)
            self.assertEqual(profiles._compose(profiles._active)['tools'], self.app.tools)
            self.assertTrue(any(item.name.startswith('knowledge.kb_') for item in profiles._compose({})['tools']))
        finally:
            profiles._active.clear()

    def test_pending_task_blocks_creation_without_changing_catalog(self):
        identifier = self.create()['id']
        plugin = self.library.get(identifier)
        upload_material(plugin, 'guide.md', base64.b64encode(b'synthetic').decode())
        run = self.app.submit_tool(f'knowledge.{identifier}.import_file', {'path': 'guide.md'})
        self.app.wait(run['id'])
        with self.assertRaises(RuntimeUpdateError):
            self.create('b')
        self.assertEqual(len(self.library.collections()), 1)
        self.assertFalse((self.startup / 'knowledge' / ('kb_' + 'b' * 24)).exists())

    def test_runtime_publication_does_not_hold_mcp_status_lock(self):
        publish = self.app.publish_tool_update
        def checked_publish(*args, **kwargs):
            observed = threading.Event()
            def read_status():
                self.services['mcp_profiles'].status()
                observed.set()
            reader = threading.Thread(target=read_status, daemon=True)
            reader.start()
            self.assertTrue(observed.wait(timeout=1), 'status reads must not wait on publication')
            reader.join(timeout=1)
            return publish(*args, **kwargs)
        with patch.object(self.app, 'publish_tool_update', side_effect=checked_publish):
            self.create()

    def test_wrong_workspace_and_host_state_paths_rejected(self):
        self.select(self.app, self.services['workspaces'], self.other)
        with self.assertRaises(WorkspaceError):
            self.create()
        with self.assertRaises(WorkspaceError):
            self.services['workspaces'].inspect(library_directory(self.database))

    def test_upload_large_utf8_preserves_bytes_and_rejects_overwrite(self):
        identifier = self.create()['id']
        self.server()
        raw = ('中文\r\n' * 15000).encode()
        self.assertGreater(len(raw), 65536)
        endpoint = f'/api/knowledge/{identifier}/files'
        self.assertEqual(self.request(endpoint, self.upload_body(identifier, raw))[0], 201)
        self.assertTrue(self.request(endpoint, self.upload_body(identifier, raw))[1]['unchanged'])
        self.assertEqual(self.request(endpoint, self.upload_body(identifier, b'different'))[1]['error'], 'knowledge_file_exists')
        self.assertEqual((self.library.get(identifier).root / 'guide.md').read_bytes(), raw)
        self.assertEqual(self.request(endpoint, self.upload_body(identifier, b'x' * 128001))[0], 400)
        self.assertEqual(self.library.get(identifier).catalog()['documents'], [])

    def test_invalid_uploads_and_stale_identity_have_no_side_effects(self):
        identifier = self.create()['id']
        self.server()
        endpoint = f'/api/knowledge/{identifier}/files'
        for filename, raw in [('..\\escape.md', b'x'), ('/escape.md', b'x'), ('.env', b'x'), ('credentials.txt', b'x'),
                              ('CON.txt', b'x'), ('guide.pdf', b'x'), ('bad.txt', b'\xff'), ('binary.txt', b'x\0y')]:
            with self.subTest(filename=filename):
                self.assertEqual(self.request(endpoint, self.upload_body(identifier, raw, filename))[0], 400)
        body = self.upload_body(identifier)
        body['expected_identity'] = '0' * 64
        self.assertEqual(self.request(endpoint, body)[0], 409)
        body = self.upload_body(identifier)
        self.assertEqual(self.request(endpoint, body, {'Origin': 'https://untrusted.invalid'})[0], 403)
        self.assertEqual(list(self.library.get(identifier).root.iterdir()), [])

    def test_replaced_material_directory_is_rejected(self):
        identifier = self.create()['id']
        plugin = self.library.get(identifier)
        plugin.root.rename(plugin.root.with_name(plugin.root.name + '-old'))
        plugin.root.mkdir()
        with self.assertRaises(KnowledgeLibraryError):
            upload_material(plugin, 'guide.md', base64.b64encode(b'x').decode())
        self.assertEqual(list(plugin.root.iterdir()), [])

    def test_embedding_configuration_remains_editable_and_requires_restart(self):
        identifier = self.create()['id']
        before = self.library.embedding_configuration(identifier)
        self.assertTrue(before['editable'])
        profile = {**before['saved'], 'revision': '2'}
        saved = self.library.save_embedding_configuration(identifier, profile, before['config_revision'])
        self.assertTrue(saved['restart_required'])
        with self.assertRaises(KnowledgeLibraryError):
            self.create('b')


if __name__ == '__main__':
    unittest.main()
