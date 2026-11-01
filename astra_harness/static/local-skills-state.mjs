const errors = Object.freeze({
  skill_catalog_unavailable: '当前工作区的 Skill 目录不可用，请检查目录权限与文件格式。',
  skill_catalog_conflict: '启用记录已被其他操作修改。请刷新并重新查看当前版本。',
  invalid_skill_selection: 'Skill 选择格式无效，请清除选择后重新查看。',
  skill_disabled: '这份 Skill 当前未启用。请先查看并启用当前版本。',
  skill_changed: 'Skill 内容或文件身份已变化。请刷新、查看新版本并重新确认；不会自动换用新内容。',
  skill_not_found: 'Skill 已不存在，请刷新列表并核对工作区。',
  skill_catalog_limit: 'Skill 数量超过上限，请整理当前工作区的 skills 目录。',
  skills_limit: 'Skill 或扫描条目数量超过上限，请整理 skills 目录后刷新。',
  skill_file_limit: 'Skill 文件超过 16 KiB，请缩短内容后刷新。',
  skill_header_limit: 'Skill 元数据头超过大小限制，请只保留简短的 name 与 description。',
  skill_metadata_invalid: 'Skill 文件头格式无效。请按面板示例填写 name 与 description，正文放在结束分隔线之后。',
  skill_invalid_utf8: 'Skill 不是有效的 UTF-8 文本，请在文本编辑器中转换编码后刷新。',
  skill_binary: 'Skill 包含二进制内容，不能作为任务指导。',
  skill_body_empty: 'Skill 正文为空，请在元数据头之后补充任务指导。',
  skill_not_file: 'SKILL.md 不是可读取的普通文件，请检查本地目录。',
  skill_unavailable: 'Skill 文件当前不可读取，请检查本地权限并刷新。',
  task_context_invalid: '本次任务指导无效，请清除选择并重新查看 Skill。',
  task_context_unavailable: '本次任务指导当前不可用，请检查工作区并重新查看 Skill。',
  task_context_changed: '本次任务指导的版本已变化，请重新确认；原任务不会自动改用新内容。',
});
export const skillError = code => typeof code === 'string' && Object.hasOwn(errors, code) ? errors[code] : null;
export function skillRunLabel(run) {
  const context = run?.task_context;
  return context?.selection ? `任务指导：${context.metadata?.name || context.selection.id} · ${context.selection.id} · ${(context.selection.expected_identity || '').slice(0, 12)}` : '任务指导：未附加 Skill';
}

export class LocalSkillsSession {
  constructor({api, changed = () => {}, blocked = () => false}) {
    Object.assign(this, {api, changed, blocked}); this.epoch = 0; this.controller = null;
    this.state = {workspaceId: null, catalog: null, detail: null, selected: null, selectionStale: false, busy: '', uncertain: false, error: '', notice: ''};
  }
  emit() { this.changed(this.state); }
  reset(workspaceId) {
    if (workspaceId === this.state.workspaceId) return;
    this.epoch++; this.controller?.abort(); this.controller = null;
    Object.assign(this.state, {workspaceId, catalog: null, detail: null, selected: null, selectionStale: false, busy: '', uncertain: false, error: '', notice: ''}); this.emit();
  }
  locked() { return this.state.busy === 'toggle' || this.state.uncertain; }
  mutable() { return !this.state.busy && !this.state.uncertain && !this.blocked() && !!this.state.workspaceId; }
  query(path) { return path + (path.includes('?') ? '&' : '?') + 'workspace_id=' + encodeURIComponent(this.state.workspaceId); }
  syncSelection() {
    const selected = this.state.selected;
    if (!selected) {this.state.selectionStale = false; return;}
    const item = this.state.catalog?.items.find(item => item.id === selected.id);
    this.state.selectionStale = !item || item.identity !== selected.expected_identity || !item.enabled || item.needs_review;
  }
  async load() {
    if (this.state.busy === 'toggle' || !this.state.workspaceId) return;
    const workspaceId = this.state.workspaceId, epoch = ++this.epoch;
    this.controller?.abort(); this.controller = new AbortController(); this.state.busy = 'list'; this.state.error = ''; this.emit();
    try {
      const catalog = await this.api(this.query('/api/skills'), 'GET', undefined, this.controller.signal);
      if (epoch !== this.epoch) return;
      if (catalog.workspace_id !== workspaceId) throw new Error('scope');
      this.state.catalog = catalog; this.state.uncertain = false;
      const detail = this.state.detail, current = catalog.items.find(item => item.id === detail?.id);
      if (detail && (!current || current.identity !== detail.identity)) this.state.detail = null;
      else if (detail) this.state.detail = {...detail, ...current, revision: catalog.revision};
      this.syncSelection();
    } catch (error) { if (epoch === this.epoch) {this.state.error = skillError(error.code) || '无法读取当前工作区的 Skill，请刷新后核对。'; if (this.state.selected) this.state.selectionStale = true;} }
    finally {if (epoch === this.epoch) {this.state.busy = ''; this.controller = null; this.emit();}}
  }
  async view(id) {
    if (this.state.busy === 'toggle' || this.state.uncertain) return;
    const item = this.state.catalog?.items.find(item => item.id === id); if (!item) return;
    const workspaceId = this.state.workspaceId, epoch = ++this.epoch;
    this.controller?.abort(); this.controller = new AbortController(); this.state.busy = 'view'; this.state.detail = null; this.state.error = ''; this.state.notice = ''; this.emit();
    try {
      const detail = await this.api(this.query(`/api/skills/${encodeURIComponent(id)}?expected_identity=${encodeURIComponent(item.identity)}`), 'GET', undefined, this.controller.signal);
      if (epoch !== this.epoch) return;
      if (detail.workspace_id !== workspaceId || detail.id !== id || detail.identity !== item.identity || typeof detail.body !== 'string') throw new Error('identity');
      this.state.detail = detail;
    } catch (error) { if (epoch === this.epoch) this.state.error = skillError(error.code) || '无法确认这份 Skill 的正文与版本，请刷新后重新查看。'; }
    finally {if (epoch === this.epoch) {this.state.busy = ''; this.controller = null; this.emit();}}
  }
  async setEnabled(enabled, confirmed = false) {
    const detail = this.state.detail;
    if (!this.mutable() || !detail || typeof enabled !== 'boolean' || enabled && !confirmed) return;
    const epoch = ++this.epoch, body = {workspace_id: this.state.workspaceId, id: detail.id, expected_identity: detail.identity, expected_revision: detail.revision, enabled};
    this.state.busy = 'toggle'; this.state.error = ''; this.state.notice = ''; this.emit();
    try {
      const catalog = await this.api('/api/skills/enabled', 'POST', body);
      if (epoch !== this.epoch) return;
      if (catalog.workspace_id !== body.workspace_id) throw new Error('scope');
      this.state.catalog = catalog;
      const item = catalog.items.find(item => item.id === detail.id);
      this.state.detail = item?.identity === detail.identity ? {...detail, ...item, revision: catalog.revision} : null;
      this.state.notice = enabled ? '启用记录已返回，请核对当前版本状态。启用不会自动用于任务。' : '停用记录已返回；已有运行保留原任务指导快照。';
      this.syncSelection();
    } catch (error) {
      if (epoch !== this.epoch) return;
      this.state.uncertain = !error.status || error.status >= 500;
      this.state.error = skillError(error.code) || '启停结果未能确认。请刷新核对，不会自动重发。';
      if (!this.state.uncertain) this.state.detail = null;
      if (this.state.selected) this.state.selectionStale = true;
    } finally {if (epoch === this.epoch) {this.state.busy = ''; this.emit();}}
  }
  choose() {
    const detail = this.state.detail;
    if (!this.mutable() || !detail?.enabled || detail.needs_review) return;
    this.state.selected = {id: detail.id, expected_identity: detail.identity, name: detail.name, workspace_id: this.state.workspaceId};
    this.state.selectionStale = false; this.state.notice = '仅为下一次提交选择这份 Skill；提交确认成功后会清除，不自动继承。'; this.emit();
  }
  clear() {if (this.blocked()) return; this.consume();}
  consume() {this.state.selected = null; this.state.selectionStale = false; this.emit();}
  selection() {const item = this.state.selected; return item ? {id: item.id, expected_identity: item.expected_identity} : null;}
}
