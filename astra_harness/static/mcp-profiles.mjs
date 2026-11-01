import {MCPProfilesSession, mcpProfileError, mcpCanConnect} from './mcp-profiles-state.mjs';

const states = {disabled: '已停用', offline: '未连接 · 需要手动激活', connecting: '正在连接', starting: '正在启动', ready: '已连接', failed: '连接失败', closing: '正在关闭', closed: '已关闭'};
export function createMCPProfilesView({api, accepted = () => {}, changed = () => {}, blocked = () => false, scope = () => null}) {
  const $ = id => document.getElementById(id), dialog = $('mcp-profiles-dialog');
  let deleteId = null, consentBinding = null, renderedCatalog, renderedFlags, visibleFeedback = null;
  const session = new MCPProfilesSession({api, accepted, blocked, scope, changed: () => {render(); changed();}});
  function node(tag, text, className) { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element; }
  function button(text, callback, disabled = false) { const element = node('button', text, 'secondary'); element.type = 'button'; element.disabled = disabled; element.addEventListener('click', callback); return element; }
  function render() {
    const s = session.state, item = session.saved(), locked = session.locked() || blocked(), allowed = session.allowed();
    $('mcp-scope').textContent = !s.catalog ? '正在读取服务配置…' : allowed ? '绑定启动工作区 · 工具经统一注册与审批执行' : '仅可查看：请切回启动工作区后管理服务。';
    $('mcp-scope-note').textContent = '保存不启动。测试只发现工具并关闭连接，不执行工具。启用后工具可用于新任务；重开宿主不会自动启动已保存的服务。';
    $('mcp-error').textContent = s.error; $('mcp-error').hidden = !s.error;
    $('mcp-notice').textContent = s.notice; $('mcp-notice').hidden = !s.notice;
    $('mcp-uncertain').hidden = !s.uncertain && !s.catalog?.cleanup_required;
    $('mcp-uncertain').textContent = s.catalog?.cleanup_required ? '资源清理尚未确认。请停止宿主，核查相关服务进程后再重开；此页不会自动再次启动服务。' : '上次操作结果尚未确认。请重新读取核对，不要重复提交；关闭面板不代表后端已停止。';
    $('mcp-operation').hidden = !s.busy && !s.catalog?.operation;
    $('mcp-operation').textContent = s.busy === 'load' ? '正在读取…' : s.busy || s.catalog?.operation ? '服务操作进行中，请等待完成。关闭面板不会取消后端操作。' : '';
    $('mcp-reload').disabled = !!s.busy;
    $('mcp-new').disabled = locked || !allowed || s.dirty;
    $('mcp-empty').hidden = !s.catalog || s.catalog.items.length > 0;
    const flags = `${locked}:${allowed}:${s.dirty}:${s.draft.id}`;
    if (renderedCatalog !== s.catalog || flags !== renderedFlags) {
      renderedCatalog = s.catalog; renderedFlags = flags; const list = $('mcp-list'); list.replaceChildren();
      for (const service of s.catalog?.items || []) {
        const card = node('article', undefined, 'model-profile-card' + (service.id === s.draft.id ? ' selected' : ''));
        card.append(node('strong', service.name), node('span', `${service.transport === 'stdio' ? '本地程序' : '本机 HTTP'} · ${states[service.state] || '状态未确认'}`, 'model-profile-model'),
          node('p', `${service.tools?.length || 0} 个已注册工具${service.activation_required ? ' · 保留启用意图，尚未激活' : ''}`),
          button(service.enabled || !allowed ? '查看配置' : '编辑配置', () => {deleteId = null; session.begin(service.id); $('mcp-name').focus();}, !!s.busy || s.uncertain || s.dirty));
        if (service.error_code && ['failed', 'closed'].includes(service.state)) card.append(node('p', mcpProfileError(service.error_code) || '服务报告失败，请检查本地运行状态。'));
        list.append(card);
      }
    }
    const readonly = locked || !allowed || !!item?.enabled;
    $('mcp-form-title').textContent = s.draft.id ? item?.enabled ? '查看已启用配置' : '编辑服务配置' : '新增 MCP 服务';
    $('mcp-dirty').textContent = s.dirty ? '有未保存修改 · 关闭面板保留草稿，不会自动保存' : item?.enabled ? '先停用服务，才能编辑或删除；离线但保留启用意图时也需要停用。' : '配置、参数和环境变量引用会保存。不要在参数中放入密钥。';
    for (const field of ['name', 'transport', 'command', 'args_text', 'cwd', 'env_refs_text', 'endpoint', 'timeout', 'service_revision']) {
      const control = $('mcp-' + field), value = String(s.draft[field] ?? ''); if (control.value !== value) control.value = value;
      control.disabled = readonly || (['command', 'args_text', 'cwd', 'env_refs_text'].includes(field) && s.draft.transport !== 'stdio') || (field === 'endpoint' && s.draft.transport !== 'http');
    }
    $('mcp-stdio-fields').hidden = s.draft.transport !== 'stdio'; $('mcp-http-fields').hidden = s.draft.transport !== 'http';
    $('mcp-save').disabled = readonly || !s.dirty || s.stale || !s.catalog;
    $('mcp-discard').disabled = !!s.busy || s.uncertain || !s.dirty;
    $('mcp-delete').hidden = !item; $('mcp-delete').disabled = locked || !allowed || s.dirty || !!item?.enabled;
    $('mcp-delete-confirmation').hidden = !deleteId || deleteId !== item?.id;
    $('mcp-delete-confirm').disabled = locked || !allowed || s.dirty || !!item?.enabled;
    $('mcp-delete-cancel').disabled = !!s.busy;
    const binding = item ? `${item.id}:${item.identity}:${s.catalog.revision}:${scope()}:${s.dirty}` : null;
    if (binding !== consentBinding) { $('mcp-confirmed').checked = false; deleteId = null; consentBinding = binding; }
    const available = !!item && !locked && allowed && !s.dirty && !s.stale;
    $('mcp-target').textContent = !item ? '先保存服务配置，再核对连接目标。' : item.transport === 'stdio'
      ? `将以宿主账号执行：${item.command}\n参数：${JSON.stringify(item.args)}\n工作目录：启动工作区 / ${item.cwd}\n这不是操作系统沙箱；只运行你信任的程序。`
      : `将连接本机 HTTP 服务：${item.endpoint}\n服务程序仍可能自行访问文件或网络；回环地址不代表服务没有副作用。`;
    $('mcp-confirmed').disabled = !available || !mcpCanConnect(item);
    $('mcp-test').disabled = !available || !mcpCanConnect(item) || !$('mcp-confirmed').checked;
    $('mcp-enable').disabled = !available || !mcpCanConnect(item) || !$('mcp-confirmed').checked;
    $('mcp-enable').textContent = item?.activation_required ? '重新激活服务' : '启用服务';
    $('mcp-disable').disabled = !available || (!item?.enabled && !['ready', 'failed'].includes(item?.state));
    $('mcp-disable').hidden = !item;
    $('mcp-active-state').textContent = item ? `实际状态：${states[item.state] || '未确认'}。${item.activation_required ? '保存的启用意图不会自动启动程序，请核对后重新激活。' : ''}` : '';
    const tools = $('mcp-tools'); tools.replaceChildren();
    if (item) { tools.append(node('strong', '当前已注册工具')); for (const name of item.tools || []) tools.append(node('code', name)); if (!item.tools?.length) tools.append(node('p', '当前没有已注册工具。')); }
    const result = $('mcp-test-result'); result.replaceChildren();
    if (s.testResult) {
      result.append(node('strong', s.testResult.passed ? '工具发现通过' : '工具发现未通过'));
      if (s.testResult.passed) { result.append(node('p', `发现 ${s.testResult.tools.length} 个工具；连接已关闭，没有注册或执行这些工具。`)); for (const name of s.testResult.tools) result.append(node('code', name)); }
      else result.append(node('p', '响应未证明本次所选服务测试成功，请重新读取并核对。'));
    }
    // Long forms must reveal a new outcome once, without jumping on every
    // unrelated render or moving the page while this dialog is closed.
    const feedback = s.error ? ['error:' + s.error, $('mcp-error')]
      : s.uncertain || s.catalog?.cleanup_required ? [$('mcp-uncertain').textContent, $('mcp-uncertain')]
      : s.testResult ? [s.testResult, result] : s.notice ? ['notice:' + s.notice, $('mcp-notice')] : null;
    if (!dialog.open || !feedback) visibleFeedback = null;
    else if (visibleFeedback !== feedback[0]) {
      visibleFeedback = feedback[0]; feedback[1].scrollIntoView({block: 'nearest', inline: 'nearest'});
    }
  }
  function close() { $('mcp-confirmed').checked = false; visibleFeedback = null; dialog.close(); }
  $('mcp-profiles-close').addEventListener('click', close); dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
  $('mcp-reload').addEventListener('click', () => session.load());
  $('mcp-new').addEventListener('click', () => {deleteId = null; session.begin(); $('mcp-name').focus();});
  $('mcp-discard').addEventListener('click', () => {deleteId = null; session.discard();});
  $('mcp-form').addEventListener('submit', event => {event.preventDefault(); $('mcp-confirmed').checked = false; deleteId = null; session.save();});
  for (const field of ['name', 'transport', 'command', 'args_text', 'cwd', 'env_refs_text', 'endpoint', 'timeout', 'service_revision']) $('mcp-' + field).addEventListener(field === 'transport' ? 'change' : 'input', event => {
    deleteId = null; $('mcp-confirmed').checked = false; session.update({[field]: field === 'timeout' ? Number(event.target.value) : event.target.value});
  });
  $('mcp-delete').addEventListener('click', () => {deleteId = session.saved()?.id; render();});
  $('mcp-delete-cancel').addEventListener('click', () => {deleteId = null; render();});
  $('mcp-delete-confirm').addEventListener('click', () => {deleteId = null; session.operate('delete');});
  $('mcp-confirmed').addEventListener('change', render);
  for (const action of ['test', 'enable', 'disable']) $('mcp-' + action).addEventListener('click', () => {const confirmed = $('mcp-confirmed').checked; $('mcp-confirmed').checked = false; session.operate(action, confirmed);});
  return {load: () => session.load(), open: () => {render(); if (!dialog.open) dialog.showModal(); session.load();}, close, locked: () => session.locked(), refresh: render, get state() {return session.state;}};
}
