// This client tracks one explicit operation per collection. It never approves,
// resubmits with a new ID, or cancels server work because a view was closed.
export const operationTerminal = new Set(['completed', 'failed', 'cancelled', 'unknown']);
export const actionLabels = {import_file: '导入 / 更新', remove: '移除索引', search: '词法检索',
  embedding_test: '测试 embedding 连接', embedding_rebuild: '重建语义索引', semantic_search: '语义检索'};
export const embeddingActions = new Set(['embedding_test', 'embedding_rebuild', 'semantic_search']);

export async function encodeKnowledgeFile(file) {
  if (!file || !/\.(md|txt)$/i.test(file.name) || file.size < 1 || file.size > 128000) {
    throw new Error('请选择 1 至 128,000 字节的 UTF-8 .md / .txt 文件。');
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (bytes.length !== file.size) throw new Error('文件已变化，请重新选择。');
  let text;
  try { text = new TextDecoder('utf-8', {fatal: true}).decode(bytes); }
  catch { throw new Error('文件不是有效的 UTF-8 文本，请转换编码后重试。'); }
  if (text.includes('\0')) throw new Error('文件包含二进制内容，请选择纯文本材料。');
  let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export class KnowledgeCreation {
  constructor({request, read = () => null, write = () => {}, changed = () => {}, makeId = () => 'kb_' + crypto.randomUUID().replaceAll('-', '').slice(0, 24)}) {
    Object.assign(this, {request, read, write, changed, makeId}); this.generation = 0;
    this.state = {info: null, pending: null, busy: false, error: ''};
  }
  select(info) {
    this.generation++;
    let pending = null;
    try {
      const stored = JSON.parse(this.read(info.workspace_id) || 'null');
      if (stored?.workspace_id === info.workspace_id && /^kb_[a-f0-9]{24}$/.test(stored.id)
          && typeof stored.label === 'string' && stored.label.length <= 64 && /^[a-f0-9]{64}$/.test(stored.expected_revision)) pending = stored;
    } catch {}
    if (pending && info.collections?.some(item => item.id === pending.id && item.label === pending.label)) {
      this.write(info.workspace_id, ''); pending = null;
    }
    this.state = {info, pending, busy: false, error: pending ? '上次创建结果尚未确认。点击“重新确认创建”复用原请求。' : ''};
    this.changed();
  }
  async start(label) {
    const {info, busy} = this.state;
    if (busy || !info?.management?.can_create || !info.workspace_id || !label.trim()) return null;
    const generation = this.generation;
    const body = this.state.pending || {id: this.makeId(), label: label.trim(), workspace_id: info.workspace_id, expected_revision: info.management.revision};
    this.state.pending = body; this.write(info.workspace_id, JSON.stringify(body));
    this.state.busy = true; this.state.error = ''; this.changed();
    try {
      const result = await this.request('/api/knowledge/collections', 'POST', body);
      if (result.ok !== true || result.id !== body.id) throw new Error('创建结果无法确认，请刷新知识库列表。');
      this.write(info.workspace_id, '');
      if (generation === this.generation) this.state.pending = null;
      return body.id;
    } catch (error) {
      const definite = [400, 403, 404, 409].includes(error.status);
      if (definite) this.write(info.workspace_id, '');
      if (generation === this.generation) {
        if (definite) this.state.pending = null;
        this.state.error = definite ? error.message : '创建结果尚未确认。请重新确认创建，或刷新列表核对；不会自动创建第二份。';
      }
      return null;
    } finally {
      if (generation === this.generation) { this.state.busy = false; this.changed(); }
    }
  }
}
const identifier = /^[A-Za-z0-9_-]{1,128}$/;

export function actionRecoveryIdentity(collection, action) {
  if (Object.prototype.hasOwnProperty.call(collection?.tool_recovery_identities || {}, action)) return collection.tool_recovery_identities[action];
  return ['import_file', 'remove', 'search'].includes(action) ? collection?.recovery_identity : undefined;
}

export function embeddingReadiness(config, status, action) {
  if (!config) return {allowed: false, reason: '先读取当前配置。'};
  if (config.restart_required) return {allowed: false, reason: '配置已保存；重启工作台后再执行语义操作。'};
  if (!config.active?.enabled) return {allowed: false, reason: '当前未启用 embedding，词法检索仍可使用。'};
  if (action === 'embedding_rebuild' && status?.chunk_count === 0 && /^[a-f0-9]{64}$/.test(status.corpus_sha256 || '')) {
    return {allowed: true, reason: '空语料只清理本地语义索引，不发送片段。'};
  }
  if (!config.active_key_present || ['missing_key', 'invalid_key'].includes(status?.state)) return {allowed: false, reason: '请在后端环境设置有效凭据，再重启工作台。'};
  if (action === 'embedding_test') return {allowed: true, reason: ''};
  if (!status || !/^[a-f0-9]{64}$/.test(status.corpus_sha256 || '')) return {allowed: false, reason: '先刷新语义索引状态。'};
  if (action === 'semantic_search' && (status.state !== 'ready' || !status.index_compatible)) return {allowed: false, reason: '语义索引尚未就绪，请检查材料并重建索引。'};
  if (action === 'embedding_rebuild' && (!Number.isInteger(status.chunk_count) || status.chunk_count < 0 || status.chunk_count > (status.max_chunks || 128))) return {allowed: false, reason: '请将片段数量控制在重建上限内，再刷新状态。'};
  return {allowed: embeddingActions.has(action), reason: ''};
}

export function embeddingUsage(value) {
  const count = item => Number.isInteger(item) && item >= 0 ? item : null;
  return {requests: count(value?.request_count), inputTokens: count(value?.usage?.input_tokens ?? value?.usage?.prompt_tokens),
    totalTokens: count(value?.usage?.total_tokens)};
}

export function operationOutcome(run) {
  if (!run) return {kind: 'idle', label: '尚未执行'};
  if (run.status === 'unknown') return {kind: 'unknown', label: '结果未知，需要核查', code: run.error};
  if (run.status === 'waiting_approval') return {kind: 'approval', label: '等待你的审批'};
  if (run.status === 'paused') return {kind: 'paused', label: '运行已暂停'};
  if (run.status === 'cancelled') return {kind: 'cancelled', label: '已取消', code: run.error};
  if (run.status === 'failed') return {kind: 'failed', label: '操作未完成', code: run.error};
  if (run.status !== 'completed') return {kind: 'running', label: '正在执行'};
  const result = run.tool_result;
  if (run.error || !result || result.name !== run.operation?.name || result.error_code || result.value?.ok !== true) {
    return {kind: 'failed', label: '操作未完成', code: run.error || result?.error_code || result?.value?.error_code || 'invalid_result'};
  }
  return {kind: 'success', label: '操作已完成', value: result.value};
}

function canonical(value) {
  if (Array.isArray(value)) return JSON.stringify(value.map(item => JSON.parse(canonical(item))));
  if (value && typeof value === 'object') return JSON.stringify(Object.fromEntries(Object.keys(value).sort().map(key => [key, JSON.parse(canonical(value[key]))])));
  return JSON.stringify(value);
}

export function belongsToCollection(run, collection, request) {
  const name = collection?.tools?.[request?.action];
  const snapshot = run?.tool_snapshot?.find(item => item.name === name);
  const identity = actionRecoveryIdentity(collection, request?.action);
  return !!name && run?.kind === 'tool' && identifier.test(run.id || '') && run.operation?.name === name
    && /^[a-f0-9]{64}$/.test(identity || '') && snapshot?.recovery_identity === identity
    && canonical(run.operation.arguments) === canonical(request.arguments)
    && (!run.tool_result || run.tool_result.name === name);
}

export function canImport(inspected) {
  return inspected?.ok === true && ['current', 'stale', 'unindexed'].includes(inspected.status)
    && /^[a-f0-9]{64}$/.test(inspected.source_sha256 || '')
    && (inspected.indexed_sha256 === '' || /^[a-f0-9]{64}$/.test(inspected.indexed_sha256 || ''));
}

export class KnowledgeSession {
  constructor({request, read = () => null, write = () => {}, changed = () => {},
    allowed = () => true,
    makeId = () => crypto.randomUUID(), later = (callback, delay) => globalThis.setTimeout(callback, delay),
    cancelLater = timer => globalThis.clearTimeout(timer)}) {
    this.request = request; this.write = write; this.changed = changed; this.makeId = makeId; this.allowed = allowed;
    this.later = later; this.cancelLater = cancelLater; this.records = {}; this.revisions = {}; this.generation = 0;
    try {
      const records = JSON.parse(read() || '{}');
      for (const [id, record] of Object.entries(records).slice(0, 16)) {
        if (identifier.test(id) && record && identifier.test(record.request?.request_id || '')
          && actionLabels[record.request?.action] && record.request.arguments && typeof record.request.arguments === 'object'
          && (!record.run_id || identifier.test(record.run_id))) this.records[id] = record;
      }
    } catch { /* Unavailable or corrupt browser storage never authorizes work. */ }
    this.state = {collection: null, run: null, busy: false, pending: null, error: ''};
  }
  _save() { try { this.write(JSON.stringify(this.records)); } catch {} }
  _emit() { this.changed(this.state); }
  _next(collection) { return this.revisions[collection.id] = (this.revisions[collection.id] || 0) + 1; }
  _current(collection, request, revision) {
    return this.records[collection.id]?.request.request_id === request.request_id && this.revisions[collection.id] === revision;
  }
  suspend() {
    this.generation++; this.cancelLater(this.timer); this.timer = null;
    this.controller?.abort(); this.controller = null;
  }
  async select(collection) {
    this.suspend(); const generation = this.generation;
    const record = this.records[collection.id];
    this.state = {collection, run: null, busy: false, pending: record?.request || null, error: ''}; this._emit();
    if (record?.run_id) await this.refresh(generation);
    else if (record) { this.state.error = '上一请求尚未确认。请用原请求重新确认，避免重复执行。'; this._emit(); }
  }
  locked() { return this.state.busy || !!this.state.pending && (!this.state.run || !operationTerminal.has(this.state.run.status)) || this.state.run?.status === 'unknown'; }
  _schedule(generation) {
    this.cancelLater(this.timer);
    if (generation === this.generation && this.state.run && !operationTerminal.has(this.state.run.status)) {
      this.timer = this.later(() => this.refresh(generation), 1000);
    }
  }
  _accept(run, collection, request, generation, revision) {
    // View epochs alone cannot reject a late GET for an earlier operation in
    // the same collection, or a snapshot fetched before a cancel/approval.
    if (!this._current(collection, request, revision)) return;
    if (!belongsToCollection(run, collection, request)) {
      const error = new Error('运行与当前集合或请求不匹配，已停止显示和审批。');
      error.code = 'operation_mismatch'; throw error;
    }
    this.records[collection.id] = {run_id: run.id, request}; this._save();
    if (generation !== this.generation) return;
    const unchanged = !this.state.error && JSON.stringify(this.state.run) === JSON.stringify(run);
    this.state.run = run; this.state.pending = request; this.state.error = '';
    if (!unchanged) this._emit();
    this._schedule(generation);
  }
  async start(action, args) {
    if (!this.state.collection || this.locked() || !actionLabels[action] || !this.allowed(action)) return false;
    const request = {action, arguments: structuredClone(args), request_id: this.makeId()};
    this.records[this.state.collection.id] = {request}; this._save();
    this.state.pending = request; this.state.run = null;
    await this._send(request); return true;
  }
  async retry() {
    if (this.state.busy || !this.state.pending || this.state.run || !this.allowed(this.state.pending.action)) return;
    await this._send(this.state.pending);
  }
  async _send(request) {
    const collection = this.state.collection, generation = this.generation;
    const revision = this._next(collection);
    this.state.busy = true; this.state.error = ''; this._emit();
    try {
      // A POST may have reached the server even if the browser view changes.
      // Do not abort it or replace its request ID on a transport failure.
      const run = await this.request(`/api/knowledge/${encodeURIComponent(collection.id)}/operations`, 'POST', request);
      this._accept(run, collection, request, generation, revision);
    } catch (error) {
      if (!this._current(collection, request, revision)) return;
      if ([400, 403, 404].includes(error.status)) {
        delete this.records[collection.id]; this._save();
        if (generation === this.generation) this.state.pending = null;
      }
      if (generation === this.generation) this.state.error = error.message;
    } finally {
      if (generation === this.generation) { this.state.busy = false; this._emit(); }
    }
  }
  async refresh(generation = this.generation) {
    const collection = this.state.collection, record = this.records[collection?.id];
    if (!record?.run_id || generation !== this.generation) return;
    const revision = this._next(collection);
    this.controller?.abort(); this.controller = new AbortController();
    try {
      const run = await this.request('/api/runs/' + encodeURIComponent(record.run_id), 'GET', undefined, this.controller.signal);
      this._accept(run, collection, record.request, generation, revision);
    } catch (error) {
      if (this._current(collection, record.request, revision) && generation === this.generation && error.name !== 'AbortError') {
        if (error.code === 'operation_mismatch') this.state.run = null;
        this.state.error = error.message; this._emit();
      }
    }
  }
  async control(action, body = {}) {
    const {run, collection, pending} = this.state;
    if (!run || this.state.busy || !belongsToCollection(run, collection, pending)) return;
    if (action === 'approve' && run.status !== 'waiting_approval') return;
    if (action === 'cancel' && operationTerminal.has(run.status)) return;
    if (action === 'resume' && run.status !== 'paused') return;
    if (!['approve', 'cancel', 'resume'].includes(action)) return;
    if ((action === 'resume' || action === 'approve' && body.approved === true) && !this.allowed(pending.action)) return;
    const generation = this.generation;
    const revision = this._next(collection);
    this.controller?.abort();
    this.cancelLater(this.timer); this.state.busy = true; this.state.error = ''; this._emit();
    try {
      const updated = await this.request(`/api/runs/${encodeURIComponent(run.id)}/${action}`, 'POST', body);
      this._accept(updated, collection, pending, generation, revision);
    } catch (error) {
      if (this._current(collection, pending, revision) && generation === this.generation) { this.state.error = error.message; await this.refresh(generation); }
    } finally {
      if (generation === this.generation) { this.state.busy = false; this._emit(); }
    }
  }
}

// Saving non-secret settings is separate from a provider operation. A failed
// or uncertain CAS write never silently retries or replaces a newer profile.
export class EmbeddingConfigSession {
  constructor({request, changed = () => {}}) {
    this.request = request; this.changed = changed; this.generation = 0;
    this.state = {collectionId: null, data: null, busy: false, error: '', uncertain: false};
  }
  suspend() { this.generation++; }
  async select(collectionId) {
    const generation = ++this.generation;
    this.state = {collectionId, data: null, busy: true, error: '', uncertain: false}; this.changed(this.state);
    try {
      const data = await this.request(`/api/knowledge/${encodeURIComponent(collectionId)}/embedding-config`);
      if (generation === this.generation) this.state.data = data;
    } catch (error) {
      if (generation === this.generation) this.state.error = error.message;
    } finally {
      if (generation === this.generation) { this.state.busy = false; this.changed(this.state); }
    }
  }
  async save(embedding) {
    const {collectionId, data, busy, uncertain} = this.state;
    if (busy || uncertain || !data?.editable || !/^[a-f0-9]{64}$/.test(data.config_revision || '')) return false;
    const generation = this.generation;
    const body = {embedding: structuredClone(embedding), expected_revision: data.config_revision};
    this.state.busy = true; this.state.error = ''; this.changed(this.state);
    try {
      const updated = await this.request(`/api/knowledge/${encodeURIComponent(collectionId)}/embedding-config`, 'PUT', body);
      if (generation === this.generation) this.state.data = updated;
      return true;
    } catch (error) {
      if (generation === this.generation) {
        this.state.uncertain = ![400, 403, 404, 409].includes(error.status);
        this.state.error = error.status === 409 ? '配置已被其他操作修改。保留了当前表单；请重新读取配置后核对再保存。'
          : this.state.uncertain ? '保存结果尚未确认。请重新读取配置并核对，页面不会自动重复保存。' : error.message;
      }
      return false;
    } finally {
      if (generation === this.generation) { this.state.busy = false; this.changed(this.state); }
    }
  }
}
