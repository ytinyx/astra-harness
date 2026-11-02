import {createKnowledgeView} from './knowledge.mjs';
import {createWorkspaceView} from './workspace.mjs';
import {createModelProfilesView} from './model-profiles.mjs';
import {createMCPProfilesView} from './mcp-profiles.mjs';
import {mcpProfileError} from './mcp-profiles-state.mjs';
import {createLocalSkillsView} from './local-skills.mjs';
import {createServerControl} from './server-control.mjs';
import {skillError, skillRunLabel} from './local-skills-state.mjs';
import {sessionModelMismatch, modelProfileError} from './model-profiles-state.mjs';
import {scopedSubmission, workspaceQuery} from './workspace-state.mjs';
import {renderMarkdown} from './markdown.mjs';
import {terminal, statusLabels, createRunView, applyEvent, latestText, toolLabel, commandFailed, formatSize, runMetrics, formatDuration, findActiveRun, runControls} from './state.mjs';

const $ = id => document.getElementById(id);
const shell = document.querySelector('.app-shell');
const state = {info: null, sessions: [], session: null, run: null, view: createRunView(),
  archived: false, busy: false, loadingSelection: false, generation: 0, controller: new AbortController(), source: null,
  pendingSubmission: null, filePath: '', fileContent: '', fileGeneration: 0, previewKey: null, previewLoadingKey: null,
  terminalLoaded: null, renderPending: false, workspaceInfo: null, workspaceEpoch: 0, sessionsRevision: 0, modelProfiles: null};
let workspace, profiles, mcpProfiles, skills;
const workspaceId = () => state.workspaceInfo?.selected_id;
const modelIdentity = () => state.modelProfiles?.selected_identity || 'startup';
const modelReady = () => !!state.modelProfiles && !profiles?.locked();
const storageKey = name => `astra.${name}.${workspaceId()}`;
const scopeReady = () => !!workspaceId() && !workspace?.locked();
const saved = key => { try { return localStorage.getItem(key); } catch { return null; } };
function remember(key, value) { try { if (value) localStorage.setItem(key, value); else localStorage.removeItem(key); } catch {} }
function persistSubmission() {
  try {
    if (state.pendingSubmission) sessionStorage.setItem('astra.pendingSubmission', JSON.stringify(state.pendingSubmission));
    else sessionStorage.removeItem('astra.pendingSubmission');
  } catch {}
}
function node(tag, text, className) {
  const value = document.createElement(tag);
  if (text !== undefined) value.textContent = text;
  if (className) value.className = className;
  return value;
}
function button(text, className, handler) {
  const value = node('button', text, className); value.type = 'button'; value.addEventListener('click', handler); return value;
}
function showError(message = '') { $('error').textContent = message; $('error').hidden = !message; }
function report(error) { if (error.name !== 'AbortError') showError(error.message); }
let toastTimer;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => {$('toast').hidden = true;}, 2800); }
async function api(path, method = 'GET', body, signal) {
  if (state.serverStopping) throw new DOMException('服务正在退出', 'AbortError');
  const options = {method, headers: {}, signal};
  if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('服务响应无法读取，请检查本地服务。');
  }
  if (!response.ok) {
    const labels = {conflict: '当前状态不允许这项操作。请检查活动任务、归档状态或待核查的操作。',
      invalid_request: '请求无法处理，请检查输入、文件类型或路径。', not_found: '记录或文件不存在。',
      internal_error: '服务暂时无法完成请求，请查看本地服务状态。', body_too_large: '内容超过允许大小。',
      invalid_origin: '页面来源校验失败，请通过本地工作台地址打开。',
      workspace_disabled: '文件工作区已禁用。', invalid_workspace: '请输入后端电脑上已存在的绝对目录路径。',
      protected_workspace: '该目录属于受保护位置，不能作为工作区。', workspace_overlap: '目录与已有工作区重叠，请选择互不包含的目录。',
      workspace_changed: '目录身份已变化。请选择另一个可用目录，或恢复原目录后刷新工作区；同路径的新目录不能替代原绑定。', workspace_conflict: '工作区已被其他页面修改，请确认当前工作区后重新校验。',
      workspace_unavailable: '工作区目录当前不可访问。', workspace_not_selected: '当前工作区已变化，请确认当前工作区。',
      workspace_not_configured: '此工作区未配置这项集成。', workspace_identity_unavailable: '无法确认目录身份，请选择其他目录。',
      workspace_session_mismatch: '此对话属于另一个工作区，请在当前工作区新建对话。',
      model_session_mismatch: '此对话绑定了另一模型配置。请新建对话，不会把旧历史发送到新模型。',
      model_selection_changed: '模型选择已变化，请重新读取模型配置并核对后再发送。',
      model_profile_conflict: '模型配置已变化，请重新读取后核对。',
      model_profile_not_found: '模型配置不存在，请重新读取列表。',
      model_missing_key: '后端缺少配置指定的密钥环境变量；页面不接收密钥值。',
      invalid_model_profile: '模型配置无法保存。请检查基础地址、模型名、环境变量名和数值范围；不要填写密钥值。',
      model_profile_not_selected: '当前模型选择已变化。请打开模型配置重新读取，核对后再发送。',
      model_profile_changed: '本次运行绑定的模型或凭据已变化，不能沿用旧授权。请核对配置并新建任务。',
      model_profile_in_use: '配置仍在运行或测试中，请等待结束后再操作。',
      model_profile_unavailable: '后端当前无法读取或使用模型配置，请检查本地服务。',
      model_profiles_disabled: '当前宿主未启用模型配置管理。'};
    Object.assign(labels, {
      knowledge_invalid_file: '请选择有效的 UTF-8 .md / .txt 文件，大小为 1 至 128,000 字节，文件名不能包含路径或特殊字符。',
      knowledge_invalid_collection: '知识库名称无效，请填写 1 至 64 个字符。',
      knowledge_file_exists: '已有同名文件且内容不同，请先重命名要上传的文件。原文件未被覆盖。',
      knowledge_limit: '知识库数量或材料容量达到上限，请整理材料后重试。',
      knowledge_conflict: '知识库配置已变化，请刷新列表后核对。待生效的语义配置需要先重启。',
      knowledge_unavailable: '知识库暂时无法保存，请检查本地目录或服务状态，再刷新核对。',
      runtime_update_busy: '请先完成或取消正在执行、暂停、待审批的任务，再新建知识库。'
    });
    const error = new Error(skillError(data.error) || mcpProfileError(data.error, path) || modelProfileError(data.error) || (Object.hasOwn(labels, data.error) ? labels[data.error] : `请求失败（${response.status}）。`));
    error.status = response.status; error.code = data.error; throw error;
  }
  return data;
}
function disconnect() { if (state.source) state.source.close(); state.source = null; }
function selection() {
  disconnect(); state.controller.abort(); state.controller = new AbortController();
  state.generation++; state.previewKey = null; state.terminalLoaded = null;
  return state.generation;
}
function activeRun() { return findActiveRun(state.run, state.session?.runs || []); }
function controlState() {
  return runControls(state.run, {runs: state.session?.runs || [], busy: state.busy || !!workspace?.locked(), loading: state.loadingSelection,
    archived: !!state.session?.archived, blocked: !scopeReady() || !modelReady() || !!mcpProfiles?.locked() || (!state.pendingSubmission && (!!skills?.locked() || !!skills?.invalid() || sessionModelMismatch(state.session, modelIdentity()))) || (state.info?.workspace_available === false && !state.pendingSubmission) || (!!state.session?.workspace_id && state.session.workspace_id !== workspaceId()) || !!state.info?.unresolved_runs?.length});
}
function controls() {
  const policy = controlState(), active = policy.active, blocked = !!state.info?.unresolved_runs?.length;
  $('send').disabled = !policy.canSubmit;
  $('prompt').disabled = state.busy || state.loadingSelection || !!state.session?.archived || !scopeReady() || !modelReady() || !!mcpProfiles?.locked() || sessionModelMismatch(state.session, modelIdentity()) || !!state.pendingSubmission || state.info?.workspace_available === false;
  $('send').textContent = state.pendingSubmission ? '确认上次提交' : '↑';
  $('send').setAttribute('aria-label', state.pendingSubmission ? '确认上次提交' : '发送任务');
  $('send').classList.toggle('pending-submission', !!state.pendingSubmission);
  $('cancel').hidden = !state.run || terminal.has(state.run.status);
  $('cancel').disabled = !policy.canCancel;
  $('pause').hidden = !state.run || terminal.has(state.run.status) || state.run.status === 'paused';
  $('pause').disabled = !policy.canPause;
  $('pause').textContent = policy.pausePending ? '暂停中…' : '暂停';
  $('resume').hidden = state.run?.status !== 'paused';
  $('resume').disabled = !policy.canResume;
  $('run-select').disabled = !policy.canSelectHistory;
  $('new-session').disabled = state.busy || !!state.pendingSubmission || !scopeReady();
  $('open-knowledge').disabled = $('open-knowledge-top').disabled = !scopeReady();
  $('refresh-files').disabled = !scopeReady() || !state.workspaceInfo?.enabled;
  workspace?.refresh();
  profiles?.refresh();
  mcpProfiles?.refresh();
  skills?.refresh();
  $('open-skills').disabled = !scopeReady();
  $('model-session-notice').hidden = !sessionModelMismatch(state.session, modelIdentity());
  $('model-new-session').disabled = state.busy || !!state.pendingSubmission || !scopeReady();
  for (const id of ['approve', 'reject']) $(id).disabled = !policy.canApprove;
  $('resolve').disabled = state.busy || state.loadingSelection;
  $('prompt').placeholder = state.loadingSelection ? '正在加载所选对话…' : state.info?.workspace_available === false ? '当前目录不可用。请选择其他目录，或恢复原目录后刷新工作区。' : state.session?.archived ? '此对话已归档，恢复后可继续。' : blocked ? '请先核查上方提示的未知操作，再提交新任务。' : active?.status === 'paused' ? '当前任务已暂停，继续或停止运行后再发送…' : active ? '可以先写下下一条消息，当前任务完成后发送…' : '描述你想完成的任务…';
}
function panel(name, open = true) {
  for (const item of ['files', 'activity', 'tools']) {
    $('tab-' + item).setAttribute('aria-selected', String(item === name)); $('panel-' + item).hidden = item !== name;
  }
  if (open) { shell.classList.remove('hide-inspector'); shell.classList.add('inspector-open'); $('inspector-toggle').setAttribute('aria-expanded', 'true'); }
}
function closeInspector() { shell.classList.add('hide-inspector'); shell.classList.remove('inspector-open'); $('inspector-toggle').setAttribute('aria-expanded', 'false'); }
const sidebarMedia = matchMedia('(max-width:700px)');
let sidebarReturnFocus = null;
function sidebar(open, {restoreFocus = false, trigger = null} = {}) {
  const wasOpen = shell.classList.contains('sidebar-open');
  open = !!open && sidebarMedia.matches;
  if (open && !wasOpen) sidebarReturnFocus = trigger || document.activeElement;
  shell.classList.toggle('sidebar-open', open); $('mobile-backdrop').hidden = !open;
  for (const id of ['sidebar-toggle', 'knowledge-menu']) $(id).setAttribute('aria-expanded', String(open));
  if (open && !wasOpen) $('sidebar-close').focus();
  else if (!open && wasOpen && restoreFocus && sidebarReturnFocus?.isConnected !== false && !sidebarReturnFocus?.disabled) sidebarReturnFocus?.focus();
}
// A drawer opened on a narrow screen must not survive a desktop resize as an
// invisible backdrop or stale aria-expanded state.
sidebarMedia.addEventListener?.('change', () => { if (!sidebarMedia.matches) sidebar(false); });

async function refreshSessions() {
  const id = workspaceId(), epoch = state.workspaceEpoch, revision = ++state.sessionsRevision;
  const [sessions, info] = await Promise.all([api(workspaceQuery('/api/sessions?archived=1', id)), api('/api/info')]);
  if (id !== workspaceId() || epoch !== state.workspaceEpoch || revision !== state.sessionsRevision) return;
  state.sessions = sessions; state.info = info; renderSessions(); renderCapabilities(); renderBlocked(); renderModelMode(); controls();
}
function renderBlocked() {
  const runs = state.info?.unresolved_runs || [], notice = $('global-notice');
  notice.hidden = !runs.length; notice.replaceChildren();
  if (!runs.length) return;
  notice.append(node('strong', '有操作尚未完成核查，新任务暂时停止提交。'));
  notice.append(node('p', '先检查实际结果并记录结论，原操作不会自动重试。'));
  for (const run of runs) notice.append(button('查看待核查运行 ' + run.id.slice(0, 8) + ' →', 'text-button', async () => {
    try { await loadSession(run.session_id); if (state.run?.id !== run.id) await selectRun(run.id); scrollBottom(); } catch (error) { report(error); }
  }));
}
function renderSessions() {
  const list = $('sessions'); list.replaceChildren();
  const visible = state.sessions.filter(item => !!item.archived === state.archived);
  for (const item of visible) {
    const row = node('div', undefined, 'session-row' + (state.session?.id === item.id ? ' selected' : ''));
    const main = button('', 'session-main', () => { if (!state.busy) loadSession(item.id).catch(report); });
    main.append(node('span', '◷', 'session-icon'), node('span', item.title || '未命名对话', 'session-name'));
    main.title = item.title || '未命名对话';
    const more = button('⋯', 'session-menu', () => sessionMenu(item)); more.setAttribute('aria-label', `管理对话：${item.title || '未命名对话'}`);
    row.append(main, more); list.append(row);
  }
  $('session-empty').hidden = visible.length > 0;
  $('session-empty').textContent = state.archived ? '暂无已归档的对话。' : '还没有对话，从一个想完成的任务开始。';
  $('archive-toggle').textContent = state.archived ? '返回对话' : '查看归档';
  $('archive-toggle').setAttribute('aria-pressed', String(state.archived));
}
function message(role, text) {
  const article = node('article', undefined, 'message ' + role), avatar = node('div', role === 'user' ? '你' : '✦', 'avatar ' + (role === 'user' ? 'user-avatar' : 'astra-avatar'));
  const content = node('div', undefined, 'message-content'); content.append(node('div', role === 'user' ? '你' : 'Astra', 'message-heading'));
  const body = node('div', undefined, role === 'user' ? 'user-text' : 'markdown');
  if (role === 'user') body.textContent = text; else renderMarkdown(body, text);
  content.append(body); article.append(avatar, content); return article;
}
function renderMessages() {
  $('messages').replaceChildren();
  for (const item of state.session?.messages || []) {
    if (['user', 'assistant'].includes(item.role)) $('messages').append(message(item.role, item.text || item.content || ''));
  }
  if (state.run && state.run.status !== 'completed') {
    const current = message('user', state.run.prompt || ''); current.id = 'current-prompt';
    if (terminal.has(state.run.status)) current.querySelector('.message-heading').append(node('span', '历史运行 · ' + (statusLabels[state.run.status] || state.run.status)));
    $('messages').append(current);
  }
  $('intro').hidden = $('messages').childElementCount > 0 || !!state.run;
  $('conversation-title').textContent = state.session?.title || '新对话';
  $('archived-badge').hidden = !state.session?.archived;
  const path = state.run?.workspace_path || state.session?.workspace_path;
  $('conversation-workspace').hidden = !path;
  $('conversation-workspace').textContent = path ? '本次对话目录 · ' + path : '';
  const boundModel = state.run?.model_snapshot || state.session?.model_snapshot;
  $('conversation-model').hidden = !state.run && !state.session;
  $('conversation-model').textContent = boundModel ? `本次对话模型 · ${boundModel.name || boundModel.model} · ${boundModel.model}` : '本次对话模型 · 启动配置';
}
function scrollBottom() { const target = $('chat-scroll'); target.scrollTop = target.scrollHeight; }
function renderRunOptions() {
  const select = $('run-select'); select.replaceChildren();
  const runs = state.session?.runs || [];
  if (!runs.length) select.append(node('option', '暂无运行'));
  for (const run of runs) {
    const option = node('option', `${statusLabels[run.status] || run.status} · ${(run.prompt || run.id).slice(0, 34)}`);
    option.value = run.id; select.append(option);
  }
  select.value = state.run?.id || ''; select.disabled = !controlState().canSelectHistory;
}
function updateRun(run) {
  state.run = {...state.run, ...run};
  const runs = state.session?.runs;
  if (runs) { const index = runs.findIndex(item => item.id === run.id); if (index < 0) runs.unshift(state.run); else runs[index] = state.run; }
  renderRun(); controls();
}
function renderLive() {
  const run = state.run, text = latestText(state.view);
  const visible = !!run && run.status !== 'completed' && (!terminal.has(run.status) || !!text);
  $('live-message').hidden = !visible;
  if (visible) {
    const nearBottom = $('chat-scroll').scrollHeight - $('chat-scroll').scrollTop - $('chat-scroll').clientHeight < 130;
    renderMarkdown($('live-text'), text);
    $('live-phase').textContent = terminal.has(run.status) ? '本次未完成的输出' : run.status === 'paused' ? '已暂停，可从当前位置继续' : run.status === 'waiting_approval' ? '等待你的确认' : run.phase === 'tool' ? '正在使用工具' : '正在生成';
    $('thinking').hidden = terminal.has(run.status) || ['waiting_approval', 'paused'].includes(run.status) || !!text;
    if (nearBottom) scrollBottom();
  }
}
function renderRun() {
  const run = state.run;
  const failedCommand = commandFailed(run?.tool_result);
  $('run-strip').hidden = !run; $('approval').hidden = run?.status !== 'waiting_approval';
  $('unknown-panel').hidden = run?.status !== 'unknown'; $('run-error').hidden = (!run?.error && !failedCommand) || run.status === 'unknown';
  if (!run) { $('live-message').hidden = true; $('run-details').textContent = '发送任务后，可以在这里查看模型与工具的运行过程。'; $('tool-cards').replaceChildren(); $('events').replaceChildren(); return; }
  $('status').textContent = controlState().pausePending ? '将在当前步骤结束后暂停' : run.status === 'paused' && run.resume_status === 'waiting_approval' ? '已暂停 · 继续后仍需批准' : statusLabels[run.status] || run.status;
  if (failedCommand && run.status === 'completed') $('status').textContent = '调用已结束 · 命令未成功';
  $('run-indicator').className = 'run-indicator ' + (failedCommand && run.status === 'completed' ? 'failed' : run.status);
  $('run-error').textContent = `${statusLabels[run.status] || run.status}：${errorLabel(run.error)}${run.error ? '（' + run.error + '）' : ''}`;
  if (failedCommand) $('run-error').textContent = `命令已退出，退出码 ${run.tool_result.value.exit_code ?? '未报告'}。调用有确定结果不代表命令成功，也不代表没有产生副作用；请检查输出与实际产物。`;
  const usage = run.usage;
  $('usage-summary').textContent = usage?.reported_calls > 0 ? `已报告用量：输入 ${usage.input_tokens.toLocaleString()} / 输出 ${usage.output_tokens.toLocaleString()} tokens` : 'Token 用量未报告 · 文件写入需审批';
  renderRunMetrics();
  if (run.status === 'unknown') {
    $('resolve').hidden = !!run.resolution || !run.can_resolve_unknown;
    $('unknown-help').textContent = run.resolution ? '已保留人工核查记录。原任务维持结果未知，不会重新执行。' : run.can_resolve_unknown ? '请先检查文件或外部系统中的实际结果，再填写核查结论。解除阻断后可以提交新任务，原任务不会重试。' : '尚不能确认原执行进程已经退出。请先停止原执行进程，再检查实际产物。原进程信息缺失、无效或无法确认退出时不能自动解除；请保留记录，重启服务本身不会解除阻断。';
    $('resolution-note').textContent = run.resolution ? `核查结论：${run.resolution.note || ''}` : '';
  }
  if (run.status === 'waiting_approval') loadApproval().catch(report);
  renderLive(); renderTools(); renderRunOptions();
}
function renderRunMetrics() {
  const run = state.run;
  if (!run) return;
  const metrics = runMetrics(run, state.view), limits = run.limits || {};
  $('counts').textContent = `模型 ${metrics.modelCalls} 轮 · 工具 ${metrics.toolCalls} 次`;
  const elapsed = Number.isFinite(run.elapsed) ? formatDuration(run.elapsed * 1000) : '未报告';
  const budget = Number.isFinite(limits.run_timeout) ? formatDuration(limits.run_timeout * 1000) : '未配置';
  $('run-details').textContent = `运行 ${run.id.slice(0, 12)} · ${statusLabels[run.status] || run.status}\n绑定目录 ${run.workspace_path || '未记录'}\n模型调用 ${metrics.modelCalls} / ${limits.max_model_calls ?? '未配置'} 次\n工具调用 ${metrics.toolCalls} / ${limits.max_tool_calls ?? '未配置'} 次\n已记录执行耗时 ${elapsed} / ${budget}\n暂停与审批等待不计入执行耗时\n模型耗时${metrics.durationMs === null ? '未报告' : ' ' + formatDuration(metrics.durationMs)}`;
  $('run-details').textContent += '\n' + skillRunLabel(run);
}
function errorLabel(error) {
  return ({approval_denied: '你已拒绝这次操作', cancelled: '运行已取消', model_error: '模型调用失败，请检查后端配置或网络',
    model_timeout: '模型响应超时', run_timeout: '运行时间超限', context_limit: '当前内容超过上下文预算',
    configuration_changed: '运行配置已变化，旧任务停止恢复', tool_limit: '工具次数已达到上限',
    model_limit: '模型轮数已达到上限', interrupted: '上次运行被中断',
    edit_rejected: '文件编辑或恢复被拒绝，未应用本次修改；请查看工具记录中的具体原因',
    command_rejected: '命令在启动前被拒绝，请查看工具记录中的具体原因',
    execution_unknown: '工具效果尚不能确认，请核查实际结果，不要直接重试'})[error] || '请查看执行过程中的记录';
}
function renderTools() {
  const expanded = new Set([...$('tool-cards').children].filter(card => card.open).map(card => card.dataset.callId));
  $('tool-cards').replaceChildren();
  for (const call of state.view.tools.values()) {
    const details = node('details', undefined, 'tool-card'); details.dataset.callId = call.call_id; details.open = expanded.has(call.call_id);
    const summary = node('summary'); summary.append(node('span', undefined, 'tool-card-status ' + call.status), node('span', toolLabel(call.name)));
    if (call.status === 'waiting_approval') summary.append(node('span', '待审批', 'tool-tag'));
    if (call.status === 'rejected') summary.append(node('span', '已拒绝 · 未执行', 'tool-tag'));
    const body = node('div', undefined, 'tool-card-body'); body.append(node('code', call.name));
    if (call.status === 'rejected') body.append(node('p', '此次调用未获批准，工具没有执行。原请求保留在历史记录中。'));
    if (call.arguments !== undefined) body.append(node('p', '调用参数'), node('pre', JSON.stringify(call.arguments, null, 2)));
    if (call.result) {
      const unknown = call.result.error_code === 'execution_unknown' || state.run?.status === 'unknown' && state.run.tool_result?.call_id === call.call_id;
      body.append(node('p', unknown ? '结果未知 · 以下仅为已观察到的诊断' : commandFailed(call.result) ? '命令未成功 · 已观察到退出结果' : call.result.error_code ? '执行被拒绝或失败' : '执行结果'));
      if (call.result.error_code) body.append(node('code', call.result.error_code));
      if (call.result.value !== undefined && call.result.value !== null) body.append(node('pre', typeof call.result.value === 'string' ? call.result.value : JSON.stringify(call.result.value, null, 2)));
      if (unknown) body.append(node('p', '超时或取消不保证进程已停止，也不能证明没有副作用。请核对诊断和实际产物；本任务不会自动重试。'));
      const path = call.arguments?.path;
      if (!call.result.error_code && state.run?.workspace_id === workspaceId() && ['files.read', 'files.write'].includes(call.name) && path) body.append(button('查看文件 ↗', 'text-button', () => { panel('files'); readFile(path).catch(report); }));
    }
    details.append(summary, body); $('tool-cards').append(details);
  }
  if (!state.view.tools.size) $('tool-cards').append(node('p', '本次运行尚无工具调用。', 'empty-small'));
  $('events').replaceChildren();
  const labels = {'run.submitted': '任务已提交', 'model.start': '模型开始', 'model.end': '模型回答完成',
    'tool.start': '工具开始', 'tool.end': '工具完成', 'run.waiting': '等待审批', 'approval.decision': '审批已处理',
    'run.status': '状态更新', 'run.end': '运行结束', 'run.recovered': '状态恢复', 'cancel.requested': '已请求取消', 'run.resolved': '人工核查',
    'run.pause_requested': '已请求暂停', 'run.paused': '运行已暂停', 'run.resumed': '运行已继续'};
  for (const event of state.view.events) {
    const row = node('div', undefined, 'event-row');
    row.append(node('span', '#' + event.seq), node('span', (labels[event.type] || event.type) + (event.status ? ' · ' + (statusLabels[event.status] || event.status) : ''))); $('events').append(row);
  }
}
function queueRender() {
  if (state.renderPending) return; state.renderPending = true;
  requestAnimationFrame(() => { state.renderPending = false; renderRunMetrics(); renderLive(); renderTools(); });
}

async function loadSession(id, restoringWorkspace = false) {
  knowledge.close();
  if (state.busy || state.pendingSubmission || workspace?.locked() && !restoringWorkspace) return;
  const generation = selection(); state.run = null; state.session = null; state.view = createRunView();
  state.loadingSelection = true;
  showError(); renderMessages(); renderRun(); controls(); sidebar(false);
  try {
    const detail = await api('/api/sessions/' + encodeURIComponent(id), 'GET', undefined, state.controller.signal);
    if (generation !== state.generation) return;
    state.session = detail; if (detail.workspace_id === workspaceId()) remember(storageKey('session'), id); renderMessages(); renderSessions(); renderRunOptions();
    const runs = detail.runs || [], rememberedRun = saved(storageKey('run'));
    const latest = runs.find(run => !terminal.has(run.status)) || runs.find(run => run.id === rememberedRun) || runs[0];
    if (latest) await selectRun(latest.id, false);
    else remember(storageKey('run'), null);
    scrollBottom();
  } finally {
    // A superseded request must not unlock the newer selection's composer.
    if (generation === state.generation) { state.loadingSelection = false; controls(); }
  }
}
async function selectRun(id, changeSelection = true) {
  if (state.busy) return;
  const active = activeRun();
  if (active && active.id !== id) return;
  const generation = changeSelection ? selection() : state.generation;
  disconnect(); state.view = createRunView(); state.terminalLoaded = null; state.previewKey = null;
  if (changeSelection) { state.loadingSelection = true; controls(); }
  try {
    const run = await api('/api/runs/' + encodeURIComponent(id), 'GET', undefined, state.controller.signal);
    if (generation !== state.generation || run.session_id !== state.session?.id) return;
    updateRun(run); if (run.workspace_id === workspaceId()) remember(storageKey('run'), id); renderMessages(); watch(id, generation);
  } finally {
    if (changeSelection && generation === state.generation) { state.loadingSelection = false; controls(); }
  }
}
function watch(id, generation = state.generation) {
  disconnect();
  if (state.serverStopping) return;
  const source = new EventSource(`/api/runs/${encodeURIComponent(id)}/events?after=${state.view.lastSeq}`); state.source = source;
  const current = () => generation === state.generation && state.run?.id === id && state.source === source;
  source.addEventListener('run', event => {
    if (!current()) return;
    try { if (applyEvent(state.view, JSON.parse(event.data))) queueRender(); } catch { showError('运行事件无法解析，请刷新页面恢复记录。'); }
  });
  source.addEventListener('snapshot', event => {
    if (!current()) return;
    try {
      const run = JSON.parse(event.data); updateRun(run);
      if (terminal.has(run.status)) { disconnect(); finishRun(id, generation).catch(report); }
    } catch (error) { report(error); }
  });
  source.onerror = () => { if (current()) showError('运行连接暂时中断，正在恢复记录。请勿重复发送任务。'); };
  source.onopen = () => { if (current()) showError(); };
}
async function finishRun(id, generation) {
  if (state.terminalLoaded === id) return; state.terminalLoaded = id;
  const signal = state.controller.signal;
  const [run, detail] = await Promise.all([api('/api/runs/' + encodeURIComponent(id), 'GET', undefined, signal), api('/api/sessions/' + encodeURIComponent(state.session.id), 'GET', undefined, signal)]);
  if (generation !== state.generation || state.run?.id !== id) return;
  state.session = detail; updateRun(run); renderMessages(); renderLive(); await refreshSessions();
  if (run.status === 'completed' && run.workspace_id === workspaceId()) loadFiles(state.filePath).catch(() => {});
}
function newSession() {
  if (state.busy || state.pendingSubmission) return; selection(); state.session = null; state.run = null; state.view = createRunView();
  state.loadingSelection = false;
  remember(storageKey('session'), null); remember(storageKey('run'), null);
  $('prompt').value = ''; renderMessages(); renderRun(); renderSessions(); renderRunOptions(); controls(); showError(); sidebar(false); $('prompt').focus();
}
async function submit(event) {
  event.preventDefault(); const prompt = $('prompt').value.trim();
  if (!prompt || !controlState().canSubmit) return;
  state.busy = true; controls(); showError();
  state.pendingSubmission = scopedSubmission(state.pendingSubmission, prompt, state.session?.id || null, workspaceId(),
    () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`, modelIdentity(), skills.selection());
  persistSubmission(); controls();
  try {
    const run = await api('/api/runs', 'POST', state.pendingSubmission); state.pendingSubmission = null; persistSubmission(); skills.consume();
    const generation = selection(); state.view = createRunView(); state.run = run;
    // Persist the acknowledged ID before any follow-up GET. A transient history-read
    // failure must never make the user resubmit an already accepted side effect.
    if (state.session?.id !== run.session_id) state.session = {id: run.session_id, title: prompt.slice(0, 60), messages: [], runs: [], workspace_id: run.workspace_id, workspace_path: run.workspace_path, model_identity: run.model_identity, model_snapshot: run.model_snapshot};
    remember(`astra.session.${run.workspace_id}`, run.session_id); remember(`astra.run.${run.workspace_id}`, run.id); $('prompt').value = '';
    renderMessages(); updateRun(run); watch(run.id, generation); panel('activity', false); scrollBottom(); await refreshSessions();
  } catch (error) {
    if (error.status >= 400 && error.status < 500) { state.pendingSubmission = null; persistSubmission(); }
    report(error);
    if (state.pendingSubmission) showError('任务提交结果尚未确认。请用“确认上次提交”核对原请求；确认前不能切换目录或提交新任务。');
  }
  finally { state.busy = false; controls(); }
}
async function act(action, body) {
  if (!state.run || state.busy || state.loadingSelection || workspace?.locked()) return;
  const policy = controlState(), permitted = {pause: policy.canPause, resume: policy.canResume, cancel: policy.canCancel, approve: policy.canApprove};
  if (Object.hasOwn(permitted, action) && !permitted[action]) return;
  const id = state.run.id; state.busy = true; controls(); showError();
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}/${action}`, 'POST', body);
    state.previewKey = null; state.terminalLoaded = null; updateRun(run); watch(id);
    if (action === 'resolve') await refreshSessions();
  } catch (error) { report(error); }
  finally { state.busy = false; controls(); }
}
async function loadApproval(retryKey = null) {
  const run = state.run, approval = run.approval;
  if (!approval) return;
  const key = run.id + ':' + approval.key;
  const loadingKey = state.generation + ':' + key;
  if ((retryKey !== null && retryKey !== key) || state.previewLoadingKey === loadingKey
      || (state.previewKey === key && retryKey !== key)) return;
  state.previewKey = key;
  $('approval-caption').textContent = toolLabel(approval.name) + ' · 批准仅适用于这一次调用' + (run.workspace_path ? '\n绑定目录：' + run.workspace_path : '');
  $('approval-content').replaceChildren(node('pre', JSON.stringify(approval.arguments, null, 2), 'approval-params'));
  if (approval.name === 'commands.run') {
    $('approval-content').append(node('p', '执行参数 argv 按数组传递；工作目录 cwd 为绑定目录内的相对路径：' + (approval.arguments?.cwd || '.')),
      node('p', '这是本机子进程，不是操作系统沙箱。批准后程序可能读写文件或访问网络；目录约束不等于限制子进程的全部能力。超时清理也不能保证撤销副作用。'));
    return;
  }
  if (!['files.write', 'files.edit', 'files.restore'].includes(approval.name)) return;
  const generation = state.generation;
  state.previewLoadingKey = loadingKey;
  const loading = node('p', '正在读取文件预览…', 'approval-params'); loading.setAttribute('role', 'status');
  $('approval-content').append(loading);
  try {
    const preview = await api(`/api/runs/${encodeURIComponent(run.id)}/preview`, 'GET', undefined, state.controller.signal);
    if (generation !== state.generation || state.previewKey !== key || state.run.status !== 'waiting_approval') return;
    const head = node('div', undefined, 'diff-head'); head.append(node('span', preview.path), node('span', preview.exists ? preview.changed ? '修改已有文件' : '内容相同' : '新建文件'));
    const grid = node('div', undefined, 'diff-grid');
    for (const [label, text, type] of [['当前文件', preview.exists ? preview.before : '文件尚不存在', 'before'], [approval.name === 'files.restore' ? '批准后恢复' : '批准后写入', preview.after, 'after']]) {
      const side = node('div', undefined, 'diff-side ' + type); side.append(node('div', label, 'diff-label'), node('pre', text || '（空文件）')); grid.append(side);
    }
    $('approval-content').replaceChildren(head, grid);
  } catch (error) {
    if (error.name === 'AbortError') return;
    if (generation !== state.generation || state.previewKey !== key || state.run?.status !== 'waiting_approval') return;
    // Keep the full proposed parameters and the original approval. Retrying a
    // preview is a read, never a replay of approval or tool execution.
    loading.textContent = '当前文件预览不可用。可重新读取，或核对上方完整调用参数后再决定是否批准。';
    $('approval-content').append(button('重新读取文件预览', 'secondary', () => {
      if (state.run?.id === run.id && state.run?.status === 'waiting_approval' && state.previewKey === key) loadApproval(key).catch(report);
    }));
  } finally {
    if (state.previewLoadingKey === loadingKey) state.previewLoadingKey = null;
  }
}

async function loadFiles(path = '') {
  if (!workspaceId() || !state.workspaceInfo?.enabled) return;
  const generation = ++state.fileGeneration, id = workspaceId();
  const result = await api(workspaceQuery('/api/files?path=' + encodeURIComponent(path), id));
  if (generation !== state.fileGeneration || id !== workspaceId()) return;
  state.filePath = result.path || ''; $('file-path').textContent = '/' + state.filePath; $('file-up').disabled = !state.filePath;
  $('file-list').replaceChildren();
  for (const entry of result.entries || []) {
    const directory = ['directory', 'dir'].includes(entry.type);
    const row = button('', 'file-row', () => (directory ? loadFiles(entry.path) : readFile(entry.path)).catch(report));
    row.append(node('span', directory ? '▱' : '▤', 'file-icon'), node('span', entry.name, 'file-row-name'), node('span', directory ? '' : formatSize(entry.size), 'file-size'));
    row.title = entry.path; $('file-list').append(row);
  }
  if (!result.entries?.length) $('file-list').append(node('p', '这个目录暂时没有文件。', 'empty-small'));
}
let readGeneration = 0;
async function readFile(path) {
  if (!scopeReady() || !state.workspaceInfo?.enabled) return;
  const generation = ++readGeneration, id = workspaceId();
  const result = await api(workspaceQuery('/api/files/read?path=' + encodeURIComponent(path), id));
  if (generation !== readGeneration || id !== workspaceId()) return;
  $('file-preview').hidden = false; $('file-name').textContent = result.path; state.fileContent = result.content;
  $('file-content').textContent = result.content; panel('files');
}
function renderCapabilities() {
  const startup = workspaceId() === state.workspaceInfo?.startup_id;
  const services = startup && Array.isArray(state.info?.services) ? state.info.services : [];
  $('workspace-integrations').hidden = startup;
  $('mcp-services').hidden = !services.length;
  $('services-list').replaceChildren();
  const labels = {new: '未连接', starting: '正在连接', ready: '连接就绪', failed: '连接失败', closed: '已关闭'};
  for (const service of services) {
    const block = node('div', undefined, 'capability service-card'), head = node('div', undefined, 'capability-head');
    const label = Object.hasOwn(labels, service.state) ? labels[service.state] : '状态未知';
    const badge = node('span', label, 'capability-badge service-status');
    if (service.state === 'ready') badge.classList.add('service-ready');
    else if (service.state === 'failed') badge.classList.add('service-failed');
    head.append(node('span', service.name), badge);
    block.append(head, node('p', `${service.tools?.length || 0} 个已登记工具 · STDIO`));
    if (service.error_code) block.append(node('code', service.error_code, 'service-error'));
    $('services-list').append(block);
  }
  $('tools-list').replaceChildren();
  const details = state.info?.tool_details || (state.info?.tools || []).map(name => ({name}));
  for (const tool of details) {
    const block = node('div', undefined, 'capability'), head = node('div', undefined, 'capability-head');
    head.append(node('span', toolLabel(tool.name)));
    if (tool.requires_approval) head.append(node('span', '需审批', 'capability-badge'));
    block.append(head, node('code', tool.name));
    if (tool.description) block.append(node('p', tool.description)); $('tools-list').append(block);
  }
  if (!details.length) $('tools-list').append(node('p', '当前未启用工具。', 'empty-small'));
}

let dialogSubmit = null, dialogRevision = 0, dialogPending = null;
function openDialog(title, body, onSubmit = null, confirm = '确认') {
  sidebar(false); dialogRevision++;
  $('dialog-title').textContent = title; $('dialog-body').replaceChildren(body); dialogSubmit = onSubmit;
  $('dialog-error').textContent = ''; $('dialog-error').hidden = true;
  $('dialog-actions').hidden = !onSubmit; $('dialog-confirm').textContent = confirm; $('dialog-confirm').disabled = false;
  if (!$('dialog').open) $('dialog').showModal();
}
function closeDialog() { dialogRevision++; dialogSubmit = null; $('dialog').close(); }
function sessionMenu(item) {
  if (state.busy) return;
  const menu = node('div', undefined, 'dialog-menu');
  menu.append(button('重命名', '', () => renameSession(item)), button(item.archived ? '恢复到对话列表' : '归档对话', '', () => mutateSession(item, {archived: !item.archived}).catch(report)), button('删除对话与运行记录', 'danger', () => deleteSession(item)));
  openDialog(item.title || '管理对话', menu);
}
function renameSession(item) {
  const body = node('div'), label = node('label', '对话名称'); label.htmlFor = 'dialog-input';
  const input = node('input'); input.id = 'dialog-input'; input.value = item.title || ''; input.required = true; input.maxLength = 120;
  body.append(label, input); openDialog('重命名对话', body, () => mutateSession(item, {title: input.value.trim()}), '保存'); input.focus(); input.select();
}
async function mutateSession(item, payload) {
  const revision = dialogRevision;
  await api('/api/sessions/' + encodeURIComponent(item.id), 'POST', payload);
  if (revision === dialogRevision) closeDialog();
  if (state.session?.id === item.id) { Object.assign(state.session, payload); renderMessages(); controls(); }
  await refreshSessions().catch(report); toast(payload.title ? '名称已更新' : payload.archived ? '对话已归档' : '对话已恢复');
}
function deleteSession(item) {
  const body = node('div'); body.append(node('p', `将删除“${item.title || '未命名对话'}”的对话与运行记录，此操作无法撤销。工作区文件会保留。`));
  openDialog('删除这段对话？', body, async () => {
    const revision = dialogRevision;
    await api('/api/sessions/' + encodeURIComponent(item.id) + '/delete', 'POST', {});
    if (revision === dialogRevision) closeDialog();
    if (state.session?.id === item.id) newSession(); await refreshSessions().catch(report); toast('对话已删除');
  }, '确认删除');
}
function resolveDialog() {
  if (!state.run?.can_resolve_unknown) return;
  const body = node('div'), label = node('label', '实际检查了什么，结果是什么？'); label.htmlFor = 'dialog-input';
  const input = node('textarea'); input.id = 'dialog-input'; input.required = true; input.maxLength = 2000; input.placeholder = '例如：已检查 todos.json，文件内容完整；无需再次写入。';
  body.append(node('p', '请核查实际文件或外部系统后填写。该操作只记录结论并解除阻断，不会把原运行改为成功，也不会重放工具。'), label, input);
  openDialog('记录人工核查', body, async () => { const note = input.value.trim(); if (!note) return; closeDialog(); await act('resolve', {note}); }, '记录并解除阻断'); input.focus();
}
function aboutDialog() {
  const body = node('div'), live = state.info?.mode === 'live';
  body.append(node('p', live ? '当前使用后端配置的真实模型。回答、工具和供应商报告的用量会随运行显示。' : '当前使用固定规则演示模型，真实执行工作区工具。支持会议待办、两数加法和“搜索 关键词”，不代表通用语言理解。'));
  body.append(node('p', '工作区：' + (state.info?.workspace || '未启用文件工具')));
  if (!live) body.append(node('p', '启用真实模型：在启动进程环境中设置 ASTRA_API_KEY、ASTRA_MODEL、ASTRA_BASE_URL，然后执行：'), node('pre', 'python -m astra_harness serve --live'));
  body.append(node('p', '凭据保留在后端。这里展示配置状态，不在网页输入或保存密钥。'));
  openDialog('运行配置', body);
}
function renderModelMode() {
  const selected = state.modelProfiles?.items.find(item => item.id === state.modelProfiles.selected_id);
  const live = !!selected || state.info?.mode === 'live';
  $('mode').classList.toggle('demo', !live);
  $('mode-label').textContent = selected ? selected.name : state.modelProfiles ? (live ? '启动配置' : '离线演示') : '连接中';
  $('mode').title = selected ? `模型配置：${selected.name} · ${selected.model}` : '选择或管理模型配置';
  $('mode-note').textContent = selected ? `新任务使用 ${selected.model}。配置已选择，连接与工具调用能力仍需验证。`
    : live ? '使用启动时的真实模型配置。工具写入需审批。' : '当前为固定规则演示 · 支持会议整理、两数加法与关键词搜索';
}

const knowledge = createKnowledgeView({api,
  onChanged: () => { refreshSessions().catch(report); loadFiles(state.filePath).catch(report); },
  onOpen: () => { shell.classList.add('knowledge-open'); $('knowledge-workspace').hidden = false; sidebar(false); },
  onClose: () => { shell.classList.remove('knowledge-open'); $('knowledge-workspace').hidden = true; },
  onMenu: event => sidebar(!shell.classList.contains('sidebar-open'), {trigger: event.currentTarget}),
  onOpenRun: async run => { knowledge.close(); try { await loadSession(run.session_id); await selectRun(run.id); panel('activity'); } catch (error) { report(error); } },
});
profiles = createModelProfilesView({api, blocked: () => state.busy || !!state.pendingSubmission,
  changed: controls,
  accepted: catalog => {
    state.modelProfiles = catalog; renderModelMode(); controls();
    if (state.workspaceInfo) refreshSessions().catch(report);
  },
});
mcpProfiles = createMCPProfilesView({api, scope: () => workspaceId() || null,
  blocked: () => state.busy || !!state.pendingSubmission || !!workspace?.locked(),
  changed: controls, accepted: () => { if (state.workspaceInfo) refreshSessions().catch(report); },
});
skills = createLocalSkillsView({api, blocked: () => state.busy || !!state.pendingSubmission || !!workspace?.locked(),
  pending: () => state.pendingSubmission, changed: controls,
});
workspace = createWorkspaceView({api,
  blocked: () => state.busy || !!state.pendingSubmission || !!mcpProfiles?.state.busy || !!skills?.locked(),
  onChange: controls,
  onApplied: async snapshot => {
    // This invalidates outstanding reads and SSE without cancelling any run.
    state.workspaceInfo = snapshot; state.workspaceEpoch++; state.sessionsRevision++;
    selection(); state.fileGeneration++; readGeneration++; knowledge.reset(); skills.reset(snapshot.selected_id || null);
    if ($('dialog').open) closeDialog();
    state.session = null; state.run = null; state.view = createRunView(); state.sessions = [];
    state.archived = false; state.loadingSelection = false; state.filePath = ''; state.fileContent = '';
    $('file-path').textContent = '/'; $('file-up').disabled = true;
    $('file-list').replaceChildren(); $('file-preview').hidden = true; $('file-content').textContent = ''; $('file-name').textContent = '';
    $('prompt').value = state.pendingSubmission?.prompt || ''; showError();
    renderMessages(); renderRun(); renderRunOptions(); renderSessions(); controls();
    await refreshSessions();
    const id = saved(storageKey('session'));
    if (id && state.sessions.some(item => item.id === id)) await loadSession(id, true);
    if (snapshot.enabled) await loadFiles();
    else $('file-list').append(node('p', '文件工具未开启。', 'empty-small'));
  },
});
$('open-knowledge').addEventListener('click', () => knowledge.open());
$('open-knowledge-top').addEventListener('click', () => knowledge.open());
$('new-session').addEventListener('click', () => { knowledge.close(); newSession(); });
$('open-model-profiles').addEventListener('click', () => {sidebar(false); profiles.open();});
$('open-mcp-profiles').addEventListener('click', () => {sidebar(false); mcpProfiles.open();});
$('open-skills').addEventListener('click', () => {sidebar(false); skills.open();});
$('skill-picker').addEventListener('click', () => skills.open());
$('model-new-session').addEventListener('click', () => { knowledge.close(); newSession(); });
$('archive-toggle').addEventListener('click', () => {state.archived = !state.archived; renderSessions();});
$('sidebar-toggle').addEventListener('click', event => sidebar(!shell.classList.contains('sidebar-open'), {trigger: event.currentTarget}));
$('sidebar-close').addEventListener('click', () => sidebar(false, {restoreFocus: true}));
$('mobile-backdrop').addEventListener('click', () => sidebar(false, {restoreFocus: true}));
window.addEventListener('keydown', event => {
  if (event.key === 'Escape' && shell.classList.contains('sidebar-open')) {
    event.preventDefault(); sidebar(false, {restoreFocus: true});
  }
});
$('inspector-toggle').addEventListener('click', () => {const narrow = matchMedia('(max-width:1150px)').matches; const open = narrow ? shell.classList.contains('inspector-open') : !shell.classList.contains('hide-inspector'); if (open) closeInspector(); else panel(document.querySelector('[role=tab][aria-selected=true]').id.slice(4));});
$('inspector-close').addEventListener('click', closeInspector);
for (const name of ['files', 'activity', 'tools']) $('tab-' + name).addEventListener('click', () => panel(name));
$('browse-files').addEventListener('click', () => {panel('files'); loadFiles(state.filePath).catch(report);});
$('refresh-files').addEventListener('click', () => loadFiles(state.filePath).catch(report));
$('refresh-services').addEventListener('click', async () => {
  const control = $('refresh-services');
  if (control.disabled) return;
  control.disabled = true;
  const epoch = state.workspaceEpoch;
  try {
    const info = await api('/api/info');
    if (epoch !== state.workspaceEpoch) return;
    state.info = info;
    renderCapabilities(); renderBlocked(); controls();
  } catch (error) { report(error); }
  finally { control.disabled = false; }
});
$('file-up').addEventListener('click', () => loadFiles(state.filePath.split('/').slice(0, -1).join('/')).catch(report));
$('copy-file').addEventListener('click', async () => {try {await navigator.clipboard.writeText(state.fileContent); toast('文件内容已复制');} catch {toast('请选中文件内容后复制');}});
$('example-meeting').addEventListener('click', () => {$('prompt').value = '读取工作区的会议记录，整理待办为 task、owner、due 字段，保存为 todos.json。'; $('prompt').focus();});
$('example-search').addEventListener('click', () => {$('prompt').value = '搜索 负责人'; $('prompt').focus();});
$('composer').addEventListener('submit', submit);
$('prompt').addEventListener('keydown', event => {if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); if (!$('send').disabled) $('composer').requestSubmit();}});
$('cancel').addEventListener('click', () => act('cancel', {}));
$('pause').addEventListener('click', () => act('pause', {}));
$('resume').addEventListener('click', () => act('resume', {}));
$('approve').addEventListener('click', () => act('approve', {approved: true}));
$('reject').addEventListener('click', () => act('approve', {approved: false}));
$('resolve').addEventListener('click', resolveDialog);
$('run-select').addEventListener('change', () => {if ($('run-select').value) selectRun($('run-select').value).catch(report);});
$('about-button').addEventListener('click', aboutDialog); $('mode').addEventListener('click', () => profiles.open());
$('dialog-close').addEventListener('click', closeDialog); $('dialog-cancel').addEventListener('click', closeDialog);
$('dialog').addEventListener('cancel', event => {event.preventDefault(); closeDialog();});
$('dialog-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!dialogSubmit || !$('dialog').open || dialogPending === dialogRevision) return;
  const revision = dialogRevision, submit = dialogSubmit;
  dialogPending = revision; $('dialog-confirm').disabled = true;
  $('dialog-error').textContent = ''; $('dialog-error').hidden = true;
  try { await submit(); }
  catch (error) {
    // Preserve this draft, but never let a cancelled/older request replace the
    // feedback or controls of a subsequently opened dialog.
    if (revision !== dialogRevision || !$('dialog').open) return;
    $('dialog-error').textContent = error.message; $('dialog-error').hidden = false;
  } finally {
    if (dialogPending === revision) dialogPending = null;
    if (revision === dialogRevision && $('dialog').open) $('dialog-confirm').disabled = false;
  }
});
window.addEventListener('beforeunload', disconnect);
createServerControl({$, onOpen: () => sidebar(false),
  onClose: () => $(sidebarMedia.matches ? 'sidebar-toggle' : 'exit-service').focus(), onAccepted: () => {
  state.serverStopping = true; selection(); state.controller.abort(); knowledge.reset();
  shell.inert = true; document.title = 'Astra Harness · 服务退出';
}});

async function initialize() {
  try {
    const pending = JSON.parse(sessionStorage.getItem('astra.pendingSubmission'));
    if (pending && typeof pending.prompt === 'string' && typeof pending.workspace_id === 'string'
        && typeof pending.request_id === 'string' && (pending.session_id === null || typeof pending.session_id === 'string')) state.pendingSubmission = {...pending, expected_model_identity: pending.expected_model_identity || 'startup', skill: pending.skill ?? null};
  } catch {}
  state.info = await api('/api/info');
  renderModelMode(); await profiles.load();
  await workspace.load();
}
initialize().catch(report);
