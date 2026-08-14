import MarkdownIt from 'markdown-it';
import {
  getWriterSpanColor,
  getWriterSpanStyles,
  writerBackgroundColorHex,
  writerTextColorHex,
  type WriterBlock,
  type WriterDocument,
  type WriterSpan,
} from './writerIR';

const WRITER_IR_SCHEMA = 'lazyllm.tools.writer.data_models.writer_ir.WriterDocument';
const WRITER_ARTIFACT_SCHEMA_VERSION = '0.1';

interface MarkdownToken {
  type: string;
  tag: string;
  attrs: Array<[string, string]> | null;
  map: [number, number] | null;
  nesting: number;
  level: number;
  children: MarkdownToken[] | null;
  content: string;
  markup: string;
  info: string;
  hidden: boolean;
}

interface InlineContent {
  content: string;
  spans: WriterSpan[];
  references: Array<Record<string, unknown>>;
}

interface MarkdownSourcePayload {
  markdown_source?: unknown;
  markdown_source_start?: unknown;
  markdown_source_end?: unknown;
  markdown_signature?: unknown;
  [key: string]: unknown;
}

const markdownParser = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: false,
});

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function tokenAttribute(token: MarkdownToken, name: string): string {
  return token.attrs?.find(([key]) => key === name)?.[1] ?? '';
}

function normalizeDocumentId(documentId: string): string {
  return documentId
    .replace(/[^a-zA-Z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'writer-document';
}

function hashText(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16);
}

function semanticProviderPayload(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => !key.startsWith('markdown_')),
  );
}

function blockSnapshot(block: WriterBlock): Record<string, unknown> {
  return {
    type: block.type,
    content: block.content ?? '',
    spans: block.spans ?? [],
    numbering: block.numbering ?? {},
    references: block.references ?? [],
    language: block.language ?? '',
    editable: block.editable,
    provider_payload: semanticProviderPayload(block.provider_payload),
    children: (block.children ?? []).map(blockSnapshot),
  };
}

function blockSignature(block: WriterBlock): string {
  return hashText(JSON.stringify(blockSnapshot(block)));
}

function documentSignature(document: WriterDocument): string {
  return hashText(JSON.stringify({
    title: document.title,
    blocks: document.blocks.map(blockSnapshot),
  }));
}

function addPreservationSignatures(block: WriterBlock): WriterBlock {
  const children = (block.children ?? []).map(addPreservationSignatures);
  const withChildren = children.length > 0 ? { ...block, children } : block;
  if (!isRecord(withChildren.provider_payload)) return withChildren;
  return {
    ...withChildren,
    provider_payload: {
      ...withChildren.provider_payload,
      markdown_signature: blockSignature(withChildren),
    },
  };
}

function sameStyles(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function appendSpan(
  result: InlineContent,
  text: string,
  style: Record<string, unknown>,
): void {
  if (!text) return;
  result.content += text;
  const previous = result.spans[result.spans.length - 1];
  if (previous && isRecord(previous.style) && sameStyles(previous.style, style)) {
    previous.text += text;
  } else {
    result.spans.push({ text, style: { ...style } });
  }
}

function inlineContent(tokens: MarkdownToken[] | null | undefined): InlineContent {
  const result: InlineContent = { content: '', spans: [], references: [] };
  const styles: Record<string, unknown> = {};
  const links: Array<{ start: number; url: string; title: string }> = [];

  for (const token of tokens ?? []) {
    if (token.type === 'strong_open') {
      styles.bold = true;
      continue;
    }
    if (token.type === 'strong_close') {
      delete styles.bold;
      continue;
    }
    if (token.type === 'em_open') {
      styles.italic = true;
      continue;
    }
    if (token.type === 'em_close') {
      delete styles.italic;
      continue;
    }
    if (token.type === 's_open') {
      styles.strikethrough = true;
      continue;
    }
    if (token.type === 's_close') {
      delete styles.strikethrough;
      continue;
    }
    if (token.type === 'link_open') {
      links.push({
        start: result.content.length,
        url: tokenAttribute(token, 'href'),
        title: tokenAttribute(token, 'title'),
      });
      continue;
    }
    if (token.type === 'link_close') {
      const link = links.pop();
      if (link) {
        result.references.push({
          type: 'link',
          url: link.url,
          ...(link.title ? { title: link.title } : {}),
          start: link.start,
          end: result.content.length,
        });
      }
      continue;
    }
    if (token.type === 'code_inline') {
      appendSpan(result, token.content, { ...styles, inline_code: true });
      continue;
    }
    if (token.type === 'softbreak' || token.type === 'hardbreak') {
      appendSpan(result, '\n', styles);
      if (token.type === 'hardbreak') {
        result.references.push({ type: 'hard_break', offset: result.content.length - 1 });
      }
      continue;
    }
    if (token.type === 'image') {
      const start = result.content.length;
      const alt = token.content || inlineContent(token.children).content;
      appendSpan(result, alt, styles);
      result.references.push({
        type: 'markdown_image',
        url: tokenAttribute(token, 'src'),
        alt,
        ...(tokenAttribute(token, 'title') ? { title: tokenAttribute(token, 'title') } : {}),
        start,
        end: result.content.length,
      });
      continue;
    }
    if (token.type === 'html_inline') {
      const start = result.content.length;
      appendSpan(result, token.content, styles);
      result.references.push({
        type: 'html_inline',
        source: token.content,
        start,
        end: result.content.length,
      });
      continue;
    }
    if (token.type === 'text') {
      appendSpan(result, token.content, styles);
      continue;
    }
    if (token.content) appendSpan(result, token.content, styles);
  }
  return result;
}

function sliceInline(value: InlineContent, start: number): InlineContent {
  let offset = 0;
  const spans: WriterSpan[] = [];
  for (const span of value.spans) {
    const end = offset + span.text.length;
    if (end > start) {
      spans.push({ ...span, text: span.text.slice(Math.max(0, start - offset)) });
    }
    offset = end;
  }
  return {
    content: value.content.slice(start),
    spans,
    references: value.references.flatMap((reference) => {
      const referenceStart = Number(reference.start);
      const referenceEnd = Number(reference.end);
      if (!Number.isFinite(referenceStart) || !Number.isFinite(referenceEnd)) {
        return [reference];
      }
      if (referenceEnd <= start) return [];
      return [{
        ...reference,
        start: Math.max(0, referenceStart - start),
        end: Math.max(0, referenceEnd - start),
      }];
    }),
  };
}

function closingTokenIndex(tokens: MarkdownToken[], openingIndex: number): number {
  let depth = 0;
  for (let index = openingIndex; index < tokens.length; index += 1) {
    depth += tokens[index].nesting;
    if (depth === 0) return index;
  }
  return openingIndex;
}

function sourceRange(
  lines: string[],
  token: MarkdownToken,
): { source: string; start: number; end: number } {
  const start = token.map?.[0] ?? 0;
  const end = token.map?.[1] ?? start;
  return { source: lines.slice(start, end).join('\n'), start, end };
}

function rawBlockType(source: string): string {
  const trimmed = source.trim();
  if (/^\[\^[^\]]+\]:/.test(trimmed)) return 'footnote';
  if (/^\[[^\]^]+\]:\s*\S+/.test(trimmed)) return 'reference_definition';
  if (/^(?:\$\$|\\\[)/.test(trimmed)) return 'math';
  if (/^---\s*\n[\s\S]*\n---\s*$/.test(trimmed)) return 'frontmatter';
  return 'raw_markdown';
}

function headingTree(blocks: WriterBlock[]): WriterBlock[] {
  const roots: WriterBlock[] = [];
  const stack: Array<{ level: number; block: WriterBlock }> = [];
  for (const block of blocks) {
    if (block.type === 'heading') {
      const level = Math.min(6, Math.max(1, Number(block.numbering?.level ?? 2)));
      while (stack.length > 0 && stack[stack.length - 1].level >= level) stack.pop();
      if (stack.length > 0) {
        stack[stack.length - 1].block.children = [
          ...(stack[stack.length - 1].block.children ?? []),
          block,
        ];
      } else {
        roots.push(block);
      }
      stack.push({ level, block });
    } else if (stack.length > 0) {
      stack[stack.length - 1].block.children = [
        ...(stack[stack.length - 1].block.children ?? []),
        block,
      ];
    } else {
      roots.push(block);
    }
  }
  return roots;
}

/** Convert CommonMark, GFM, and losslessly preserved extension syntax to Writer IR. */
export function writerDocumentFromMarkdown(
  markdown: string,
  documentId = 'writer-document',
): WriterDocument {
  const normalizedId = normalizeDocumentId(documentId);
  const normalizedMarkdown = markdown.replace(/\r\n?/g, '\n');
  const lines = normalizedMarkdown.split('\n');
  const tokens = markdownParser.parse(normalizedMarkdown, {}) as MarkdownToken[];
  const blocks: WriterBlock[] = [];
  let sequence = 0;
  let title = '';
  let emittedBlock = false;

  const nextId = (type: string): string => {
    sequence += 1;
    return `${normalizedId}-${type.replace(/[^a-zA-Z0-9_-]+/g, '-')}-${sequence}`;
  };

  const createBlock = (
    type: string,
    content: string,
    token: MarkdownToken,
    extra: Partial<WriterBlock> = {},
  ): WriterBlock => {
    const range = sourceRange(lines, token);
    return {
      node_id: nextId(type),
      type,
      content,
      stage: 'final',
      ...extra,
      provider_payload: {
        ...extra.provider_payload,
        markdown_source: range.source,
        markdown_source_start: range.start,
        markdown_source_end: range.end,
      },
    };
  };

  const parseSequence = (start: number, end: number): WriterBlock[] => {
    const parsed: WriterBlock[] = [];
    for (let index = start; index < end;) {
      const token = tokens[index];
      if (token.type === 'heading_open') {
        const close = closingTokenIndex(tokens, index);
        const inline = tokens.slice(index + 1, close).find((item) => item.type === 'inline');
        const rich = inlineContent(inline?.children);
        const level = Number(token.tag.slice(1)) || 1;
        if (level === 1 && !emittedBlock && !title) {
          title = rich.content.trim();
          emittedBlock = true;
        } else {
          parsed.push(createBlock('heading', rich.content, token, {
            numbering: { level },
            spans: rich.spans,
            references: rich.references,
          }));
          emittedBlock = true;
        }
        index = close + 1;
        continue;
      }

      if (token.type === 'paragraph_open') {
        const close = closingTokenIndex(tokens, index);
        const inline = tokens.slice(index + 1, close).find((item) => item.type === 'inline');
        let rich = inlineContent(inline?.children);
        const range = sourceRange(lines, token);
        const math = /^(?:\$\$[\s\S]*\$\$|\\\[[\s\S]*\\\])\s*$/.test(range.source.trim());
        const preservedType = rawBlockType(range.source);
        const rawSyntax = preservedType !== 'raw_markdown';
        const visibleTokens = (inline?.children ?? []).filter(
          (item) => item.type !== 'text' || item.content.trim() !== '',
        );
        const soleImage = visibleTokens.length === 1 && visibleTokens[0].type === 'image';
        if (rawSyntax) {
          parsed.push(createBlock(preservedType, range.source, token, { editable: false }));
        } else if (soleImage) {
          const image = visibleTokens[0];
          parsed.push(createBlock('image', image.content, token, {
            spans: rich.spans,
            references: [{
              type: 'media_asset',
              url: tokenAttribute(image, 'src'),
              path: tokenAttribute(image, 'src'),
              alt: image.content,
              ...(tokenAttribute(image, 'title') ? { title: tokenAttribute(image, 'title') } : {}),
            }],
          }));
        } else {
          const task = /^\[([ xX])\]\s+/.exec(rich.content);
          if (task) rich = sliceInline(rich, task[0].length);
          parsed.push(createBlock(math ? 'math' : 'paragraph', math ? range.source : rich.content, token, {
            spans: math ? [] : rich.spans,
            references: math ? [] : rich.references,
            ...(math ? { editable: false } : {}),
            ...(task ? { numbering: { task: true, checked: task[1].toLowerCase() === 'x' } } : {}),
          }));
        }
        emittedBlock = true;
        index = close + 1;
        continue;
      }

      if (token.type === 'bullet_list_open' || token.type === 'ordered_list_open') {
        const close = closingTokenIndex(tokens, index);
        const ordered = token.type === 'ordered_list_open';
        const declaredStart = Number(tokenAttribute(token, 'start')) || 1;
        let ordinal = declaredStart;
        for (let cursor = index + 1; cursor < close;) {
          const itemToken = tokens[cursor];
          if (itemToken.type !== 'list_item_open' || itemToken.level !== token.level + 1) {
            cursor += 1;
            continue;
          }
          const itemClose = closingTokenIndex(tokens, cursor);
          const itemParts = parseSequence(cursor + 1, itemClose);
          const first = itemParts.shift();
          let content = first?.content ?? '';
          let spans = first?.spans ?? [];
          let references = first?.references ?? [];
          const itemRange = sourceRange(lines, itemToken);
          const markerPattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/;
          const firstLineBody = itemRange.source.split('\n')[0].replace(markerPattern, '');
          const task = /^\[([ xX])\]\s+/.exec(firstLineBody);
          if (task && /^\[([ xX])\]\s+/.test(content)) {
            const rich = sliceInline({ content, spans, references }, task[0].length);
            ({ content, spans, references } = rich);
          }
          const explicitOrdinal = Number(itemToken.info);
          const item = createBlock('list_item', content, itemToken, {
            spans,
            references,
            children: [
              ...(first?.children ?? []),
              ...itemParts,
            ],
            numbering: {
              ordered,
              marker: ordered ? `${Number.isFinite(explicitOrdinal) && explicitOrdinal > 0 ? explicitOrdinal : ordinal}.` : (itemToken.markup || '-'),
              ...(ordered ? {
                number: [Number.isFinite(explicitOrdinal) && explicitOrdinal > 0 ? explicitOrdinal : ordinal],
                start: declaredStart,
              } : {}),
              ...(task ? { task: true, checked: task[1].toLowerCase() === 'x' } : {}),
            },
          });
          parsed.push(item);
          ordinal += 1;
          cursor = itemClose + 1;
        }
        emittedBlock = true;
        index = close + 1;
        continue;
      }

      if (token.type === 'blockquote_open') {
        const close = closingTokenIndex(tokens, index);
        const parts = parseSequence(index + 1, close);
        const first = parts.shift();
        parsed.push(createBlock('quote', first?.content ?? '', token, {
          spans: first?.spans ?? [],
          references: first?.references ?? [],
          children: [...(first?.children ?? []), ...parts],
        }));
        emittedBlock = true;
        index = close + 1;
        continue;
      }

      if (token.type === 'table_open') {
        const close = closingTokenIndex(tokens, index);
        const range = sourceRange(lines, token);
        parsed.push(createBlock('table', range.source, token, { editable: false }));
        emittedBlock = true;
        index = close + 1;
        continue;
      }

      if (token.type === 'fence' || token.type === 'code_block') {
        const info = token.info.trim();
        const [language = '', ...meta] = info.split(/\s+/).filter(Boolean);
        parsed.push(createBlock('code', token.content.replace(/\n$/, ''), token, {
          ...(language ? { language } : {}),
          ...((language || meta.length > 0) ? {
            provider_payload: {
              ...(language ? { code_language: language } : {}),
              ...(meta.length > 0 ? { code_meta: meta.join(' ') } : {}),
            },
          } : {}),
        }));
        emittedBlock = true;
        index += 1;
        continue;
      }

      if (token.type === 'hr') {
        parsed.push(createBlock('divider', token.markup || '---', token));
        emittedBlock = true;
        index += 1;
        continue;
      }

      if (token.type === 'html_block') {
        parsed.push(createBlock('html', token.content, token, { editable: false }));
        emittedBlock = true;
        index += 1;
        continue;
      }

      if (token.nesting === 0 && token.map && token.content.trim()) {
        parsed.push(createBlock(token.type || 'raw_markdown', token.content, token, {
          editable: false,
        }));
        emittedBlock = true;
      }
      index += 1;
    }
    return parsed;
  };

  blocks.push(...parseSequence(0, tokens.length));

  const coveredLines = new Set<number>();
  tokens.forEach((token) => {
    if (!token.map) return;
    for (let line = token.map[0]; line < token.map[1]; line += 1) coveredLines.add(line);
  });
  for (let line = 0; line < lines.length;) {
    if (coveredLines.has(line) || !lines[line].trim()) {
      line += 1;
      continue;
    }
    const start = line;
    while (line < lines.length && !coveredLines.has(line) && lines[line].trim()) line += 1;
    const end = line;
    const source = lines.slice(start, end).join('\n');
    blocks.push({
      node_id: nextId(rawBlockType(source)),
      type: rawBlockType(source),
      content: source,
      stage: 'final',
      editable: false,
      provider_payload: {
        markdown_source: source,
        markdown_source_start: start,
        markdown_source_end: end,
      },
    });
  }

  blocks.sort((left, right) => {
    const leftStart = Number(left.provider_payload?.markdown_source_start);
    const rightStart = Number(right.provider_payload?.markdown_source_start);
    return (Number.isFinite(leftStart) ? leftStart : Number.MAX_SAFE_INTEGER)
      - (Number.isFinite(rightStart) ? rightStart : Number.MAX_SAFE_INTEGER);
  });

  const signedBlocks = headingTree(blocks).map(addPreservationSignatures);
  const document: WriterDocument = {
    document_id: normalizedId,
    stage: 'final',
    title,
    blocks: signedBlocks,
    ui_editable: false,
    metadata: {
      source: 'markdown-download',
      markdown_source: markdown,
    },
  };
  document.metadata = {
    ...document.metadata,
    markdown_signature: documentSignature(document),
  };
  return document;
}

function preservedDocumentSource(document: WriterDocument): string | undefined {
  const source = document.metadata?.markdown_source;
  const signature = document.metadata?.markdown_signature;
  return typeof source === 'string'
    && typeof signature === 'string'
    && signature === documentSignature(document)
    ? source
    : undefined;
}

function preservedBlockSource(block: WriterBlock): string | undefined {
  const payload = block.provider_payload as MarkdownSourcePayload | undefined;
  return typeof payload?.markdown_source === 'string'
    && typeof payload.markdown_signature === 'string'
    && payload.markdown_signature === blockSignature(block)
    ? payload.markdown_source
    : undefined;
}

function escapeMarkdownText(value: string): string {
  return value.replace(/([\\`*_[\]{}#+.!|>-])/g, '\\$1');
}

function inlineCode(value: string): string {
  const longest = Math.max(0, ...Array.from(value.matchAll(/`+/g), (match) => match[0].length));
  const fence = '`'.repeat(longest + 1);
  const padding = /^`|`$|^\s|\s$/.test(value) ? ' ' : '';
  return `${fence}${padding}${value}${padding}${fence}`;
}

function renderStyledText(value: string, span: WriterSpan | undefined): string {
  let result = escapeMarkdownText(value);
  if (!span) return result;
  const styles = getWriterSpanStyles(span);
  if (styles.includes('code')) result = inlineCode(value);
  if (styles.includes('strong') || styles.includes('bold')) result = `**${result}**`;
  if (styles.includes('italic')) result = `*${result}*`;
  if (styles.includes('strike') || styles.includes('strikethrough')) result = `~~${result}~~`;
  if (styles.includes('underline')) result = `<u>${result}</u>`;

  const textColor = writerTextColorHex(getWriterSpanColor(span, 'text_color'));
  const backgroundColor = writerBackgroundColorHex(getWriterSpanColor(span, 'background_color'));
  if (textColor || backgroundColor) {
    const style = [
      textColor ? `color:${textColor}` : '',
      backgroundColor ? `background-color:${backgroundColor}` : '',
    ].filter(Boolean).join(';');
    result = `<span style="${style}">${result}</span>`;
  }
  return result;
}

function spanAt(block: WriterBlock, position: number): WriterSpan | undefined {
  let offset = 0;
  for (const span of block.spans ?? []) {
    const end = offset + span.text.length;
    if (position >= offset && position < end) return span;
    offset = end;
  }
  return undefined;
}

function renderStyledRange(block: WriterBlock, start: number, end: number): string {
  const content = block.content ?? '';
  const spans = block.spans ?? [];
  if (spans.map((span) => span.text).join('') !== content) {
    return escapeMarkdownText(content.slice(start, end));
  }
  const boundaries = new Set([start, end]);
  let offset = 0;
  spans.forEach((span) => {
    offset += span.text.length;
    if (offset > start && offset < end) boundaries.add(offset);
  });
  const sorted = [...boundaries].sort((left, right) => left - right);
  return sorted.slice(0, -1).map((position, index) => (
    renderStyledText(content.slice(position, sorted[index + 1]), spanAt(block, position))
  )).join('');
}

function renderInline(block: WriterBlock): string {
  const content = block.content ?? '';
  const media = block.references?.find((reference) => reference.type === 'media_asset');
  if (media) {
    const url = String(media.url ?? media.path ?? '').replace(/\s/g, '%20');
    if (url) {
      const title = typeof media.title === 'string' && media.title
        ? ` "${media.title.replace(/"/g, '\\"')}"`
        : '';
      return `![${escapeMarkdownText(String(media.alt ?? content))}](${url}${title})`;
    }
  }
  const references = (block.references ?? []).filter((reference) => (
    (reference.type === 'link' || reference.type === 'markdown_image')
    && Number.isFinite(Number(reference.start))
    && Number.isFinite(Number(reference.end))
  )).sort((left, right) => Number(left.start) - Number(right.start));
  let cursor = 0;
  let output = '';
  for (const reference of references) {
    const start = Math.max(cursor, Math.min(content.length, Number(reference.start)));
    const end = Math.max(start, Math.min(content.length, Number(reference.end)));
    output += renderStyledRange(block, cursor, start);
    const url = String(reference.url ?? reference.href ?? '').replace(/\s/g, '%20');
    const title = typeof reference.title === 'string' && reference.title
      ? ` "${reference.title.replace(/"/g, '\\"')}"`
      : '';
    if (reference.type === 'markdown_image') {
      output += `![${escapeMarkdownText(String(reference.alt ?? content.slice(start, end)))}](${url}${title})`;
    } else {
      output += `[${renderStyledRange(block, start, end)}](${url}${title})`;
    }
    cursor = end;
  }
  return output + renderStyledRange(block, cursor, content.length);
}

function listOrdinal(block: WriterBlock, fallback: number): number {
  const number = block.numbering?.number;
  if (Array.isArray(number)) {
    const last = Number(number[number.length - 1]);
    if (Number.isFinite(last) && last > 0) return last;
  }
  const value = Number(block.numbering?.value ?? block.numbering?.start);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function renderListItem(block: WriterBlock, depth: number, fallbackOrdinal: number): string {
  const raw = preservedBlockSource(block);
  if (raw) return raw;
  const ordered = Boolean(block.numbering?.ordered);
  const marker = ordered ? `${listOrdinal(block, fallbackOrdinal)}.` : String(block.numbering?.marker ?? '-');
  const checkbox = block.numbering?.task
    ? `[${block.numbering.checked ? 'x' : ' '}] `
    : '';
  const indent = '  '.repeat(depth);
  const continuation = `${indent}${' '.repeat(marker.length + 1)}`;
  const content = `${checkbox}${renderInline(block)}`;
  const lines = content.split('\n');
  let output = `${indent}${marker} ${lines[0] ?? ''}`;
  if (lines.length > 1) output += `\n${lines.slice(1).map((line) => `${continuation}${line}`).join('\n')}`;
  if ((block.children?.length ?? 0) > 0) {
    const children = renderBlockSequence(block.children ?? [], depth + 1, false);
    if (children) output += `\n${children}`;
  }
  return output;
}

function codeFence(content: string): string {
  const longest = Math.max(2, ...Array.from(content.matchAll(/^\s*(`{3,})/gm), (match) => match[1].length));
  return '`'.repeat(longest + 1);
}

function renderBlockStructured(block: WriterBlock, depth: number, allowRaw: boolean): string {
  if (allowRaw) {
    const raw = preservedBlockSource(block);
    if (raw) return raw;
  }
  const content = block.content ?? '';
  let current = '';
  if (block.type === 'document') {
    return renderBlockSequence(block.children ?? [], depth, allowRaw);
  }
  if (block.type === 'heading') {
    const level = Math.min(6, Math.max(1, Number(block.numbering?.level ?? 2)));
    current = `${'#'.repeat(level)} ${renderInline(block)}`;
  } else if (block.type === 'paragraph') {
    current = renderInline(block);
  } else if (block.type === 'quote') {
    const body = [renderInline(block), renderBlockSequence(block.children ?? [], depth, false)]
      .filter(Boolean).join('\n\n');
    current = body.split('\n').map((line) => line ? `> ${line}` : '>').join('\n');
    return current;
  } else if (block.type === 'code') {
    if (/^\s*(```|~~~)/.test(content)) current = content;
    else {
      const fence = codeFence(content);
      const language = typeof block.language === 'string' && block.language.trim()
        ? block.language.trim()
        : isRecord(block.provider_payload) && typeof block.provider_payload.code_language === 'string'
          ? block.provider_payload.code_language.trim()
          : '';
      const meta = isRecord(block.provider_payload) && typeof block.provider_payload.code_meta === 'string'
        ? ` ${block.provider_payload.code_meta.trim()}`
        : '';
      current = `${fence}${language}${meta}\n${content}\n${fence}`;
    }
  } else if (block.type === 'divider') {
    current = content.trim() && /^(?:[-*_]\s*){3,}$/.test(content.trim()) ? content.trim() : '---';
  } else if (block.type === 'image') {
    const reference = block.references?.find((item) => item.type === 'media_asset') ?? {};
    const url = String(reference.url ?? reference.path ?? '').replace(/\s/g, '%20');
    const title = typeof reference.title === 'string' && reference.title
      ? ` "${reference.title.replace(/"/g, '\\"')}"`
      : '';
    current = url ? `![${escapeMarkdownText(content)}](${url}${title})` : escapeMarkdownText(content);
  } else {
    current = content;
  }

  const children = renderBlockSequence(block.children ?? [], depth, allowRaw);
  return [current, children].filter(Boolean).join('\n\n');
}

function renderBlockSequence(
  blocks: WriterBlock[],
  listDepth = 0,
  allowRaw = true,
): string {
  const parts: string[] = [];
  for (let index = 0; index < blocks.length;) {
    const block = blocks[index];
    if (block.type !== 'list_item') {
      const rendered = renderBlockStructured(block, listDepth, allowRaw);
      if (rendered) parts.push(rendered);
      index += 1;
      continue;
    }
    const ordered = Boolean(block.numbering?.ordered);
    const group: string[] = [];
    let fallbackOrdinal = listOrdinal(block, 1);
    while (
      index < blocks.length
      && blocks[index].type === 'list_item'
      && Boolean(blocks[index].numbering?.ordered) === ordered
    ) {
      group.push(renderListItem(blocks[index], listDepth, fallbackOrdinal));
      fallbackOrdinal += 1;
      index += 1;
    }
    parts.push(group.join('\n'));
  }
  return parts.join('\n\n');
}

/** Render every Writer block type, with lossless source reuse when it is unchanged. */
export function writerDocumentToMarkdown(document: WriterDocument): string {
  const preserved = preservedDocumentSource(document);
  if (preserved !== undefined) return preserved;
  const title = document.title.trim() ? `# ${escapeMarkdownText(document.title.trim())}` : '';
  const body = renderBlockSequence(document.blocks);
  const rendered = [title, body].filter(Boolean).join('\n\n').trimEnd();
  return rendered ? `${rendered}\n` : '';
}

export function writerDocumentToLmdContent(document: WriterDocument): string {
  return `${JSON.stringify({
    schema: WRITER_IR_SCHEMA,
    schema_version: WRITER_ARTIFACT_SCHEMA_VERSION,
    data: document,
    meta: {
      created_by: 'lazymind-download',
      created_at: new Date().toISOString(),
    },
  }, null, 2)}\n`;
}
