import test from 'node:test';
import assert from 'node:assert/strict';
import {ShutdownSession} from '../../astra_harness/static/server-control.mjs';

const running = {supported: true, instance_id: 'a'.repeat(32), state: 'running'};
const stopping = {...running, state: 'stopping'};
const deferred = () => { let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve}; };

test('inspect and cancel never send shutdown; late inspection cannot reopen confirmation', async () => {
  const gate = deferred(), calls = [];
  const session = new ShutdownSession({send: (...args) => {calls.push(args); return gate.promise;}});
  const pending = session.inspect(); session.close(); gate.resolve(running); await pending;
  assert.equal(session.state.phase, 'idle'); assert.deepEqual(calls, [['/api/server']]);
  await session.confirm(); assert.equal(calls.length, 1);
});

test('double click sends one bound request and success waits for disconnect', async () => {
  const gate = deferred(), calls = []; let accepted = 0;
  const session = new ShutdownSession({wait: async () => {}, onAccepted: () => {accepted++;}, send: async (path, method, body) => {
    calls.push({path, method, body});
    if (method === 'POST') return gate.promise;
    if (accepted) throw new TypeError('offline');
    return running;
  }});
  await session.inspect(); const pending = session.confirm(); await session.confirm();
  assert.equal(session.state.phase, 'sending'); assert.equal(accepted, 0);
  gate.resolve(stopping); await pending;
  assert.equal(session.state.phase, 'disconnected'); assert.equal(accepted, 1);
  assert.deepEqual(calls.filter(call => call.method === 'POST'), [{path: '/api/server/shutdown', method: 'POST',
    body: {instance_id: running.instance_id, confirmed: true}}]);
});

test('busy server leaves workbench usable and needs a new confirmation', async () => {
  let accepted = false, posts = 0;
  const session = new ShutdownSession({onAccepted: () => {accepted = true;}, send: async (path, method) => {
    if (method === 'POST') {posts++; throw Object.assign(new Error('先暂停任务'), {code: 'server_busy'});}
    return running;
  }});
  await session.inspect(); await session.confirm();
  assert.equal(session.state.phase, 'confirm'); assert.equal(session.state.error, '先暂停任务');
  assert.equal(posts, 1); assert.equal(accepted, false);
});

test('lost acknowledgement stays uncertain without automatic retry or false success', async () => {
  let accepted = false, posts = 0;
  const session = new ShutdownSession({onAccepted: () => {accepted = true;}, send: async (path, method) => {
    if (method === 'POST') {posts++; throw new TypeError('lost response');}
    return running;
  }});
  await session.inspect(); await session.confirm(); await session.confirm();
  assert.equal(session.state.phase, 'uncertain'); assert.equal(accepted, false); assert.equal(posts, 1);
  await session.inspect(); assert.equal(session.state.phase, 'confirm'); assert.equal(posts, 1);
});

test('slow cleanup stays visible and polling is bounded', async () => {
  let reads = 0;
  const session = new ShutdownSession({wait: async () => {}, send: async (path, method) => {
    if (method === 'POST') return stopping;
    return ++reads === 1 ? running : stopping;
  }});
  await session.inspect(); await session.confirm();
  assert.equal(session.state.phase, 'slow'); assert.equal(reads, 9);
});

test('restarted instance is never shut down by status polling', async () => {
  const calls = [];
  const session = new ShutdownSession({wait: async () => {}, send: async (path, method) => {
    calls.push(method);
    if (method === 'POST') return stopping;
    return calls.length === 1 ? running : {...running, instance_id: 'b'.repeat(32)};
  }});
  await session.inspect(); await session.confirm();
  assert.equal(session.state.phase, 'restarted'); assert.equal(calls.filter(method => method === 'POST').length, 1);
});

test('unsupported host does not offer confirmation', async () => {
  const session = new ShutdownSession({send: async () => ({supported: false})});
  await session.inspect(); await session.confirm(); assert.equal(session.state.phase, 'unavailable');
});

test('another tab can observe already accepted shutdown without another POST', async () => {
  const calls = []; let accepted = 0;
  const session = new ShutdownSession({wait: async () => {}, onAccepted: () => {accepted++;}, send: async (path, method) => {
    calls.push(method); if (calls.length > 1) throw new TypeError('offline'); return stopping;
  }});
  await session.inspect(); assert.equal(accepted, 1); assert.equal(session.state.phase, 'disconnected');
  assert.equal(calls.filter(method => method === 'POST').length, 0);
});
