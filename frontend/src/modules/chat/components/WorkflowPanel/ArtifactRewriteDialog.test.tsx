import { fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const workflowApi = vi.hoisted(() => ({ patchSlotItem: vi.fn(), previewRewriteSelection: vi.fn(), executeArtifactAction: vi.fn() }));

vi.mock('@/modules/chat/utils/request', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/modules/chat/utils/request')>(),
  WorkflowSessionApi: () => workflowApi,
}));

import {
  ArtifactRewriteDialog,
  ArtifactRewriteInlineDiff,
  type ArtifactRewriteSelection,
} from './ArtifactRewriteDialog';

const previewApi = workflowApi.previewRewriteSelection;
const executeApi = workflowApi.executeArtifactAction;

vi.mock('react-i18next', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-i18next')>();
  const translate = (key: string) => key;
  return {
    ...actual,
    useTranslation: () => ({ t: translate }),
  };
});

const selection: ArtifactRewriteSelection = {
  type: 'markdown',
  selected_text: 'Selected text',
  selectedText: 'Selected text',
  anchor: { top: 120, left: 240, placement: 'above' },
};

function renderDialog(requestPreview = vi.fn()) {
  render(
    <ArtifactRewriteDialog
      open
      sessionId='session-1'
      slotId='draft_document'
      listIndex={0}
      baseRevision={1}
      selection={selection}
      onClose={vi.fn()}
      onApplied={vi.fn()}
      requestPreview={requestPreview}
    />,
  );
  return requestPreview;
}

describe('ArtifactRewriteDialog', () => {
  beforeEach(() => {
    workflowApi.patchSlotItem.mockReset();
    previewApi.mockReset();
    executeApi.mockReset();
  });

  it.each([
    ['ir', 6], ['markdown', 6], ['ir', undefined], ['markdown', undefined],
  ] as const)('accepts the single paragraph with the preview commit token (%s, draft %s)', async (representation, draftVersion) => {
    const target = document.createElement('p');
    target.textContent = 'Original';
    const layer = document.createElement('div');
    document.body.append(target, layer);
    executeApi.mockResolvedValue({ data: { code: 0, data: { status: 'applied', revision: 5, draft_version: 1 } } });
    const onApplied = vi.fn();
    try {
      render(<ArtifactRewriteInlineDiff target={target} layer={layer} sessionId='session'
        slotId='draft_document' listIndex={-1} onApplied={onApplied} onReject={vi.fn()}
        preview={{ status: 'ready', action: 'rewrite_selection', base_revision: 4,
          base_draft_version: draftVersion, representation, target: { type: 'block', block_type: 'paragraph' },
          preview: { old_text: 'Original', new_text: 'Polished' },
          patch: { type: 'string_replace_set', payload: {} },
          artifact: { content_type: 'text', value: 'Polished' }, commit: { token: 'preview-token' } }} />);
      fireEvent.click(screen.getByRole('button', { name: 'chat.artifactRewrite.apply' }));
      await waitFor(() => expect(onApplied).toHaveBeenCalledWith(5, 1));
      expect(executeApi).toHaveBeenCalledWith('session', 'draft_document', -1, {
        action: 'rewrite_selection', base_revision: 4,
        ...(draftVersion !== undefined ? { base_draft_version: draftVersion } : {}),
        input: { commit_token: 'preview-token' },
      });
    } finally {
      cleanup();
      target.remove();
      layer.remove();
    }
  });
  it.each(['ir', 'markdown'] as const)('sends %s type once with one selected paragraph', async (type) => {
    const selected = type === 'ir'
      ? { type, node_id: 'p1', selectedText: 'Selected text' }
      : selection;
    previewApi.mockResolvedValue({ data: { data: {
      status: 'ready', action: 'rewrite_selection', base_revision: 1, representation: type,
      target: { type: 'block', block_type: 'paragraph' },
      preview: { old_text: 'Full paragraph', new_text: 'Polished paragraph' },
      patch: { type: type === 'ir' ? 'writer_ir_patch' : 'string_replace_set', payload: {} },
      artifact: { content_type: 'text', value: 'Polished paragraph' },
    } } });
    const onPreviewReady = vi.fn();
    render(<ArtifactRewriteDialog open sessionId='session' slotId='draft_document' listIndex={-1}
      baseRevision={1} selection={selected} onClose={vi.fn()} onApplied={vi.fn()}
      onPreviewReady={onPreviewReady} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Polish' } });
    fireEvent.click(screen.getByRole('button', { name: 'chat.artifactRewrite.preview' }));
    await waitFor(() => expect(onPreviewReady).toHaveBeenCalled());
    expect(previewApi.mock.lastCall?.[3].input).toEqual({
      type, instruction: 'Polish', selection_ranges: [type === 'ir'
        ? { node_id: 'p1', selected_text: 'Selected text' } : { selected_text: 'Selected text' }],
    });
  });
  it('does not submit an empty or whitespace-only instruction', () => {
    const requestPreview = renderDialog();
    const input = screen.getByRole('textbox');
    const submit = screen.getByRole('button', { name: 'chat.artifactRewrite.preview' });

    expect(submit).toBeDisabled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(requestPreview).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: '   ' } });
    expect(submit).toBeDisabled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(requestPreview).not.toHaveBeenCalled();
  });

  it('submits a trimmed non-empty instruction', async () => {
    const requestPreview = renderDialog(vi.fn().mockResolvedValue({
      status: 'ready',
      action: 'rewrite_selection',
      base_revision: 1,
      representation: 'markdown',
      target: { type: 'block', block_type: 'paragraph' },
      preview: { old_text: 'Selected text', new_text: 'Rewritten text' },
      patch: { type: 'string_replace_set', payload: {} },
      artifact: { content_type: 'text/markdown', value: 'Rewritten text' },
    }));
    const input = screen.getByRole('textbox');
    const submit = screen.getByRole('button', { name: 'chat.artifactRewrite.preview' });

    fireEvent.change(input, { target: { value: '  Make it clearer  ' } });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => {
      expect(requestPreview).toHaveBeenCalledWith('Make it clearer', selection);
    });
  });

  it('returns both revision baselines after applying a preview', async () => {
    workflowApi.patchSlotItem.mockResolvedValue({
      data: {
        code: 0,
        data: { type: 'slot_item_patched', revision: 3, draft_version: 7 },
      },
    });
    const target = document.createElement('p');
    target.textContent = 'Selected text';
    const layer = document.createElement('div');
    document.body.append(target, layer);
    const onApplied = vi.fn();

    render(
      <ArtifactRewriteInlineDiff
        target={target}
        layer={layer}
        sessionId='session-1'
        slotId='draft_document'
        listIndex={-1}
        preview={{
          status: 'ready',
          action: 'rewrite_selection',
          base_revision: 3,
          base_draft_version: 6,
          representation: 'markdown',
          target: { type: 'block', block_type: 'paragraph' },
          preview: { old_text: 'Selected text', new_text: 'Rewritten text' },
          patch: { type: 'string_replace_set', payload: {} },
          artifact: { content_type: 'text/markdown', value: 'Rewritten text' },
        }}
        onApplied={onApplied}
        onReject={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'chat.artifactRewrite.apply' }));
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(3, 7));
  });
});
