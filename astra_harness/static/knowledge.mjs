import {KnowledgeSession, EmbeddingConfigSession, operationOutcome, operationTerminal, actionLabels,
  embeddingActions, embeddingReadiness, embeddingUsage, canImport} from './knowledge-state.mjs';
import {formatSize} from './state.mjs';

const statusNames = {current: '来源一致', stale: '来源已变化', missing: '源文件已移除', unreadable: '来源暂不可读', unindexed: '尚未导入'};
const problemNames = {source_changed: '材料在确认后发生了变化。请重新检查材料，再提交审批。',
  index_changed: '索引版本已变化。请刷新资料，再重新确认操作。', approval_denied: '你已拒绝此次操作，索引没有因此变更。',
  permission_denied: '当前运行没有所需权限。', forbidden_path: '这个路径不允许作为知识材料。',
  index_inconsistent: '索引片段与原文不一致。请核查后移除并重新导入。', invalid_result: '没有收到可验证的工具结果。',
  corpus_changed: '语料版本已变化。请刷新状态，再重新确认操作。', embedding_index_stale: '语义索引已失效，请重建后再检索。',
  embedding_not_configured: '尚未启用 embedding，词法检索仍可使用。', embedding_missing_key: '后端缺少配置的密钥环境变量。',
  embedding_provider_error: '服务请求未成功。请检查服务配置与运行记录；页面没有改用词法结果。',
  cancelled: '运行已取消。请以工具记录和索引实际状态为准。'};
const embeddingStateNames = {not_configured: '未配置', missing_key: '缺少凭据', invalid_key: '凭据无效',
  not_built: '尚未建立语义索引', ready: '语义索引就绪', rebuild_required: '需要重建', provider_error: '服务请求失败'};
const $ = id => document.getElementById(id);
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function button(text, handler, className = 'secondary') {
  const element = node('button', text, className); element.type = 'button'; element.addEventListener('click', handler); return element;
}
function saved(key) { try { return localStorage.getItem(key); } catch { return null; } }
function remember(key, value) { try { localStorage.setItem(key, value); } catch {} }
function explain(code) { return problemNames[code] || (code ? `操作未完成（${code}）。请查看运行记录。` : '请检查本地服务后重试。'); }

export function createKnowledgeView({api, onOpen, onClose, onMenu, onOpenRun}) {
  let visible = false, generation = 0, controller = null, collection = null, collections = [], catalog = null;
  let loading = false, inspecting = false, prepared = null, queryResult = null, observedTerminal = null;
  let embeddingStatus = null, statusLoading = false, statusError = '', configDirty = false, renderedConfig = null, statusRevision = 0;
  const session = new KnowledgeSession({request: api,
    read: () => saved('astra.knowledge.operations'), write: value => remember('astra.knowledge.operations', value),
    changed: renderOperation, allowed: embeddingAllowed});
  const settings = new EmbeddingConfigSession({request: api, changed: renderEmbeddingConfig});

  function embeddingAllowed(action) {
    if (!embeddingActions.has(action)) return true;
    return !settings.state.busy && !settings.state.uncertain && !configDirty && !statusLoading
      && !!collection?.tools?.[action] && embeddingReadiness(settings.state.data, embeddingStatus, action).allowed;
  }
  function semanticMode() { return $('knowledge-mode').value === 'semantic'; }

  function showError(message = '') { $('knowledge-error').textContent = message; $('knowledge-error').hidden = !message; }
  function resetReads() {
    generation++; controller?.abort(); controller = new AbortController();
    // The previous catalog read cannot clear this flag after its generation
    // is invalidated. Its busy state must not survive closing or reopening.
    loading = false;
    return generation;
  }
  function controls() {
    const blocked = session.locked(), available = !!collection;
    $('knowledge-refresh').disabled = !available || loading || inspecting;
    $('knowledge-file').disabled = !available || loading || inspecting || blocked;
    $('knowledge-inspect').disabled = !available || loading || inspecting || blocked || !$('knowledge-file').value;
    $('knowledge-search').disabled = !available || blocked || loading || !$('knowledge-query').value.trim()
      || semanticMode() && !embeddingAllowed('semantic_search');
    $('knowledge-query').disabled = !available || blocked;
    for (const control of $('knowledge-documents').querySelectorAll('button')) control.disabled = blocked || loading || inspecting;
    const submit = $('knowledge-import'); if (submit) submit.disabled = blocked || inspecting || !canImport(prepared);
    const config = settings.state;
    const editable = config.data?.editable && !config.busy && !config.uncertain && !blocked;
    for (const input of $('embedding-config-form').querySelectorAll('input')) input.disabled = !editable;
    $('embedding-save').disabled = !editable || !configDirty;
    $('embedding-reload').disabled = config.busy;
    $('embedding-refresh').disabled = !available || statusLoading;
    $('embedding-test').disabled = blocked || !embeddingAllowed('embedding_test');
    $('embedding-rebuild').disabled = blocked || !embeddingAllowed('embedding_rebuild');
    $('knowledge-mode').disabled = !available || blocked;
  }

  function formProfile() {
    return {enabled: $('embedding-enabled').checked, endpoint: $('embedding-endpoint').value.trim(),
      model: $('embedding-model').value.trim(), key_env: $('embedding-key-env').value.trim(),
      dimensions: $('embedding-dimensions').value === '' ? null : Number($('embedding-dimensions').value),
      revision: $('embedding-revision').value.trim(), timeout: Number($('embedding-timeout').value)};
  }
  function renderEmbeddingConfig() {
    const {data, busy, error, uncertain} = settings.state;
    if (data && data !== renderedConfig) {
      const profile = data.saved || {};
      $('embedding-enabled').checked = !!profile.enabled;
      for (const field of ['endpoint', 'model', 'key_env', 'dimensions', 'revision', 'timeout']) {
        $('embedding-' + field.replace('_', '-')).value = profile[field] ?? '';
      }
      configDirty = false; renderedConfig = data;
    }
    $('embedding-endpoint').required = $('embedding-enabled').checked;
    $('embedding-model').required = $('embedding-enabled').checked;
    $('embedding-key-env').required = true; $('embedding-revision').required = true; $('embedding-timeout').required = true;
    const profiles = $('embedding-profiles'); profiles.replaceChildren();
    if (data) {
      for (const [label, profile] of [['当前加载', data.active], ['已保存', data.saved]]) {
        const row = node('div'); row.append(node('strong', label), node('span', profile?.enabled
          ? `${profile.model} · ${profile.endpoint}` : '未启用，使用本地词法检索'));
        profiles.append(row);
      }
    }
    const notice = data?.restart_required ? '配置已保存，尚未加载。请重启工作台后重新打开知识库；重启前禁止新的语义操作。'
      : data && !data.editable ? '此启动方式没有可保存的知识配置文件。请使用 --knowledge-config 配置集合。'
      : uncertain ? '保存结果未确认，请重新读取配置并核对。'
      : configDirty ? '表单有未保存修改。保存或重新读取后，才能执行语义操作。' : '';
    $('embedding-config-notice').textContent = notice; $('embedding-config-notice').hidden = !notice;
    $('embedding-config-error').textContent = error; $('embedding-config-error').hidden = !error;
    $('embedding-save').textContent = busy ? '正在读取 / 保存…' : '保存非秘密配置';
    if (data?.restart_required) { queryResult = null; renderResults(); }
    renderEmbeddingStatus(); renderOperation(); controls();
  }
  function renderEmbeddingStatus() {
    const config = settings.state.data;
    $('embedding-state').textContent = config?.restart_required ? '待重启生效' : settings.state.busy || statusLoading
      ? '正在读取…' : embeddingStateNames[embeddingStatus?.state] || '状态未读取';
    $('embedding-status-details').textContent = embeddingStatus
      ? `已导入 ${embeddingStatus.document_count ?? 0} 份文档 · ${embeddingStatus.chunk_count ?? 0} 个片段 / 上限 ${embeddingStatus.max_chunks ?? 128}${embeddingStatus.estimated_requests ? ' · 重建预计 ' + embeddingStatus.estimated_requests + ' 次请求' : ''}` : '';
    $('embedding-readiness').textContent = statusError || (embeddingStatus?.error_code ? explain(embeddingStatus.error_code) : '')
      || (configDirty ? '有未保存的配置修改。' : embeddingReadiness(config, embeddingStatus, semanticMode() ? 'semantic_search' : 'embedding_rebuild').reason);
    const transfer = config?.active?.enabled ? `外传目标：${config.active.endpoint} · 模型：${config.active.model}。测试发送固定合成短文本；重建发送本集合已导入片段；语义查询发送查询文本。均需审批，服务可能计费。` : '';
    $('embedding-transfer-note').textContent = transfer; $('embedding-transfer-note').hidden = !transfer;
    controls();
  }
  async function refreshEmbeddingStatus() {
    if (!collection) return;
    const token = generation, revision = ++statusRevision; statusLoading = true; statusError = ''; renderEmbeddingStatus();
    try {
      const data = await api(`/api/knowledge/${encodeURIComponent(collection.id)}/embedding-status`, 'GET', undefined, controller.signal);
      if (token !== generation || revision !== statusRevision || !visible) return;
      if (data.ok !== true) throw new Error(explain(data.error_code));
      embeddingStatus = data;
    } catch (error) {
      if (token === generation && revision === statusRevision && error.name !== 'AbortError') { embeddingStatus = null; statusError = error.message; }
    } finally {
      if (token === generation && revision === statusRevision) { statusLoading = false; renderEmbeddingStatus(); renderOperation(); }
    }
  }
  async function refreshCatalog() {
    if (!collection) return;
    const token = generation, current = collection;
    loading = true; $('knowledge-loading').hidden = false; controls();
    try {
      const data = await api('/api/knowledge/' + encodeURIComponent(current.id), 'GET', undefined, controller.signal);
      if (token !== generation || !visible) return;
      if (data.ok !== true) throw new Error(explain(data.error_code));
      catalog = data; renderCatalog();
    } catch (error) { if (token === generation && error.name !== 'AbortError') showError(error.message); }
    finally { if (token === generation) { loading = false; $('knowledge-loading').hidden = true; controls(); } }
  }
  function renderCatalog() {
    const select = $('knowledge-file'), previous = select.value;
    select.replaceChildren(node('option', '选择一个 .md / .txt 材料')); select.firstChild.value = '';
    for (const file of catalog?.available_files || []) {
      const option = node('option', `${file.path} · ${formatSize(file.bytes)}`); option.value = file.path; select.append(option);
    }
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
    $('knowledge-candidate-note').textContent = (catalog?.available_files?.length || 0)
      ? '只列出本集合目录中的材料。选中后读取摘要，不会自动导入。'
      : catalog?.documents?.length ? '可见材料已在索引中；如需更新，使用下方“检查 / 更新”。'
      : '没有可导入材料。请先把 UTF-8 .md / .txt 放入本集合的材料目录，再刷新。';
    $('knowledge-truncated').hidden = !catalog?.truncated;
    const list = $('knowledge-documents'); list.replaceChildren();
    const docs = catalog?.documents || [];
    $('knowledge-document-count').textContent = `${docs.length} 份已索引`;
    if (!docs.length) list.append(node('p', '索引还是空的。选择一份材料，走完一次导入审批。', 'knowledge-empty'));
    for (const doc of docs) {
      const item = node('article', undefined, 'knowledge-document');
      const heading = node('div', undefined, 'knowledge-document-title'); heading.append(node('strong', doc.path));
      const status = node('span', statusNames[doc.status] || '待检查', 'knowledge-badge ' + (doc.status === 'current' ? 'current' : 'stale'));
      item.append(heading, status, node('p', `${formatSize(doc.indexed_bytes)} 已索引${doc.status === 'stale' ? ' · 更新前不会返回旧内容' : ''}`, 'knowledge-subtle'));
      const actions = node('div', undefined, 'knowledge-inline-actions');
      actions.append(button('检查 / 更新', () => inspect(doc.path), 'text-button'));
      actions.append(button('移除索引', () => { prepared = null; renderPrepared(); session.start('remove', {path: doc.path, expected_index_sha256: doc.indexed_sha256}); }, 'text-button knowledge-remove'));
      item.append(actions); list.append(item);
    }
    controls();
  }
  async function inspect(path) {
    if (!collection || session.locked() || inspecting) return;
    const token = generation; inspecting = true; prepared = null; renderPrepared(); showError(); controls();
    try {
      const value = await api(`/api/knowledge/${encodeURIComponent(collection.id)}/inspect?path=${encodeURIComponent(path)}`, 'GET', undefined, controller.signal);
      if (token !== generation || !visible) return;
      prepared = value; renderPrepared();
      if (!canImport(value)) showError(explain(value.error_code) + ' 该材料暂时不能导入。');
    } catch (error) { if (token === generation && error.name !== 'AbortError') showError(error.message); }
    finally { if (token === generation) { inspecting = false; controls(); } }
  }
  function renderPrepared() {
    const box = $('knowledge-prepared'); box.replaceChildren(); box.hidden = !prepared;
    if (!prepared) return;
    box.append(node('span', '确认材料', 'knowledge-overline'), node('strong', prepared.path),
      node('p', `${statusNames[prepared.status] || '需要检查'} · ${formatSize(prepared.source_bytes)}`, 'knowledge-subtle'));
    const details = node('details'); details.append(node('summary', '查看本次绑定的摘要'),
      node('code', `原文 SHA-256：${prepared.source_sha256 || '不可用'}\n索引 SHA-256：${prepared.indexed_sha256 || '尚未索引'}`));
    box.append(details, node('p', '提交后仍需批准。材料或索引版本变化时，这次批准不会被用于新内容。', 'knowledge-subtle'));
    const submit = button('提交导入审批', () => {
      if (!canImport(prepared)) return;
      session.start('import_file', {path: prepared.path, expected_source_sha256: prepared.source_sha256,
        expected_index_sha256: prepared.indexed_sha256});
    }, 'primary'); submit.id = 'knowledge-import';
    const actions = node('div', undefined, 'knowledge-inline-actions');
    actions.append(submit, button('取消准备', () => { prepared = null; renderPrepared(); }, 'text-button'));
    box.append(actions); controls();
  }
  function renderOperation() {
    const state = session.state, box = $('knowledge-operation');
    if (!box) return;
    const parametersOpen = box.querySelector('details')?.open || false;
    box.replaceChildren(); box.hidden = !state.pending;
    if (!state.pending) { if (state.error) showError(state.error); controls(); return; }
    const outcome = operationOutcome(state.run), request = state.pending;
    box.className = 'knowledge-operation ' + outcome.kind;
    const head = node('div', undefined, 'knowledge-operation-head');
    head.append(node('strong', actionLabels[request.action] || '知识操作'), node('span', state.busy ? '正在确认…' : state.run ? outcome.label : '请求尚未确认', 'knowledge-badge'));
    const external = embeddingActions.has(request.action);
    const target = request.arguments.path || (request.action === 'embedding_test' ? '固定合成短文本，不读取材料'
      : request.action === 'embedding_rebuild' ? `本集合已导入片段 · 语料 ${request.arguments.expected_corpus_sha256?.slice(0, 12) || ''}`
      : `查询：${request.arguments.query || ''}`);
    box.append(head, node('p', target, 'knowledge-operation-target'));
    if (state.run) box.append(node('p', `运行 ${state.run.id.slice(0, 12)} · 任务模型 ${state.run.model_calls ?? 0} 次 · 工具 ${state.run.tool_calls ?? 0} 次`, 'knowledge-subtle'));
    if (['import_file', 'remove'].includes(request.action)) {
      const details = node('details'); details.open = parametersOpen;
      details.append(node('summary', '查看本次审批参数'), node('code',
        `${request.action === 'import_file' ? '原文 SHA-256：' + request.arguments.expected_source_sha256 + '\n' : ''}索引 SHA-256：${request.arguments.expected_index_sha256 || '尚未索引'}\n材料：${request.arguments.path}`));
      box.append(details);
    }
    if (external) {
      const active = settings.state.data?.active;
      const usage = embeddingUsage(state.run?.tool_result?.value);
      const waiting = state.run?.status === 'waiting_approval' && state.run.tool_calls === 0;
      const emptyRebuild = request.action === 'embedding_rebuild' && embeddingStatus?.chunk_count === 0
        && request.arguments.expected_corpus_sha256 === embeddingStatus.corpus_sha256;
      box.append(node('p', `Embedding 请求：${usage.requests === null ? waiting ? '等待审批，工具尚未执行' : '未报告，不能按零次计算' : usage.requests + ' 次'}${usage.inputTokens !== null ? ' · 已报告输入 ' + usage.inputTokens + ' token' : ''}${usage.totalTokens !== null ? ' · 已报告总计 ' + usage.totalTokens + ' token' : ''}`, 'knowledge-subtle'));
      if (outcome.kind === 'success' && Number.isInteger(outcome.value.dimensions)) box.append(node('p', `已验证向量维数：${outcome.value.dimensions}${Number.isInteger(outcome.value.chunk_count) ? ' · 索引片段 ' + outcome.value.chunk_count : ''}`, 'knowledge-subtle'));
      box.append(node('p', emptyRebuild ? '当前语料为空。本次仅清理本地语义索引，不向服务发送片段。' : active?.enabled ? `使用当前加载的 ${active.model}，目标为 ${active.endpoint}；服务调用可能产生费用。`
        : '这是 embedding 操作。当前配置尚未读取完成，暂不能批准。', 'knowledge-stale-note'));
      const details = node('details'); details.open = parametersOpen;
      const binding = state.run?.tool_snapshot?.find(item => item.name === state.run?.operation?.name)?.recovery_identity || '';
      details.append(node('summary', '查看本次审批参数'), node('code', `语料 SHA-256：${request.arguments.expected_corpus_sha256 || '固定测试文本，不发送语料'}\n配置身份：${binding || '等待服务确认'}`));
      box.append(details);
      if (settings.state.data?.restart_required) box.append(node('p', '这条记录属于当前已加载的旧配置。新配置尚未生效，不能把它当作新配置的验证结果。'));
    }
    if (outcome.kind === 'approval') {
      box.append(node('p', external ? request.action === 'embedding_test' ? '批准后发送一条固定合成短文本，验证连接与向量响应。'
        : request.action === 'embedding_rebuild' ? embeddingStatus?.chunk_count === 0 && request.arguments.expected_corpus_sha256 === embeddingStatus.corpus_sha256
          ? '批准后只清理当前空语料对应的本地语义索引；不会请求 embedding 服务。'
          : '批准后发送本集合已导入片段并建立语义索引；授权绑定当前语料与配置。'
        : '批准后发送这条查询文本进行语义检索；授权绑定当前语料与配置。'
        : request.action === 'remove' ? '只移除此版本的索引记录，源文件会保留。' : '批准后才会更新索引。授权绑定当前选定材料与索引摘要。'));
    } else if (outcome.kind === 'unknown') {
      box.append(node('p', '执行是否产生效果尚不确定。请查看执行过程并核查索引，不能在这里重试这次操作。'));
    } else if (outcome.kind === 'failed' || outcome.kind === 'cancelled') box.append(node('p', explain(outcome.code)));
    if (state.error) box.append(node('p', state.error, 'knowledge-operation-error'));
    const actions = node('div', undefined, 'knowledge-inline-actions');
    if (!state.run) {
      const hasRun = session.records[state.collection?.id]?.run_id;
      actions.append(button(hasRun ? '重新读取状态' : '重新确认请求', () => hasRun ? session.refresh() : session.retry(), 'secondary'));
    }
    else {
      if (outcome.kind === 'approval') {
        actions.append(button('批准并执行', () => session.control('approve', {approved: true}), 'primary'),
          button('拒绝', () => session.control('approve', {approved: false}), 'secondary'));
      }
      if (outcome.kind === 'paused') actions.append(button('继续运行', () => session.control('resume'), 'primary'));
      if (!operationTerminal.has(state.run.status)) actions.append(button('取消运行', () => session.control('cancel'), 'secondary'));
      if (state.error) actions.append(button('重新读取状态', () => session.refresh(), 'secondary'));
      actions.append(button('查看执行过程 →', () => onOpenRun(state.run), 'text-button'));
    }
    for (const control of actions.children) control.disabled = state.busy || external && !embeddingAllowed(request.action)
      && ['批准并执行', '继续运行', '重新确认请求'].includes(control.textContent);
    box.append(actions); controls();
    if (state.run && operationTerminal.has(state.run.status)) {
      const marker = `${state.run.id}:${state.run.status}`;
      if (marker !== observedTerminal) {
        observedTerminal = marker;
        if (['search', 'semantic_search'].includes(request.action)) {
          queryResult = outcome.kind === 'success' && !(external && settings.state.data?.restart_required) ? outcome.value : null; renderResults();
        } else { prepared = null; queryResult = null; renderPrepared(); renderResults(); if (visible) refreshCatalog(); }
        if (visible) refreshEmbeddingStatus();
      }
    }
  }
  function renderResults() {
    const box = $('knowledge-results'); box.replaceChildren();
    if (!queryResult) { box.append(node('p', semanticMode()
      ? '输入查询后，先确认外传目标并审批。语义检索不会自动改用词法结果，也不会生成对话答案。'
      : '输入材料中的字词，查看可核对的原文片段。词法检索在本机执行，不会生成模型答案。', 'knowledge-empty')); return; }
    const hits = queryResult.hits || [];
    if (queryResult.stale_count) box.append(node('p', `有 ${queryResult.stale_count} 个候选片段的来源已变化或不可读，未返回旧内容。请刷新资料并更新索引。`, 'knowledge-stale-note'));
    const method = queryResult.mode === 'semantic' ? '语义结果' : '词法结果';
    box.append(node('p', hits.length ? `${method} · “${queryResult.query || ''}” · ${hits.length} 个片段 · 已按查询时的原文核对`
      : `${method}：没有匹配片段。请检查材料是否已导入，或调整查询。`, 'knowledge-subtle'));
    for (const [index, hit] of hits.entries()) {
      const item = node('article', undefined, 'knowledge-hit');
      const heading = node('div', undefined, 'knowledge-hit-heading'); heading.append(node('span', String(index + 1).padStart(2, '0'), 'knowledge-hit-number'), node('strong', hit.path));
      item.append(heading, node('p', `第 ${hit.start_line} 行 ${hit.start_column} 列 → 第 ${hit.end_line} 行 ${hit.end_column} 列（末端不含）`, 'knowledge-subtle'), node('pre', hit.text, 'knowledge-quote'));
      if (queryResult.mode === 'semantic' && Number.isFinite(hit.score)) item.append(node('p', `向量相似度 ${hit.score.toFixed(3)} · 不是答案正确率`, 'knowledge-subtle'));
      const details = node('details'); details.append(node('summary', '核对引用'), node('code', `SHA-256：${hit.document_sha256}\n字符范围：[${hit.start_offset}, ${hit.end_offset})\n片段 ID：${hit.chunk_id}`));
      item.append(details); box.append(item);
    }
    if (queryResult.truncated) box.append(node('p', '达到本次返回上限，可用更具体的词语继续检索。', 'knowledge-subtle'));
  }
  async function choose(id) {
    const selected = collections.find(item => item.id === id); if (!selected) return;
    resetReads(); collection = selected; catalog = null; prepared = null; queryResult = null; observedTerminal = null; inspecting = false;
    embeddingStatus = null; statusError = ''; statusLoading = false; configDirty = false; renderedConfig = null;
    $('embedding-config-form').reset(); $('knowledge-mode').value = 'lexical'; updateSearchMode();
    remember('astra.knowledge.collection', id); $('knowledge-collection').value = id;
    $('knowledge-scope').textContent = `材料目录：${selected.source_scope} · 独立索引，不跨集合检索`;
    showError(); renderPrepared(); renderResults(); renderCatalog();
    await Promise.all([session.select(selected), refreshCatalog(), settings.select(selected.id), refreshEmbeddingStatus()]);
  }
  async function open() {
    visible = true; onOpen(); const token = resetReads();
    $('knowledge-unavailable').hidden = true; $('knowledge-content').hidden = true; $('knowledge-loading').hidden = false; showError();
    try {
      const info = await api('/api/knowledge', 'GET', undefined, controller.signal);
      if (token !== generation || !visible) return;
      collections = Array.isArray(info.collections) ? info.collections : [];
      if (!info.enabled || !collections.length) {
        const otherWorkspace = info.reason === 'workspace_not_configured';
        $('knowledge-unavailable-title').textContent = otherWorkspace ? '未为此工作区配置知识库。' : '先为工作台配置一份本地索引。';
        $('knowledge-unavailable-help').textContent = otherWorkspace ? '知识库与 MCP 绑定启动目录。切回启动工作区后可继续查看原材料；当前目录不会沿用旧集合或索引。' : '启动时提供 --knowledge-index，或通过 --knowledge-config 配置多个独立集合。页面不会自动读取或导入你的材料。';
        $('knowledge-unavailable').hidden = false; return;
      }
      const select = $('knowledge-collection'); select.replaceChildren();
      for (const item of collections) { const option = node('option', item.label); option.value = item.id; select.append(option); }
      $('knowledge-content').hidden = false;
      await choose(collections.some(item => item.id === saved('astra.knowledge.collection')) ? saved('astra.knowledge.collection') : collections[0].id);
    } catch (error) { if (token === generation && error.name !== 'AbortError') showError(error.message); }
    finally { if (token === generation && visible && !loading) $('knowledge-loading').hidden = true; }
  }
  function close() { visible = false; resetReads(); session.suspend(); settings.suspend(); onClose(); }
  function reset() {
    close(); collection = null; collections = []; catalog = null; prepared = null; queryResult = null;
    embeddingStatus = null; statusRevision++; loading = false; inspecting = false;
    $('knowledge-content').hidden = true; $('knowledge-unavailable').hidden = true;
    $('knowledge-collection').replaceChildren(); $('knowledge-query').value = '';
    // Persisted operation records remain available on returning to their binding.
    // No operation is retried or cancelled merely by changing the visible root.
    renderPrepared(); renderResults(); showError();
  }
  function updateSearchMode() {
    $('knowledge-search-mode-badge').textContent = semanticMode() ? '语义检索' : '词法检索';
    $('knowledge-query-label').textContent = semanticMode() ? '你想寻找的内容' : '材料中的字词';
    queryResult = null; renderResults(); renderEmbeddingStatus(); controls();
  }
  $('knowledge-back').addEventListener('click', close);
  $('knowledge-menu').addEventListener('click', onMenu);
  $('knowledge-collection').addEventListener('change', event => choose(event.target.value));
  $('knowledge-refresh').addEventListener('click', () => { showError(); prepared = null; queryResult = null; renderPrepared(); renderResults(); refreshCatalog(); refreshEmbeddingStatus(); });
  $('knowledge-file').addEventListener('change', () => { prepared = null; renderPrepared(); controls(); });
  $('knowledge-inspect').addEventListener('click', () => inspect($('knowledge-file').value));
  $('knowledge-query').addEventListener('input', controls);
  $('knowledge-mode').addEventListener('change', updateSearchMode);
  $('knowledge-search-form').addEventListener('submit', event => {
    event.preventDefault(); const query = $('knowledge-query').value.trim(); if (!query || session.locked()) return;
    const semantic = semanticMode(); if (semantic && !embeddingAllowed('semantic_search')) return;
    queryResult = null; renderResults(); showError(); session.start(semantic ? 'semantic_search' : 'search', semantic
      ? {query, limit: 3, expected_corpus_sha256: embeddingStatus.corpus_sha256} : {query, limit: 5});
  });
  $('embedding-config-form').addEventListener('input', () => { configDirty = true; renderEmbeddingConfig(); });
  $('embedding-config-form').addEventListener('submit', async event => {
    event.preventDefault(); if (session.locked() || !configDirty || !$('embedding-config-form').reportValidity()) return;
    await settings.save(formProfile());
  });
  $('embedding-reload').addEventListener('click', open);
  $('embedding-refresh').addEventListener('click', refreshEmbeddingStatus);
  $('embedding-test').addEventListener('click', () => { if (embeddingAllowed('embedding_test')) session.start('embedding_test', {}); });
  $('embedding-rebuild').addEventListener('click', () => { if (embeddingAllowed('embedding_rebuild')) session.start('embedding_rebuild', {expected_corpus_sha256: embeddingStatus.corpus_sha256}); });
  window.addEventListener('beforeunload', () => session.suspend());
  return {open, close, reset};
}
