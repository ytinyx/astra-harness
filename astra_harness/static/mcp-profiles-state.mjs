const errors = Object.freeze({
  invalid_mcp_profile: '配置格式不正确。请检查服务标识、连接参数、环境变量引用及数值范围。',
  mcp_profile_confirmation_required: '请先核对所示连接目标，并明确确认执行程序或连接本机服务。',
  mcp_profile_not_found: '服务配置已不存在，请重新读取。',
  mcp_profile_conflict: '配置版本或服务身份已变化，也可能存在同名服务。请重新读取并核对；草稿会保留。',
  mcp_profile_in_use: '服务仍在使用或保留启用意图。请先停用，再编辑或删除。',
  mcp_profile_busy: '有服务操作正在进行，请等待结束后重新读取。',
  mcp_profile_limit: '服务或工具数量已达上限，请核对已保存和已启用的服务。',
  mcp_profile_unavailable: 'MCP 配置当前不可用，请检查本地宿主状态。',
  mcp_profile_connection_failed: '连接或工具发现失败。请检查程序、参数或本机端点；不会自动重试。',
  mcp_profile_timeout: '等待连接或发现工具超时。请重新读取状态；不会自动重试。',
  mcp_profile_cleanup_failed: '服务资源清理未能确认完成。请核查后端进程并重启宿主；不能据此认为程序已停止。',
  mcp_profile_workspace_mismatch: 'MCP 配置绑定启动工作区，请切回启动工作区后操作。',
  workspace_not_configured: 'MCP 配置绑定启动工作区，请切回启动工作区后操作。',
  runtime_update_busy: '仍有任务、待审批或待核查操作，暂时不能变更工具集合。请先处理这些运行。',
  runtime_update_invalid: '工具集合变更未通过检查，请核对服务和工具命名。',
  runtime_update_unavailable: '运行时暂时不能变更工具集合，请检查宿主状态。',
});
export function mcpProfileError(code, path = '/api/mcp-profiles') {
  if (code === 'workspace_not_configured' && path !== '/api/mcp-profiles' && !path.startsWith('/api/mcp-profiles/')) return null;
  return typeof code === 'string' && Object.hasOwn(errors, code) ? errors[code] : null;
}
export const blankMCPProfile = () => ({name: '', transport: 'stdio', command: '', args_text: '[]', cwd: '.', env_refs_text: '', endpoint: '', timeout: 10, service_revision: '1'});
export const mcpCanConnect = item => !!item && !['ready', 'starting', 'connecting', 'closing'].includes(item.state) && (!item.enabled || item.activation_required === true);
export function mcpDraft(profile) {
  return {...blankMCPProfile(), ...Object.fromEntries(['id', 'name', 'transport', 'command', 'cwd', 'endpoint', 'timeout', 'service_revision'].filter(key => Object.hasOwn(profile, key)).map(key => [key, profile[key]])),
    args_text: JSON.stringify(profile.args || [], null, 2), env_refs_text: Object.entries(profile.env_refs || {}).map(([key, value]) => `${key}=${value}`).join('\n')};
}
export function mcpPayload(draft) {
  const profile = {name: draft.name, transport: draft.transport, timeout: draft.timeout, service_revision: draft.service_revision};
  if (draft.id) profile.id = draft.id;
  if (draft.transport === 'http') profile.endpoint = draft.endpoint;
  else {
    let args; try { args = JSON.parse(draft.args_text); } catch { throw new Error('参数需要是 JSON 字符串数组，例如 ["--mode", "stdio"]。'); }
    if (!Array.isArray(args) || args.length > 63 || args.some(arg => typeof arg !== 'string' || arg.includes('\0'))) throw new Error('参数需要是最多 63 项的字符串数组。');
    const refs = {};
    for (const line of draft.env_refs_text.split(/\r?\n/).filter(line => line.trim())) {
      const match = /^([A-Za-z_][A-Za-z0-9_]{0,63})=(ASTRA_MCP_[A-Z][A-Z0-9_]{0,79})$/.exec(line.trim());
      if (!match || Object.keys(refs).some(key => key.toUpperCase() === match[1].toUpperCase()) || match[1] === '__proto__') throw new Error('环境引用每行填写 TARGET=ASTRA_MCP_名称；只允许变量名，不填密钥值或重复名称。');
      Object.defineProperty(refs, match[1], {value: match[2], enumerable: true});
    }
    if (Object.keys(refs).length > 16) throw new Error('环境变量引用最多 16 项。');
    Object.assign(profile, {command: draft.command, args, cwd: draft.cwd, env_refs: refs});
  }
  return profile;
}

export class MCPProfilesSession {
  constructor({api, changed = () => {}, accepted = () => {}, blocked = () => false, scope = () => null}) {
    Object.assign(this, {api, changed, accepted, blocked, scope});
    this.state = {catalog: null, draft: blankMCPProfile(), draftRevision: null, dirty: false, busy: '', error: '', notice: '', uncertain: false, stale: false, testResult: null};
  }
  emit() { this.changed(this.state); }
  saved() { return this.state.catalog?.items.find(item => item.id === this.state.draft.id); }
  locked() { return !!this.state.busy || this.state.uncertain || !!this.state.catalog?.operation || !!this.state.catalog?.cleanup_required; }
  allowed() { const catalog = this.state.catalog; return !!catalog && catalog.actions_allowed !== false && (catalog.current_workspace ?? null) === this.scope(); }
  mutable() { return !this.locked() && !this.blocked() && this.allowed(); }
  async load() {
    if (this.state.busy) return;
    this.state.busy = 'load'; this.state.error = ''; this.emit();
    try {
      const catalog = await this.api('/api/mcp-profiles');
      this.state.catalog = catalog; this.state.uncertain = false;
      if (!this.state.dirty) this.resetDraft();
      else this.state.notice = '已重新读取；未保存草稿保留。若版本已变化，请放弃修改后重新编辑。';
      this.accepted(catalog);
    } catch (error) { this.state.error = mcpProfileError(error.code) || '无法读取 MCP 配置，请检查本地服务。'; }
    finally { this.state.busy = ''; this.emit(); }
  }
  begin(id = null) {
    if (this.state.busy || this.state.dirty || this.state.uncertain) return;
    const item = this.state.catalog?.items.find(item => item.id === id);
    if (id && !item) return;
    this.state.draft = item ? mcpDraft(item) : blankMCPProfile(); this.state.draftRevision = this.state.catalog?.revision;
    this.state.stale = false; this.state.error = ''; this.state.notice = ''; this.state.testResult = null; this.emit();
  }
  update(values) {
    if (!this.mutable() || this.saved()?.enabled) return;
    for (const key of Object.keys(blankMCPProfile())) if (Object.hasOwn(values, key)) this.state.draft[key] = values[key];
    this.state.dirty = true; this.state.testResult = null; this.emit();
  }
  resetDraft() {
    const item = this.saved(); this.state.draft = item ? mcpDraft(item) : blankMCPProfile();
    this.state.draftRevision = this.state.catalog?.revision; this.state.dirty = false; this.state.stale = false; this.state.testResult = null;
  }
  discard() { if (this.state.busy || this.state.uncertain) return; this.resetDraft(); this.state.error = ''; this.state.notice = '已放弃未保存修改。'; this.emit(); }
  async save() {
    if (!this.mutable() || !this.state.dirty || this.state.stale || this.saved()?.enabled) return;
    let profile;
    try { profile = mcpPayload(this.state.draft); } catch (error) { this.state.error = error.message; this.emit(); return; }
    return this.request('save', {profile, expected_revision: this.state.draftRevision});
  }
  async operate(action, confirmed = false) {
    const item = this.saved();
    if (!['test', 'enable', 'disable', 'delete'].includes(action) || !this.mutable() || this.state.dirty || this.state.stale || !item) return;
    if (['test', 'enable'].includes(action) && (!confirmed || !mcpCanConnect(item))) return;
    if (action === 'delete' && item.enabled) return;
    if (action === 'disable' && !item.enabled && !['ready', 'failed'].includes(item.state)) return;
    return this.request(action, {id: item.id, expected_revision: this.state.catalog.revision, expected_identity: item.identity,
      ...(['test', 'enable'].includes(action) ? {confirmed: true} : {})});
  }
  async request(action, body) {
    const known = new Set(this.state.catalog.items.map(item => item.id));
    this.state.busy = action; this.state.error = ''; this.state.notice = ''; this.state.testResult = null; this.emit();
    try {
      const result = await this.api(`/api/mcp-profiles/${action}`, 'POST', body);
      if (action === 'test') {
        const matches = result?.id === body.id && result.identity === body.expected_identity && result.revision === body.expected_revision;
        this.state.testResult = matches && result.ok === true && result.registered === false && Array.isArray(result.tools)
          ? {passed: true, tools: result.tools} : {passed: false};
        if (!matches) this.state.uncertain = true;
      } else {
        this.state.catalog = result; this.state.uncertain = false; this.state.stale = false;
        if (action === 'save') {
          const item = result.items.find(item => body.profile.id ? item.id === body.profile.id : !known.has(item.id));
          if (item) this.state.draft = mcpDraft(item);
        } else if (action === 'delete') this.state.draft = blankMCPProfile();
        this.state.dirty = false; this.state.draftRevision = result.revision;
        this.state.notice = action === 'save' ? '已保存，未启动程序或连接服务。' : action === 'delete' ? '配置已删除，已有运行记录保留。'
          : action === 'disable' ? '停用操作已返回，请核对下方实际状态。' : '激活操作已返回，请核对下方实际状态与工具。';
        this.accepted(result);
      }
    } catch (error) {
      this.state.uncertain = !error.status || error.status >= 500;
      this.state.stale = error.code === 'mcp_profile_conflict';
      this.state.error = mcpProfileError(error.code) || '操作未能确认完成，请重新读取并核对状态。';
    } finally { this.state.busy = ''; this.emit(); }
  }
}
