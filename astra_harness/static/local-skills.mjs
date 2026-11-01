import {LocalSkillsSession, skillError} from './local-skills-state.mjs';

export function createLocalSkillsView({api, changed = () => {}, blocked = () => false, pending = () => null}) {
  const $ = id => document.getElementById(id), dialog = $('skills-dialog');
  let consentBinding = null, renderedCatalog, renderedFlags, feedbackKey = null;
  const session = new LocalSkillsSession({api, blocked, changed: () => {render(); changed();}});
  function node(tag, text, className) {const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item;}
  function render() {
    const s = session.state, detail = s.detail, locked = session.locked() || blocked();
    $('skills-error').textContent = s.error; $('skills-error').hidden = !s.error;
    $('skills-notice').textContent = s.notice; $('skills-notice').hidden = !s.notice;
    $('skills-loading').hidden = !s.busy; $('skills-loading').textContent = s.busy === 'toggle' ? '正在保存启用状态…' : s.busy === 'view' ? '正在读取所选版本…' : '正在读取当前工作区…';
    $('skills-uncertain').hidden = !s.uncertain;
    $('skills-reload').disabled = s.busy === 'toggle' || !s.workspaceId;
    $('skills-scope').textContent = s.workspaceId ? '当前工作区 / skills/<slug>/SKILL.md' : '请先确认当前工作区。';
    $('skills-empty').hidden = !s.catalog || s.catalog.items.length > 0;
    $('skills-empty').textContent = s.catalog?.exists === false ? '当前工作区还没有 skills 目录。按下面的格式创建本地文件，再刷新列表。' : '没有可用的 Skill 文件。可新建文件，或查看下方格式与读取问题。';
    const flags = `${s.busy}:${s.uncertain}:${detail?.id}:${s.selected?.id}:${s.selected?.expected_identity}`;
    if (renderedCatalog !== s.catalog || renderedFlags !== flags) {
      renderedCatalog = s.catalog; renderedFlags = flags; const list = $('skills-list'); list.replaceChildren();
      for (const item of s.catalog?.items || []) {
        const card = node('article', undefined, 'model-profile-card' + (item.id === detail?.id ? ' selected' : ''));
        const status = item.needs_review ? '内容变化 · 待重新确认' : item.enabled ? '当前版本已启用' : '未启用';
        card.append(node('strong', item.name), node('p', item.description), node('code', item.path), node('p', status));
        const button = node('button', '查看正文', 'secondary'); button.type = 'button'; button.disabled = s.busy === 'toggle' || s.uncertain;
        button.addEventListener('click', () => session.view(item.id)); card.append(button); list.append(card);
      }
      const issues = $('skills-issues'); issues.replaceChildren();
      for (const issue of s.catalog?.issues || []) issues.append(node('p', `${issue.id || 'skills'}：${skillError(issue.error_code) || '文件不可用或未通过格式检查，请在本地核对后刷新。'}`));
      issues.hidden = !s.catalog?.issues?.length;
    }
    $('skill-detail-empty').hidden = !!detail; $('skill-detail').hidden = !detail;
    $('skill-name').textContent = detail?.name || ''; $('skill-description').textContent = detail?.description || '';
    $('skill-version').textContent = detail ? `${detail.path} · ${detail.byte_count} 字节\n版本 SHA：${detail.identity}` : '';
    $('skill-body').textContent = detail?.body || '';
    $('skill-state').textContent = !detail ? '' : detail.needs_review ? '内容已变化，旧版本授权不适用。请阅读并重新确认当前版本。' : detail.enabled ? '当前版本已启用。可以选择用于下一次任务。' : '当前版本未启用。启用前请阅读正文。';
    const binding = detail ? `${s.workspaceId}:${detail.id}:${detail.identity}:${detail.revision}` : null;
    if (binding !== consentBinding) { $('skill-confirmed').checked = false; consentBinding = binding; }
    $('skill-confirmed').disabled = locked || !!s.busy || !detail || !!detail.enabled;
    $('skill-enable').disabled = locked || !!s.busy || !detail || !!detail.enabled || !$('skill-confirmed').checked;
    $('skill-disable').disabled = locked || !!s.busy || !detail?.enabled;
    $('skill-choose').disabled = locked || !!s.busy || !detail?.enabled || !!detail?.needs_review;
    const retry = pending(), selection = retry ? retry.skill : s.selected;
    $('skill-selection').textContent = retry ? selection ? `待确认的原请求 · Skill：${selection.id} · ${String(selection.expected_identity || '').slice(0, 12)}` : '待确认的原请求 · 未附加 Skill'
      : selection ? `下一次任务 · ${selection.name} · ${selection.expected_identity.slice(0, 12)}${s.selectionStale ? ' · 已失效，请重新查看或清除' : ''}` : '本次任务不附加 Skill';
    $('skill-selection').classList.toggle('skill-selection-stale', !retry && s.selectionStale);
    $('skill-clear').hidden = !selection; $('skill-clear').disabled = blocked() || !!retry;
    $('skill-picker').disabled = !s.workspaceId;
    const feedback = s.error ? ['error:' + s.error, $('skills-error')] : s.uncertain ? ['uncertain', $('skills-uncertain')] : s.notice ? ['notice:' + s.notice, $('skills-notice')] : null;
    if (!dialog.open || !feedback) feedbackKey = null;
    else if (feedbackKey !== feedback[0]) {feedbackKey = feedback[0]; feedback[1].scrollIntoView({block: 'nearest', inline: 'nearest'});}
  }
  function close() {$('skill-confirmed').checked = false; dialog.close(); feedbackKey = null;}
  $('skills-close').addEventListener('click', close); dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
  $('skills-reload').addEventListener('click', () => session.load());
  $('skill-confirmed').addEventListener('change', render);
  $('skill-enable').addEventListener('click', () => {const confirmed = $('skill-confirmed').checked; $('skill-confirmed').checked = false; session.setEnabled(true, confirmed);});
  $('skill-disable').addEventListener('click', () => session.setEnabled(false));
  $('skill-choose').addEventListener('click', () => {session.choose(); if (session.state.selected?.id === session.state.detail?.id && !blocked()) close();});
  $('skill-clear').addEventListener('click', () => session.clear());
  return {open: () => {render(); if (!dialog.open) dialog.showModal(); session.load();}, close, refresh: render, reset: id => session.reset(id),
    locked: () => session.locked(), invalid: () => !!session.state.selected && session.state.selectionStale,
    selection: () => session.selection(), consume: () => session.consume(), get state() {return session.state;}};
}
