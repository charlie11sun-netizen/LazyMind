package workflow

import (
	"reflect"
	"testing"
)

func TestRewriteMarkdownSourceBoundaries(t *testing.T) {
	for _, source := range []string{"😀 First.\n\nLast.", "😀 First.\r\n\r\nLast.", "😀 First.\n  \nLast."} {
		selections := []map[string]any{{"start": 0, "end": len([]rune(source)), "selected_text": source}}
		blocks, ok := selectedRewriteMarkdownBlocks(source, selections)
		if !ok || len(blocks) != 2 || blocks[0].raw != "😀 First." || blocks[1].raw != "Last." {
			t.Fatalf("bad paragraph boundaries for %q: %#v", source, blocks)
		}
		for _, block := range blocks {
			if string([]rune(source)[block.start:block.end]) != block.raw {
				t.Fatal("offsets are not source code points")
			}
		}
	}
}
func TestRewriteMarkdownSourceRejectsUnsupportedOrAmbiguousQuotes(t *testing.T) {
	for _, source := range []string{"First.\n\n# Heading\n\nLast.", "First.\n\n```\nCode\n\nStill code\n```\n\nLast.", "First.\n# Heading\nLast."} {
		if _, ok := selectedRewriteMarkdownBlocks(source, []map[string]any{{"start": 0, "end": len([]rune(source)), "selected_text": source}}); ok {
			t.Fatalf("accepted unsupported structure: %q", source)
		}
	}
	if _, ok := selectedRewriteMarkdownBlocks("Same.\n\nSame.", []map[string]any{{"selected_text": "Same"}}); ok {
		t.Fatal("accepted ambiguous rendered quote")
	}
}
func TestRewriteMarkdownFormattedAndEscapedQuotes(t *testing.T) {
	source := "前缀**原文**后缀。\n\nA &amp; B with `x_y` and [link](https://example.test)."
	selected := []map[string]any{{"selected_text": "前缀原文"}, {"selected_text": "后缀"}, {"selected_text": "A &amp; B"}, {"selected_text": "x_y"}, {"selected_text": "link"}}
	blocks, ok := selectedRewriteMarkdownBlocks(source, selected)
	if !ok || len(blocks) != 2 {
		t.Fatalf("rendered quotes failed or were not deduplicated: %#v", blocks)
	}
}

func TestRewriteIRSpanDefaultsNested(t *testing.T) {
	source := map[string]any{"blocks": []any{map[string]any{"node_id": "parent", "children": []any{map[string]any{"node_id": "child", "spans": []any{map[string]any{"text": "", "style": map[string]any{}}}}}}}}
	candidate := map[string]any{"blocks": []any{map[string]any{"node_id": "parent", "children": []any{map[string]any{"node_id": "child", "spans": []any{map[string]any{}}}}}}}
	normalizeRewriteIRDefaults(source, true)
	normalizeRewriteIRDefaults(candidate, true)
	if !reflect.DeepEqual(source, candidate) {
		t.Fatal("nested span defaults were treated as content changes")
	}
}
