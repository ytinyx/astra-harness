import test from 'node:test';
import assert from 'node:assert/strict';
import {applyEvent, createRunView, toolLabel, toolResultStatus, commandFailed, runMetrics} from '../../astra_harness/static/state.mjs';

test('command nonzero is a known failed command while arbitrary domain ok fields are not reinterpreted', () => {
  const failure = {name: 'commands.run', value: {ok: false, exit_code: 7, stdout: 'partial work'}, error_code: null};
  assert.equal(commandFailed(failure), true); assert.equal(toolResultStatus(failure), 'failed');
  assert.equal(commandFailed({...failure, name: 'business.lookup'}), false);
  assert.equal(toolResultStatus({...failure, name: 'business.lookup'}), 'completed');
  assert.equal(commandFailed({...failure, value: {ok: true, exit_code: 0}}), false);
});

test('unknown event retains bounded observations without a successful tool status', () => {
  const result = {call_id: 'cmd', name: 'commands.run', error_code: 'execution_unknown', value: {reason: 'timeout', stdout: '<script>text only</script>', cleanup_complete: false}};
  const view = createRunView();
  applyEvent(view, {seq: 1, type: 'tool.start', call_id: 'cmd', name: result.name});
  applyEvent(view, {seq: 2, type: 'tool.end', result});
  assert.equal(view.tools.get('cmd').status, 'unknown');
  assert.deepEqual(view.tools.get('cmd').result.value, result.value);
  assert.equal(commandFailed(result), false);
});

test('known rejections stay failed and new built-in names are readable', () => {
  assert.equal(toolResultStatus({error_code: 'edit_rejected', value: {reason: 'stale_file', applied: false}}), 'failed');
  assert.equal(toolLabel('files.inspect'), '检查文件版本');
  assert.equal(toolLabel('files.edit'), '精确编辑文件');
  assert.equal(toolLabel('files.restore'), '从备份恢复文件');
  assert.equal(toolLabel('commands.run'), '运行本地命令');
});

test('approval rejection closes the waiting card without inventing execution or losing the original request', () => {
  const view = createRunView(), approval = {call_id: 'restore', name: 'files.restore', arguments: {backup_id: 'a'.repeat(64), expected_sha256: 'b'.repeat(64)}};
  const waiting = {seq: 1, type: 'run.waiting', approval};
  applyEvent(view, waiting);
  applyEvent(view, {seq: 2, type: 'approval.decision', approved: false});
  const card = view.tools.get('restore');
  assert.equal(card.status, 'rejected'); assert.deepEqual(card.arguments, approval.arguments);
  assert.equal(card.result, undefined); assert.equal(runMetrics({tool_calls: 0}, view).toolCalls, 0);
  assert.equal(applyEvent(view, waiting), false);
  assert.equal(view.tools.get('restore').status, 'rejected');
  assert.deepEqual(view.events.map(event => event.type), ['run.waiting', 'approval.decision']);
});
