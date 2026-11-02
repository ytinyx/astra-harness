const errors = {
  server_busy: '还有任务或配置操作正在进行。请先暂停或停止运行，并等待连接测试、配置保存完成后再退出。',
  server_changed: '服务已重新启动，请刷新页面后再操作。',
  server_shutdown_unavailable: '当前启动方式不支持网页退出，请在启动终端停止服务。',
  invalid_origin: '页面来源校验失败，请从本地工作台地址重新打开。',
};

async function request(path, method = 'GET', body) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch(path, {method, signal: controller.signal, cache: 'no-store',
      ...(body ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(errors[data.error] || '服务未接受退出请求，请稍后重试。');
      error.code = data.error || 'request_failed'; throw error;
    }
    return data;
  } finally { clearTimeout(timer); }
}

export class ShutdownSession {
  constructor({send = request, onChange = () => {}, onAccepted = () => {},
    wait = ms => new Promise(resolve => setTimeout(resolve, ms))} = {}) {
    Object.assign(this, {send, onChange, onAccepted, wait});
    this.state = {phase: 'idle', error: '', instance: null};
    this.generation = 0;
  }
  update(phase, error = '') { Object.assign(this.state, {phase, error}); this.onChange(this.state); }
  close() { this.generation++; this.update('idle'); }
  async inspect() {
    const generation = ++this.generation;
    this.update('loading');
    try {
      const status = await this.send('/api/server');
      if (generation !== this.generation) return;
      if (!status.supported) { this.update('unavailable', errors.server_shutdown_unavailable); return; }
      this.state.instance = status.instance_id;
      if (status.state === 'stopping') { this.accept(); await this.observe(); }
      else if (status.state === 'running') this.update('confirm');
      else this.update('unavailable', '另一页面正在检查退出条件，请稍后重新检查。');
    } catch {
      if (generation === this.generation) this.update('uncertain', '无法连接本地服务，尚未确认退出状态。可以检查连接，或查看启动终端。');
    }
  }
  accept() { this.update('stopping'); this.onAccepted(); }
  async confirm() {
    if (this.state.phase !== 'confirm') return;
    this.update('sending');
    let status;
    try {
      status = await this.send('/api/server/shutdown', 'POST', {instance_id: this.state.instance, confirmed: true});
    } catch (error) {
      // A lost acknowledgement is not proof of either success or rejection.
      // Only an explicit new confirmation may send another shutdown request.
      if (error.code) this.update(error.code === 'server_busy' ? 'confirm' : 'unavailable', error.message);
      else this.update('uncertain', '未收到退出确认。请求可能已送达，请检查连接；不会自动重复退出请求。');
      return;
    }
    if (status.state !== 'stopping' || status.instance_id !== this.state.instance) {
      this.update('uncertain', '退出响应与当前服务不一致，请检查连接。'); return;
    }
    this.accept(); await this.observe();
  }
  async observe() {
    this.update('stopping');
    for (let attempt = 0; attempt < 8; attempt++) {
      await this.wait(500);
      let status;
      try { status = await this.send('/api/server'); }
      catch { this.update('disconnected'); return; }
      if (status.instance_id !== this.state.instance || status.state === 'running') {
        this.update('restarted'); return;
      }
    }
    this.update('slow');
  }
}

export function createServerControl({$, onAccepted, onOpen = () => {}, onClose = () => $('exit-service').focus()}) {
  const dialog = $('shutdown-dialog');
  let accepted = false;
  const session = new ShutdownSession({onChange: render, onAccepted: () => {
    if (!accepted) { accepted = true; onAccepted(); }
  }});
  function render(s) {
    const titles = {loading: '检查服务状态', confirm: '退出 Astra 服务？', sending: '正在提交退出请求',
      stopping: '正在退出服务', disconnected: '服务已断开连接', uncertain: '退出状态尚未确认',
      unavailable: '暂时无法退出', slow: '仍在等待服务关闭', restarted: '服务已重新启动'};
    $('shutdown-title').textContent = titles[s.phase] || '退出 Astra 服务？';
    $('shutdown-error').textContent = s.error; $('shutdown-error').hidden = !s.error;
    $('shutdown-description').textContent = accepted
      ? '退出请求已接受，已保存的对话、配置和工作区文件会保留。'
      : '这会关闭本地后台服务。已保存的对话、配置和文件会保留；输入框里未发送的内容和未保存的修改不会自动保存。';
    $('shutdown-help').textContent = accepted
      ? s.phase === 'restarted' ? '检测到可用服务，请刷新页面继续。' : s.phase === 'slow'
        ? '后台仍在清理资源。可以再次检查连接；若长时间未退出，请查看启动终端。'
        : '可以关闭此页面。下次使用时，先重新启动 Astra，再刷新页面。'
      : '正在执行的任务请先暂停或停止。已暂停、待审批的任务会保留，重开后仍需手动继续或批准。';
    $('shutdown-confirm').hidden = !['confirm', 'sending'].includes(s.phase);
    $('shutdown-confirm').disabled = s.phase !== 'confirm';
    $('shutdown-confirm').textContent = s.phase === 'sending' ? '正在提交…' : '确认退出服务';
    $('shutdown-cancel').hidden = accepted;
    $('shutdown-cancel').disabled = s.phase === 'sending';
    $('shutdown-check').hidden = !['uncertain', 'unavailable', 'slow'].includes(s.phase);
    $('shutdown-refresh').hidden = !['disconnected', 'restarted'].includes(s.phase);
    $('shutdown-refresh').textContent = s.phase === 'restarted' ? '刷新页面' : '启动后刷新页面';
    if (accepted && (!dialog.contains(document.activeElement) || document.activeElement.hidden || document.activeElement.disabled)) $('shutdown-title').focus();
  }
  $('exit-service').addEventListener('click', () => {
    onOpen(); dialog.showModal(); $('shutdown-cancel').focus(); session.inspect();
  });
  function close() {
    if (accepted || session.state.phase === 'sending') return;
    session.close(); dialog.close(); onClose();
  }
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  $('shutdown-cancel').addEventListener('click', close);
  $('shutdown-confirm').addEventListener('click', () => session.confirm());
  $('shutdown-check').addEventListener('click', () => accepted ? session.observe() : session.inspect());
  $('shutdown-refresh').addEventListener('click', () => location.reload());
  return session;
}
