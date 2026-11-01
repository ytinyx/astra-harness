import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {MCPProfilesSession, blankMCPProfile, mcpDraft, mcpPayload, mcpProfileError} from '../../astra_harness/static/mcp-profiles-state.mjs';
import {createMCPProfilesView} from '../../astra_harness/static/mcp-profiles.mjs';

const root = 'a'.repeat(32), otherRoot = 'b'.repeat(32);
const service = {id: '1'.repeat(32), name: 'synthetic', transport: 'stdio', command: 'C:\\trusted\\fixture.exe', args: ['--stdio'], cwd: '.', env_refs: {TOKEN: 'ASTRA_MCP_TEST_TOKEN'}, timeout: 10, service_revision: '1', config_revision: 1, identity: '1'.repeat(64), enabled: false, state: 'disabled', tools: [], activation_required: false};
const catalog = (items = [service], revision = 1, extra = {}) => ({items, revision, operation: null, cleanup_required: false, bound_workspace: root, current_workspace: root, actions_allowed: true, ...extra});
const tested = (extra = {}) => ({ok: true, id: service.id, identity: service.identity, revision: 1, registered: false, tools: ['mcp.synthetic.add'], tool_count: 1, ...extra});
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {resolve, reject, promise};};
const make = options => new MCPProfilesSession({scope: () => root, ...options});

test('blank MCP configuration has no program, endpoint or secret; load never connects', async () => {
  const calls = [], session = make({api: async (...args) => {calls.push(args); return catalog([]);}});
  await session.load(); session.begin();
  assert.deepEqual(session.state.draft, blankMCPProfile());
  assert.equal(session.state.draft.command, ''); assert.equal(session.state.draft.endpoint, ''); assert.equal(session.state.draft.env_refs_text, '');
  assert.deepEqual(calls.map(call => call[0]), ['/api/mcp-profiles']);
});

test('transport-specific payload omits all readonly metadata and accepts environment references only', () => {
  const draft = mcpDraft(service), payload = mcpPayload({...draft, enabled: true, identity: 'forged'});
  assert.deepEqual(Object.keys(payload).sort(), ['args', 'command', 'cwd', 'env_refs', 'id', 'name', 'service_revision', 'timeout', 'transport']);
  assert.deepEqual(payload.env_refs, {TOKEN: 'ASTRA_MCP_TEST_TOKEN'});
  const http = mcpPayload({...draft, transport: 'http', endpoint: 'http://127.0.0.1:7000/mcp', args_text: 'not parsed'});
  assert.deepEqual(Object.keys(http).sort(), ['endpoint', 'id', 'name', 'service_revision', 'timeout', 'transport']);
  for (const refs of ['TOKEN=literal-private-value', 'TOKEN=ASTRA_MCP_A\ntoken=ASTRA_MCP_B', '__proto__=ASTRA_MCP_A']) assert.throws(() => mcpPayload({...draft, env_refs_text: refs}));
  for (const args of ['{}', '[5]', 'invalid']) assert.throws(() => mcpPayload({...draft, args_text: args}));
});

test('save canonicalizes server ID but does not enable or test, and duplicate click submits once', async () => {
  const pending = deferred(), calls = [];
  const session = make({api: async (path, method, body) => {calls.push({path, body}); return path.endsWith('/save') ? pending.promise : catalog([]);}});
  await session.load(); session.update(mcpDraft(service));
  assert.equal(session.state.draft.id, undefined);
  const first = session.save(); await session.save();
  assert.equal(calls.length, 2); assert.equal(calls[1].body.expected_revision, 1); assert.equal(calls[1].body.profile.id, undefined);
  pending.resolve(catalog([service], 2)); await first;
  assert.equal(session.state.draft.id, service.id); assert.equal(session.state.dirty, false); assert.equal(session.saved().enabled, false);
});

test('invalid argument text stays in unsaved draft and cannot dispatch save', async () => {
  const calls = [], session = make({api: async path => {calls.push(path); return catalog();}});
  await session.load(); session.begin(service.id); session.update({args_text: 'broken'}); await session.save();
  assert.equal(calls.length, 1); assert.match(session.state.error, /JSON/); assert.equal(session.state.draft.args_text, 'broken');
  session.begin(); assert.equal(session.state.draft.id, service.id);
  session.discard(); assert.equal(session.state.dirty, false);
});

test('CAS conflict preserves draft and never silently upgrades its expected revision', async () => {
  let revision = 1, saves = 0;
  const session = make({api: async path => {if (path.endsWith('/save')) {saves++; throw Object.assign(new Error('private server text'), {status: 409, code: 'mcp_profile_conflict'});} return catalog([service], revision);}});
  await session.load(); session.begin(service.id); session.update({name: 'local-change'}); await session.save();
  revision = 2; await session.load(); await session.save();
  assert.equal(saves, 1); assert.equal(session.state.draft.name, 'local-change'); assert.equal(session.state.draftRevision, 1);
  session.discard(); assert.equal(session.state.draftRevision, 2); assert.equal(session.state.draft.name, service.name);
});

test('test and activation require clean saved configuration and fresh explicit confirmation', async () => {
  const calls = [], session = make({api: async (path, method, body) => {calls.push({path, body}); return path.endsWith('/test') ? tested() : catalog();}});
  await session.load(); await session.operate('test', true); session.begin(service.id); await session.operate('test', false);
  assert.equal(calls.length, 1); await session.operate('test', true);
  assert.deepEqual(calls.at(-1).body, {id: service.id, expected_identity: service.identity, expected_revision: 1, confirmed: true});
  assert.equal(session.state.testResult.passed, true); assert.equal(session.saved().enabled, false); assert.deepEqual(session.saved().tools, []);
  session.update({name: 'dirty'}); await session.operate('enable', true); assert.equal(calls.length, 2);
});

test('test success needs matching id/revision/identity and proves only discovery, not registration', async () => {
  for (const result of [tested({ok: false}), tested({registered: true}), tested({id: 'wrong'}), tested({identity: 'changed'}), tested({revision: 2}), {}]) {
    const session = make({api: async path => path.endsWith('/test') ? result : catalog()});
    await session.load(); session.begin(service.id); await session.operate('test', true);
    assert.equal(session.state.testResult.passed, false);
  }
});

test('uncertain activation is not replayed and only an explicit catalog read reconciles it', async () => {
  const pending = deferred(), calls = [];
  const session = make({api: async (path, method, body) => {calls.push({path, body}); return path.endsWith('/enable') ? pending.promise : catalog();}});
  await session.load(); session.begin(service.id); const activation = session.operate('enable', true); await session.operate('enable', true);
  pending.reject(new TypeError('network lost')); await activation; assert.equal(session.state.uncertain, true);
  await session.operate('enable', true); await session.operate('delete'); session.update({name: 'bad'});
  assert.equal(calls.length, 2); assert.equal(session.state.dirty, false);
  await session.load(); assert.equal(session.state.uncertain, false); assert.equal(calls.length, 3);
});

test('enabled offline intent cannot be edited or deleted but supports explicit activation and disabling', async () => {
  const offline = {...service, enabled: true, state: 'offline', activation_required: true}, calls = [];
  const session = make({api: async (path, method, body) => {calls.push({path, body}); return catalog([offline], path.endsWith('/enable') ? 2 : 1);}});
  await session.load(); session.begin(service.id); session.update({name: 'rejected'}); await session.save(); await session.operate('delete');
  assert.equal(calls.length, 1); assert.equal(session.state.draft.name, service.name);
  await session.operate('enable', true); assert.equal(calls.length, 2);
  await session.operate('disable'); assert.deepEqual(calls.at(-1).body, {id: service.id, expected_revision: 2, expected_identity: service.identity});
});

test('a changed workspace or backend restriction blocks all writes even with a previously loaded catalog', async () => {
  let current = root, restricted = false, calls = 0;
  const session = make({scope: () => current, api: async () => {calls++; return catalog([service], 1, {actions_allowed: !restricted});}});
  await session.load(); session.begin(service.id); current = otherRoot;
  session.update({name: 'wrong-root'}); await session.operate('test', true); await session.operate('delete'); assert.equal(calls, 1);
  current = root; restricted = true; await session.load(); await session.operate('enable', true); assert.equal(calls, 2);
});

test('a failed owned connection must be disabled before another test or activation', async () => {
  const calls = [], session = make({api: async path => {calls.push(path); return catalog([{...service, enabled: true, state: 'failed', activation_required: false}]);}});
  await session.load(); session.begin(service.id); await session.operate('test', true); await session.operate('enable', true);
  assert.equal(calls.length, 1); await session.operate('disable'); assert.equal(calls.at(-1), '/api/mcp-profiles/disable');
});

test('pending task, another MCP operation or unconfirmed cleanup forbids mutations', async () => {
  for (const extra of [{operation: {id: service.id, action: 'enable'}}, {cleanup_required: true}, {}]) {
    let calls = 0; const session = make({blocked: () => !Object.keys(extra).length, api: async () => {calls++; return catalog([service], 1, extra);}});
    await session.load(); session.begin(service.id); session.update({name: 'blocked'}); await session.operate('test', true); await session.operate('enable', true); await session.operate('delete');
    assert.equal(calls, 1);
  }
});

test('fixed failures never render exception details and timeout does not imply the program stopped', async () => {
  const session = make({api: async path => {if (path.endsWith('/test')) throw Object.assign(new Error('PRIVATE VALUE'), {status: 504, code: 'mcp_profile_timeout'}); return catalog();}});
  await session.load(); session.begin(service.id); await session.operate('test', true);
  assert.match(session.state.error, /超时/); assert.doesNotMatch(session.state.error, /PRIVATE/); assert.equal(session.state.uncertain, true);
  assert.match(mcpProfileError('mcp_profile_cleanup_failed'), /不能据此/);
  assert.match(mcpProfileError('runtime_update_busy'), /待审批/); assert.equal(mcpProfileError('__proto__'), null); assert.equal(mcpProfileError('private-secret'), null);
  assert.equal(mcpProfileError('workspace_not_configured', '/api/mcp-profiles/enable'), mcpProfileError('mcp_profile_workspace_mismatch'));
  assert.equal(mcpProfileError('workspace_not_configured', '/api/knowledge/default/operations'), null);
  const changedRoot = make({api: async path => {if (path.endsWith('/enable')) throw Object.assign(new Error('private detail'), {status: 409, code: 'workspace_not_configured'}); return catalog();}});
  await changedRoot.load(); changedRoot.begin(service.id); await changedRoot.operate('enable', true);
  assert.match(changedRoot.state.error, /切回启动工作区/); assert.doesNotMatch(changedRoot.state.error, /private/);
  assert.equal(changedRoot.state.uncertain, false);
});

test('actual MCP view binds consent, retains drafts and reveals each new outcome once inside the open dialog', async () => {
  const scrolls = [];
  class Element extends EventTarget {
    constructor(tag = 'div') {super(); this.tagName = tag; this.children = []; this.value = ''; this.checked = false; this.hidden = false; this.disabled = false; this._text = '';}
    set textContent(value) {this._text = String(value); this.children = [];}
    get textContent() {return this._text + this.children.map(child => child.textContent).join('');}
    append(...children) {this.children.push(...children);}
    replaceChildren(...children) {this._text = ''; this.children = children;}
    focus() {} close() {this.open = false;} showModal() {this.open = true;}
    scrollIntoView(options) {scrolls.push({element: this, options});}
  }
  const html = await readFile(new URL('../../astra_harness/static/index.html', import.meta.url), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, new Element()]));
  const original = globalThis.document;
  globalThis.document = {getElementById: id => {assert.ok(elements.has(id), id); return elements.get(id);}, createElement: tag => new Element(tag)};
  const other = {...service, id: '2'.repeat(32), identity: '2'.repeat(64), name: '<script>untrusted</script>'};
  let current = root, data = catalog([service, other]), failure = null; const calls = [];
  const el = id => elements.get(id), fire = (element, type = 'click') => element.dispatchEvent(new Event(type));
  const find = (element, text) => element.tagName === 'button' && element.textContent === text ? element : element.children.map(child => find(child, text)).find(Boolean);
  try {
    const view = createMCPProfilesView({api: async (path, method, body) => {
      calls.push(path); if (failure && path.endsWith('/enable')) throw failure;
      if (path.endsWith('/test')) return tested({id: body.id, identity: body.expected_identity, revision: body.expected_revision});
      return data;
    }, scope: () => current}); await view.load();
    fire(find(el('mcp-list').children[0], '编辑配置'));
    el('mcp-confirmed').checked = true; fire(el('mcp-confirmed'), 'change'); assert.equal(el('mcp-test').disabled, false);
    view.refresh(); assert.equal(el('mcp-confirmed').checked, true);
    fire(find(el('mcp-list').children[1], '编辑配置')); assert.equal(el('mcp-confirmed').checked, false);
    assert.ok(el('mcp-list').textContent.includes('<script>untrusted</script>'));
    el('mcp-confirmed').checked = true; current = otherRoot; view.refresh();
    assert.equal(el('mcp-confirmed').checked, false); assert.equal(el('mcp-enable').disabled, true); assert.match(el('mcp-scope').textContent, /切回/);
    current = root; view.refresh(); el('mcp-confirmed').checked = true;
    data = catalog([service, {...other, identity: '3'.repeat(64), config_revision: 2}], 2); await view.load(); assert.equal(el('mcp-confirmed').checked, false);
    el('mcp-name').value = 'unsaved'; fire(el('mcp-name'), 'input'); view.close();
    assert.equal(view.state.dirty, true); assert.equal(view.state.draft.name, 'unsaved'); assert.equal(calls.every(path => path === '/api/mcp-profiles'), true);
    fire(el('mcp-discard')); assert.equal(scrolls.length, 0, 'closed dialog never scrolls the page');
    el('mcp-profiles-dialog').showModal();
    failure = Object.assign(new Error('private error'), {status: 409, code: 'runtime_update_busy'});
    el('mcp-confirmed').checked = true; fire(el('mcp-enable')); await new Promise(resolve => setImmediate(resolve));
    assert.equal(scrolls.at(-1).element, el('mcp-error')); assert.deepEqual(scrolls.at(-1).options, {block: 'nearest', inline: 'nearest'});
    let count = scrolls.length; view.refresh(); view.refresh(); assert.equal(scrolls.length, count, 'ordinary redraw does not interrupt reading');
    failure = null; el('mcp-confirmed').checked = true; fire(el('mcp-enable')); await new Promise(resolve => setImmediate(resolve));
    assert.equal(scrolls.at(-1).element, el('mcp-notice'));
    count = scrolls.length; view.refresh(); assert.equal(scrolls.length, count);
    el('mcp-confirmed').checked = true; fire(el('mcp-test')); await new Promise(resolve => setImmediate(resolve));
    assert.equal(scrolls.at(-1).element, el('mcp-test-result'));
    count = scrolls.length; view.refresh(); assert.equal(scrolls.length, count);
    el('mcp-name').value = 'another-draft'; fire(el('mcp-name'), 'input'); view.refresh();
    assert.equal(scrolls.length, count, 'typing does not create feedback scrolling');
    data = catalog([{...service, enabled: true, state: 'ready', error_code: 'mcp_profile_busy'}, {...other, state: 'failed', error_code: 'mcp_profile_connection_failed'}], 3);
    await view.load();
    assert.match(el('mcp-list').children[0].textContent, /已连接/);
    assert.doesNotMatch(el('mcp-list').children[0].textContent, /有服务操作正在进行/);
    assert.match(el('mcp-list').children[1].textContent, /连接或工具发现失败/);
  } finally {if (original === undefined) delete globalThis.document; else globalThis.document = original;}
});
