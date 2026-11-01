import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {WorkspaceSelection, scopedSubmission, workspaceQuery} from '../../astra_harness/static/workspace-state.mjs';

const a = {id: 'a'.repeat(32), path: 'C:\\tasks\\a', identity: '1'.repeat(64)};
const b = {id: 'b'.repeat(32), path: 'C:\\tasks\\b', identity: '2'.repeat(64)};
const snapshot = (selected = a, revision = 0) => ({enabled: true, startup_id: a.id, selected_id: selected.id, revision, items: [a, b]});
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};};

test('validation reads metadata only; confirmation binds normalized path, identity and captured revision', async () => {
  const calls = [], applied = [];
  const picker = new WorkspaceSelection({api: async (url, method, body) => {
    calls.push({url, method, body});
    return url.endsWith('validate') ? b : url.endsWith('select') ? snapshot(b, 4) : snapshot(a, 3);
  }, onApplied: value => applied.push(value.selected_id)});
  await picker.load(); picker.open(); picker.edit('  C:\\tasks\\b  '); await picker.validate();
  assert.equal(picker.state.snapshot.selected_id, a.id);
  assert.equal(calls.filter(call => call.url.endsWith('select')).length, 0);
  await picker.select();
  assert.deepEqual(calls.at(-1).body, {path: b.path, expected_identity: b.identity, expected_revision: 3});
  assert.deepEqual(applied, [a.id, b.id]); assert.equal(picker.state.open, false);
});

test('changing or cancelling a path invalidates an outstanding metadata response', async () => {
  const pending = deferred();
  const picker = new WorkspaceSelection({api: url => url.endsWith('validate') ? pending.promise : snapshot()});
  await picker.load(); picker.edit(b.path); const validation = picker.validate();
  picker.edit(a.path); pending.resolve(b); await validation;
  assert.equal(picker.state.candidate, null); assert.equal(picker.state.draft, a.path);
  picker.state.candidate = {...b, revision: 0}; picker.cancel();
  assert.equal(picker.state.candidate, null); assert.equal(picker.state.snapshot.selected_id, a.id);
});

test('double confirm sends one select; cancelling cannot abandon an in-flight mutation', async () => {
  const pending = deferred(); let count = 0;
  const picker = new WorkspaceSelection({api: url => {
    if (url.endsWith('select')) {count++; return pending.promise;}
    return url.endsWith('validate') ? b : snapshot();
  }});
  await picker.load(); picker.edit(b.path); await picker.validate();
  const first = picker.select(); await picker.select(); picker.cancel(); picker.edit(a.path);
  assert.equal(count, 1); assert.equal(picker.state.phase, 'selecting'); assert.equal(picker.state.draft, b.path);
  pending.resolve(snapshot(b, 1)); await first; assert.equal(picker.state.snapshot.selected_id, b.id);
});

test('CAS conflict discards stale confirmation and does not retry selection', async () => {
  let selects = 0;
  const picker = new WorkspaceSelection({api: async url => {
    if (url.endsWith('select')) {selects++; throw Object.assign(new Error('changed'), {status: 409});}
    return url.endsWith('validate') ? b : snapshot();
  }});
  await picker.load(); picker.edit(b.path); await picker.validate(); await picker.select(); await picker.select();
  assert.equal(selects, 1); assert.equal(picker.state.candidate, null); assert.equal(picker.state.uncertain, false);
  assert.equal(picker.state.snapshot.selected_id, a.id); assert.equal(picker.state.error, 'changed');
});

test('unknown select result blocks another select until GET reconciles the actual root', async () => {
  let selected = a; const methods = [];
  const picker = new WorkspaceSelection({api: async (url, method = 'GET') => {
    methods.push([url, method]);
    if (url.endsWith('select')) {selected = b; throw new TypeError('connection lost');}
    return url.endsWith('validate') ? b : snapshot(selected);
  }});
  await picker.load(); picker.edit(b.path); await picker.validate(); await picker.select();
  assert.equal(picker.locked(), true); picker.edit(a.path); await picker.select();
  await picker.load(); assert.equal(picker.state.snapshot.selected_id, b.id); assert.equal(picker.locked(), false);
  assert.equal(methods.filter(([, method]) => method === 'POST').length, 2);
  assert.deepEqual(methods.at(-1), ['/api/workspaces', 'GET']);
});

test('failed reconciliation remains uncertain and disabled selection never sends a validation', async () => {
  const picker = new WorkspaceSelection({api: async () => {throw new Error('offline');}});
  picker.state.uncertain = true; await picker.load(); assert.equal(picker.locked(), true);
  let calls = 0; const disabled = new WorkspaceSelection({api: async () => {calls++; return {...snapshot(), enabled: false};}});
  await disabled.load(); disabled.edit(b.path); await disabled.validate(); await disabled.select(); assert.equal(calls, 1);
});

test('unconfirmed run creation blocks directory changes but an active background run need not', async () => {
  let blocked = true, calls = 0;
  const picker = new WorkspaceSelection({api: async url => {calls++; return url.endsWith('validate') ? b : snapshot();}, blocked: () => blocked});
  await picker.load(); picker.edit(b.path); await picker.validate(); await picker.load(); assert.equal(calls, 1);
  blocked = false; await picker.validate(); assert.equal(calls, 2); assert.equal(picker.state.candidate.id, b.id);
});

test('accepted switch remains acknowledged if subsequent session loading fails', async () => {
  const picker = new WorkspaceSelection({api: async url => url.endsWith('validate') ? b : snapshot(url.endsWith('select') ? b : a),
    onApplied: async value => {if (value.selected_id === b.id) throw new Error('history unavailable');}});
  await picker.load(); picker.edit(b.path); await picker.validate(); await picker.select();
  assert.equal(picker.state.snapshot.selected_id, b.id); assert.equal(picker.state.uncertain, false);
  assert.equal(picker.state.error, 'history unavailable');
});

test('pending submissions retain their exact request identity and root across changed drafts or selected roots', () => {
  const request = scopedSubmission(null, 'read file', null, a.id, () => 'request-one');
  const retry = scopedSubmission(request, 'different', 'another-session', b.id, () => 'should-not-be-used');
  assert.strictEqual(retry, request); assert.equal(retry.workspace_id, a.id); assert.equal(retry.prompt, 'read file');
  assert.throws(() => scopedSubmission(null, 'task', null, '', () => 'x'));
  assert.equal(scopedSubmission(null, 'read file', null, b.id, () => 'new').workspace_id, b.id);
});

test('file and session queries always carry an explicit encoded workspace scope', () => {
  assert.equal(workspaceQuery('/api/files?path=a%20b', a.id), `/api/files?path=a%20b&workspace_id=${a.id}`);
  assert.equal(workspaceQuery('/api/sessions', b.id), `/api/sessions?workspace_id=${b.id}`);
  assert.throws(() => workspaceQuery('/api/files', null));
});

test('app keeps scope, approval preview retry, and mobile navigation interactions safe', async t => {
  class Element extends EventTarget {
    constructor(tag = 'div') {super(); this.tagName = tag; this.children = []; this.value = ''; this.hidden = false; this.disabled = false; this.dataset = {}; this.attrs = {}; this._text = ''; this._classes = new Set();
      this.classList = {add: (...xs) => xs.forEach(x => this._classes.add(x)), remove: (...xs) => xs.forEach(x => this._classes.delete(x)), contains: x => this._classes.has(x), toggle: (x, on = !this._classes.has(x)) => on ? this._classes.add(x) : this._classes.delete(x)};}
    set textContent(value) {this._text = String(value); this.children = [];}
    get textContent() {return this._text + this.children.map(child => child.textContent).join('');}
    set className(value) {this._classes = new Set(value.split(' '));}
    get className() {return [...this._classes].join(' ');}
    append(...children) {this.children.push(...children.map(child => typeof child === 'string' ? Object.assign(new Element(), {textContent: child}) : child));}
    appendChild(child) {this.append(child); return child;}
    replaceChildren(...children) {this._text = ''; this.children = []; this.append(...children);}
    get childElementCount() {return this.children.length;}
    setAttribute(name, value) {this.attrs[name] = String(value);}
    querySelectorAll(selector) {return this.children.flatMap(child => [...(selector.startsWith('.') ? child.classList.contains(selector.slice(1)) : child.tagName === selector) ? [child] : [], ...child.querySelectorAll(selector)]);}
    querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
    focus() {document.activeElement = this;} select() {} close() {this.open = false;} showModal() {this.open = true;} scrollIntoView() {}
  }
  const html = await readFile(new URL('../../astra_harness/static/index.html', import.meta.url), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, Object.assign(new Element(), {id})]));
  const shell = new Element();
  const dom = {activeElement: null, getElementById: id => {assert.ok(elements.has(id), 'actual HTML has ' + id); return elements.get(id);},
    createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element(), {textContent: text}), createDocumentFragment: () => new Element(),
    querySelector: selector => selector === '.app-shell' ? shell : elements.get('tab-files')};
  const storage = () => {const values = new Map(); return {getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};};
  const originals = new Map();
  const install = (key, value) => {originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key)); Object.defineProperty(globalThis, key, {configurable: true, writable: true, value});};
  const timers = new Set(), nativeSetTimeout = globalThis.setTimeout, nativeClearTimeout = globalThis.clearTimeout;
  install('setTimeout', (callback, delay, ...args) => {
    const timer = nativeSetTimeout(() => {timers.delete(timer); callback(...args);}, delay);
    timers.add(timer); return timer;
  });
  const sources = [];
  class Source extends EventTarget {constructor(url) {super(); this.url = url; sources.push(this);} close() {this.closed = true;}}
  const mobileMedia = Object.assign(new EventTarget(), {matches: false});
  let selected = a, revision = 0, lateList = null, lateRead = null, loseSubmit = true, unavailable = false, previewReads = 0;
  const retriedPreview = deferred(), latePreview = deferred();
  const renameReplies = [];
  let savedTitle = 'Synthetic session';
  const requests = [], run = {id: 'run-b', session_id: 'session-b', workspace_id: b.id, workspace_path: b.path,
    prompt: 'search data', status: 'waiting_approval', limits: {},
    approval: {key: 'preview-fixture', name: 'files.write', arguments: {path: 'result.json', content: '{"ok":true}'}},
    task_context: {selection: {id: 'report', expected_identity: b.id.repeat(2)}, metadata: {name: '指导 B'}}};
  const fetcher = async (path, options = {}) => {
    const url = new URL(path, 'http://local'), method = options.method || 'GET';
    const body = options.body && JSON.parse(options.body); requests.push({url, method, body});
    let value;
    if (url.pathname === '/api/info') value = {mode: 'demo', tools: ['files.read'], unresolved_runs: [], workspace_available: !(unavailable && selected.id === b.id)};
    else if (url.pathname === '/api/model-profiles') value = {revision: 0, selected_id: null, selected_identity: 'startup', items: []};
    else if (url.pathname.startsWith('/api/skills')) {
      assert.equal(url.searchParams.get('workspace_id'), selected.id);
      const skill = {id: 'report', name: selected.id === a.id ? '指导 A' : '指导 B', description: 'Fixture', path: 'skills/report/SKILL.md', identity: selected.id.repeat(2), enabled: true, needs_review: false, byte_count: 80};
      if (url.pathname === '/api/skills') value = {workspace_id: selected.id, revision: 1, exists: true, items: [skill], issues: []};
      else {assert.equal(url.searchParams.get('expected_identity'), skill.identity); value = {...skill, workspace_id: selected.id, revision: 1, body: 'Synthetic task guidance'};}
    }
    else if (url.pathname === '/api/workspaces') value = snapshot(selected, revision);
    else if (url.pathname.endsWith('/validate')) value = body.path === a.path ? a : b;
    else if (url.pathname.endsWith('/select')) {selected = body.path === a.path ? a : b; revision++; value = snapshot(selected, revision);}
    else if (url.pathname === '/api/sessions') {
      assert.equal(url.searchParams.get('workspace_id'), selected.id);
      value = selected.id === a.id ? [{id: 'session-fixture', title: savedTitle, archived: false}] : [];
    } else if (url.pathname === '/api/sessions/session-fixture') {
      assert.equal(method, 'POST'); assert.ok(renameReplies.length, 'rename has an explicit fixture response');
      const reply = await renameReplies.shift().promise;
      if (reply.status !== 200) return new Response(JSON.stringify({error: reply.error}), {status: reply.status});
      savedTitle = body.title; value = {id: 'session-fixture', title: savedTitle};
    }
    else if (url.pathname === '/api/files') {
      if (lateList) {const pending = lateList; lateList = null; value = await pending.promise;}
      else value = {path: '', entries: [{path: selected.id === a.id ? 'a.txt' : 'b.txt', name: selected.id === a.id ? 'a.txt' : 'b.txt', type: 'file', size: 3}]};
    } else if (url.pathname === '/api/files/read') {assert.equal(url.searchParams.get('workspace_id'), a.id); value = await lateRead.promise;}
    else if (url.pathname === '/api/runs') {if (loseSubmit) {loseSubmit = false; throw new TypeError('lost response');} value = run;}
    else if (url.pathname === '/api/runs/run-b/preview') {
      assert.equal(method, 'GET'); previewReads++;
      if (previewReads === 1) return new Response(JSON.stringify({error: 'internal_error'}), {status: 503});
      value = await (previewReads === 2 ? retriedPreview : latePreview).promise;
    }
    else throw new Error('unexpected API ' + method + ' ' + url.pathname);
    return new Response(JSON.stringify(value), {status: 200, headers: {'Content-Type': 'application/json'}});
  };
  install('document', dom); install('window', new EventTarget()); install('localStorage', storage()); install('sessionStorage', storage());
  install('fetch', fetcher); install('EventSource', Source); install('requestAnimationFrame', callback => callback());
  install('matchMedia', query => query === '(max-width:700px)' ? mobileMedia : {matches: false});
  const el = id => elements.get(id), fire = (id, event) => el(id).dispatchEvent(new Event(event, {cancelable: true}));
  const wait = async predicate => {for (let i = 0; i < 50; i++) {if (predicate()) return; await new Promise(resolve => setImmediate(resolve));} assert.ok(predicate(), 'async UI settled');};
  const choose = async path => {
    fire('workspace-open', 'click'); el('workspace-input').value = path; fire('workspace-input', 'input'); fire('workspace-form', 'submit');
    await wait(() => !el('workspace-candidate').hidden); fire('workspace-confirm', 'click');
    await wait(() => el('workspace-path').textContent === path && !el('workspace-open').disabled);
  };
  const chooseSkill = async () => {
    fire('skill-picker', 'click'); await wait(() => el('skills-list').children.length === 1 && el('skills-loading').hidden);
    el('skills-list').children[0].children.at(-1).dispatchEvent(new Event('click'));
    await wait(() => !el('skill-detail').hidden && !el('skill-choose').disabled); fire('skill-choose', 'click');
    assert.match(el('skill-selection').textContent, /下一次任务/);
  };
  const renameRequests = () => requests.filter(item => item.url.pathname === '/api/sessions/session-fixture');
  const openRename = () => {
    el('sessions').querySelector('.session-menu').dispatchEvent(new Event('click'));
    el('dialog-body').querySelector('button').dispatchEvent(new Event('click'));
    assert.equal(el('dialog-title').textContent, '重命名对话');
    return el('dialog-body').querySelector('input');
  };
  try {
    await import('../../astra_harness/static/app.mjs?workspace-test');
    await wait(() => el('file-list').textContent.includes('a.txt'));
    assert.equal(el('error').hidden, true);
    // Actual app handlers: both openers, explicit close/Escape/backdrop, navigation,
    // and desktop resizing share one drawer state and do not issue task requests.
    mobileMedia.matches = true; mobileMedia.dispatchEvent(new Event('change'));
    el('sidebar-toggle').focus(); fire('sidebar-toggle', 'click');
    assert.equal(shell.classList.contains('sidebar-open'), true);
    assert.equal(el('sidebar-toggle').attrs['aria-expanded'], 'true');
    assert.equal(el('knowledge-menu').attrs['aria-expanded'], 'true');
    assert.equal(dom.activeElement, el('sidebar-close'));
    const escape = Object.assign(new Event('keydown', {cancelable: true}), {key: 'Escape'});
    window.dispatchEvent(escape);
    assert.equal(escape.defaultPrevented, true);
    assert.equal(shell.classList.contains('sidebar-open'), false);
    assert.equal(el('mobile-backdrop').hidden, true);
    assert.equal(el('sidebar-toggle').attrs['aria-expanded'], 'false');
    assert.equal(dom.activeElement, el('sidebar-toggle'));
    el('knowledge-menu').focus(); fire('knowledge-menu', 'click'); fire('sidebar-close', 'click');
    assert.equal(dom.activeElement, el('knowledge-menu'));
    fire('sidebar-toggle', 'click'); fire('mobile-backdrop', 'click');
    assert.equal(dom.activeElement, el('sidebar-toggle'));
    fire('sidebar-toggle', 'click'); fire('open-model-profiles', 'click');
    await wait(() => el('model-profiles-dialog').open);
    assert.equal(shell.classList.contains('sidebar-open'), false);
    assert.equal(el('sidebar-toggle').attrs['aria-expanded'], 'false');
    fire('model-profiles-close', 'click');
    fire('sidebar-toggle', 'click'); fire('about-button', 'click');
    assert.equal(el('dialog').open, true); assert.equal(shell.classList.contains('sidebar-open'), false);
    fire('dialog-close', 'click');
    fire('sidebar-toggle', 'click'); mobileMedia.matches = false; mobileMedia.dispatchEvent(new Event('change'));
    assert.equal(shell.classList.contains('sidebar-open'), false);
    assert.equal(el('mobile-backdrop').hidden, true);
    assert.equal(el('knowledge-menu').attrs['aria-expanded'], 'false');
    fire('sidebar-toggle', 'click'); assert.equal(shell.classList.contains('sidebar-open'), false);
    await t.test('failed rename keeps its exact draft and retries once without duplicate submissions', async () => {
      const input = openRename(); input.value = 'Original draft with spaces';
      const failure = deferred(); renameReplies.push(failure);
      fire('dialog-form', 'submit');
      assert.equal(el('dialog-confirm').disabled, true);
      failure.resolve({status: 400, error: 'invalid_request'});
      await wait(() => !el('dialog-confirm').disabled);
      assert.equal(el('dialog').open, true, 'a rejected rename must keep the draft dialog open');
      assert.equal(el('dialog-body').querySelector('input'), input);
      assert.equal(input.value, 'Original draft with spaces');
      assert.equal(el('dialog-error').hidden, false); assert.match(el('dialog-error').textContent, /请求无法处理/);
      assert.equal(el('error').hidden, true, 'rename errors belong to the open dialog');
      const retry = deferred(); renameReplies.push(retry);
      fire('dialog-form', 'submit'); fire('dialog-form', 'submit');
      assert.equal(renameRequests().length, 2);
      assert.equal(el('dialog-error').hidden, true);
      retry.resolve({status: 200});
      await wait(() => !el('dialog').open && el('sessions').textContent.includes(input.value));
      assert.deepEqual(renameRequests().map(item => item.body), [{title: input.value}, {title: input.value}]);
    });
    await t.test('cancelled rename failure cannot replace a newer draft or unlock its pending save', async () => {
      const previous = deferred(); renameReplies.push(previous);
      openRename().value = 'Cancelled pending draft'; fire('dialog-form', 'submit');
      fire('dialog-cancel', 'click'); assert.equal(el('dialog').open, false);
      const input = openRename(); input.value = 'Newer unsaved draft';
      assert.equal(el('dialog-error').hidden, true);
      const next = deferred(); renameReplies.push(next); fire('dialog-form', 'submit');
      previous.resolve({status: 500, error: 'internal_error'});
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(el('dialog').open, true); assert.equal(el('dialog-body').querySelector('input'), input);
      assert.equal(input.value, 'Newer unsaved draft'); assert.equal(el('dialog-confirm').disabled, true);
      assert.equal(el('dialog-error').hidden, true); assert.equal(el('error').hidden, true);
      const count = renameRequests().length; fire('dialog-form', 'submit');
      assert.equal(renameRequests().length, count, 'the older finally must not release the newer single-flight guard');
      next.resolve({status: 400, error: 'invalid_request'});
      await wait(() => !el('dialog-confirm').disabled);
      assert.equal(input.value, 'Newer unsaved draft'); assert.equal(el('dialog-error').hidden, false);
      fire('dialog-cancel', 'click');
      assert.equal(openRename().value, savedTitle, 'explicit cancellation discards only the unsaved draft');
      assert.equal(el('dialog-error').hidden, true); fire('dialog-close', 'click');
    });
    await t.test('Escape invalidates an old rename while its eventual success leaves the new dialog open', async () => {
      const pending = deferred(); renameReplies.push(pending);
      openRename().value = 'Acknowledged after close'; fire('dialog-form', 'submit');
      const cancel = new Event('cancel', {cancelable: true}); el('dialog').dispatchEvent(cancel);
      assert.equal(cancel.defaultPrevented, true); assert.equal(el('dialog').open, false);
      fire('about-button', 'click');
      pending.resolve({status: 200});
      await wait(() => el('sessions').textContent.includes('Acknowledged after close'));
      assert.equal(el('dialog').open, true); assert.equal(el('dialog-title').textContent, '运行配置');
      assert.equal(el('dialog-error').hidden, true); assert.equal(el('error').hidden, true);
      fire('dialog-close', 'click');
    });
    await chooseSkill(); assert.match(el('skill-selection').textContent, /指导 A/);
    lateRead = deferred(); el('file-list').children[0].dispatchEvent(new Event('click'));
    const oldList = deferred(); lateList = oldList; fire('refresh-files', 'click');
    await choose(b.path);
    assert.equal(el('skill-selection').textContent, '本次任务不附加 Skill');
    oldList.resolve({path: 'old', entries: [{path: 'old.txt', name: 'OLD ROOT', type: 'file'}]});
    lateRead.resolve({path: 'a.txt', content: 'OLD PRIVATE CONTENT'});
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(el('file-list').textContent.includes('b.txt')); assert.equal(el('file-preview').hidden, true);
    assert.equal(el('file-content').textContent, '');
    await chooseSkill(); assert.match(el('skill-selection').textContent, /指导 B/);
    el('prompt').value = 'search data'; fire('composer', 'submit');
    await wait(() => el('send').textContent === '确认上次提交' && !el('send').disabled);
    assert.equal(el('workspace-open').disabled, true); assert.equal(el('new-session').disabled, true);
    const pending = JSON.parse(sessionStorage.getItem('astra.pendingSubmission'));
    assert.equal(pending.workspace_id, b.id);
    assert.deepEqual(pending.skill, {id: 'report', expected_identity: b.id.repeat(2)});
    assert.equal(el('skill-clear').disabled, true); fire('skill-clear', 'click'); assert.match(el('skill-selection').textContent, /待确认的原请求/);
    fire('composer', 'submit'); await wait(() => sources.length === 1 && !el('workspace-open').disabled);
    const submissions = requests.filter(item => item.url.pathname === '/api/runs');
    assert.equal(submissions.length, 2); assert.deepEqual(submissions[0].body, submissions[1].body);
    assert.equal(sessionStorage.getItem('astra.pendingSubmission'), null);
    assert.equal(el('skill-selection').textContent, '本次任务不附加 Skill');
    assert.ok(el('run-details').textContent.includes(b.path));
    assert.match(el('run-details').textContent, /任务指导：指导 B/);
    await wait(() => el('approval-content').textContent.includes('重新读取文件预览'));
    assert.match(el('approval-content').textContent, /result\.json/);
    assert.equal(el('approve').disabled, false); assert.equal(el('reject').disabled, false);
    const snapshotEvent = () => {
      const event = new Event('snapshot'); event.data = JSON.stringify(run); sources[0].dispatchEvent(event);
    };
    snapshotEvent(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(previewReads, 1, 'same approval snapshots never retry a failed read automatically');
    const retry = el('approval-content').querySelectorAll('button').find(item => item.textContent === '重新读取文件预览');
    retry.dispatchEvent(new Event('click')); retry.dispatchEvent(new Event('click')); snapshotEvent();
    assert.equal(previewReads, 2, 'rapid explicit retries share the pending preview read');
    assert.match(el('approval-content').textContent, /正在读取文件预览/);
    retriedPreview.resolve({path: 'result.json', exists: false, before: '', after: '{"ok":true}'});
    await wait(() => el('approval-content').querySelector('.diff-grid'));
    assert.match(el('approval-content').textContent, /批准后写入/);
    assert.equal(el('approve').disabled, false); assert.equal(el('reject').disabled, false);
    assert.equal(requests.some(item => item.url.pathname.endsWith('/approve')), false, 'preview retry never submits approval');
    run.approval = {...run.approval, key: 'next-approval', arguments: {path: 'next.json', content: 'next'}};
    snapshotEvent(); assert.equal(previewReads, 3);
    retry.dispatchEvent(new Event('click'));
    assert.equal(previewReads, 3, 'a previous approval retry cannot read for a changed approval');
    unavailable = true; fire('refresh-services', 'click');
    await wait(() => el('prompt').disabled && !el('refresh-services').disabled);
    assert.equal(el('send').disabled, true);
    assert.equal(el('workspace-open').disabled, false); assert.equal(el('workspace-reconcile').disabled, false);
    assert.equal(el('cancel').disabled, false); assert.equal(el('approve').disabled, false);
    await choose(a.path);
    latePreview.resolve({path: 'OBSOLETE PREVIEW', exists: false, after: 'OLD APPROVAL CONTENT'});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(el('approval-content').textContent.includes('OBSOLETE PREVIEW'), false);
    assert.equal(sources[0].closed, true); assert.equal(el('conversation-title').textContent, '新对话');
    assert.equal(el('prompt').disabled, false); assert.equal(el('send').disabled, false);
    assert.equal(requests.some(item => item.url.pathname.endsWith('/cancel')), false);
    assert.equal(requests.filter(item => item.url.pathname === '/api/files').every(item => item.url.searchParams.has('workspace_id')), true);
  } finally {
    for (const timer of timers) nativeClearTimeout(timer);
    for (const [key, descriptor] of originals) {if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];}
  }
});
