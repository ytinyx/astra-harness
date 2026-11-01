import {ModelProfilesSession, modelProfileError} from './model-profiles-state.mjs';

export function createModelProfilesView({api, accepted, changed = () => {}, blocked = () => false}) {
  const $ = id => document.getElementById(id), dialog = $('model-profiles-dialog');
  let deleteId = null, renderedCatalog, renderedListFlags, consentBinding = null, visibleFeedback = null;
  const session = new ModelProfilesSession({api, accepted, blocked, changed: () => {render(); changed();}});
  function node(tag, text, className) {
    const element = document.createElement(tag); if (text !== undefined) element.textContent = text;
    if (className) element.className = className; return element;
  }
  function button(label, callback, disabled = false, className = 'secondary') {
    const element = node('button', label, className); element.type = 'button'; element.disabled = disabled;
    element.addEventListener('click', callback); return element;
  }
  function render() {
    const s = session.state, locked = session.locked() || blocked(), draft = s.draft;
    const profile = s.catalog?.items.find(item => item.id === s.catalog.selected_id);
    $('profiles-current').textContent = profile ? `新任务使用：${profile.name} · ${profile.model}` : s.catalog ? '新任务使用：启动配置' : '正在读取当前选择…';
    $('profiles-current-note').textContent = '保存和选择不发送请求。选择只影响新任务；已有运行保持原模型，同一对话不能悄悄改用另一模型。';
    $('profiles-error').textContent = s.error; $('profiles-error').hidden = !s.error;
    $('profiles-notice').textContent = s.notice; $('profiles-notice').hidden = !s.notice;
    $('profiles-dirty').textContent = s.dirty ? '有未保存修改 · 关闭面板保留草稿，不会自动保存' : '配置只保存环境变量名，密钥留在后端';
    $('profiles-reload').disabled = !!s.busy || s.testing;
    $('profiles-new').disabled = locked || s.testing || s.dirty;
    const listFlags = `${locked}:${s.testing}:${s.dirty}`;
    if (renderedCatalog !== s.catalog || renderedListFlags !== listFlags) {
    renderedCatalog = s.catalog; renderedListFlags = listFlags;
    const list = $('profiles-list'); list.replaceChildren();
    const startup = node('article', undefined, 'model-profile-card' + (s.catalog?.selected_id === null ? ' selected' : ''));
    startup.append(node('strong', '启动配置'), node('p', '保留离线演示，或启动时已有的 --live 环境配置。'));
    startup.append(button(s.catalog?.selected_id === null ? '当前选择' : '使用启动配置', () => session.select(null), locked || s.testing || s.dirty || !s.catalog || s.catalog.selected_id === null));
    list.append(startup);
    for (const item of s.catalog?.items || []) {
      const card = node('article', undefined, 'model-profile-card' + (item.id === s.catalog.selected_id ? ' selected' : ''));
      const actions = node('div', undefined, 'model-profile-actions');
      actions.append(button('编辑', () => {deleteId = null; session.begin(item.id); $('profile-name').focus();}, locked || s.testing || s.dirty),
        button(item.id === s.catalog.selected_id ? '当前选择' : '选择此配置', () => session.select(item.id), locked || s.testing || s.dirty || item.id === s.catalog.selected_id));
      card.append(node('strong', item.name), node('span', item.model, 'model-profile-model'), node('code', item.base_url), actions); list.append(card);
    }
    }
    $('profiles-empty').hidden = !s.catalog || s.catalog.items.length > 0;
    for (const field of ['name', 'base_url', 'model', 'key_env', 'timeout', 'max_output_tokens']) {
      const control = $('profile-' + field);
      const value = String(draft[field] ?? ''); if (control.value !== value) control.value = value;
      control.disabled = locked || s.testing;
    }
    $('profile-form-title').textContent = draft.id ? '编辑已保存配置' : '新增模型配置';
    $('profile-save').disabled = locked || s.testing || !s.dirty || s.stale || !s.catalog;
    $('profile-save').textContent = s.busy === 'save' ? '正在保存…' : '保存配置';
    $('profile-discard').disabled = locked || s.testing || !s.dirty;
    $('profile-delete').hidden = !draft.id;
    $('profile-delete').disabled = locked || s.testing || s.dirty;
    $('profile-delete-confirmation').hidden = deleteId !== draft.id || !deleteId;
    $('profile-delete-confirm').disabled = locked || s.testing;
    $('profile-delete-cancel').disabled = !!s.busy;
    const saved = s.catalog?.items.find(item => item.id === draft.id);
    // Consent is for the displayed destination and revision, not a reusable
    // approval for whichever profile happens to be edited next.
    const binding = saved ? `${saved.id}:${saved.identity}:${saved.revision}:${s.catalog.revision}` : null;
    if (binding !== consentBinding) { $('profile-test-confirmed').checked = false; consentBinding = binding; }
    $('profile-test-target').textContent = saved ? `外传目标：${saved.base_url} · 模型：${saved.model}。仅发送固定合成文本，不发送你的会话或文件。可能产生费用。` : '先保存配置，再单独确认测试连接。';
    $('profile-test-confirmed').disabled = locked || s.testing || s.dirty || !saved || s.stale;
    $('profile-test').disabled = locked || s.testing || s.dirty || !saved || s.stale || !$('profile-test-confirmed').checked;
    $('profile-test').textContent = s.testing ? '正在测试…' : '测试连接';
    $('profile-test-cancel').hidden = !s.testing;
    $('profile-test-result').replaceChildren();
    const result = s.testResult;
    if (result) {
      const output = $('profile-test-result');
      output.append(node('strong', result.kind === 'passed' ? '接口测试通过' : result.kind === 'cancelled' ? '已停止等待测试' : '测试未通过'));
      if (result.kind === 'cancelled' || result.uncertain) output.append(node('p', '已发出的请求可能产生费用，取消等待不证明服务未处理；不会自动重试。'));
      if (result.error) output.append(node('p', result.error));
      if (result.result) {
        const value = result.result;
        output.append(node('p', value.request_issued === false ? '后端报告：没有发出测试请求。' : value.request_issued === true ? '后端报告：已发出测试请求。' : '是否发出请求未报告。'));
        if (value.error_code) {
          const explanation = modelProfileError(value.error_code);
          output.append(node('p', explanation || '服务未返回可识别的错误原因，请检查后端运行状态。'));
          if (explanation) output.append(node('code', value.error_code));
        }
        if (value.usage) output.append(node('pre', JSON.stringify(value.usage, null, 2)));
        else output.append(node('p', 'Token 用量未报告，不能据此认定免费。'));
      }
      output.append(node('p', '这只是一次合成文本请求，不代表已验证工具调用能力或所有任务。'));
    }
    // Saving at the bottom of a long form must reveal its outcome once. Routine
    // refreshes and responses after closing must not move the surrounding page.
    const feedback = s.error ? ['error:' + s.error, $('profiles-error')]
      : s.testResult ? [s.testResult, $('profile-test-result')]
      : s.notice ? ['notice:' + s.notice, $('profiles-notice')] : null;
    if (!dialog.open || !feedback) visibleFeedback = null;
    else if (!s.busy && !s.testing && visibleFeedback !== feedback[0]) {
      visibleFeedback = feedback[0]; feedback[1].scrollIntoView({block: 'nearest', inline: 'nearest'});
    }
  }
  function close() {dialog.close(); visibleFeedback = null; session.cancelTest();}
  $('model-profiles-close').addEventListener('click', close);
  dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
  $('profiles-reload').addEventListener('click', () => session.load());
  $('profiles-new').addEventListener('click', () => {deleteId = null; session.begin(); $('profile-name').focus();});
  $('profile-discard').addEventListener('click', () => {deleteId = null; session.discard();});
  $('profile-form').addEventListener('submit', event => {event.preventDefault(); $('profile-test-confirmed').checked = false; deleteId = null; session.save();});
  for (const field of ['name', 'base_url', 'model', 'key_env', 'timeout', 'max_output_tokens']) $('profile-' + field).addEventListener('input', event => {
    deleteId = null; $('profile-test-confirmed').checked = false;
    session.update({[field]: ['timeout', 'max_output_tokens'].includes(field) ? Number(event.target.value) : event.target.value});
  });
  $('profile-delete').addEventListener('click', () => {deleteId = session.state.draft.id; render();});
  $('profile-delete-cancel').addEventListener('click', () => {deleteId = null; render();});
  $('profile-delete-confirm').addEventListener('click', () => {const id = deleteId; deleteId = null; if (id) session.remove(id);});
  $('profile-test-confirmed').addEventListener('change', render);
  $('profile-test').addEventListener('click', () => {const confirmed = $('profile-test-confirmed').checked; $('profile-test-confirmed').checked = false; session.test(confirmed);});
  $('profile-test-cancel').addEventListener('click', () => session.cancelTest());
  window.addEventListener('beforeunload', () => session.cancelTest());
  return {load: () => session.load(), open: () => {render(); if (!dialog.open) dialog.showModal(); session.load();}, close,
    locked: () => session.locked(), refresh: render, get state() {return session.state;}};
}
