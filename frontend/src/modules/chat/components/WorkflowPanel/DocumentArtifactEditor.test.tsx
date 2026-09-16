vi.mock('./useWriterProviderAvailability', () => ({ useWriterProviderAvailability: () => ({ states: { feishu: 'ready' }, refresh: vi.fn() }) }));
import { act, fireEvent, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import type { SlotRevision } from '@/modules/chat/store/workflowPanel';
import { DocumentArtifactEditor } from './DocumentArtifactEditor';
import { SlotEditingContext, type SlotFooterAction } from './slotEditingContext';

const api = vi.hoisted(() => ({ previewDocumentAction: vi.fn(), listDocumentProviders: vi.fn(), publishDocument: vi.fn(), getPublicationForArtifact: vi.fn(async()=>({data:{data:{}}})) }));
const download = vi.hoisted(() => vi.fn());
const confirm = vi.hoisted(() => vi.fn());
vi.mock('@/modules/chat/utils/request', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/modules/chat/utils/request')>(),
  WorkflowSessionApi: () => api,
}));
vi.mock('antd', async (importOriginal) => ({
  ...await importOriginal<typeof import('antd')>(), Modal: { confirm },
}));

vi.mock('./MarkdownArtifactEditor', () => ({
  MarkdownArtifactEditor: ({ markdown }: { markdown: string }) => <article>{markdown}</article>,
}));
vi.mock('./WriterIRControl', () => ({ WriterIRControl: () => null }));
vi.mock('./ArtifactRewriteDialog', () => ({ ArtifactRewriteDialog: () => null }));
vi.mock('./useDocumentCopy', () => ({ useDocumentCopy: () => {} }));
vi.mock('./WriterDownloadFormat', () => ({
  downloadSource: download,
  WriterDownloadFormatDialog: () => null,
  writerDownloadFilename: () => '',
  writerDownloadCacheKey: () => '',
}));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('document artifact display', () => {
  it('shows the failed publication stage returned by the backend', async () => {
    await i18n.changeLanguage('zh-CN');
    api.listDocumentProviders.mockResolvedValue({ data: { data: { providers: [{ id: 'feishu', capabilities: ['create', 'replace'] }] } } });
    api.publishDocument.mockRejectedValue({ response: { data: { data: { code: 'DOCUMENT_CONVERSION_FAILED' } } } });
    const registerFooterAction = vi.fn((_key: string, _action: SlotFooterAction | null) => () => {});
    const slot = {
      artifact_id: 'artifact-test', slot_id: 'draft_document', revision: 1,
      artifact_value: { text: '# Completed story' },
      document: { representation: 'markdown', editable: true, capabilities: ['save', 'publish_document'] },
    } as SlotRevision;
    render(<SlotEditingContext.Provider value={{ setEditing: vi.fn(), registerFlush: () => () => {}, registerFooterAction }}>
      <DocumentArtifactEditor slot={slot} sessionId='session-test' />
    </SlotEditingContext.Provider>);
    await waitFor(() => expect(registerFooterAction).toHaveBeenCalledWith(
      expect.any(String), expect.objectContaining({ icon: 'write-back', disabled: false }),
    ));
    const action = registerFooterAction.mock.calls.at(-1)![1]!;
    await act(async () => action.onClick());
    expect(confirm).not.toHaveBeenCalled();
    expect(await screen.findByRole('alert')).toHaveTextContent('飞书 文档转换阶段失败，尚未写入');
    expect(api.publishDocument).toHaveBeenCalledTimes(1);
  });
  it.each([['zh-CN', '下载'], ['en-US', 'Download']])(
    'loads the completed Markdown and registers a translated download label in %s',
    async (language, label) => {
      await i18n.changeLanguage(language);
      const registerFooterAction = vi.fn(() => () => {});
      const slot = {
        artifact_id: 'artifact-test', slot_id: 'draft_document', revision: 1,
        artifact_value: { text: '# Completed story\n\nThe first paragraph.' },
        document: { representation: 'markdown', editable: true, capabilities: ['save', 'convert_document'] },
      } as SlotRevision;
      render(
        <SlotEditingContext.Provider value={{
          setEditing: vi.fn(), registerFlush: () => () => {}, registerFooterAction,
        }}>
          <DocumentArtifactEditor slot={slot} sessionId='session-test' />
        </SlotEditingContext.Provider>,
      );
      expect(await screen.findByRole('article')).toHaveTextContent('The first paragraph.');
      await waitFor(() => expect(registerFooterAction).toHaveBeenCalledWith(
        expect.any(String), expect.objectContaining({ icon: 'download', label }),
      ));
    },
  );
});
it('downloads the click-time draft and retains that snapshot for retry without saving', async () => {
  await i18n.changeLanguage('zh-CN');
  let snapshot = '# 当前未保存草稿';
  let finish!: (value: unknown) => void;
  let downloadAction: SlotFooterAction | undefined;
  api.previewDocumentAction.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValueOnce({ data: { data: { content: 'Converted original snapshot' } } });
  download.mockRejectedValueOnce(new Error('download failed')).mockResolvedValueOnce(undefined);
  const slot = { artifact_id: 'download-fixture', slot_id: 'draft_document', revision: 3, draft_version: 7,
    artifact_value: { text: '# 保存的旧稿' }, document: { representation: 'markdown', editable: true, capabilities: ['save', 'convert_document'] } } as SlotRevision;
  render(<SlotEditingContext.Provider value={{ setEditing: vi.fn(), registerFlush: () => () => {}, getSnapshot: () => snapshot,
    registerFooterAction: (_key, action) => { if (action?.icon === 'download') downloadAction = action; return () => {}; },
  }}><DocumentArtifactEditor slot={slot} sessionId='fixture' /></SlotEditingContext.Provider>);
  await waitFor(() => expect(downloadAction).toBeDefined());
  act(() => downloadAction!.menu!.find(item => item.key === 'text')!.onClick());
  expect(api.previewDocumentAction.mock.calls[0][1].input).toEqual({ output_format: 'text', document: '# 当前未保存草稿' });
  snapshot = '# 下载过程中新增的文字';
  await act(async () => finish({ data: { data: { content: 'Converted original snapshot' } } }));
  expect(screen.getByRole('alert')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  await waitFor(() => expect(download).toHaveBeenCalledTimes(2));
  expect(api.previewDocumentAction.mock.calls[1][1].input.document).toBe('# 当前未保存草稿');
  expect(download.mock.calls[1][0].content).toBe('Converted original snapshot');
});
it('confirms the existing publication target before an overwrite', async () => {
  await i18n.changeLanguage('zh-CN');
  api.listDocumentProviders.mockResolvedValue({ data: { data: { providers: [{ id: 'feishu' }] } } });
  api.publishDocument.mockResolvedValue({ data: { data: { artifact_id: 'saved', revision: 2, draft_version: 1, document: '# Draft', provider_synced: true, artifact_saved: true } } });
  let action: SlotFooterAction | undefined;
  const slot = { artifact_id: 'bound', slot_id: 'draft_document', revision: 1, provider: 'feishu', provider_document_id: 'existing-target', write_back_ready: true,
    write_back_url: 'https://example.test/document', artifact_value: { text: '# Draft' }, document: { representation: 'markdown', editable: true, capabilities: ['save', 'publish_document'] } } as SlotRevision;
  render(<SlotEditingContext.Provider value={{ setEditing: vi.fn(), registerFlush: () => () => {}, registerFooterAction: (_key, next) => { if (next?.icon === 'write-back') action = next; return () => {}; } }}>
    <DocumentArtifactEditor slot={slot} sessionId='fixture' />
  </SlotEditingContext.Provider>);
  await waitFor(() => expect(action?.disabled).toBe(false));
  act(() => action!.onClick());
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(api.publishDocument).not.toHaveBeenCalled();
  await act(async () => confirm.mock.calls[0][0].onOk());
  expect(api.publishDocument).toHaveBeenCalledTimes(1);
});

it('shows the returned cloud document link in the footer immediately after a successful publication', async () => {
  await i18n.changeLanguage('zh-CN');
  api.listDocumentProviders.mockResolvedValue({ data: { data: { providers: [{ id: 'feishu' }] } } });
  api.publishDocument.mockResolvedValue({ data: { data: { artifact_id: 'saved', revision: 2, draft_version: 1,
    document: '# Draft', provider_synced: true, artifact_saved: true, target_document: { uri: 'https://example.test/published-document' } } } });
  let action: SlotFooterAction | undefined;
  const slot = { artifact_id: 'publication-link', slot_id: 'draft_document', revision: 1, artifact_value: { text: '# Draft' },
    document: { representation: 'markdown', editable: true, capabilities: ['save', 'publish_document'] } } as SlotRevision;
  render(<SlotEditingContext.Provider value={{ setEditing: vi.fn(), registerFlush: () => () => {},
    registerFooterAction: (_key, next) => { if (next?.icon === 'write-back') action = next; return () => {}; },
  }}><DocumentArtifactEditor slot={slot} sessionId='fixture' /></SlotEditingContext.Provider>);
  await waitFor(() => expect(action?.disabled).toBe(false));
  await act(async () => action!.onClick());
  await waitFor(() => expect(action?.statusLink).toEqual({ href: 'https://example.test/published-document', label: '打开云文档' }));
  expect(action?.statusText).toBe('已写回云文档');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(confirm).not.toHaveBeenCalled();
});

it.each(['https://example.test/published-document', 'javascript:alert(1)', 'https://user:password@example.test/document'])('restores a safe success link without a recovery banner: %s', async url => {
  await i18n.changeLanguage('zh-CN');
  api.listDocumentProviders.mockResolvedValue({ data: { data: { providers: [{ id: 'feishu' }] } } });
  api.getPublicationForArtifact.mockResolvedValueOnce({ data: { data: { operation: { operation_id: 'completed-operation', status: 'succeeded',
    provider: 'feishu', provider_synced: true, artifact_id: 'publication-link', source_slot_id: 'draft_document', item_index: -1,
    updated_at: '2026-09-15T00:00:00Z', target_url: url, actions: [] } } } });
  let action: SlotFooterAction | undefined;
  const slot = { artifact_id: 'publication-link', slot_id: 'draft_document', revision: 1, artifact_value: { text: '# Draft' },
    document: { representation: 'markdown', editable: true, capabilities: ['save', 'publish_document'] } } as SlotRevision;
  render(<SlotEditingContext.Provider value={{ setEditing: vi.fn(), registerFlush: () => () => {},
    registerFooterAction: (_key, next) => { if (next?.icon === 'write-back') action = next; return () => {}; },
  }}><DocumentArtifactEditor slot={slot} sessionId='fixture' /></SlotEditingContext.Provider>);
  await waitFor(() => expect(action?.disabled).toBe(false));
  expect(action?.statusLink).toEqual(url === 'https://example.test/published-document' ? { href: url, label: '打开云文档' } : undefined);
  expect(action?.statusText).toBe('已写回云文档');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByText('上次云文档发布')).not.toBeInTheDocument();
  expect(api.publishDocument).not.toHaveBeenCalled();
});

it.each(['outcome_unknown_released', 'confirmed_detached', 'outcome_unknown'])('keeps publication state below the editor and the target link in the footer: %s', async status => {
  await i18n.changeLanguage('zh-CN');
  api.listDocumentProviders.mockResolvedValue({ data: { data: { providers: [{ id: 'feishu' }] } } });
  api.getPublicationForArtifact.mockResolvedValueOnce({ data: { data: { operation: { operation_id: 'publication-fixture', status,
    provider: 'feishu', source_slot_id: 'draft_document', item_index: -1, updated_at: '2026-09-15T00:00:00Z',
    target_url: 'https://example.test/document', actions: [] } } } });
  let action: SlotFooterAction | undefined;
  const slot = { artifact_id: 'publication-fixture', slot_id: 'draft_document', revision: 1, artifact_value: { text: '# Draft' },
    document: { representation: 'markdown', editable: true, capabilities: ['save', 'publish_document'] } } as SlotRevision;
  render(<SlotEditingContext.Provider value={{ setEditing: vi.fn(), registerFlush: () => () => {},
    registerFooterAction: (_key, next) => { if (next?.icon === 'write-back') action = next; return () => {}; },
  }}><DocumentArtifactEditor slot={slot} sessionId='fixture' /></SlotEditingContext.Provider>);
  await waitFor(() => expect(action?.statusLink?.href).toBe('https://example.test/document'));
  expect(action?.statusText).not.toBe('已写回云文档');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByText('上次云文档发布')).not.toBeInTheDocument();
  if (status === 'outcome_unknown') {
    const toggle = screen.getByRole('button', { name: '发布状态' });
    expect(screen.getByRole('article').compareDocumentPosition(toggle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(action?.disabled).toBe(true);
  } else expect(screen.queryByRole('button', { name: '发布状态' })).not.toBeInTheDocument();
  expect(api.publishDocument).not.toHaveBeenCalled();
});
