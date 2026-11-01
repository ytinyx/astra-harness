import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {KnowledgeSession, EmbeddingConfigSession, operationOutcome, belongsToCollection, canImport,
  actionRecoveryIdentity, embeddingReadiness, embeddingUsage, embeddingActions} from '../../astra_harness/static/knowledge-state.mjs';
import {createKnowledgeView} from '../../astra_harness/static/knowledge.mjs';

const collection = id => ({id, label: id, recovery_identity: (id === 'product' ? 'a' : 'b').repeat(64),
  tools: Object.fromEntries(['import_file', 'search', 'remove'].map(action => [action, `knowledge.${id}.${action}`]))});
const a = collection('product'), b = collection('notes');
const sha = 'a'.repeat(64), indexed = 'b'.repeat(64);
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject}; };
function operation(library, request, status = 'waiting_approval') {
  const name = library.tools[request.action];
  return {id: `run-${library.id}`, session_id: `session-${library.id}`, kind: 'tool', status,
    operation: {name, arguments: structuredClone(request.arguments)}, model_calls: 0, tool_calls: 0,
    tool_snapshot: [{name, recovery_identity: actionRecoveryIdentity(library, request.action)}],
    tool_result: status === 'completed' ? {call_id: 'call', name, error_code: null, value: {ok: true, hits: []}} : null};
}
function client(request, options = {}) {
  return new KnowledgeSession({request, makeId: () => 'request-1', later: () => 1, cancelLater: () => {}, ...options});
}

async function withKnowledgeView(api, check) {
  class Element extends EventTarget {
    constructor(tag = 'div') {super(); this.tagName = tag; this.children = []; this.value = ''; this.checked = false; this.hidden = false; this._text = '';}
    set textContent(value) {this._text = String(value); this.children = [];}
    get textContent() {return this._text + this.children.map(child => child.textContent).join('');}
    append(...children) {this.children.push(...children);}
    replaceChildren(...children) {this._text = ''; this.children = children;}
    get firstChild() {return this.children[0];}
    get options() {return this.children;}
    querySelectorAll(tag) {return this.children.flatMap(child => [...(child.tagName === tag ? [child] : []), ...child.querySelectorAll(tag)]);}
    querySelector(tag) {return this.querySelectorAll(tag)[0] || null;}
    reset() {}
  }
  const html = await readFile(new URL('../../astra_harness/static/index.html', import.meta.url), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, new Element()]));
  const originals = new Map();
  for (const [key, value] of Object.entries({
    document: {getElementById: id => elements.get(id) || null, createElement: tag => new Element(tag)},
    window: new EventTarget(), localStorage: {getItem: () => null, setItem: () => {}},
  })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, {configurable: true, writable: true, value});
  }
  let view;
  try {
    view = createKnowledgeView({api, onOpen: () => {}, onClose: () => {}, onMenu: () => {}, onOpenRun: () => {}});
    await check(view, id => elements.get(id));
  } finally {
    view?.close();
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  }
}

test('closing a pending catalog read cannot leave loading stuck when reopening fails', async () => {
  const pendingCatalog = deferred(), catalogStarted = deferred(), calls = [];
  const emptyCatalog = {ok: true, available_files: [], documents: []};
  let openings = 0, catalogs = 0, oldSignal;
  await withKnowledgeView(async (path, method = 'GET', body, signal) => {
    calls.push({path, method});
    if (path === '/api/knowledge') {
      if (++openings === 2) throw new Error('Synthetic catalog unavailable');
      return {enabled: true, collections: [{...a, source_scope: 'synthetic-materials'}]};
    }
    if (path === '/api/knowledge/product') {
      if (++catalogs > 1) return emptyCatalog;
      oldSignal = signal; catalogStarted.resolve(); return pendingCatalog.promise;
    }
    if (path.endsWith('/embedding-config')) return {editable: false, active: {enabled: false}};
    if (path.endsWith('/embedding-status')) return {ok: true, state: 'not_configured'};
    throw new Error('Unexpected read: ' + path);
  }, async (view, el) => {
    const opening = view.open(); await catalogStarted.promise;
    assert.equal(el('knowledge-loading').hidden, false);
    view.close(); assert.equal(oldSignal.aborted, true);
    await view.open();
    // Even an old transport that ignores abort must finish without changing
    // the current error or keeping its previous busy state alive.
    pendingCatalog.resolve(emptyCatalog); await opening;
    assert.match(el('knowledge-error').textContent, /Synthetic catalog unavailable/);
    assert.equal(el('knowledge-error').hidden, false);
    assert.equal(el('knowledge-loading').hidden, true, 'failed reopening must stop its loading indicator');
    await view.open();
    assert.equal(el('knowledge-error').hidden, true);
    assert.equal(el('knowledge-loading').hidden, true);
    assert.equal(el('knowledge-refresh').disabled, false);
    assert.ok(calls.every(call => call.method === 'GET'), 'closing and reopening never cancel or replay server operations');
  });
});

test('a closed opening cannot hide the loading indicator of a newer opening', async () => {
  const oldRead = deferred(), newRead = deferred(), calls = [];
  await withKnowledgeView(async (path, method = 'GET') => {
    calls.push({path, method});
    assert.equal(path, '/api/knowledge');
    return calls.length === 1 ? oldRead.promise : newRead.promise;
  }, async (view, el) => {
    const oldOpening = view.open(); view.close(); const newOpening = view.open();
    oldRead.resolve({enabled: false, collections: []}); await oldOpening;
    assert.equal(el('knowledge-loading').hidden, false, 'only the current opening owns its loading indicator');
    newRead.reject(new Error('Synthetic current read failed')); await newOpening;
    assert.equal(el('knowledge-loading').hidden, true);
    assert.match(el('knowledge-error').textContent, /Synthetic current read failed/);
    assert.ok(calls.every(call => call.method === 'GET'));
  });
});

test('completion requires the expected tool and a successful domain result', () => {
  const req = {action: 'import_file', arguments: {path: 'guide.md'}};
  const run = operation(a, req, 'completed');
  assert.equal(operationOutcome(run).kind, 'success');
  assert.equal(operationOutcome({...run, tool_result: null}).kind, 'failed');
  assert.equal(operationOutcome({...run, tool_result: {...run.tool_result, name: b.tools.import_file}}).kind, 'failed');
  assert.equal(operationOutcome({...run, tool_result: {...run.tool_result, value: {ok: false, error_code: 'source_changed'}}}).code, 'source_changed');
  assert.equal(operationOutcome({...run, tool_result: {...run.tool_result, error_code: 'permission_denied'}}).kind, 'failed');
  assert.equal(operationOutcome({...run, status: 'unknown'}).kind, 'unknown');
});

test('restoration binds collection, tool identity and canonical arguments', () => {
  const req = {action: 'search', arguments: {query: 'Retry delay', limit: 5}};
  const run = operation(a, req);
  run.operation.arguments = {limit: 5, query: 'Retry delay'};
  assert.equal(belongsToCollection(run, a, req), true);
  assert.equal(belongsToCollection(run, b, req), false);
  assert.equal(belongsToCollection({...run, kind: 'agent'}, a, req), false);
  assert.equal(belongsToCollection(run, a, {...req, arguments: {...req.arguments, query: 'different'}}), false);
  assert.equal(belongsToCollection(run, {...a, recovery_identity: 'c'.repeat(64)}, req), false);
  assert.equal(belongsToCollection({...run, tool_snapshot: []}, a, req), false);
});

test('import requires an inspect result with both version guards', () => {
  assert.equal(canImport({ok: true, status: 'unindexed', source_sha256: sha, indexed_sha256: ''}), true);
  assert.equal(canImport({ok: true, status: 'stale', source_sha256: sha, indexed_sha256: indexed}), true);
  for (const status of ['missing', 'unreadable']) assert.equal(canImport({ok: true, status, source_sha256: sha, indexed_sha256: indexed}), false);
  assert.equal(canImport({ok: true, status: 'current', source_sha256: sha}), false);
  assert.equal(canImport({ok: false, status: 'current', source_sha256: sha, indexed_sha256: indexed}), false);
});

test('double submission sends once and freezes the selected source/index hashes', async () => {
  const pending = deferred(), calls = [];
  const session = client(async (...args) => { calls.push(args); return pending.promise; });
  await session.select(a);
  const args = {path: 'guide.md', expected_source_sha256: sha, expected_index_sha256: indexed};
  const first = session.start('import_file', args);
  args.expected_source_sha256 = 'c'.repeat(64);
  assert.equal(await session.start('import_file', args), false);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2].arguments.expected_source_sha256, sha);
  assert.equal(calls[0][2].arguments.expected_index_sha256, indexed);
  pending.resolve(operation(a, calls[0][2])); await first;
  assert.equal(session.state.run.status, 'waiting_approval');
  assert.equal(calls.length, 1, 'approval must be a separate explicit action');
});

test('uncertain POST survives reload and explicitly retries the original request ID', async () => {
  let storage = null, idCalls = 0; const calls = [];
  const options = {read: () => storage, write: value => {storage = value;}, makeId: () => `request-${++idCalls}`};
  const first = client(async (...args) => { calls.push(args); throw new TypeError('connection lost'); }, options);
  await first.select(a); await first.start('remove', {path: 'guide.md', expected_index_sha256: indexed});
  assert.equal(first.locked(), true); first.suspend();
  const reopened = client(async (...args) => { calls.push(args); return operation(a, args[2]); }, options);
  await reopened.select(a); assert.equal(calls.length, 1, 'opening must not silently retry a write');
  await reopened.retry();
  assert.equal(calls.length, 2); assert.deepEqual(calls[0][2], calls[1][2]); assert.equal(idCalls, 1);
});

test('late POST completion is retained for its collection without changing the new view', async () => {
  const pending = deferred(), calls = [];
  const session = client(async (...args) => { calls.push(args); return pending.promise; });
  await session.select(a); const first = session.start('search', {query: 'Gold', limit: 5});
  const original = calls[0][2]; await session.select(b);
  pending.resolve(operation(a, original, 'completed')); await first;
  assert.equal(session.state.collection.id, 'notes'); assert.equal(session.state.run, null);
  assert.equal(session.records.product.run_id, 'run-product'); assert.equal(session.records.notes, undefined);
});

test('late GET from an old collection cannot replace the current run', async () => {
  const pending = deferred(), req = {action: 'search', arguments: {query: 'Gold'}, request_id: 'request-1'};
  const storage = JSON.stringify({product: {run_id: 'run-product', request: req}});
  const session = client(async () => pending.promise, {read: () => storage});
  const opening = session.select(a); await session.select(b);
  pending.resolve(operation(a, req, 'completed')); await opening;
  assert.equal(session.state.collection.id, 'notes'); assert.equal(session.state.run, null);
});

test('restored approval performs no POST and mismatched updates disable approval', async () => {
  const req = {action: 'remove', arguments: {path: 'guide.md', expected_index_sha256: indexed}, request_id: 'request-1'};
  let response = operation(a, req); const methods = [];
  const session = client(async (path, method) => {methods.push(method); return response;},
    {read: () => JSON.stringify({product: {run_id: 'run-product', request: req}})});
  await session.select(a); assert.equal(session.state.run.status, 'waiting_approval');
  assert.deepEqual(methods, ['GET']);
  response = operation(b, req); await session.refresh();
  assert.equal(session.state.run, null); assert.match(session.state.error, /不匹配/);
  await session.control('approve', {approved: true}); assert.deepEqual(methods, ['GET', 'GET']);
});

test('double approval is a single POST; closing does not issue cancellation', async () => {
  const req = {action: 'import_file', arguments: {path: 'guide.md'}, request_id: 'request-1'};
  const pending = deferred(), calls = [];
  const session = client(async (path, method, body) => { calls.push({path, method, body}); return method === 'GET' ? operation(a, req) : pending.promise; },
    {read: () => JSON.stringify({product: {run_id: 'run-product', request: req}})});
  await session.select(a);
  const first = session.control('approve', {approved: true});
  await session.control('approve', {approved: true}); session.suspend();
  pending.resolve(operation(a, req, 'completed')); await first;
  assert.equal(calls.filter(item => item.method === 'POST').length, 1);
  assert.equal(calls.some(item => item.path.endsWith('/cancel')), false);
});

test('unknown run is never resubmitted or cancelled from the knowledge view', async () => {
  const req = {action: 'remove', arguments: {path: 'guide.md'}, request_id: 'request-1'}; const calls = [];
  const session = client(async (...args) => {calls.push(args); return operation(a, req, 'unknown');},
    {read: () => JSON.stringify({product: {run_id: 'run-product', request: req}})});
  await session.select(a); assert.equal(session.locked(), true);
  assert.equal(await session.start('remove', req.arguments), false);
  await session.control('cancel'); await session.retry();
  assert.equal(calls.length, 1);
});

test('a definite validation rejection releases the draft, unlike an unknown network result', async () => {
  const error = Object.assign(new Error('invalid request'), {status: 400});
  const session = client(async () => {throw error;});
  await session.select(a); await session.start('search', {query: 'Gold'});
  assert.equal(session.locked(), false); assert.equal(session.state.pending, null);
  assert.equal(session.records.product, undefined); assert.equal(session.state.error, 'invalid request');
});

test('a late snapshot from before cancellation cannot replace a new operation in the same collection', async () => {
  const old = {action: 'remove', arguments: {path: 'old.md'}, request_id: 'old-request'};
  const pending = deferred(); let reads = 0;
  const session = client(async (path, method, body) => {
    if (method === 'GET') return ++reads === 1 ? operation(a, old) : pending.promise;
    if (path.endsWith('/cancel')) return operation(a, old, 'cancelled');
    return {...operation(a, body), id: 'new-run'};
  }, {read: () => JSON.stringify({product: {run_id: 'run-product', request: old}}), makeId: () => 'new-request'});
  await session.select(a); const reading = session.refresh();
  await session.control('cancel');
  assert.equal(session.state.run.status, 'cancelled');
  await session.start('import_file', {path: 'new.md', expected_source_sha256: sha, expected_index_sha256: ''});
  pending.resolve(operation(a, old)); await reading;
  assert.equal(session.state.run.id, 'new-run');
  assert.equal(session.records.product.request.request_id, 'new-request');
  assert.equal(session.state.pending.arguments.path, 'new.md');
});

test('a late pre-approval GET cannot move the same operation back to waiting approval', async () => {
  const req = {action: 'remove', arguments: {path: 'guide.md'}, request_id: 'request-1'};
  const pending = deferred(); let reads = 0;
  const session = client(async (path, method) => method === 'GET'
    ? (++reads === 1 ? operation(a, req) : pending.promise) : operation(a, req, 'completed'),
  {read: () => JSON.stringify({product: {run_id: 'run-product', request: req}})});
  await session.select(a); const reading = session.refresh();
  await session.control('approve', {approved: true});
  pending.resolve(operation(a, req)); await reading;
  assert.equal(session.state.run.status, 'completed');
  assert.equal(operationOutcome(session.state.run).kind, 'success');
});

test('browser timer methods retain their global receiver rather than the client instance', async () => {
  const originalSet = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
  let scheduled = 0, cleared = 0;
  try {
    globalThis.setTimeout = function () { assert.equal(this, globalThis); scheduled++; return 9; };
    globalThis.clearTimeout = function () { assert.equal(this, globalThis); cleared++; };
    const session = new KnowledgeSession({request: async (path, method, body) => operation(a, body), makeId: () => 'request-1'});
    await session.select(a); await session.start('search', {query: 'Gold'}); session.suspend();
    assert.equal(scheduled, 1); assert.ok(cleared >= 2);
  } finally { globalThis.setTimeout = originalSet; globalThis.clearTimeout = originalClear; }
});

test('unchanged approval polling does not repeatedly rebuild focused controls', async () => {
  const req = {action: 'remove', arguments: {path: 'guide.md'}, request_id: 'request-1'};
  let rendered = 0;
  const session = client(async () => operation(a, req), {changed: () => {rendered++;},
    read: () => JSON.stringify({product: {run_id: 'run-product', request: req}})});
  await session.select(a); const initial = rendered;
  await session.refresh(); await session.refresh();
  assert.equal(rendered, initial);
});

const semanticCollection = {...a,
  tools: {...a.tools, embedding_test: 'knowledge.product.embedding_test', embedding_rebuild: 'knowledge.product.embedding_rebuild', semantic_search: 'knowledge.product.semantic_search'},
  tool_recovery_identities: {embedding_test: 'c'.repeat(64), embedding_rebuild: 'c'.repeat(64), semantic_search: 'c'.repeat(64)}};
const profile = {enabled: true, endpoint: 'http://127.0.0.1:12345/v1/embeddings', model: 'mock',
  key_env: 'ASTRA_EMBEDDING_API_KEY', dimensions: null, revision: '1', timeout: 10};
const config = {editable: true, saved: profile, active: profile, active_key_present: true, saved_key_present: true,
  config_revision: sha, restart_required: false};
const semanticStatus = {ok: true, state: 'ready', corpus_sha256: indexed, chunk_count: 2, max_chunks: 128, index_compatible: true};

test('semantic runs use action-specific profile identities and never fall back to a lexical identity', () => {
  const req = {action: 'semantic_search', arguments: {query: 'meaning', expected_corpus_sha256: indexed}};
  const run = operation(semanticCollection, req, 'completed');
  assert.equal(belongsToCollection(run, semanticCollection, req), true);
  assert.equal(belongsToCollection(run, {...semanticCollection, tool_recovery_identities: {}}, req), false);
  assert.equal(belongsToCollection(run, {...semanticCollection, tool_recovery_identities: {semantic_search: 'd'.repeat(64)}}, req), false);
  assert.equal(actionRecoveryIdentity(a, 'search'), a.recovery_identity);
  assert.equal(actionRecoveryIdentity(a, 'semantic_search'), undefined);
});

test('saved-but-not-loaded settings block every provider action while lexical operations remain independent', () => {
  for (const action of embeddingActions) assert.equal(embeddingReadiness({...config, restart_required: true}, semanticStatus, action).allowed, false);
  assert.equal(embeddingReadiness(config, {...semanticStatus, state: 'not_built', index_compatible: false}, 'semantic_search').allowed, false);
  assert.equal(embeddingReadiness(config, {...semanticStatus, state: 'not_built'}, 'embedding_test').allowed, true);
  assert.equal(embeddingReadiness({...config, active_key_present: false}, semanticStatus, 'embedding_test').allowed, false);
  assert.equal(embeddingReadiness(config, semanticStatus, 'semantic_search').allowed, true);
});

test('empty-corpus rebuild can clean local vectors without a key but cannot authorize a query', () => {
  const empty = {...semanticStatus, state: 'missing_key', chunk_count: 0};
  const missing = {...config, active_key_present: false};
  assert.equal(embeddingReadiness(missing, empty, 'embedding_rebuild').allowed, true);
  assert.match(embeddingReadiness(missing, empty, 'embedding_rebuild').reason, /不发送/);
  assert.equal(embeddingReadiness(missing, empty, 'semantic_search').allowed, false);
  assert.equal(embeddingReadiness(missing, {...empty, corpus_sha256: ''}, 'embedding_rebuild').allowed, false);
  assert.equal(embeddingReadiness(config, {...semanticStatus, chunk_count: 129}, 'embedding_rebuild').allowed, false);
});

test('embedding usage distinguishes an explicit zero from missing or invalid counts', () => {
  assert.deepEqual(embeddingUsage({}), {requests: null, inputTokens: null, totalTokens: null});
  assert.deepEqual(embeddingUsage({request_count: 0}), {requests: 0, inputTokens: null, totalTokens: null});
  assert.deepEqual(embeddingUsage({request_count: 2, usage: {prompt_tokens: 7, total_tokens: 7}}), {requests: 2, inputTokens: 7, totalTokens: 7});
  assert.deepEqual(embeddingUsage({request_count: -1, usage: {input_tokens: '0'}}), {requests: null, inputTokens: null, totalTokens: null});
});

test('saving configuration is a single CAS PUT, preserves the loaded profile, and never submits a run', async () => {
  const pending = deferred(), calls = [];
  const settings = new EmbeddingConfigSession({request: async (path, method = 'GET', body) => {
    calls.push({path, method, body}); return method === 'GET' ? config : pending.promise;
  }});
  await settings.select('product');
  const next = {...profile, model: 'new-model'};
  const saving = settings.save(next); next.model = 'mutated-after-click';
  assert.equal(await settings.save(next), false); assert.equal(calls.length, 2);
  assert.equal(calls[1].method, 'PUT'); assert.equal(calls[1].body.expected_revision, sha);
  assert.equal(calls[1].body.embedding.model, 'new-model');
  assert.equal(calls.some(call => call.path.includes('/operations')), false);
  pending.resolve({...config, saved: {...profile, model: 'new-model'}, config_revision: indexed, restart_required: true});
  assert.equal(await saving, true); assert.equal(settings.state.data.active.model, 'mock');
  assert.equal(settings.state.data.restart_required, true);
});

test('a CAS conflict preserves the previously read configuration and never automatically repeats the write', async () => {
  let writes = 0;
  const settings = new EmbeddingConfigSession({request: async (path, method = 'GET') => {
    if (method === 'GET') return config;
    writes++; throw Object.assign(new Error('conflict'), {status: 409});
  }});
  await settings.select('product'); assert.equal(await settings.save({...profile, model: 'draft'}), false);
  assert.equal(writes, 1); assert.equal(settings.state.data, config);
  assert.equal(settings.state.uncertain, false); assert.match(settings.state.error, /保留.*表单/);
});

test('an uncertain settings write blocks another save until an explicit reload', async () => {
  let writes = 0, reads = 0;
  const settings = new EmbeddingConfigSession({request: async (path, method = 'GET') => {
    if (method === 'GET') {reads++; return reads === 1 ? config : {...config, config_revision: indexed};}
    writes++; throw new TypeError('network unavailable');
  }});
  await settings.select('product'); await settings.save(profile);
  assert.equal(settings.state.uncertain, true); assert.equal(await settings.save(profile), false);
  assert.equal(writes, 1); await settings.select('product');
  assert.equal(settings.state.uncertain, false); assert.equal(settings.state.data.config_revision, indexed);
});

test('late configuration reads and saves cannot replace another collection view', async () => {
  const pendingRead = deferred(), pendingWrite = deferred();
  const settings = new EmbeddingConfigSession({request: async (path, method = 'GET') => {
    if (path.includes('/notes/')) return {...config, saved: {...profile, model: 'notes'}};
    return method === 'PUT' ? pendingWrite.promise : pendingRead.promise;
  }});
  const first = settings.select('product'); await settings.select('notes');
  pendingRead.resolve(config); await first; assert.equal(settings.state.data.saved.model, 'notes');
  await settings.select('product'); const saving = settings.save({...profile, model: 'draft'});
  await settings.select('notes'); pendingWrite.resolve({...config, saved: {...profile, model: 'draft'}, restart_required: true});
  await saving; assert.equal(settings.state.collectionId, 'notes'); assert.equal(settings.state.data.saved.model, 'notes');
});

test('pending profile blocks semantic submit, approval and resume but permits explicit rejection', async () => {
  const req = {action: 'embedding_rebuild', arguments: {expected_corpus_sha256: indexed}, request_id: 'request-1'};
  let pending = true; const calls = [];
  const session = client(async (path, method = 'GET', body) => {
    calls.push({path, method, body}); return operation(semanticCollection, req, method === 'GET' ? 'waiting_approval' : 'failed');
  }, {allowed: action => !embeddingActions.has(action) || !pending,
    read: () => JSON.stringify({product: {run_id: 'run-product', request: req}})});
  await session.select(semanticCollection); await session.control('approve', {approved: true});
  assert.equal(calls.length, 1); session.state.run.status = 'paused'; await session.control('resume');
  assert.equal(calls.length, 1); session.state.run.status = 'waiting_approval'; await session.control('approve', {approved: false});
  assert.equal(calls.length, 2); assert.deepEqual(calls[1].body, {approved: false});
  assert.equal(await session.start('semantic_search', {query: 'test', expected_corpus_sha256: indexed}), false);
  pending = false; assert.equal(await session.start('semantic_search', {query: 'test', expected_corpus_sha256: indexed}), true);
});

test('restoring a semantic operation after profile replacement cannot authorize the old request', async () => {
  const req = {action: 'embedding_test', arguments: {}, request_id: 'request-1'}, calls = [];
  const changed = {...semanticCollection, tool_recovery_identities: {embedding_test: 'd'.repeat(64)}};
  const session = client(async (...args) => {calls.push(args); return operation(semanticCollection, req);},
    {read: () => JSON.stringify({product: {run_id: 'run-product', request: req}})});
  await session.select(changed); assert.equal(session.state.run, null); assert.match(session.state.error, /不匹配/);
  await session.control('approve', {approved: true}); assert.equal(calls.length, 1);
});
