"""Real local embedding protocol with hand-assigned fixture vectors, not a semantic model."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import tempfile

from astra_harness.application import Application
from astra_harness.builtin.embeddings import OpenAICompatibleEmbeddingProvider, parse_embedding_config
from astra_harness.builtin.knowledge import KnowledgePlugin
from astra_harness.builtin.semantic import SemanticKnowledgePlugin, TEST_TEXT
from astra_harness.plugins import PluginManager
from astra_harness.tools import ToolRegistry
from .embedding_mock_server import MockEmbeddingServer


ALPHA = '# 合成甲\nSynthetic A: service tier Gold; retry 17 seconds.\n'
BETA = '# 合成乙\nSynthetic B: service tier Silver; retry 43 seconds.\n'
QUERY = 'independent ranking probe'
VECTOR_TABLE = {ALPHA: [1, 0, 0], BETA: [0, 1, 0], QUERY: [0.9, 0.1, 0]}


class NoModel:
    def complete(self, request):
        raise AssertionError('the embedding transport demonstration must not call a task model')


def require(value, message):
    if not value:
        raise RuntimeError(message)


@contextmanager
def open_host(directory, config):
    # The provider is injected here. A custom provider satisfying the small
    # async embed/status/recovery_identity contract does not require Engine edits.
    provider = OpenAICompatibleEmbeddingProvider(config, environ={config.key_env: 'mock-embedding-key'})
    knowledge = KnowledgePlugin(directory/'materials', directory/'lexical.sqlite3')
    semantic = SemanticKnowledgePlugin(knowledge, provider, config)
    registry = ToolRegistry()
    plugins = PluginManager(registry)
    try:
        plugins.activate(knowledge)
        plugins.activate(semantic)
        registry.freeze()
        with Application(NoModel(), registry.tools(), database=directory/'runs.sqlite3',
                         workspace=directory/'materials', permissions=semantic.permissions,
                         limits={'max_model_calls': 1, 'max_tool_calls': 2, 'tool_timeout': 8, 'run_timeout': 20},
                         mode='embedding-mock-demo') as app:
            yield app, knowledge, semantic
    finally:
        plugins.close()


def perform(app, name, arguments, server, *, approved=True):
    before = len(server.requests)
    run = app.submit_tool(name, arguments)
    waiting = app.wait(run['id'], timeout=15)
    require(waiting['status'] == 'waiting_approval', 'external/write operation skipped approval')
    require(len(server.requests) == before, 'a provider request occurred before approval')
    app.approve(run['id'], approved=approved)
    final = app.wait(run['id'], timeout=15)
    require(final['model_calls'] == 0, 'unexpected task model call')
    events = [{key: event[key] for key in ('seq', 'type', 'status', 'approved', 'result') if key in event}
              for event in app.events(run['id'])]
    return final, events


def verify_hit(hit):
    raw = ALPHA.encode('utf-8')
    require(hit['path'] == 'alpha.md' and hit['document_sha256'] == hashlib.sha256(raw).hexdigest(),
            'nearest hit source differs from the independent fixture expectation')
    require(hit['text'] == ALPHA[hit['start_offset']:hit['end_offset']], 'citation is not the actual source slice')
    for offset, line, column in ((hit['start_offset'], hit['start_line'], hit['start_column']),
                                (hit['end_offset'], hit['end_line'], hit['end_column'])):
        require(line == ALPHA[:offset].count('\n') + 1 and column == offset - ALPHA.rfind('\n', 0, offset),
                'citation coordinates do not match source code points')
    require(math.isclose(hit['score'], 0.9/math.sqrt(0.82), rel_tol=1e-8), 'cosine score differs from independent arithmetic')


def execute(directory, *, retained):
    directory.mkdir(parents=True, exist_ok=False)
    (directory/'materials').mkdir()
    for name, text in (('alpha.md', ALPHA), ('beta.md', BETA)):
        (directory/'materials'/name).write_bytes(text.encode('utf-8'))
    trace = {}
    with MockEmbeddingServer(vectors_by_text=VECTOR_TABLE, dimension=3) as server:
        config = parse_embedding_config({'enabled': True, 'endpoint': server.url,
                                         'model': 'mock-embedding-v1', 'dimensions': 3})
        with open_host(directory, config) as (app, lexical, semantic):
            require(server.requests == [], 'startup contacted a provider')
            denied, trace['denied_test'] = perform(app, 'knowledge.embedding_test', {}, server, approved=False)
            require(denied['status'] == 'failed' and denied['error'] == 'approval_denied' and server.requests == [],
                    'denied test sent a provider request')
            tested, trace['connection_test'] = perform(app, 'knowledge.embedding_test', {}, server)
            require(tested['status'] == 'completed' and tested['tool_result']['value']['ok'], 'fixed-text test failed')
            for name in ('alpha.md', 'beta.md'):
                imported, trace['import_'+name] = perform(app, 'knowledge.import_file', {'path': name}, server)
                require(imported['tool_result']['value']['ok'], 'lexical import failed')
            require(len(server.requests) == 1, 'lexical import unexpectedly used embedding')
            corpus = semantic.status()['corpus_sha256']
            rebuilt, trace['rebuild'] = perform(app, 'knowledge.embedding_rebuild', {'expected_corpus_sha256': corpus}, server)
            require(rebuilt['status'] == 'completed' and rebuilt['tool_result']['value']['ok'], 'vector rebuild failed')
            require(semantic.status()['state'] == 'ready', 'index did not become ready')
            require(lexical.search(QUERY)['hits'] == [], 'fixture query must not be a lexical match')
            searched, trace['query'] = perform(app, 'knowledge.semantic_search',
                {'query': QUERY, 'expected_corpus_sha256': corpus, 'limit': 1}, server)
            answer = searched['tool_result']['value']
            require(searched['status'] == 'completed' and answer['ok'] and answer['mode'] == 'semantic', 'semantic query failed')
            verify_hit(answer['hits'][0])
        count_before_reopen = len(server.requests)
        with open_host(directory, config) as (app, lexical, semantic):
            require(len(server.requests) == count_before_reopen and semantic.status()['state'] == 'ready',
                    'reopening rebuilt vectors or contacted the provider')
            again, trace['query_after_reopen'] = perform(app, 'knowledge.semantic_search',
                {'query': QUERY, 'expected_corpus_sha256': corpus, 'limit': 1}, server)
            verify_hit(again['tool_result']['value']['hits'][0])
            require(len(server.requests) == count_before_reopen + 1, 'reopening sent more than one query embedding request')
            (directory/'materials/alpha.md').write_bytes((ALPHA+'Source changed.\n').encode('utf-8'))
            before_stale = len(server.requests)
            stale, trace['stale_query'] = perform(app, 'knowledge.semantic_search',
                {'query': QUERY, 'expected_corpus_sha256': corpus, 'limit': 1}, server)
            require(stale['status'] == 'completed' and stale['tool_result']['value']['ok'] is False,
                    'stale source was treated as a valid semantic result')
            require(len(server.requests) == before_stale, 'stale corpus sent a billable query')
        sent_inputs = [request['input'] for request in server.requests]
        require(sent_inputs == [[TEST_TEXT], [ALPHA, BETA], [QUERY], [QUERY]],
                'actual request inputs differed, repeated a rebuild, or included other data')
        summary = {'passed': True, 'provider_kind': 'local_mock_with_hand_assigned_vectors', 'real_provider_requests': 0,
                   'task_model_calls': 0, 'mock_http_requests': len(server.requests), 'dimensions': 3,
                   'nearest_path': 'alpha.md', 'cosine_score': answer['hits'][0]['score'],
                   'denied_request_count': 0, 'rebuild_request_count': 1, 'reopen_only_query': True,
                   'stale_query_request_count': 0, 'stale_error': stale['tool_result']['value']['error_code'],
                   'retained': retained, 'boundary': 'Protocol, numeric ranking, persistence and source checks; not real-model semantic quality.'}
    require(server.closed, 'owned mock server did not close')
    for name, value in (('summary.json', summary), ('trace.json', trace), ('answer.json', answer), ('sent-inputs.json', sent_inputs)):
        (directory/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return summary


def run_demo(output_dir=None):
    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix='astra-embedding-') as temporary:
            return execute(Path(temporary)/'run', retained=False)
    return execute(Path(output_dir).resolve(), retained=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, help='Create new synthetic output; never overwrite existing files.')
    args = parser.parse_args(argv)
    try:
        summary = run_demo(args.output_dir)
    except FileExistsError:
        parser.exit(2, 'Output exists; choose a new --output-dir. Existing data was not changed.\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
