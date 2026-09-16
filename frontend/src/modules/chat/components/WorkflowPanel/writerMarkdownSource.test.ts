import { describe, expect, it } from 'vitest';
import { markdownParagraphAtRange, markdownSelectionRange, preserveMarkdownSource } from './writerMarkdownSource';

describe('source positions and untouched Markdown', () => {
 it('reattaches the correct repeated paragraph after the editor DOM is rebuilt', () => {
  const source = 'Accepted and expanded\n\n😀 **Same**\n\nMiddle edit\n\n😀 **Same**';
  const root = document.createElement('div');
  root.innerHTML = '<div class="mdxeditor-root-contenteditable"><p>Accepted and expanded</p><p>😀 <strong>Same</strong></p><p>Middle edit</p><p>😀 <strong>Same</strong></p></div>';
  const start = source.lastIndexOf('😀');
  expect(markdownParagraphAtRange(root, source, start, source.length)).toBe(root.querySelectorAll('p')[3]);
  root.querySelectorAll('p')[3].remove();
  expect(markdownParagraphAtRange(root, source, start, source.length)).toBeNull();
 });
 it('locates the second identical paragraph with Unicode and formatting', () => {
  const source = '😀 **相同** [链接](https://example.org)\n\n😀 **相同** [链接](https://example.org)';
  const editor = document.createElement('div'); editor.className = 'mdxeditor-root-contenteditable';
  editor.innerHTML = '<p>😀 <strong>相同</strong> <a>链接</a></p><p>😀 <strong>相同</strong> <a>链接</a></p>';
  const result = markdownSelectionRange(source, { selectedText:'相同 链接', paragraph: editor.querySelectorAll('p')[1], startOffset:3 });
  const runes = Array.from(source);
  expect(result.start).toBeGreaterThan(Array.from(source).length / 2);
  expect(runes.slice(result.start, result.end).join('')).toBe(result.selected_text);
  expect(result.selected_text).toBe('相同** [链接');
 });
 it('refuses to guess an identical paragraph without DOM identity', () => {
  expect(() => markdownSelectionRange('same\n\nsame', {selectedText:'same'})).toThrow(/ambiguous/);
 });
 it('preserves unedited links, tables, ampersands and terminal newlines', () => {
  const original = '\nA & B\n\nhttps://example.org\n\n| A | B |\n|---|---|\n|1|2|\n\n';
  const normalized = 'A \\& B\n\n[https://example.org](https://example.org)\n\n| A | B |\n| - | - |\n| 1 | 2 |';
  expect(preserveMarkdownSource(original, normalized, normalized.replace('A \\& B','Updated'))).toBe(original.replace('A & B','Updated'));
  expect(preserveMarkdownSource(original, normalized, normalized)).toBe(original);
 });
});

it('keeps untouched inline URL and ampersand spelling while editing the same paragraph', () => {
  const source = 'Old https://example.org A & B.\n';
  const normalized = 'Old [https://example.org](https://example.org) A \\& B.';
  expect(preserveMarkdownSource(source, normalized, normalized.replace('Old','New'))).toBe(source.replace('Old','New'));
});

it('changes inline formatting without normalizing an unrelated URL in that paragraph', () => {
  const source = 'Old https://example.org A & B.\n';
  const normalized = 'Old [https://example.org](https://example.org) A \\& B.';
  expect(preserveMarkdownSource(source, normalized, normalized.replace('Old','**Old**'))).toBe(source.replace('Old','**Old**'));
});

it('preserves existing source blocks when paragraphs are inserted or removed', () => {
 const source='Old\n\nhttps://example.org\n\n| A | B |\n|---|---|\n|1|2|\n';
 const previous='Old\n\n[https://example.org](https://example.org)\n\n| A | B |\n| - | - |\n| 1 | 2 |';
 expect(preserveMarkdownSource(source,previous,previous.replace('Old','Old\n\nNew paragraph'))).toBe(source.replace('Old','Old\n\nNew paragraph'));
 expect(preserveMarkdownSource(source,previous,previous.replace('Old\n\n',''))).toBe(source.replace('Old\n\n',''));
});
it('applies text and formatting edits together without normalizing untouched text', () => {
 expect(preserveMarkdownSource('Old A & B.\n','Old A \\& B.','**New** A \\& B.')).toBe('**New** A & B.\n');
});
it('preserves inline source when splitting and merging paragraphs', () => {
 const source='Old https://example.org A & B.\n';
 const before='Old [https://example.org](https://example.org) A \\& B.';
 const split=before.replace('Old ','Old\n\n');
 expect(preserveMarkdownSource(source,before,split)).toBe(source.replace('Old ','Old\n\n'));
 expect(preserveMarkdownSource(source.replace('Old ','Old\n\n'),split,before)).toBe(source);
});
it('keeps edited link destinations with parentheses while preserving unrelated source spelling',()=>{
 const url='https://example.org/a(b)/old';
 const source=`Visit ${url} A & B.\n`;
 const before=`Visit [${url}](${url}) A \\& B.`;
 const after=before.replace('/old)','/new)');
 expect(preserveMarkdownSource(source,before,after)).toBe(`Visit [${url}](https://example.org/a(b)/new) A & B.\n`);
});
