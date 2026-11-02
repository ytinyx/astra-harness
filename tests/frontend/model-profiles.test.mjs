import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {ModelProfilesSession, blankModelProfile, sessionModelMismatch, modelProfileError} from '../../astra_harness/static/model-profiles-state.mjs';
import {createModelProfilesView} from '../../astra_harness/static/model-profiles.mjs';
import {runControls} from '../../astra_harness/static/state.mjs';
import {scopedSubmission} from '../../astra_harness/static/workspace-state.mjs';

const profile = {id: 'a'.repeat(32), name: 'Synthetic model', provider: 'chat_completions', base_url: 'http://127.0.0.1:7001/v1', model: 'fixture', key_env: 'ASTRA_MODEL_TEST_API_KEY', timeout: 30, max_output_tokens: 1024, revision: 1, adapter_revision: '1', identity: '1'.repeat(64)};
const catalog = (items = [profile], selected = null, revision = 1) => ({revision, items, selected_id: selected?.id || null, selected_identity: selected?.identity || 'startup'});
const result = (overrides = {}) => ({id: profile.id, identity: profile.identity, revision: 1, profile_revision: 1, ok: true, request_issued: true, requested_model: profile.model, ...overrides});
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};};

test('web keys are write-only, cleared on submit, and blank edits omit the secret', async () => {
  const saved = {...profile, key_env: null, has_api_key: true}, calls = [];
  const session = new ModelProfilesSession({api: async (path, method, body) => {
    calls.push({path, body: body && structuredClone(body)}); return catalog([saved]);
  }});
  await session.load(); session.begin();
  session.update({name: 'Direct key', base_url: profile.base_url, model: 'fixture', api_key: 'synthetic-web-key'});
  const saving = session.save();
  assert.equal(session.state.draft.api_key, '', 'clear the password before waiting for the response');
  await saving;
  assert.equal(calls.at(-1).body.profile.api_key, 'synthetic-web-key');
  assert.equal(calls.at(-1).body.profile.key_env, null);
  assert.equal(session.state.draft.api_key, '');
  session.begin(saved.id); session.update({name: 'Rename only'}); await session.save();
  assert.equal(Object.hasOwn(calls.at(-1).body.profile, 'api_key'), false);
  assert.equal(calls.some(call => call.path.endsWith('/test')), false);
});

test('failed save and closing clear the secret while retaining other draft fields', async () => {
  const session = new ModelProfilesSession({api: async path => {
    if (path.endsWith('/save')) throw Object.assign(new Error('private raw detail'), {status: 400, code: 'model_credentials_invalid'});
    return catalog([]);
  }});
  await session.load(); session.update({name: 'Keep this name', api_key: 'synthetic-web-key'});
  await session.save(); assert.equal(session.state.draft.api_key, '');
  assert.match(session.state.error, /API Key 格式/); assert.doesNotMatch(session.state.error, /private/);
  session.update({api_key: 'synthetic-again'}); session.clearSecret();
  assert.equal(session.state.draft.api_key, ''); assert.equal(session.state.draft.name, 'Keep this name');
});

test('deleting only the stored key uses a separate revision-bound endpoint and preserves the profile', async () => {
  const saved = {...profile, key_env: null, has_api_key: true}, calls = [], pending = deferred();
  const session = new ModelProfilesSession({api: async (path, method, body) => {
    calls.push({path, body}); return path.endsWith('/delete-key') ? pending.promise : catalog([saved], saved);
  }});
  await session.load(); session.begin(saved.id);
  const deleting = session.removeKey(saved.id); await session.removeKey(saved.id);
  assert.equal(calls.filter(call => call.path.endsWith('/delete-key')).length, 1);
  assert.deepEqual(calls.at(-1).body, {id: saved.id, expected_revision: 1});
  const missing = {...saved, has_api_key: false, revision: 2, identity: '2'.repeat(64)};
  pending.resolve(catalog([missing], missing, 2)); await deleting;
  assert.equal(session.state.draft.id, saved.id); assert.equal(session.state.draft.base_url, saved.base_url);
  assert.equal(session.state.catalog.items[0].has_api_key, false);
  assert.match(session.state.notice, /密钥已删除/); assert.match(session.state.notice, /配置保留/);
  assert.equal(calls.some(call => call.path.endsWith('/test') || call.path.endsWith('/delete')), false);
});

test('unsupported hosts default to the existing environment workflow', async () => {
  const session = new ModelProfilesSession({api: async () => ({...catalog([]), can_store_api_key: false})});
  await session.load(); session.begin();
  assert.equal(session.state.draft.key_env, '');
  session.update({key_env: 'ASTRA_MODEL_TEST_API_KEY', api_key: 'do-not-send'});
  assert.equal(session.state.draft.api_key, '');
});

test('new drafts do not prefill endpoints, model names or credential names and listing does not test', async () => {
  const calls = [], session = new ModelProfilesSession({api: async (...args) => {calls.push(args); return catalog();}});
  await session.load(); session.begin();
  assert.equal(session.state.draft.base_url, ''); assert.equal(session.state.draft.model, ''); assert.equal(session.state.draft.key_env, null);
  assert.equal(session.state.draft.api_key, '');
  assert.deepEqual(calls.map(call => call[0]), ['/api/model-profiles']);
  assert.equal(session.state.catalog.selected_identity, 'startup');
});

test('save strips readonly identity fields and adopts the backend created ID after canonicalization', async () => {
  const calls = [], session = new ModelProfilesSession({api: async (path, method, body) => {
    calls.push({path, method, body}); return path.endsWith('/save') ? catalog() : catalog([]);
  }});
  await session.load(); session.update({...profile, id: 'injected', identity: 'injected', adapter_revision: 'wrong'});
  await session.save();
  assert.deepEqual(Object.keys(calls.at(-1).body.profile).sort(), ['base_url', 'key_env', 'max_output_tokens', 'model', 'name', 'provider', 'timeout']);
  assert.equal(calls.at(-1).body.expected_revision, 1);
  assert.equal(session.state.draft.id, profile.id); assert.equal(session.state.dirty, false);
  assert.equal(calls.some(call => call.path.endsWith('/test')), false);
});

test('double save creates one request; unknown outcome blocks writes until an explicit read', async () => {
  const waiting = deferred(); let saves = 0;
  const session = new ModelProfilesSession({api: (path) => {
    if (path.endsWith('/save')) {saves++; return waiting.promise;} return catalog();
  }});
  await session.load(); session.begin(profile.id); session.update({name: 'Edited'});
  const first = session.save(); await session.save(); assert.equal(saves, 1);
  waiting.reject(new TypeError('offline')); await first;
  assert.equal(session.state.uncertain, true); await session.select(null); await session.save(); assert.equal(saves, 1);
  await session.load(); assert.equal(session.state.uncertain, false); assert.equal(session.state.draft.name, 'Edited');
});

test('CAS conflict keeps the draft and cannot save over a newer revision until explicit discard', async () => {
  let revision = 1, saves = 0;
  const session = new ModelProfilesSession({api: async path => {
    if (path.endsWith('/save')) {saves++; throw Object.assign(new Error('conflict'), {status: 409});}
    return catalog([profile], null, revision);
  }});
  await session.load(); session.begin(profile.id); session.update({name: 'My draft'}); await session.save();
  revision = 2; await session.load(); await session.save();
  assert.equal(saves, 1); assert.equal(session.state.draft.name, 'My draft'); assert.equal(session.state.draftRevision, 1);
  session.discard(); assert.equal(session.state.draft.name, profile.name); assert.equal(session.state.draftRevision, 2);
});

test('select and delete are revision-bound metadata operations and do not implicitly test', async () => {
  const calls = [], session = new ModelProfilesSession({api: async (path, method, body) => {
    calls.push({path, body});
    if (path.endsWith('/select')) return catalog([profile], profile, 2);
    if (path.endsWith('/delete')) return catalog([], null, 3);
    return catalog();
  }});
  await session.load(); await session.select(profile.id);
  assert.deepEqual(calls.at(-1).body, {id: profile.id, expected_revision: 1});
  await session.remove(profile.id); assert.deepEqual(calls.at(-1).body, {id: profile.id, expected_revision: 2});
  assert.equal(calls.some(call => call.path.endsWith('/test')), false);
});

test('dirty drafts cannot silently switch or delete profiles and closing does not persist them', async () => {
  let calls = 0;
  const session = new ModelProfilesSession({api: async () => {calls++; return catalog();}});
  await session.load(); session.begin(profile.id); session.update({name: 'Unsaved'});
  session.begin(); await session.select(profile.id); await session.remove(profile.id); session.cancelTest();
  assert.equal(calls, 1); assert.equal(session.state.draft.name, 'Unsaved'); assert.equal(session.state.dirty, true);
});

test('connection test requires saved clean configuration and explicit consent', async () => {
  const calls = [], session = new ModelProfilesSession({api: async (path, method, body) => {calls.push({path, body}); return path.endsWith('/test') ? result() : catalog();}});
  await session.load(); await session.test(true); session.begin(profile.id); await session.test(false);
  assert.equal(calls.length, 1); await session.test(true);
  assert.deepEqual(calls.at(-1).body, {id: profile.id, expected_revision: 1, confirmed: true});
  assert.equal(session.state.testResult.kind, 'passed');
  session.update({model: 'different'}); await session.test(true); assert.equal(calls.length, 2);
});

test('HTTP success with business failure or a mismatched configuration cannot be presented as a pass', async () => {
  for (const response of [result({ok: false, request_issued: false, error_code: 'missing_key'}), result({identity: '2'.repeat(64)}), result({profile_revision: 3}), {}]) {
    const session = new ModelProfilesSession({api: async path => path.endsWith('/test') ? response : catalog()});
    await session.load(); session.begin(profile.id); await session.test(true);
    assert.equal(session.state.testResult.kind, 'failed');
  }
});

test('double test sends once; cancellation aborts and a late successful reply cannot replace cancelled state', async () => {
  const pending = deferred(); let signal, tests = 0;
  const session = new ModelProfilesSession({api: async (path, method, body, currentSignal) => {
    if (path.endsWith('/test')) {tests++; signal = currentSignal; return pending.promise;} return catalog();
  }});
  await session.load(); session.begin(profile.id); const running = session.test(true); await session.test(true);
  session.update({model: 'changed'}); assert.equal(session.state.draft.model, profile.model);
  session.cancelTest(); assert.equal(signal.aborted, true); assert.equal(tests, 1);
  pending.resolve(result()); await running;
  assert.equal(session.state.testResult.kind, 'cancelled'); assert.equal(session.state.testing, false);
});

test('unconfirmed task submission prevents profile mutation while preserving readable metadata', async () => {
  let calls = 0;
  const session = new ModelProfilesSession({api: async () => {calls++; return catalog();}, blocked: () => true});
  await session.load(); await session.select(profile.id); await session.remove(profile.id);
  assert.equal(calls, 1); assert.equal(session.state.catalog.selected_identity, 'startup');
});

test('agent history binds its model while direct tools do not introduce an artificial model binding', () => {
  assert.equal(sessionModelMismatch(null, profile.identity), false);
  assert.equal(sessionModelMismatch({runs: [{kind: 'tool'}], messages: []}, profile.identity), false);
  assert.equal(sessionModelMismatch({runs: [{}]}, profile.identity), true);
  assert.equal(sessionModelMismatch({model_identity: profile.identity}, profile.identity), false);
  assert.equal(sessionModelMismatch({model_identity: profile.identity}, 'startup'), true);
});

test('uncertain task retries keep the originally displayed model identity even after selection changes', () => {
  const submission = scopedSubmission(null, 'task', null, 'root', () => 'original', profile.identity);
  assert.equal(submission.expected_model_identity, profile.identity);
  assert.strictEqual(scopedSubmission(submission, 'changed', null, 'root', () => 'new', 'startup'), submission);
  assert.equal(scopedSubmission(null, 'task', null, 'root', () => 'fresh').expected_model_identity, 'startup');
});

test('model mismatch blocks only new submissions, retaining the old run approval and cancellation controls', () => {
  const session = {model_identity: profile.identity};
  const policy = runControls({id: 'old-run', status: 'waiting_approval'}, {blocked: sessionModelMismatch(session, '2'.repeat(64))});
  assert.equal(policy.canSubmit, false); assert.equal(policy.canApprove, true);
  assert.equal(policy.canCancel, true); assert.equal(policy.canPause, true);
  assert.equal(runControls({id: 'old-run', status: 'paused'}, {blocked: true}).canResume, true);
});

test('actual view clears test consent when editing another profile or reloading a changed identity', async () => {
  class Element extends EventTarget {
    constructor(tag = 'div') {super(); this.tagName = tag; this.children = []; this.value = ''; this.checked = false; this._text = '';}
    set textContent(value) {this._text = String(value); this.children = [];}
    get textContent() {return this._text + this.children.map(item => item.textContent).join('');}
    append(...items) {this.children.push(...items);}
    replaceChildren(...items) {this._text = ''; this.children = [...items];}
    focus() {} close() {this.open = false;} showModal() {this.open = true;} scrollIntoView() {}
  }
  const html = await readFile(new URL('../../astra_harness/static/index.html', import.meta.url), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, new Element()]));
  const oldDocument = globalThis.document, oldWindow = globalThis.window;
  globalThis.document = {getElementById: id => {assert.ok(elements.has(id)); return elements.get(id);}, createElement: tag => new Element(tag)};
  globalThis.window = new EventTarget();
  const other = {...profile, id: 'b'.repeat(32), identity: '2'.repeat(64), name: 'Other', model: 'other', base_url: 'http://127.0.0.1:7002/v1', key_env: null, has_api_key: true};
  let current = catalog([profile, other]), calls = [];
  const el = id => elements.get(id), fire = (element, type = 'click') => element.dispatchEvent(new Event(type));
  const findButton = (element, label) => element.tagName === 'button' && element.textContent === label ? element
    : element.children.map(child => findButton(child, label)).find(Boolean);
  try {
    const view = createModelProfilesView({api: async path => {calls.push(path); return current;}, accepted: () => {}});
    await view.load();
    fire(findButton(el('profiles-list').children[1], '编辑'));
    el('profile-test-confirmed').checked = true; fire(el('profile-test-confirmed'), 'change');
    assert.equal(el('profile-test').disabled, false);
    view.refresh(); assert.equal(el('profile-test-confirmed').checked, true);
    fire(findButton(el('profiles-list').children[2], '编辑'));
    assert.equal(el('profile-test-confirmed').checked, false); assert.equal(el('profile-test').disabled, true);
    assert.equal(el('profile-key-stored').hidden, false); assert.equal(el('profile-key_env').disabled, true);
    assert.equal(el('profile-api_key').required, false); assert.equal(el('profile-api_key').value, '');
    fire(el('profile-delete-key'));
    assert.equal(el('profile-delete-key-confirmation').hidden, false);
    el('profile-test-confirmed').checked = true; fire(el('profile-test-confirmed'), 'change');
    current = catalog([profile, {...other, identity: '3'.repeat(64), revision: 2}], null, 2);
    await view.load();
    assert.equal(el('profile-test-confirmed').checked, false); assert.equal(el('profile-test').disabled, true);
    assert.equal(el('profile-delete-key-confirmation').hidden, true, 'reload invalidates deletion consent too');
    el('profile-api_key').value = 'synthetic-unsaved'; fire(el('profile-api_key'), 'input'); view.close();
    assert.equal(el('profile-api_key').value, '', 'closing actually clears the password input');
    assert.equal(calls.some(path => path.endsWith('/test')), false);
  } finally {
    if (oldDocument === undefined) delete globalThis.document; else globalThis.document = oldDocument;
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
  }
});

test('missing credential HTTP rejection gives a fixed environment-name explanation without raw exception details', async () => {
  const session = new ModelProfilesSession({api: async path => {
    if (path.endsWith('/test')) throw Object.assign(new Error('PRIVATE-CREDENTIAL-VALUE must never display'), {status: 409, code: 'model_credentials_missing'});
    return catalog();
  }});
  await session.load(); session.begin(profile.id); await session.test(true);
  assert.equal(session.state.testResult.kind, 'failed');
  assert.match(session.state.testResult.error, /密钥环境变量/);
  assert.match(session.state.testResult.error, /只填写变量名/);
  assert.doesNotMatch(session.state.testResult.error, /PRIVATE-CREDENTIAL/);
  assert.equal(session.state.testResult.uncertain, false);
});

test('long model forms reveal each outcome once without scrolling on refresh or after close', async () => {
  const scrolls = [];
  class Element extends EventTarget {
    constructor(tag = 'div') {super(); this.tagName = tag; this.children = []; this.value = ''; this.checked = false; this._text = '';}
    set textContent(value) {this._text = String(value); this.children = [];}
    get textContent() {return this._text + this.children.map(item => item.textContent).join('');}
    append(...items) {this.children.push(...items);}
    replaceChildren(...items) {this._text = ''; this.children = [...items];}
    focus() {} close() {this.open = false;} showModal() {this.open = true;}
    scrollIntoView(options) {scrolls.push({element: this, options});}
  }
  const html = await readFile(new URL('../../astra_harness/static/index.html', import.meta.url), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, new Element()]));
  const oldDocument = globalThis.document, oldWindow = globalThis.window;
  globalThis.document = {getElementById: id => {assert.ok(elements.has(id)); return elements.get(id);}, createElement: tag => new Element(tag)};
  globalThis.window = new EventTarget();
  const el = id => elements.get(id), fire = (element, type = 'click') => element.dispatchEvent(new Event(type));
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const findButton = (element, label) => element.tagName === 'button' && element.textContent === label ? element
    : element.children.map(child => findButton(child, label)).find(Boolean);
  let rejectSave = true, pendingTest = null, cancelledSignal = null, calls = [];
  try {
    const view = createModelProfilesView({accepted: () => {}, api: async (path, method, body, signal) => {
      calls.push(path);
      if (path.endsWith('/save') && rejectSave) throw Object.assign(new Error('private backend detail'), {status: 400, code: 'invalid_model_profile'});
      if (path.endsWith('/test')) {
        if (pendingTest) {cancelledSignal = signal; return pendingTest.promise;}
        return result({ok: false, error_code: 'model_protocol_error'});
      }
      return catalog();
    }});
    view.open(); await settle();
    fire(findButton(el('profiles-list').children[1], '编辑'));
    el('profile-name').value = 'Edited'; fire(el('profile-name'), 'input');
    fire(el('profile-form'), 'submit'); await settle();
    assert.equal(scrolls.length, 1); assert.equal(scrolls[0].element, el('profiles-error'));
    assert.match(el('profiles-error').textContent, /格式/); assert.doesNotMatch(el('profiles-error').textContent, /private/);
    view.refresh(); view.refresh(); assert.equal(scrolls.length, 1, 'routine renders retain the reading position');
    rejectSave = false; fire(el('profile-form'), 'submit'); await settle();
    assert.equal(scrolls.length, 2); assert.equal(scrolls[1].element, el('profiles-notice'));
    el('profile-test-confirmed').checked = true; fire(el('profile-test-confirmed'), 'change');
    fire(el('profile-test')); await settle();
    assert.equal(scrolls.length, 3); assert.equal(scrolls[2].element, el('profile-test-result'));
    assert.match(el('profile-test-result').textContent, /测试未通过/);
    view.refresh(); assert.equal(scrolls.length, 3);
    pendingTest = deferred();
    el('profile-test-confirmed').checked = true; fire(el('profile-test-confirmed'), 'change');
    fire(el('profile-test')); await settle();
    assert.equal(scrolls.length, 3, 'starting a test does not reveal a stale save notice');
    view.close(); assert.equal(cancelledSignal.aborted, true); assert.equal(scrolls.length, 3, 'closing cancels without scrolling');
    pendingTest.resolve(result()); await settle();
    assert.equal(view.state.testResult.kind, 'cancelled'); view.refresh();
    assert.equal(scrolls.length, 3, 'late responses and closed renders do not scroll');
    assert.equal(calls.filter(path => path.endsWith('/test')).length, 2);
    assert.deepEqual(scrolls[0].options, {block: 'nearest', inline: 'nearest'});
  } finally {
    if (oldDocument === undefined) delete globalThis.document; else globalThis.document = oldDocument;
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
  }
});

test('fixed profile error meanings distinguish CAS, use, credentials, protocol and timeout without unknown-code reflection', async () => {
  for (const [code, meaning] of [['invalid_model_profile', /格式/], ['model_profile_conflict', /版本/],
    ['model_profile_in_use', /仍被/], ['model_session_mismatch', /新建对话/],
    ['model_credentials_changed', /凭据/], ['model_protocol_error', /协议/], ['model_timeout', /超时/]]) {
    assert.match(modelProfileError(code), meaning);
  }
  assert.equal(modelProfileError('__proto__'), null);
  assert.equal(modelProfileError('PRIVATE-CREDENTIAL-VALUE'), null);
  const session = new ModelProfilesSession({api: async path => {
    if (path.endsWith('/delete')) throw Object.assign(new Error('raw private details'), {status: 409, code: 'model_profile_in_use'});
    return catalog();
  }});
  await session.load(); await session.remove(profile.id);
  assert.match(session.state.error, /仍被/); assert.doesNotMatch(session.state.error, /raw private/);
});
