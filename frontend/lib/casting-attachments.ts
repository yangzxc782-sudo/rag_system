import { castingDesign, type CastingInputFile } from "./casting-design";
import { isAbort, qaErrorMessage, QaApiError, type FieldIssue } from "./qa-sessions";

type AttachmentState = {
  open: boolean; loading: boolean; uploading: boolean; files: CastingInputFile[];
  before: string | null; selected: CastingInputFile | null; reusable: CastingInputFile | null;
  filename: string | null; error: string | null; issues: FieldIssue[]; retryable: boolean;
  listError: string | null;
};
/** Mounted-thread upload state only; File bytes never enter browser persistence. */
export class CastingAttachments {
  private state: AttachmentState = { open: false, loading: false, uploading: false, files: [], before: null,
    selected: null, reusable: null, filename: null, error: null, issues: [], retryable: false, listError: null };
  private serverState = this.state;
  private listeners = new Set<() => void>();
  private controller = new AbortController();
  private uploadController: AbortController | null = null;
  private listController: AbortController | null = null;
  private pending: { file: File; requestId: string } | null = null;
  constructor(readonly threadId: string) {}
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  getSnapshot = () => this.state;
  getServerSnapshot = () => this.serverState;
  get blocked() { return this.state.uploading || Boolean(this.state.filename && !this.state.selected); }
  private update(signal: AbortSignal, patch: Partial<AttachmentState>) {
    if (signal.aborted) return;
    this.state = { ...this.state, ...patch }; this.listeners.forEach(fn => fn());
  }
  connect() {
    this.controller.abort(); this.controller = new AbortController();
    return () => { this.controller.abort(); this.uploadController?.abort(); this.listController?.abort(); };
  }
  open() {
    this.update(this.controller.signal, { open: !this.state.open });
    if (this.state.open) void this.load();
  }
  async load(older = false) {
    if (older && (!this.state.before || this.state.loading)) return;
    this.listController?.abort(); this.listController = new AbortController();
    const signal = AbortSignal.any([this.controller.signal, this.listController.signal]);
    this.update(signal, { loading: true, listError: null });
    try {
      const page = await castingDesign.list(this.threadId, signal, older ? this.state.before : null);
      this.update(signal, { files: [...new Map([...(older ? this.state.files : []), ...page.items,
        ...(page.reusable_input ? [page.reusable_input] : []), ...(this.state.selected ? [this.state.selected] : [])].map(f => [f.file_id, f])).values()],
        before: page.next_before_id, reusable: page.reusable_input ?? null });
    } catch (error) {
      if (!isAbort(error)) this.update(signal, { listError: qaErrorMessage(error) });
    } finally { this.update(signal, { loading: false }); }
  }
  clear() {
    this.uploadController?.abort(); this.pending = null;
    this.update(this.controller.signal, { uploading: false, selected: null, filename: null, error: null, issues: [], retryable: false });
  }
  committed() { this.clear(); if (this.state.open) void this.load(); }
  select(id: string) {
    this.clear();
    const file = this.state.files.find(f => f.file_id === id && f.storage_state === "ready");
    if (file) this.update(this.controller.signal, { selected: file, filename: file.original_filename });
  }
  choose(file: File) {
    this.clear(); this.pending = { file, requestId: crypto.randomUUID() };
    this.update(this.controller.signal, { filename: file.name });
    void this.upload();
  }
  async upload() {
    if (!this.pending || this.state.uploading) return;
    this.uploadController = new AbortController();
    const signal = AbortSignal.any([this.controller.signal, this.uploadController.signal]);
    const { file, requestId } = this.pending;
    this.update(signal, { uploading: true, error: null, issues: [], retryable: false });
    try {
      const saved = await castingDesign.upload(this.threadId, requestId, file, signal);
      if (signal.aborted) return;
      this.pending = null;
      this.update(signal, { selected: saved, files: [saved, ...this.state.files.filter(f => f.file_id !== saved.file_id)] });
    } catch (error) {
      if (!isAbort(error)) this.update(signal, { error: qaErrorMessage(error), issues: error instanceof QaApiError ? error.issues : [],
        retryable: error instanceof QaApiError && ["NETWORK_ERROR", "REQUEST_TIMEOUT", "CASTING_STORAGE_UNAVAILABLE", "CASTING_SERVICE_UNAVAILABLE"].includes(error.code) });
    } finally { this.update(signal, { uploading: false }); }
  }
}
