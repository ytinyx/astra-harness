const fields = ['name', 'provider', 'base_url', 'model', 'key_env', 'timeout', 'max_output_tokens'];
const profileErrors = Object.freeze({
  model_profiles_disabled: '当前宿主未启用模型配置管理。',
  invalid_model_profile: '配置格式不正确。请检查基础地址、模型名、环境变量名和数值范围；不要填写密钥值。',
  model_profile_conflict: '配置版本已变化。请重新读取并核对；未保存草稿会保留，必要时放弃修改后重新编辑。',
  model_profile_not_found: '这份模型配置已不存在，请重新读取配置列表。',
  model_profile_in_use: '这份配置仍被运行或连接测试使用，请等待结束后再操作。',
  model_profile_unavailable: '后端暂时无法读取或使用模型配置，请检查本地服务状态。',
  model_profile_not_selected: '当前模型选择已变化。请重新读取配置并核对后再发送。',
  model_profile_changed: '本次运行绑定的模型配置已变化。请核对配置并新建任务；原运行不会静默换模型。',
  model_session_mismatch: '此对话绑定了另一模型配置。请新建对话；旧历史不会发送给新模型。',
  model_credentials_missing: '后端未找到此配置指定的密钥环境变量。请在启动服务的环境中设置对应变量并重启服务；表单只填写变量名，不填写密钥值。',
  model_credentials_invalid: '后端环境变量中的凭据格式无效。请在后端检查变量值；不要把密钥粘贴到网页。',
  model_credentials_changed: '凭据与运行创建时不一致。请核对后端环境并新建任务；原运行不会自动改用新凭据。',
  model_endpoint_not_public: '远程地址未通过公开服务地址检查。请使用 HTTPS 公开服务，或带明确端口的数字本机回环地址。',
  model_dns_failed: '无法解析模型服务地址。请检查地址与网络配置。',
  model_request_too_large: '模型请求超过允许大小，本次请求未能完成。',
  model_authentication_failed: '模型服务拒绝认证。请检查后端密钥及该账号的服务访问权限。',
  model_rate_limited: '模型服务限制了请求频率。请稍后再手动测试；不会自动重试。',
  model_transport_error: '模型服务连接失败。请检查网络、服务地址和运行状态；不会自动重试。',
  model_response_too_large: '模型服务响应超过允许大小，未接受该结果。',
  model_protocol_error: '服务响应不符合预期的 Chat Completions 协议。请检查基础地址、模型名和接口类型。',
  model_timeout: '等待模型服务超时。已发出的请求仍可能产生费用；不会自动重试。',
  model_test_unexpected_tools: '合成文本测试返回了未预期的工具调用。本次测试未通过，也没有执行这些工具。',
});
export function modelProfileError(code) {
  return typeof code === 'string' && Object.hasOwn(profileErrors, code) ? profileErrors[code] : null;
}
export const blankModelProfile = () => ({name: '', provider: 'chat_completions', base_url: '', model: '', key_env: '', timeout: 30, max_output_tokens: 1024});
function editable(profile) {
  const result = {};
  if (profile.id) result.id = profile.id;
  for (const key of fields) result[key] = profile[key];
  return result;
}
export function sessionModelMismatch(session, selectedIdentity = 'startup') {
  if (!session) return false;
  const identity = session.model_identity || ((session.runs || []).some(run => run.kind !== 'tool') || session.messages?.length ? 'startup' : null);
  return identity !== null && identity !== selectedIdentity;
}

export class ModelProfilesSession {
  constructor({api, changed = () => {}, accepted = () => {}, blocked = () => false}) {
    this.api = api; this.changed = changed; this.accepted = accepted; this.blocked = blocked;
    this.state = {catalog: null, draft: blankModelProfile(), draftRevision: null, dirty: false,
      busy: '', error: '', notice: '', uncertain: false, stale: false, testing: false, testResult: null};
    this.readEpoch = 0; this.testEpoch = 0; this.testController = null;
  }
  emit() { this.changed(this.state); }
  locked() { return !!this.state.busy || this.state.uncertain; }
  mutable() { return !this.locked() && !this.blocked(); }
  async load() {
    if (this.state.busy || this.state.testing) return;
    const epoch = ++this.readEpoch; this.state.busy = 'load'; this.state.error = ''; this.emit();
    try {
      const catalog = await this.api('/api/model-profiles');
      if (epoch !== this.readEpoch) return;
      this.state.catalog = catalog; this.state.uncertain = false; this.accepted(catalog);
      if (!this.state.dirty) this.resetDraft();
      else this.state.notice = '已读取最新配置，未保存草稿仍保留。若版本冲突，请放弃修改后重新编辑。';
    } catch (error) { if (epoch === this.readEpoch) this.state.error = error.message; }
    finally { if (epoch === this.readEpoch) { this.state.busy = ''; this.emit(); } }
  }
  begin(id = null) {
    if (!this.mutable() || this.state.testing || this.state.dirty) return;
    const profile = id ? this.state.catalog?.items.find(item => item.id === id) : null;
    if (id && !profile) return;
    this.state.draft = profile ? editable(profile) : blankModelProfile();
    this.state.draftRevision = this.state.catalog?.revision;
    this.state.dirty = false; this.state.stale = false; this.state.error = ''; this.state.notice = ''; this.state.testResult = null; this.emit();
  }
  update(values) {
    if (!this.mutable() || this.state.testing) return;
    for (const field of fields) if (Object.hasOwn(values, field)) this.state.draft[field] = values[field];
    this.state.dirty = true; this.state.testResult = null; this.emit();
  }
  resetDraft() {
    if (this.state.testing) return;
    const previousId = this.state.draft.id;
    const profile = this.state.catalog?.items.find(item => item.id === previousId);
    this.state.draft = profile ? editable(profile) : blankModelProfile();
    this.state.draftRevision = this.state.catalog?.revision;
    this.state.dirty = false; this.state.stale = false; this.state.testResult = null;
  }
  discard() {
    if (!this.mutable() || this.state.testing) return;
    this.resetDraft(); this.state.notice = '已放弃未保存修改。'; this.state.error = ''; this.emit();
  }
  async mutate(action, payload, revision) {
    if (!this.mutable() || this.state.testing || !this.state.catalog) return;
    this.state.busy = action; this.state.error = ''; this.state.notice = ''; this.emit();
    const knownIds = new Set(this.state.catalog.items.map(item => item.id));
    try {
      const catalog = await this.api(`/api/model-profiles/${action}`, 'POST', {...payload, expected_revision: revision});
      this.state.catalog = catalog; this.state.uncertain = false; this.state.stale = false;
      if (action === 'save') {
        // Create IDs belong to the backend. Pick the actual returned entry by
        // its submitted non-secret fields rather than generating an identity.
        const saved = catalog.items.find(item => payload.profile.id ? item.id === payload.profile.id : !knownIds.has(item.id));
        if (saved) this.state.draft = editable(saved);
        this.state.dirty = false;
      } else if (action === 'delete' && this.state.draft.id === payload.id) this.state.draft = blankModelProfile();
      this.state.draftRevision = catalog.revision; this.state.testResult = null;
      this.state.notice = action === 'save' ? '配置已保存；没有发送模型请求。' : action === 'select' ? '已选择供新任务使用的模型；没有发送模型请求。旧会话若绑定其他模型，请新建对话。' : '配置已删除；已有运行记录保留。';
      this.accepted(catalog);
    } catch (error) {
      this.state.uncertain = !(error.status >= 400 && error.status < 500);
      this.state.stale = error.status === 409;
      this.state.error = this.state.uncertain ? '保存或选择结果尚未确认。请重新读取配置核对；不会自动重发操作。'
        : modelProfileError(error.code) || (error.status === 409 ? '配置版本已变化或当前配置仍在使用。请重新读取并核对，再操作。' : error.message);
    } finally { this.state.busy = ''; this.emit(); }
  }
  save() {
    if (!this.state.dirty || this.state.stale) return;
    return this.mutate('save', {profile: editable(this.state.draft)}, this.state.draftRevision);
  }
  select(id) {
    if (this.state.dirty || id === this.state.catalog?.selected_id) return;
    return this.mutate('select', {id}, this.state.catalog?.revision);
  }
  remove(id) {
    if (this.state.dirty) return;
    return this.mutate('delete', {id}, this.state.catalog?.revision);
  }
  async test(confirmed = false) {
    if (!confirmed || !this.mutable() || this.state.dirty || this.state.stale || this.state.testing || !this.state.draft.id) return;
    const id = this.state.draft.id, revision = this.state.catalog.revision, epoch = ++this.testEpoch;
    const profile = this.state.catalog.items.find(item => item.id === id);
    this.testController = new AbortController(); this.state.testing = true; this.state.error = ''; this.state.testResult = null; this.emit();
    try {
      const result = await this.api('/api/model-profiles/test', 'POST', {id, expected_revision: revision, confirmed: true}, this.testController.signal);
      if (epoch === this.testEpoch) this.state.testResult = result?.id === id && result.identity === profile?.identity
        && result.revision === revision && result.profile_revision === profile?.revision
        ? {kind: result.ok === true ? 'passed' : 'failed', result}
        : {kind: 'failed', error: '测试响应与所选配置不匹配，请重新读取配置。', uncertain: true};
    } catch (error) {
      if (epoch === this.testEpoch) this.state.testResult = {kind: 'failed', error: modelProfileError(error.code) || '连接测试未完成，请检查本地服务与网络状态。',
        uncertain: !error.status || error.status >= 500};
    } finally {
      if (epoch === this.testEpoch) {this.state.testing = false; this.testController = null; this.emit();}
    }
  }
  cancelTest() {
    if (!this.state.testing) return;
    this.testEpoch++; this.testController?.abort(); this.testController = null; this.state.testing = false;
    this.state.testResult = {kind: 'cancelled'}; this.emit();
  }
}
