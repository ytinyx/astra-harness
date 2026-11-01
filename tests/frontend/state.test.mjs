import test from 'node:test';
import assert from 'node:assert/strict';
import {createRunView, applyEvent, latestText, reusableSubmission, formatSize, runMetrics, formatDuration, runControls, terminal} from '../../astra_harness/static/state.mjs';

test('SSE reconnect replay does not repeat text', () => {
  const view = createRunView();
  applyEvent(view, {seq: 1, type: 'model.start', step: 1});
  const delta = {seq: 2, type: 'model.delta', step: 1, text: '你好'};
  assert.equal(applyEvent(view, delta), true);
  assert.equal(applyEvent(view, delta), false);
  assert.equal(latestText(view), '你好');
  applyEvent(view, {seq: 3, type: 'model.delta', step: 1, text: '，世界'});
  assert.equal(latestText(view), '你好，世界');
});

test('final response replaces partial output and ignores a late delta', () => {
  const view = createRunView();
  applyEvent(view, {seq: 1, type: 'model.delta', step: 2, text: '已'});
  applyEvent(view, {seq: 2, type: 'model.end', step: 2, text: '已完成', tool_calls: []});
  applyEvent(view, {seq: 3, type: 'model.delta', step: 2, text: '完成'});
  assert.equal(latestText(view), '已完成');
  applyEvent(view, {seq: 4, type: 'model.start', step: 3});
  assert.equal(latestText(view), '');
});

test('tool arguments and results join by call id rather than order', () => {
  const view = createRunView();
  applyEvent(view, {seq: 1, type: 'model.end', step: 1, tool_calls: [
    {call_id: 'a', name: 'files.read', arguments: {path: 'a.md'}},
    {call_id: 'b', name: 'files.read', arguments: {path: 'b.md'}},
  ]});
  applyEvent(view, {seq: 2, type: 'tool.end', result: {call_id: 'b', name: 'files.read', value: 'second'}});
  assert.equal(view.tools.get('b').arguments.path, 'b.md');
  assert.equal(view.tools.get('b').result.value, 'second');
  assert.equal(view.tools.get('a').status, 'pending');
});

test('approval event retains the actual proposed write', () => {
  const view = createRunView();
  applyEvent(view, {seq: 1, type: 'run.waiting', approval: {call_id: 'x', name: 'files.write', arguments: {path: 'out.json', content: '{}'}}});
  assert.equal(view.tools.get('x').status, 'waiting_approval');
  applyEvent(view, {seq: 2, type: 'tool.start', call_id: 'x', name: 'files.write'});
  assert.equal(view.tools.get('x').arguments.content, '{}');
  assert.equal(view.tools.get('x').status, 'running');
});

test('network retry reuses id, while new prompt or conversation gets a new id', () => {
  let ids = 0;
  const first = reusableSubmission(null, '任务', null, () => String(++ids));
  assert.equal(reusableSubmission(first, '任务', null, () => String(++ids)), first);
  assert.equal(ids, 1);
  assert.equal(reusableSubmission(first, '任务', 'other', () => String(++ids)).request_id, '2');
  assert.equal(reusableSubmission(first, '新任务', null, () => String(++ids)).request_id, '3');
});

test('unknown byte size is not presented as measured zero', () => {
  assert.equal(formatSize(undefined), '—');
  assert.equal(formatSize(0), '0 B');
  assert.equal(formatSize(2048), '2.0 KB');
});

test('event log remains bounded while cursor continues forward', () => {
  const view = createRunView();
  for (let seq = 1; seq <= 500; seq++) applyEvent(view, {seq, type: 'run.status', status: 'running'});
  assert.equal(view.events.length, 120);
  assert.equal(view.lastSeq, 500);
  assert.equal(applyEvent(view, {seq: -1, type: 'run.status'}), false);
});

test('live model start advances displayed count before the checkpoint without inventing latency', () => {
  const view = createRunView();
  const run = {model_calls: 0, metrics: {model_duration_ms: 0, last_model: null}};
  applyEvent(view, {seq: 1, type: 'model.start', step: 1});
  assert.deepEqual(runMetrics(run, view), {modelCalls: 1, toolCalls: 0, durationMs: null});
  assert.equal(formatDuration(null), '未报告');
  applyEvent(view, {seq: 2, type: 'model.end', step: 1, text: '完成', metrics: {duration_ms: 4}});
  assert.deepEqual(runMetrics(run, view), {modelCalls: 1, toolCalls: 0, durationMs: 4});
  assert.equal(formatDuration(4), '<0.01 秒');
});

test('replayed timing does not double count the stored aggregate', () => {
  const view = createRunView();
  applyEvent(view, {seq: 1, type: 'model.end', step: 1, metrics: {duration_ms: 140}});
  applyEvent(view, {seq: 2, type: 'model.end', step: 2, metrics: {duration_ms: 200}});
  const run = {model_calls: 2, metrics: {model_duration_ms: 340, last_model: {duration_ms: 200}}};
  assert.deepEqual(runMetrics(run, view), {modelCalls: 2, toolCalls: 0, durationMs: 340});
  assert.equal(formatDuration(340), '0.34 秒');
});

test('pause request remains active until its checkpoint and still allows cancellation', () => {
  const running = {id: 'current', status: 'running'};
  assert.equal(runControls(running).canPause, true);
  const requesting = runControls({...running, pause_requested: true});
  assert.equal(requesting.pausePending, true);
  assert.equal(requesting.canPause, false);
  assert.equal(requesting.canResume, false);
  assert.equal(requesting.canCancel, true);
  assert.equal(requesting.canSubmit, false);
  assert.equal(terminal.has('paused'), false);
});

test('paused approval cannot be approved until resume restores the approval state', () => {
  const approval = {call_id: 'write', arguments: {path: 'result.json'}};
  const paused = {id: 'current', status: 'paused', resume_status: 'waiting_approval', approval};
  const stopped = runControls(paused, {runs: [paused]});
  assert.equal(stopped.canResume, true);
  assert.equal(stopped.canCancel, true);
  assert.equal(stopped.canPause, false);
  assert.equal(stopped.canApprove, false);
  assert.equal(stopped.canSubmit, false);
  assert.equal(stopped.canSelectHistory, false);
  assert.equal(runControls({...paused, status: 'waiting_approval', resume_status: null}).canApprove, true);
  assert.equal(runControls({...paused, status: 'cancelled', approval: null}).canSubmit, true);
});

test('an unfinished conversation run still blocks submission when an old result is selected', () => {
  const old = {id: 'old', status: 'completed'}, paused = {id: 'new', status: 'paused'};
  const controls = runControls(old, {runs: [paused, old]});
  assert.equal(controls.active.id, 'new');
  assert.equal(controls.canSelectHistory, false);
  assert.equal(controls.canSubmit, false);
  assert.equal(runControls(old, {runs: [old]}).canSubmit, true);
});

test('selection loading and pending requests keep runtime controls locked', () => {
  for (const lock of [{loading: true}, {busy: true}]) {
    assert.equal(runControls({status: 'paused'}, lock).canResume, false);
    assert.equal(runControls({status: 'running'}, lock).canPause, false);
    assert.equal(runControls({status: 'waiting_approval'}, lock).canApprove, false);
    assert.equal(runControls({status: 'paused'}, lock).canCancel, false);
    assert.equal(runControls({status: 'completed'}, lock).canSubmit, false);
  }
});

test('proposed tools do not consume displayed budget until execution starts', () => {
  const view = createRunView();
  applyEvent(view, {seq: 1, type: 'model.end', step: 1, tool_calls: [
    {call_id: 'a', name: 'files.read'}, {call_id: 'b', name: 'files.write'},
  ]});
  assert.equal(runMetrics({tool_calls: 0}, view).toolCalls, 0);
  applyEvent(view, {seq: 2, type: 'tool.start', call_id: 'a', name: 'files.read'});
  assert.equal(runMetrics({tool_calls: 0}, view).toolCalls, 1);
  applyEvent(view, {seq: 3, type: 'tool.end', result: {call_id: 'a', name: 'files.read', value: 'text'}});
  applyEvent(view, {seq: 4, type: 'run.waiting', approval: {call_id: 'b', name: 'files.write'}});
  assert.equal(runMetrics({tool_calls: 1}, view).toolCalls, 1);
  applyEvent(view, {seq: 5, type: 'tool.start', call_id: 'b', name: 'files.write'});
  assert.equal(runMetrics({tool_calls: 1}, view).toolCalls, 2);
});
