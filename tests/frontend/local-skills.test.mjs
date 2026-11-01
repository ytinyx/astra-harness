import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {LocalSkillsSession, skillRunLabel, skillError} from '../../astra_harness/static/local-skills-state.mjs';
import {createLocalSkillsView} from '../../astra_harness/static/local-skills.mjs';
import {scopedSubmission} from '../../astra_harness/static/workspace-state.mjs';

const root = 'a'.repeat(32), otherRoot = 'b'.repeat(32);
const item = {id: 'brief-report', name: '简明报告', description: '整理结果', path: 'skills/brief-report/SKILL.md', sha256: 'a'.repeat(64), identity: '1'.repeat(64), byte_count: 110, enabled: false, needs_review: false};
const catalog = (items = [item], revision = 0, workspace = root) => ({workspace_id: workspace, workspace_identity: '2'.repeat(64), revision, directory: 'skills', exists: true, limit: 32, items, issues: []});
const detail = (extra = {}) => ({...item, revision: 0, workspace_id: root, workspace_identity: '2'.repeat(64), body: '<script>not executable</script>\n![remote](https://invalid.example/image)', ...extra});
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};};
const create = options => {const session = new LocalSkillsSession(options); session.reset(root); return session;};

test('listing is read-only, selection starts empty and body requires explicit identity-bound view', async () => {
  const calls = [], session = create({api: async (path, method) => {calls.push({path, method}); return path.includes('/brief-report?') ? detail() : catalog();}});
  await session.load(); assert.equal(session.selection(), null); assert.equal(session.state.detail, null);
  await session.view(item.id); const url = new URL(calls.at(-1).path, 'http://fixture');
  assert.equal(url.searchParams.get('workspace_id'), root); assert.equal(url.searchParams.get('expected_identity'), item.identity);
  session.choose(); assert.equal(session.selection(), null); assert.equal(calls.every(call => call.method === 'GET'), true);
});

test('enabling requires the viewed version and explicit confirmation; repeated clicks send one CAS', async () => {
  const waiting = deferred(), calls = [], session = create({api: async (path, method, body) => {
    calls.push({path, body}); return path === '/api/skills/enabled' ? waiting.promise : path.includes('/brief-report?') ? detail() : catalog();
  }});
  await session.load(); await session.setEnabled(true, true); await session.view(item.id); await session.setEnabled(true, false);
  const first = session.setEnabled(true, true); await session.setEnabled(true, true);
  assert.equal(calls.length, 3); assert.deepEqual(calls.at(-1).body, {workspace_id: root, id: item.id, expected_identity: item.identity, expected_revision: 0, enabled: true});
  waiting.resolve(catalog([{...item, enabled: true}], 1)); await first;
  assert.equal(session.selection(), null, 'enable is not task selection'); session.choose();
  assert.deepEqual(session.selection(), {id: item.id, expected_identity: item.identity});
});

test('unknown enable outcome blocks replay and version selection until explicit read reconciles it', async () => {
  const calls = [], session = create({api: async path => {
    calls.push(path); if (path === '/api/skills/enabled') throw new TypeError('lost response'); return path.includes('/brief-report?') ? detail() : catalog();
  }});
  await session.load(); await session.view(item.id); await session.setEnabled(true, true); await session.setEnabled(true, true); session.choose();
  assert.equal(calls.length, 3); assert.equal(session.state.uncertain, true); assert.equal(session.selection(), null);
  await session.load(); assert.equal(session.state.uncertain, false); assert.equal(calls.length, 4);
});

test('CAS rejection invalidates the reviewed detail and cannot silently reuse stale consent', async () => {
  let writes = 0; const session = create({api: async path => {
    if (path === '/api/skills/enabled') {writes++; throw Object.assign(new Error('private detail'), {status: 409, code: 'skill_catalog_conflict'});}
    return path.includes('/brief-report?') ? detail() : catalog();
  }});
  await session.load(); await session.view(item.id); await session.setEnabled(true, true); await session.setEnabled(true, true);
  assert.equal(writes, 1); assert.equal(session.state.detail, null); assert.match(session.state.error, /重新查看/); assert.doesNotMatch(session.state.error, /private/);
  assert.match(skillError('skill_metadata_invalid'), /name 与 description/); assert.match(skillError('skill_file_limit'), /16 KiB/);
  assert.equal(skillError('private server details'), null); assert.equal(skillError('__proto__'), null);
});

test('content changes preserve the originally selected identity but mark it unusable instead of auto-upgrading', async () => {
  let rows = [{...item, enabled: true}]; const session = create({api: async path => path.includes('/brief-report?') ? detail({enabled: true}) : catalog(rows)});
  await session.load(); await session.view(item.id); session.choose();
  rows = [{...item, identity: '3'.repeat(64), enabled: false, needs_review: true}]; await session.load();
  assert.equal(session.state.detail, null); assert.equal(session.selection().expected_identity, item.identity); assert.equal(session.state.selectionStale, true);
  session.clear(); assert.equal(session.selection(), null);
});

test('disabled or missing selected entries become stale, including an absent directory', async () => {
  let current = catalog([{...item, enabled: true}]); const session = create({api: async path => path.includes('/brief-report?') ? detail({enabled: true}) : current});
  await session.load(); await session.view(item.id); session.choose(); current = catalog(); await session.load(); assert.equal(session.state.selectionStale, true);
  current = {...catalog([]), exists: false}; await session.load(); assert.equal(session.state.selectionStale, true); assert.equal(session.selection().id, item.id);
});

test('late reads and mutation replies from another workspace cannot restore its data or selection', async () => {
  const pending = deferred(); let mode = 'list';
  const session = create({api: async path => mode === 'late' ? pending.promise : path.includes('/brief-report?') ? detail({enabled: true}) : catalog([{...item, enabled: true}])});
  await session.load(); await session.view(item.id); session.choose(); mode = 'late'; const read = session.load(); session.reset(otherRoot);
  pending.resolve(catalog()); await read;
  assert.equal(session.state.catalog, null); assert.equal(session.state.detail, null); assert.equal(session.selection(), null); assert.equal(session.state.workspaceId, otherRoot);
  const writePending = deferred(); session.reset(root); mode = 'list'; await session.load(); await session.view(item.id);
  session.api = async () => writePending.promise; const write = session.setEnabled(false); session.reset(otherRoot);
  writePending.resolve(catalog([], 1)); await write; assert.equal(session.state.catalog, null); assert.equal(session.state.workspaceId, otherRoot);
});

test('same-workspace late details cannot replace a newer explicit choice and mismatched identities are rejected', async () => {
  const second = {...item, id: 'second', identity: '4'.repeat(64)}, firstRead = deferred();
  const session = create({api: async path => path.includes('/brief-report?') ? firstRead.promise : path.includes('/second?') ? detail({...second, enabled: true}) : catalog([item, second])});
  await session.load(); const old = session.view(item.id); await session.view(second.id); firstRead.resolve(detail()); await old;
  assert.equal(session.state.detail.id, second.id);
  session.api = async () => detail({identity: '5'.repeat(64)}); await session.view(item.id); assert.equal(session.state.detail, null);
});

test('pending submission prevents enabling, changing or clearing the reviewed selection', async () => {
  let blocked = false, writes = 0; const session = create({blocked: () => blocked, api: async path => {if (path === '/api/skills/enabled') writes++; return path.includes('/brief-report?') ? detail({enabled: true}) : catalog([{...item, enabled: true}]);}});
  await session.load(); await session.view(item.id); session.choose(); blocked = true; session.clear(); await session.setEnabled(false);
  assert.equal(writes, 0); assert.equal(session.selection().id, item.id);
  session.consume(); assert.equal(session.selection(), null, 'acknowledged submission can consume while controls are busy');
});

test('task snapshot copies exactly one selection, retries retain it, and subsequent tasks default to none', () => {
  const selected = {id: item.id, expected_identity: item.identity, name: item.name};
  const request = scopedSubmission(null, 'task', 'session', root, () => 'first', 'startup', selected);
  selected.expected_identity = 'changed'; assert.equal(request.skill.expected_identity, item.identity); assert.equal(request.skill.name, undefined);
  assert.strictEqual(scopedSubmission(request, 'changed', 'another', otherRoot, () => 'unused', 'different', null), request);
  assert.equal(scopedSubmission(null, 'next', 'session', root, () => 'second').skill, null);
  assert.equal(skillRunLabel({task_context: {selection: request.skill, metadata: {name: '历史指导'}}}), `任务指导：历史指导 · ${item.id} · ${item.identity.slice(0, 12)}`);
  assert.equal(skillRunLabel({}), '任务指导：未附加 Skill');
});

test('actual view renders body as plain text, requires consent and shows selected version at the composer', async () => {
  class Element extends EventTarget {
    constructor(tag = 'div') {super(); this.tagName = tag; this.children = []; this.value = ''; this.checked = false; this.hidden = false; this.disabled = false; this._text = ''; this.classList = {toggle() {}};}
    set textContent(value) {this._text = String(value); this.children = [];}
    get textContent() {return this._text + this.children.map(child => child.textContent).join('');}
    append(...children) {this.children.push(...children);} replaceChildren(...children) {this._text = ''; this.children = children;}
    focus() {} close() {this.open = false;} showModal() {this.open = true;} scrollIntoView() {}
  }
  const html = await readFile(new URL('../../astra_harness/static/index.html', import.meta.url), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, new Element()]));
  const previous = globalThis.document, tags = [];
  globalThis.document = {getElementById: id => {assert.ok(elements.has(id)); return elements.get(id);}, createElement: tag => {tags.push(tag); return new Element(tag);}};
  let enabled = false, revision = 0, identity = item.identity, pending = null; const calls = [];
  const el = id => elements.get(id), fire = (id, type = 'click') => el(id).dispatchEvent(new Event(type));
  const settled = async () => new Promise(resolve => setImmediate(resolve));
  try {
    const view = createLocalSkillsView({pending: () => pending, blocked: () => !!pending, api: async (path, method, body) => {
      calls.push({path, method, body}); if (path === '/api/skills/enabled') {enabled = body.enabled; revision++;}
      return path.includes('/brief-report?') ? detail({identity, enabled, revision}) : catalog([{...item, identity, enabled}], revision);
    }});
    view.reset(root); view.open(); await settled();
    el('skills-list').children[0].children.at(-1).dispatchEvent(new Event('click')); await settled();
    assert.equal(el('skill-body').textContent, detail().body); assert.equal(el('skill-body').children.length, 0); assert.equal(tags.includes('script') || tags.includes('img'), false);
    assert.equal(el('skill-enable').disabled, true); el('skill-confirmed').checked = true; fire('skill-confirmed', 'change'); assert.equal(el('skill-enable').disabled, false);
    fire('skill-enable'); await settled(); assert.equal(el('skill-confirmed').checked, false); assert.equal(el('skill-choose').disabled, false);
    fire('skill-choose'); assert.match(el('skill-selection').textContent, /下一次任务 · 简明报告/); assert.equal(el('skills-dialog').open, false);
    pending = {skill: view.selection()}; view.refresh(); fire('skill-clear'); assert.equal(view.selection().id, item.id); assert.match(el('skill-selection').textContent, /待确认的原请求/);
    pending = null; view.consume(); assert.equal(el('skill-selection').textContent, '本次任务不附加 Skill');
    view.open(); await settled(); el('skill-confirmed').checked = true; identity = '9'.repeat(64); revision++; fire('skills-reload'); await settled();
    assert.equal(el('skill-detail').hidden, true); assert.equal(el('skill-confirmed').checked, false);
    view.reset(otherRoot); assert.equal(el('skill-body').textContent, ''); assert.equal(view.selection(), null);
    assert.equal(calls.filter(call => call.method === 'POST').length, 1);
  } finally {if (previous === undefined) delete globalThis.document; else globalThis.document = previous;}
});
