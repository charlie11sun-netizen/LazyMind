package workflow

import (
	"strings"
	"unicode/utf8"

	"github.com/yuin/goldmark"
	"github.com/yuin/goldmark/ast"
	"github.com/yuin/goldmark/extension"
	"github.com/yuin/goldmark/text"
	"github.com/yuin/goldmark/util"
)

type rewriteMarkdownBlock struct {
	start, end   int
	raw, visible string
	paragraph    bool
}

// Match Writer's source-block boundaries (blank lines outside fenced code),
// then use a Markdown AST for classification and rendered quote matching.
func rewriteMarkdownBlocks(source string) []rewriteMarkdownBlock {
	parser := goldmark.New(goldmark.WithExtensions(extension.Table)).Parser()
	blocks := []rewriteMarkdownBlock{}
	appendBlock := func(start, end int) {
		raw := strings.TrimRight(source[start:end], "\r\n")
		data := []byte(raw)
		tree := parser.Parse(text.NewReader(data))
		var visible strings.Builder
		_ = ast.Walk(tree, func(node ast.Node, entering bool) (ast.WalkStatus, error) {
			if !entering {
				return ast.WalkContinue, nil
			}
			switch node := node.(type) {
			case *ast.Text:
				value := node.Segment.Value(data)
				if node.Parent() == nil || node.Parent().Kind() != ast.KindCodeSpan {
					// Writer matches Mistune AST text, which preserves entity spelling.
					value = util.UnescapePunctuations(value)
				}
				visible.Write(value)
				if node.SoftLineBreak() || node.HardLineBreak() {
					visible.WriteByte(' ')
				}
			case *ast.String:
				visible.Write(node.Value)
			case *ast.AutoLink:
				visible.Write(node.Label(data))
			}
			return ast.WalkContinue, nil
		})
		blocks = append(blocks, rewriteMarkdownBlock{start: utf8.RuneCountInString(source[:start]), end: utf8.RuneCountInString(source[:start+len(raw)]), raw: raw, visible: normalizeRewriteQuote(visible.String()), paragraph: tree.ChildCount() == 1 && tree.FirstChild().Kind() == ast.KindParagraph})
	}
	position, start := 0, -1
	var fence byte
	for _, line := range strings.SplitAfter(source, "\n") {
		offset := position
		position += len(line)
		if line == "" {
			continue
		}
		trimmed := strings.TrimSpace(line)
		marker := byte(0)
		if strings.HasPrefix(trimmed, "```") || strings.HasPrefix(trimmed, "~~~") {
			marker = trimmed[0]
		}
		if fence != 0 {
			if marker == fence {
				fence = 0
			}
			continue
		}
		if marker != 0 {
			fence = marker
		}
		if trimmed != "" {
			if start < 0 {
				start = offset
			}
			continue
		}
		if start >= 0 {
			appendBlock(start, offset)
			start = -1
		}
	}
	if start >= 0 {
		appendBlock(start, len(source))
	}
	return blocks
}

func normalizeRewriteQuote(value string) string { return strings.Join(strings.Fields(value), " ") }

func selectedRewriteMarkdownBlocks(source string, selections []map[string]any) ([]rewriteMarkdownBlock, bool) {
	blocks := rewriteMarkdownBlocks(source)
	selected := map[int]bool{}
	runes := []rune(source)
	for _, selection := range selections {
		found := 0
		if start, hasOffsets := selection["start"].(int); hasOffsets {
			end := selection["end"].(int)
			for index, block := range blocks {
				from, to := max(start, block.start), min(end, block.end)
				if from >= to || strings.TrimSpace(string(runes[from:to])) == "" {
					continue
				}
				if !block.paragraph {
					return nil, false
				}
				selected[index] = true
				found++
			}
		} else {
			quote := normalizeRewriteQuote(selection["selected_text"].(string))
			for index, block := range blocks {
				if strings.Contains(block.visible, quote) || strings.Contains(normalizeRewriteQuote(block.raw), quote) {
					if !block.paragraph {
						return nil, false
					}
					selected[index] = true
					found++
				}
			}
			if found != 1 {
				return nil, false
			}
		}
		if found == 0 {
			return nil, false
		}
	}
	result := []rewriteMarkdownBlock{}
	for index, block := range blocks {
		if selected[index] {
			result = append(result, block)
		}
	}
	return result, true
}
