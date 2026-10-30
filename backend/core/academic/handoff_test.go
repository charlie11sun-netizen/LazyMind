package academic

import (
	"encoding/json"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gorilla/mux"
	"gorm.io/gorm"
	"lazymind/core/common/orm"
)

func TestReadingHandoffIncludesOnlySelectedNotesAndFreezesInput(t *testing.T) {
	db := paperFixture(t)
	for _, id := range []string{"a", "b"} {
		var work orm.AcademicWork
		if err := db.Where("id=?", id).Take(&work).Error; err != nil {
			t.Fatal(err)
		}
		entry, err := importedLibraryEntry(t.Context(), db.DB, "papers", work, nil, "", "bibliography", true)
		if err != nil {
			t.Fatal(err)
		}
		content := "Durable note"
		if id == "b" {
			content = "Do not include"
		}
		if err = db.Create(&orm.PaperLibraryNote{DatasetID: "papers", ID: "note-" + id, PaperID: entry.ID, Title: "Note", Content: content, Kind: "note"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	body := `{"kind":"reflection","dataset_id":"papers","document_ids":["a"],"user_instruction":"关注方法贡献","request_key":"reading-notes"}`
	w := httptest.NewRecorder()
	LaunchPaperWorkflow(w, paperRequest(body))
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	var handoff orm.AcademicHandoff
	if err := db.Where("request_key=?", "reading-notes").Take(&handoff).Error; err != nil {
		t.Fatal(err)
	}
	var job orm.AsyncJob
	if err := db.Where("resource_id=? AND job_type=?", handoff.ConversationID, ChatLaunchJobType).Take(&job).Error; err != nil {
		t.Fatal(err)
	}
	var launch map[string]string
	if err := json.Unmarshal(job.PayloadJSON, &launch); err != nil {
		t.Fatal(err)
	}
	if launch["workflow_id"] != "paper-writing" || launch["query"] != "请生成阅读心得。\n关注方法贡献" {
		t.Fatal("reading task did not target F7", launch)
	}
	context := academicChatContext(t.Context(), db.DB, "u", handoff.ConversationID)
	if !strings.Contains(context, "reading_reflection") || !strings.Contains(context, "advance_step_and_hand_off") || strings.Contains(context, "生成能力待对接") {
		t.Fatal("reading task retained Mock instructions", context)
	}
	if !strings.Contains(context, "stop for user review") || !strings.Contains(context, "including through the Continue control") || !strings.Contains(context, "if finish is the only Ready step, execute it without asking for another confirmation") {
		t.Fatal("reading task must pause for review and honor confirmed continuation", context)
	}
	var input PaperTaskInput
	if err := json.Unmarshal(handoff.InputJSON, &input); err != nil {
		t.Fatal(err)
	}
	if input.UserInstruction != "关注方法贡献" || len(input.Papers) != 1 || input.Papers[0].PaperID != "a" || len(input.Notes) != 1 || input.Notes[0].PaperID != "a" || input.Notes[0].Content != "Durable note" {
		t.Fatalf("reading-note contract mismatch: %+v", input)
	}
	var fields map[string]json.RawMessage
	_ = json.Unmarshal(handoff.InputJSON, &fields)
	if fields["candidate_directions"] != nil || fields["selected_directions"] != nil {
		t.Fatal("reading notes received literature-review directions")
	}
	w = httptest.NewRecorder()
	r := paperRequest("")
	r.URL.RawQuery = "conversation_id=" + handoff.ConversationID
	AcademicChatContext(w, r)
	var taskContext struct {
		DatasetID string          `json:"dataset_id"`
		Input     json.RawMessage `json:"input"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &taskContext)
	if w.Code != 200 || taskContext.DatasetID != "papers" || string(taskContext.Input) != string(handoff.InputJSON) {
		t.Fatal("Workflow context lost the original input", w.Code, w.Body.String())
	}
	db.Model(&orm.PaperLibraryNote{}).Where("dataset_id=? AND id=?", "papers", "note-a").Update("content", "Edited after launch")
	w = httptest.NewRecorder()
	LaunchPaperWorkflow(w, paperRequest(body))
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	var jobs int64
	db.Model(&orm.AsyncJob{}).Where("resource_id=? AND job_type=?", handoff.ConversationID, ChatLaunchJobType).Count(&jobs)
	if jobs != 1 {
		t.Fatalf("replay created %d launch jobs", jobs)
	}
	w = httptest.NewRecorder()
	SubmitMockHandoff(w, mux.SetURLVars(paperRequest(`{}`), map[string]string{"handoff_id": handoff.ID}))
	var receipt struct {
		Input PaperTaskInput `json:"input"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &receipt)
	if w.Code != 200 || len(receipt.Input.Notes) != 1 || receipt.Input.Notes[0].Content != "Durable note" {
		t.Fatal("replay rewrote the frozen notes", w.Code, w.Body.String())
	}
}

func TestOtherHandoffRoutesKeepMockWorkflows(t *testing.T) {
	db := paperFixture(t)
	for _, tc := range []struct {
		kind, workflow  string
		reflectionFirst bool
	}{{"ppt", "paper_workflow_mock", false}, {"ppt", "paper_ppt_with_reflection_mock", true}} {
		input := ReviewWorkflowInput{UserInstruction: "原始要求", Papers: []WorkflowPaper{{Paper: PaperRef{PaperID: "a", Title: "论文 A", SourceDocumentID: "a"}, IsSeed: true}}}
		handoff, err := createPaperHandoff(t.Context(), db.DB, "u", "papers", "", 0, tc.kind, tc.workflow+tc.kind, input, nil, tc.reflectionFirst)
		if err != nil {
			t.Fatal(err)
		}
		var job orm.AsyncJob
		if err := db.Where("resource_id=?", handoff.ConversationID).Take(&job).Error; err != nil {
			t.Fatal(err)
		}
		var payload map[string]string
		_ = json.Unmarshal(job.PayloadJSON, &payload)
		if payload["workflow_id"] != tc.workflow || !strings.Contains(academicChatContext(t.Context(), db.DB, "u", handoff.ConversationID), "生成能力待对接") {
			t.Fatal("unrelated task route changed", tc, payload)
		}
	}
}

func TestAcademicChatContextDistinguishesAbsentTaskAndInaccessibleChat(t *testing.T) {
	db := paperFixture(t)
	conversation, err := newAcademicConversation(db.DB, "u", "papers", "普通对话", "reflection")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ conversation, user, code string }{
		{conversation, "u", "PAPER_TASK_NOT_FOUND"}, {conversation, "another-user", "NOT_FOUND"}, {"missing", "u", "NOT_FOUND"},
	} {
		w := httptest.NewRecorder()
		r := paperRequest("")
		r.Header.Set("X-User-Id", tc.user)
		r.URL.RawQuery = "conversation_id=" + tc.conversation
		AcademicChatContext(w, r)
		var result map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &result)
		if w.Code != 404 || result["code"] != tc.code {
			t.Fatal(tc, w.Code, w.Body.String())
		}
	}
	for _, table := range []string{"conversations", "academic_expansions", "academic_handoffs"} {
		t.Run(table, func(t *testing.T) {
			if err := db.Callback().Query().Before("gorm:query").Register("test:read_error", func(tx *gorm.DB) {
				if tx.Statement.Table == table {
					tx.AddError(errors.New("read failed"))
				}
			}); err != nil {
				t.Fatal(err)
			}
			defer db.Callback().Query().Remove("test:read_error")
			w := httptest.NewRecorder()
			r := paperRequest("")
			r.URL.RawQuery = "conversation_id=" + conversation
			AcademicChatContext(w, r)
			if w.Code != 500 || !strings.Contains(w.Body.String(), "READ_FAILED") {
				t.Fatal("database error treated as task absence", w.Code, w.Body.String())
			}
		})
	}
}

func TestHandoffFullTextIsScopedAndPinnedWithoutAnExpansion(t *testing.T) {
	db := paperFixture(t)
	papers := []WorkflowPaper{{Paper: PaperRef{PaperID: "a", SourceDocumentID: "a"}, IsSeed: true}}
	seeds, err := seedSources(db.DB, "papers", papers)
	if err != nil {
		t.Fatal(err)
	}
	pages := []ContentPage{}
	for page := 1; page <= 7; page++ {
		pages = append(pages, ContentPage{Page: page, Text: "Selected paper text"})
	}
	content := orm.AcademicPaperContent{ID: "handoff-body", UserID: "u", PaperID: "a", ParserVersion: orm.AcademicPaperParserVersion, SourceKey: seeds[0].key(), SourceKind: "kb", SourceDatasetID: "papers", SourceDocumentID: "a", Status: "ready", ContentHash: "original-body", PagesJSON: marshal(pages)}
	if err := db.Create(&content).Error; err != nil {
		t.Fatal(err)
	}
	handoff, err := createPaperHandoff(t.Context(), db.DB, "u", "papers", "", 0, "reflection", "reading-body", ReviewWorkflowInput{UserInstruction: "Read A", Papers: papers}, []orm.AcademicPaperContent{content}, false)
	if err != nil {
		t.Fatal(err)
	}
	body := string(marshal(map[string]string{"handoff_id": handoff.ID, "paper_id": "a"}))
	w := httptest.NewRecorder()
	ReadPaperContent(w, paperRequest(body))
	var first struct {
		Pages      []ContentPage `json:"pages"`
		NextCursor string        `json:"next_cursor"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &first)
	if w.Code != 200 || len(first.Pages) != 5 || first.NextCursor == "" {
		t.Fatal("independent reading task could not read full text", w.Code, w.Body.String())
	}
	w = httptest.NewRecorder()
	ReadPaperContent(w, paperRequest(string(marshal(map[string]string{"cursor": first.NextCursor}))))
	var last struct {
		Pages []ContentPage `json:"pages"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &last)
	if w.Code != 200 || len(last.Pages) != 2 || last.Pages[0].Page != 6 {
		t.Fatal("cursor lost the frozen handoff", w.Code, w.Body.String())
	}
	for _, tc := range []struct {
		body string
		user string
		code int
	}{
		{string(marshal(map[string]string{"handoff_id": handoff.ID, "paper_id": "b"})), "u", 422},
		{body, "another-user", 404},
		{string(marshal(map[string]string{"cursor": first.NextCursor, "handoff_id": "another-task"})), "u", 400},
		{string(marshal(map[string]string{"cursor": first.NextCursor, "expansion_id": "another-task"})), "u", 400},
	} {
		w = httptest.NewRecorder()
		r := paperRequest(tc.body)
		r.Header.Set("X-User-Id", tc.user)
		ReadPaperContent(w, r)
		if w.Code != tc.code {
			t.Fatalf("task scope bypass: want %d, got %d: %s", tc.code, w.Code, w.Body.String())
		}
	}
	db.Model(&content).Update("content_hash", "replacement-body")
	w = httptest.NewRecorder()
	ReadPaperContent(w, paperRequest(body))
	if w.Code != 409 || !strings.Contains(w.Body.String(), "VERSION_CHANGED") {
		t.Fatal("reader silently switched the frozen content version", w.Code, w.Body.String())
	}
}
