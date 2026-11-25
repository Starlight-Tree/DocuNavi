package main

// GET /documents（資料の一覧。app/api/documents.py の list_documents の移植）。
// `/documents/{id}/chunks` と `/documents/{id}/file` `/hero` は、まだ Python の API（chunks は original_pdf の判定で R2 から PDF を読む。
// ADR 0017 の段 C と合わせて決める）。ここは 404 を返す。

import (
	"context"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	maxDocuments               = 500 // 暴走した件数を返さないための上限（Python の _MAX_DOCUMENTS と同じ）
	maxRevisionChars           = 40
	detailDocumentsUnavailable = "資料を取得できませんでした。しばらく待ってから再試行してください。"
)

var (
	// 資料群の名前の形。取り込みと DB の制約（db/migrations/0001_schema.sql）と同じ
	collectionPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,40}$`)
	// 対象・資料種別の名前の形。DB の制約と同じ
	classificationPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,39}$`)
)

// pyTime は、pydantic v2 が UTC の datetime を JSON にする形（ミリ秒まで。秒未満が 0 なら小数なし。末尾は Z）。
// Python の API は、DB の時刻を UNIX ミリ秒の整数にしてから datetime に戻すので、ミリ秒未満は切り捨てる。
type pyTime time.Time

func (t pyTime) MarshalJSON() ([]byte, error) {
	u := time.UnixMilli(time.Time(t).UnixMilli()).UTC()
	layout := "2006-01-02T15:04:05Z"
	if u.Nanosecond() != 0 {
		layout = "2006-01-02T15:04:05.000000Z"
	}
	return []byte(`"` + u.Format(layout) + `"`), nil
}

type documentSummary struct {
	ID            int64   `json:"id"`
	Source        string  `json:"source"`
	Title         *string `json:"title"`
	Collection    *string `json:"collection"`
	Target        *string `json:"target"`
	DocType       *string `json:"doc_type"`
	Revision      *string `json:"revision"`
	Status        string  `json:"status"`
	FailureReason *string `json:"failure_reason"`
	Pages         *int32  `json:"pages"`
	UpdatedAt     *pyTime `json:"updated_at"`
	ChunkCount    int64   `json:"chunk_count"`
	EmbeddedCount int64   `json:"embedded_count"`
	HeroImage     bool    `json:"hero_image"`
}

type documentList struct {
	Configured bool              `json:"configured"`
	Documents  []documentSummary `json:"documents"`
}

// titleOf: 空白だけの題名は、題名なしと同じに扱う。
func titleOf(value *string) *string {
	if value == nil {
		return nil
	}
	if trimmed := strings.TrimSpace(*value); trimmed != "" {
		return &trimmed
	}
	return nil
}

// matchOrNil: 形が合うときだけ返す。それ以外は、未設定・未分類と同じ。
func matchOrNil(value *string, pattern *regexp.Regexp) *string {
	if value != nil && pattern.MatchString(*value) {
		return value
	}
	return nil
}

// revisionOf: 空白だけ・長すぎる（40 文字超）値は、版なしと同じに扱う。
func revisionOf(value *string) *string {
	if value == nil {
		return nil
	}
	trimmed := strings.TrimSpace(*value)
	if trimmed == "" || utf8.RuneCountInString(trimmed) > maxRevisionChars {
		return nil
	}
	return &trimmed
}

func summarize(row documentRow) documentSummary {
	var updated *pyTime
	if row.UpdatedAt != nil {
		t := pyTime(*row.UpdatedAt)
		updated = &t
	}
	return documentSummary{
		ID: row.ID, Source: row.Source, Title: titleOf(row.Title),
		Collection: matchOrNil(row.Collection, collectionPattern),
		Target:     matchOrNil(row.Target, classificationPattern), DocType: matchOrNil(row.DocType, classificationPattern),
		Revision: revisionOf(row.Revision), Status: row.Status, FailureReason: row.FailureReason, Pages: row.Pages,
		UpdatedAt: updated, ChunkCount: row.ChunkCount, EmbeddedCount: row.EmbeddedCount, HeroImage: row.HeroImage,
	}
}

// collectionOrEmpty は、グループの無い資料（NULL）を "" にする。グループ名は空にならない（DB の制約）ので、範囲に入ることはない。
func collectionOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func (s *server) documents(w http.ResponseWriter, r *http.Request) *httpError {
	ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
	defer cancel()
	acc, e := s.authorize(ctx, r)
	if e != nil {
		return e
	}
	if e := acc.requireAny(); e != nil {
		return e
	}
	if s.db == nil {
		writeJSON(w, http.StatusOK, documentList{Configured: false, Documents: []documentSummary{}}, noStore)
		return nil
	}
	rows, err := s.db.documents(ctx, acc.scope.sqlNames(), maxDocuments)
	if err != nil {
		// 失敗の種類だけを残す（接続先・SQL・本文は出さない）。Python と同じく、この 503 には no-store を付けない
		s.logf("資料を取得できませんでした (%s)", errorType(err))
		return newHTTPError(http.StatusServiceUnavailable, detailDocumentsUnavailable)
	}
	collections := make([]string, 0, len(rows))
	for _, row := range rows {
		collections = append(collections, collectionOrEmpty(row.Collection))
	}
	s.checker.logShadow(acc.scope, "資料の一覧", collections)
	// SQL で絞っているが、返す前にもう一度確かめる（範囲の外のグループの資料は返さない）
	out := make([]documentSummary, 0, len(rows))
	for i, row := range rows {
		if acc.scope.allows(collections[i]) {
			out = append(out, summarize(row))
		}
	}
	writeJSON(w, http.StatusOK, documentList{Configured: true, Documents: out}, noStore)
	return nil
}
