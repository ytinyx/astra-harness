// Event replay stays independent from rendering, so reconnects can be tested without a browser.
export const terminal = new Set(['completed', 'failed', 'cancelled', 'unknown']);
export const statusLabels = {
  pending: '等待执行', running: '正在执行', waiting_approval: '等待批准', paused: '已暂停',
  completed: '已完成', failed: '未完成', cancelled: '已取消', unknown: '结果待核查',
};

export function findActiveRun(run, runs = []) {
  return runs.find(item => !terminal.has(item.status)) || (run && !terminal.has(run.status) ? run : null);
}

export function runControls(run, {runs = [], busy = false, loading = false, archived = false, blocked = false} = {}) {
  // A paused run still owns its conversation. Looking at an old result must not
  // unlock submission or lose the controls for the unfinished run.
  const active = findActiveRun(run, runs), locked = busy || loading;
  const ongoing = !!run && !terminal.has(run.status);
  const pausePending = ongoing && run.status !== 'paused' && !!run.pause_requested;
  return {
    active, pausePending,
    canSubmit: !locked && !active && !archived && !blocked,
    canSelectHistory: !!runs.length && !locked && !active,
    canPause: !locked && ongoing && run.status !== 'paused' && !pausePending,
    canResume: !locked && run?.status === 'paused',
    canCancel: !locked && ongoing,
    canApprove: !locked && run?.status === 'waiting_approval' && !pausePending,
  };
}

export function createRunView() {
  return {lastSeq: 0, steps: new Map(), tools: new Map(), events: [], metrics: null};
}

export function applyEvent(view, event) {
  if (!Number.isSafeInteger(event.seq) || event.seq <= view.lastSeq) return false;
  view.lastSeq = event.seq;
  if (event.type !== 'model.delta') {
    view.events.push(event);
    if (view.events.length > 120) view.events.shift();
  }
  if (event.type === 'model.start') {
    if (!view.steps.has(event.step)) view.steps.set(event.step, {text: '', complete: false});
  } else if (event.type === 'model.delta') {
    const step = view.steps.get(event.step) || {text: '', complete: false};
    if (!step.complete && typeof event.text === 'string') step.text += event.text;
    view.steps.set(event.step, step);
  } else if (event.type === 'model.end') {
    // The final response replaces accumulated chunks; appending would duplicate the answer.
    view.steps.set(event.step, {text: event.text || '', complete: true, metrics: event.metrics || null});
    view.metrics = event.metrics || view.metrics;
    for (const call of event.tool_calls || []) {
      view.tools.set(call.call_id, {...view.tools.get(call.call_id), ...call, status: 'pending'});
    }
  } else if (event.type === 'tool.start') {
    view.tools.set(event.call_id, {...view.tools.get(event.call_id), call_id: event.call_id,
      name: event.name, status: 'running'});
  } else if (event.type === 'tool.end' && event.result) {
    const result = event.result;
    view.tools.set(result.call_id, {...view.tools.get(result.call_id), call_id: result.call_id,
      name: result.name, result, status: toolResultStatus(result)});
  } else if (event.type === 'run.waiting' && event.approval) {
    const call = event.approval;
    view.tools.set(call.call_id, {...view.tools.get(call.call_id), ...call, status: 'waiting_approval'});
  } else if (event.type === 'approval.decision' && event.approved === false) {
    // A rejection has no tool.start/end. Close only the pending approval card,
    // retaining the request as history without inventing a tool execution.
    for (const [id, call] of view.tools) {
      if (call.status === 'waiting_approval') view.tools.set(id, {...call, status: 'rejected'});
    }
  }
  return true;
}

export function latestText(view) {
  const keys = [...view.steps.keys()].sort((a, b) => a - b);
  return keys.length ? view.steps.get(keys[keys.length - 1]).text : '';
}

export function runMetrics(run, view) {
  // Events arrive before the next database checkpoint. Use their observed step
  // number without pretending an unfinished request already has measured latency.
  const steps = [...view.steps.keys()].filter(step => Number.isSafeInteger(step) && step >= 0);
  const modelCalls = Math.max(run.model_calls || 0, ...steps, 0);
  const observedTools = [...view.tools.values()].filter(call => ['running', 'completed', 'failed'].includes(call.status)).length;
  const toolCalls = Math.max(run.tool_calls || 0, observedTools);
  const durations = [...view.steps.values()].map(step => step.metrics?.duration_ms)
    .filter(value => Number.isFinite(value) && value >= 0);
  const stored = run.metrics?.last_model && Number.isFinite(run.metrics.model_duration_ms)
    ? run.metrics.model_duration_ms : null;
  const durationMs = stored === null && !durations.length ? null
    : Math.max(stored || 0, durations.reduce((sum, value) => sum + value, 0));
  return {modelCalls, toolCalls, durationMs};
}

export function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds)) return '未报告';
  return milliseconds < 10 ? '<0.01 秒' : `${(milliseconds / 1000).toFixed(2)} 秒`;
}

export function toolLabel(name = '') {
  return ({'files.list': '浏览文件', 'files.read': '读取文件', 'files.write': '写入文件',
    'files.search': '搜索文件', 'files.read_range': '读取文件片段', 'files.inspect': '检查文件版本',
    'files.edit': '精确编辑文件', 'files.restore': '从备份恢复文件', 'commands.run': '运行本地命令', add: '计算加法'})[name] || name;
}

export function commandFailed(result) {
  return result?.name === 'commands.run' && !result.error_code && result.value?.ok === false;
}

export function toolResultStatus(result) {
  if (result?.error_code === 'execution_unknown') return 'unknown';
  return result?.error_code || commandFailed(result) ? 'failed' : 'completed';
}

export function reusableSubmission(previous, prompt, sessionId, makeId) {
  if (previous && previous.prompt === prompt && previous.session_id === sessionId) return previous;
  return {prompt, session_id: sessionId, request_id: makeId()};
}

export function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return '—';
  return bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
}
