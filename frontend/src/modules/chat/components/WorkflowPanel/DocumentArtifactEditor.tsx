import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { Modal } from 'antd';
import type { DocumentProvider, DocumentPublishRequest, DocumentRewritePreviewResult, DocumentNumberingResult, DocumentConvertResult } from '@/api/generated/core-client';
import type { SlotRevision } from '@/modules/chat/store/workflowPanel';
import { WorkflowSessionApi, type RewriteSelectionPreview, type WriterNumberingState, type WriterNumberingUpdate } from '@/modules/chat/utils/request';
import { resolveCoreAssetUrl, resolveMarkdownImageUrlAsync } from '@/modules/knowledge/utils/imageUrl';
import i18n from '@/i18n';
import { MarkdownArtifactEditor, type MarkdownSaveMode } from './MarkdownArtifactEditor';
import { WriterIRControl, type WriterIRSaveMode } from './WriterIRControl';
import { isWriterDocument, type WriterDocument } from './writerIR';
import { SlotEditingContext, WorkflowPanelTabActiveContext } from './slotEditingContext';
import { ArtifactRewriteDialog, type ArtifactRewriteSelection } from './ArtifactRewriteDialog';
import { useDocumentCopy } from './useDocumentCopy';
import { WriterDownloadFormatDialog, writerDownloadFilename, writerDownloadCacheKey } from './WriterDownloadFormat';
import { WriterProviderChoice } from './DocumentProviderChoice';

function unwrap(value: unknown): unknown {
  while (value && typeof value === 'object') {
    const object = value as Record<string, unknown>;
    if ('data' in object) value = object.data;
    else if ('text' in object) value = object.text;
    else break;
  }
  return value;
}
function key() { return crypto.randomUUID(); }
function responseCode(error: unknown): string | undefined {
  return (error as { response?: { data?: { data?: { code?: string } } } })?.response?.data?.data?.code;
}
type Baseline = { id: string; revision: number; draft?: number; value: unknown };

export function DocumentArtifactEditor({ slot, sessionId, readOnly, onRefresh }: {
  slot: SlotRevision; sessionId: string; readOnly?: boolean; onRefresh?: () => void;
}) {
  const descriptor = slot.document!;
  const writable = !readOnly && descriptor.editable && descriptor.capabilities.includes('save');
  const active = useContext(WorkflowPanelTabActiveContext);
  const { registerFooterAction } = useContext(SlotEditingContext);
  const editingKey = `document:${sessionId}:${slot.slot_id}:${slot.list_index ?? -1}`;
  const initial: Baseline = { id: slot.artifact_id!, revision: slot.revision, draft: slot.draft_version, value: unwrap(slot.artifact_value) };
  const latest = useRef(initial);
  const external = useRef(initial);
  const dirty = useRef(false);
  const seen = useRef('');
  const [value, setValue] = useState<unknown>(initial.value);
  const [version, setVersion] = useState(initial.revision);
  const [loaded, setLoaded] = useState(false);
  const [providers, setProviders] = useState<DocumentProvider[]>([]);
  const providersRef = useRef(providers); providersRef.current = providers;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const draftContent = useRef(initial.value);
  const [numbering, setNumbering] = useState<WriterNumberingState>();
  const [selection, setSelection] = useState<ArtifactRewriteSelection | null>(null);
  const [preview, setPreview] = useState<{ id: string; selection: ArtifactRewriteSelection; value: RewriteSelectionPreview } | null>(null);
  const [download, setDownload] = useState(false);
  const previewSource = useRef<Baseline>();
  const attempt = useRef<{ fingerprint: string; body: DocumentPublishRequest; id: string; uncertain: boolean }>();

  useEffect(() => {
    const signature = JSON.stringify([slot.artifact_id, slot.revision, slot.draft_version]);
    if (seen.current === signature) return;
    seen.current = signature;
    let canceled = false;
    async function load() {
      let content = unwrap(slot.artifact_value);
      if (content && typeof content === 'object' && ('path' in content || 'url' in content)) {
        const carrier = content as { path?: string; url?: string };
        const response = await fetch(resolveCoreAssetUrl(carrier.url || carrier.path || ''), { credentials: 'same-origin' });
        if (!response.ok) throw new Error('document read failed');
        const text = await response.text();
        content = descriptor.representation === 'ir' ? unwrap(JSON.parse(text)) : text;
      }
      if (canceled) return;
      const next = { id: slot.artifact_id!, revision: slot.revision, draft: slot.draft_version, value: content };
      external.current = next;
      if (!dirty.current) { latest.current = next; draftContent.current = content; }
      setValue(content); setVersion(next.revision); setLoaded(true);
    }
    void load().catch(() => { if (!canceled) setError(String(i18n.t('chat.writerIR.saveFailed'))); });
    return () => { canceled = true; };
  }, [slot.artifact_id, slot.revision, slot.draft_version, slot.artifact_value, descriptor.representation]);

  useEffect(() => {
    if (!descriptor.capabilities.includes('publish_document') || !writable) return;
    let canceled = false;
    void WorkflowSessionApi().listDocumentProviders().then((response) => {
      if (!canceled) setProviders(response.data.data.providers);
    }).catch(() => { if (!canceled) setError(String(i18n.t('chat.writerIR.writeBackFailed'))); });
    return () => { canceled = true; };
  }, [descriptor.capabilities, writable]);

  const accept = useCallback((id: string, revision: number, draft: number | undefined, content: unknown) => {
    const next = { id, revision, draft, value: content };
    latest.current = next; external.current = next; dirty.current = false; draftContent.current = content;
    setValue(content); setVersion(revision);
  }, []);
  const edit = useCallback((content: unknown) => {
    draftContent.current = content;
    dirty.current = JSON.stringify(content) !== JSON.stringify(external.current.value);
    if (!dirty.current) latest.current = external.current;
  }, []);
  const save = useCallback(async (content: unknown, base: number, mode: MarkdownSaveMode | WriterIRSaveMode = 'checkpoint', numbering?: WriterNumberingUpdate) => {
    const current = latest.current;
    const artifact = descriptor.representation === 'ir'
      ? { schema: 'application/vnd.lazymind.writer+json', data: content }
      : { text: content };
    const response = await WorkflowSessionApi().saveDocumentArtifact(current.id, {
      base_revision: base, base_draft_version: current.draft, mode,
      numbering_update: numbering, content_type: descriptor.representation === 'ir' ? 'json' : 'text/markdown', value: artifact, command_id: key(),
    }, { silentError: true } as never);
    const result = response.data.result;
    if (!response.data.ok || !result?.artifact_id || typeof result.revision !== 'number' || typeof result.draft_version !== 'number') throw new Error('invalid save response');
    accept(result.artifact_id, result.revision, result.draft_version, unwrap(result.value ?? artifact));
    attempt.current = undefined;
    onRefresh?.();
    return latest.current;
  }, [accept, descriptor.representation, onRefresh]);

  useEffect(() => {
    if (!loaded || !descriptor.capabilities.includes('numbering')) return;
    let canceled = false; const current = latest.current;
    void WorkflowSessionApi().previewDocumentAction(current.id, { action: 'numbering', base_revision: current.revision,
      base_draft_version: current.draft, input: {} }).then((response) => {
        if (!canceled) setNumbering((response.data.data as DocumentNumberingResult).numbering as WriterNumberingState);
      }).catch(() => {});
    return () => { canceled = true; };
  }, [loaded, version, value, descriptor.capabilities]);
  useDocumentCopy({ enabled: loaded, editingKey, sessionId, slotId: slot.slot_id, listIndex: slot.list_index ?? -1,
    revision: version, document: value as string | WriterDocument });
  useEffect(() => {
    if (!active || !loaded || !descriptor.capabilities.includes('convert_document')) return;
    return registerFooterAction(`${editingKey}:download`, { label: String(i18n.t('common.download')), icon: 'download', onClick: () => setDownload(true) });
  }, [active, loaded, descriptor.capabilities, registerFooterAction, editingKey]);
  const convert = async (format: 'markdown' | 'latex') => {
    const current = latest.current;
    const response = await WorkflowSessionApi().previewDocumentAction(current.id, { action: 'convert_document',
      base_revision: current.revision, base_draft_version: current.draft,
      input: { output_format: format, document: draftContent.current as string | Record<string, unknown> } });
    return (response.data.data as DocumentConvertResult).content;
  };
  const applyPreview = async () => {
    if (!preview?.value.commit?.token) return undefined;
    const response = await WorkflowSessionApi().executeDocumentAction(preview.id, { action: 'rewrite_selection',
      base_revision: preview.value.base_revision, base_draft_version: preview.value.base_draft_version,
      input: { commit_token: preview.value.commit.token } });
    const result = response.data.data;
    const fetched = await WorkflowSessionApi().getDocumentArtifact(result.artifact_id);
    const artifact = fetched.data.result;
    accept(artifact.artifact_id, artifact.revision, artifact.draft_version, unwrap(artifact.value));
    setPreview(null); onRefresh?.(); return artifact.revision;
  };
  const canRewrite = writable && descriptor.capabilities.includes('rewrite_selection') && !selection && !preview;

  const publish = useCallback(async (provider: string) => {
    if (busy) return;
    const current = latest.current;
    const fingerprint = JSON.stringify([current.id, current.revision, current.draft, provider]);
    if (attempt.current?.uncertain && attempt.current.fingerprint !== fingerprint) {
      setError(String(i18n.t('chat.writerIR.writeBackFailed'))); return;
    }
    if (!attempt.current || attempt.current.fingerprint !== fingerprint) {
      attempt.current = { fingerprint, id: current.id, uncertain: false, body: { action: 'publish_document',
        base_revision: current.revision, base_draft_version: current.draft,
        input: { provider, mode: 'replace', idempotency_key: key() } } };
    }
    const request = attempt.current;
    setBusy(true); setError('');
    try {
      const response = await WorkflowSessionApi().publishDocument(request.id, request.body, { silentError: true } as never);
      const result = response.data.data;
      if (!result.provider_synced || !result.artifact_saved || !result.artifact_id) throw new Error('publication did not complete');
      accept(result.artifact_id, result.revision, result.draft_version, result.document);
      attempt.current = undefined; onRefresh?.();
    } catch (failure) {
      const code = responseCode(failure);
      const beforeWrite = ['DOCUMENT_ACTION_INVALID', 'REVISION_CONFLICT', 'DRAFT_VERSION_CONFLICT', 'ARTIFACT_IN_USE',
        'DOCUMENT_ACTION_UNSUPPORTED', 'DOCUMENT_PROVIDERS_UNAVAILABLE', 'PROVIDER_CREDENTIALS_UNAVAILABLE'];
      if (code && beforeWrite.includes(code)) attempt.current = undefined;
      else request.uncertain = true;
      setError(String(i18n.t('chat.writerIR.writeBackFailed')));
    } finally { setBusy(false); }
  }, [accept, busy, onRefresh]);
  const publishRef = useRef(publish); publishRef.current = publish;

  useEffect(() => {
    if (!active || !writable || !descriptor.capabilities.includes('publish_document')) return;
    return registerFooterAction(`${editingKey}:publish`, {
      label: String(i18n.t('chat.writerIR.writeToCloudDocument')), icon: 'write-back', tone: 'primary', order: 30,
      disabled: busy || !loaded || providers.length === 0, flushBeforeAction: true, flushKey: editingKey,
      onClick: () => {
        const catalog = providersRef.current;
        if (!catalog.length) return;
        let selected = slot.provider || catalog[0].id;
        Modal.confirm({ title: i18n.t('chat.writerIR.providerPickerTitle'),
          content: <WriterProviderChoice initialProvider={selected} githubEnabled providers={catalog} onChange={(id) => { selected = id; }} />,
          onOk: () => publishRef.current(selected),
        });
      },
      statusText: error || undefined, statusTone: error ? 'error' : undefined,
    });
  }, [active, writable, descriptor.capabilities, registerFooterAction, editingKey, loaded, providers.length, busy, error, slot.provider]);

  if (!loaded) return <div role='status'>{error || '…'}</div>;
  return <div className='workflow-slot'>
    {error && <div role='alert'>{error}</div>}
    {descriptor.representation === 'markdown' && typeof value === 'string'
      ? <MarkdownArtifactEditor markdown={value} sourceRevision={version} editingKey={editingKey} readOnly={!writable}
        resolveImageUrl={resolveMarkdownImageUrlAsync} onContentChange={edit} numbering={numbering}
        onRewriteSelection={canRewrite ? (picked) => { if (picked.supported) setSelection({ type: 'markdown', selected_text: picked.text, selectedText: picked.text, paragraph: picked.paragraph, startOffset: picked.startOffset, anchor: picked.anchor }); } : undefined}
        rewriteDialogOpen={selection !== null}
        rewritePreview={preview?.selection.paragraph ? { paragraph: preview.selection.paragraph, startOffset: preview.selection.startOffset,
          sessionId, slotId: slot.slot_id, listIndex: slot.list_index ?? -1, preview: preview.value, applyPreview } : null}
        onRewritePreviewApplied={() => setPreview(null)} onRewritePreviewRejected={() => setPreview(null)}
        onSave={async (text, revision, mode, numbering) => { const saved = await save(text, revision, mode, numbering); return { markdown: String(saved.value), revision: saved.revision }; }} />
      : isWriterDocument(value) ? <WriterIRControl document={value as WriterDocument} sourceRevision={version} editingKey={editingKey} readOnly={!writable}
        onDocumentChange={edit} numbering={numbering}
        onRewriteSelection={canRewrite ? (picked) => setSelection({ type: 'ir', node_id: picked.nodeId, selectedText: picked.selectedText, anchor: picked.anchor }) : undefined}
        rewriteDialogOpen={selection !== null}
        rewritePreview={preview?.selection.type === 'ir' ? { nodeId: preview.selection.node_id, sessionId,
          slotId: slot.slot_id, listIndex: slot.list_index ?? -1, preview: preview.value, applyPreview } : null}
        onRewritePreviewApplied={() => setPreview(null)} onRewritePreviewRejected={() => setPreview(null)} onSave={async (_source, revised, revision, mode, numbering) => {
          const saved = await save(revised, Number(revision ?? latest.current.revision), mode, numbering);
          return { document: saved.value as WriterDocument, sourceRevision: saved.revision, draftVersion: saved.draft };
        }} /> : <div role='alert'>{String(i18n.t('chat.writerIR.saveFailed'))}</div>}
    <ArtifactRewriteDialog open={selection !== null} sessionId={sessionId} slotId={slot.slot_id} listIndex={slot.list_index ?? -1}
      baseRevision={latest.current.revision} baseDraftVersion={latest.current.draft} selection={selection}
      onClose={() => setSelection(null)} onApplied={() => { setPreview(null); onRefresh?.(); }}
      requestPreview={async (instruction, picked) => {
        const current = latest.current; previewSource.current = { ...current };
        if (picked.type === 'ppt_html') throw new Error('unsupported document selection');
        const response = await WorkflowSessionApi().previewDocumentAction(current.id, { action: 'rewrite_selection',
          base_revision: current.revision, base_draft_version: current.draft,
          input: { instruction, selection: picked.type === 'ir' ? { type: 'ir', node_id: picked.node_id } : { type: 'markdown', selected_text: picked.selected_text } } });
        return { ...(response.data.data as DocumentRewritePreviewResult), status: 'ready', action: 'rewrite_selection', base_revision: current.revision, base_draft_version: current.draft } as RewriteSelectionPreview;
      }} onPreviewReady={(result) => { if (selection && previewSource.current) setPreview({ id: previewSource.current.id, selection, value: result }); }} />
    {download && <WriterDownloadFormatDialog open={download} onOpenChange={setDownload}
      markdown={{ filename: writerDownloadFilename('', 'md'), content: () => convert('markdown') }}
      latex={{ filename: writerDownloadFilename('', 'tex'), content: () => convert('latex') }}
      lmd={descriptor.representation === 'ir' ? { filename: writerDownloadFilename('', 'lmd'), content: () => JSON.stringify(draftContent.current, null, 2) }
        : { filename: writerDownloadFilename('', 'lmd'), conversionSource: String(draftContent.current), conversionSourceFormat: 'markdown', cacheKey: writerDownloadCacheKey('document-lmd', String(draftContent.current)) }} />}
  </div>;
}
