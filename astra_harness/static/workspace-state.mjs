// Directory validation is read-only. Selection is a separate, identity-bound CAS.
export class WorkspaceSelection {
  constructor({api, onChange = () => {}, onApplied = async () => {}, blocked = () => false}) {
    this.api = api; this.onChange = onChange; this.onApplied = onApplied; this.blocked = blocked;
    this.state = {snapshot: null, draft: '', candidate: null, phase: '', error: '', uncertain: false, open: false};
    this.epoch = 0;
  }
  emit() { this.onChange(this.state); }
  locked() { return !!this.state.phase || this.state.uncertain; }
  edit(path) {
    if (this.state.phase === 'selecting' || this.state.uncertain) return;
    this.epoch++; Object.assign(this.state, {draft: path, candidate: null, phase: '', error: ''}); this.emit();
  }
  open() { this.state.open = true; this.emit(); }
  cancel() {
    if (this.state.phase === 'selecting' || this.state.uncertain) return;
    this.epoch++; Object.assign(this.state, {open: false, candidate: null, phase: '', error: ''}); this.emit();
  }
  async load() {
    if (this.state.phase || this.state.snapshot && this.blocked()) return;
    const token = ++this.epoch; this.state.phase = 'loading'; this.state.error = ''; this.emit();
    try {
      const snapshot = await this.api('/api/workspaces');
      if (token !== this.epoch) return;
      await this.apply(snapshot);
      this.state.uncertain = false; this.state.candidate = null;
    } catch (error) { if (token === this.epoch) this.state.error = error.message; }
    finally { if (token === this.epoch) { this.state.phase = ''; this.emit(); } }
  }
  async apply(snapshot) {
    const old = this.state.snapshot;
    this.state.snapshot = snapshot;
    await this.onApplied(snapshot, old);
  }
  async validate() {
    if (this.locked() || this.blocked() || !this.state.snapshot?.enabled || !this.state.draft.trim()) return;
    const token = ++this.epoch, revision = this.state.snapshot.revision;
    this.state.phase = 'validating'; this.state.candidate = null; this.state.error = ''; this.emit();
    try {
      const candidate = await this.api('/api/workspaces/validate', 'POST', {path: this.state.draft.trim()});
      if (token === this.epoch) this.state.candidate = {...candidate, revision};
    } catch (error) { if (token === this.epoch) this.state.error = error.message; }
    finally { if (token === this.epoch) { this.state.phase = ''; this.emit(); } }
  }
  async select() {
    const candidate = this.state.candidate;
    if (this.locked() || this.blocked() || !candidate || !this.state.snapshot?.enabled) return;
    const token = ++this.epoch;
    this.state.phase = 'selecting'; this.state.error = ''; this.emit();
    let acknowledged = false;
    try {
      const snapshot = await this.api('/api/workspaces/select', 'POST', {
        path: candidate.path, expected_identity: candidate.identity, expected_revision: candidate.revision,
      });
      if (token !== this.epoch) return;
      acknowledged = true; this.state.candidate = null; this.state.open = false;
      await this.apply(snapshot);
    } catch (error) {
      if (token !== this.epoch) return;
      // An absent/5xx response cannot prove that selection did not commit. GET
      // the current state before any new operation; never replay a select POST.
      this.state.uncertain = !acknowledged && !(error.status >= 400 && error.status < 500);
      this.state.error = this.state.uncertain ? '切换结果尚未确认。请确认当前工作区；不会自动重发切换。' : error.message;
      this.state.candidate = null;
    } finally { if (token === this.epoch) { this.state.phase = ''; this.emit(); } }
  }
}

export function scopedSubmission(previous, prompt, sessionId, workspaceId, makeId, modelIdentity = 'startup', skill = null) {
  // An uncertain submission is immutable, even if another client changes roots.
  if (previous) return previous;
  if (!workspaceId) throw new Error('请先确认当前工作区。');
  return {prompt, session_id: sessionId, workspace_id: workspaceId, request_id: makeId(), expected_model_identity: modelIdentity,
    skill: skill ? {id: skill.id, expected_identity: skill.expected_identity} : null};
}

export function workspaceQuery(path, workspaceId) {
  if (!workspaceId) throw new Error('请先确认当前工作区。');
  return path + (path.includes('?') ? '&' : '?') + 'workspace_id=' + encodeURIComponent(workspaceId);
}
