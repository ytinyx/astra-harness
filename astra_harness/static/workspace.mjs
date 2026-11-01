import {WorkspaceSelection} from './workspace-state.mjs';

export function createWorkspaceView({api, onApplied, onChange = () => {}, blocked = () => false}) {
  const $ = id => document.getElementById(id);
  const selection = new WorkspaceSelection({api, onApplied, blocked, onChange: () => { render(); onChange(); }});
  function render() {
    const s = selection.state, snapshot = s.snapshot, current = snapshot?.items.find(item => item.id === snapshot.selected_id);
    $('workspace-path').textContent = current?.path || (snapshot?.enabled === false ? '文件工作区已禁用' : '等待确认工作区…');
    $('workspace-form').hidden = !s.open;
    $('workspace-open').disabled = !snapshot?.enabled || selection.locked() || blocked();
    $('workspace-open').setAttribute('aria-expanded', String(s.open));
    const fixed = s.phase === 'selecting' || s.uncertain;
    $('workspace-input').disabled = fixed || !snapshot?.enabled;
    if ($('workspace-input').value !== s.draft) $('workspace-input').value = s.draft;
    $('workspace-validate').disabled = selection.locked() || blocked() || !s.draft.trim() || !snapshot?.enabled;
    $('workspace-validate').textContent = s.phase === 'validating' ? '校验中…' : '校验目录';
    $('workspace-confirm').disabled = selection.locked() || blocked() || !s.candidate;
    $('workspace-confirm').textContent = s.phase === 'selecting' ? '正在切换…' : '确认切换';
    $('workspace-candidate').hidden = !s.candidate;
    $('workspace-candidate-path').textContent = s.candidate?.path || '';
    $('workspace-cancel').disabled = fixed;
    $('workspace-status').textContent = s.error || (s.phase === 'loading' ? '正在确认当前工作区…' : blocked() ? '有任务请求尚未确认，确认原请求后才能切换目录。' : snapshot?.enabled === false ? '本次启动未启用文件工具，不能选择目录。' : '已有任务继续使用原目录。知识库和 MCP 仅绑定启动工作区。');
    $('workspace-status').classList.toggle('workspace-error', !!s.error);
    $('workspace-reconcile').hidden = snapshot?.enabled === false;
    $('workspace-reconcile').textContent = s.uncertain || s.error ? '确认当前工作区' : '刷新工作区';
    $('workspace-reconcile').disabled = !!s.phase || blocked();
    const list = $('workspace-recent'); list.replaceChildren();
    for (const item of snapshot?.items || []) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'workspace-recent-item';
      button.textContent = (item.id === snapshot.selected_id ? '当前 · ' : '') + item.path;
      button.title = item.path; button.disabled = fixed || blocked();
      button.addEventListener('click', () => {selection.edit(item.path); $('workspace-input').focus();}); list.append(button);
    }
    $('workspace-recent-section').hidden = !list.childElementCount;
  }
  $('workspace-open').addEventListener('click', () => {selection.open(); $('workspace-input').focus();});
  $('workspace-input').addEventListener('input', event => selection.edit(event.target.value));
  $('workspace-form').addEventListener('submit', event => {event.preventDefault(); selection.validate();});
  $('workspace-confirm').addEventListener('click', () => selection.select());
  $('workspace-cancel').addEventListener('click', () => selection.cancel());
  $('workspace-reconcile').addEventListener('click', () => selection.load());
  return {load: () => selection.load(), locked: () => selection.locked(), refresh: render, get state() {return selection.state;}};
}
