package main

// 資料 1 件の読み取り: GET /documents/{id}/chunks（本文）・/file（PDF）・/hero（ヒーロー画像）。app/api/documents.py の移植。
//   - 順序は Python（FastAPI）と同じ: 認証 → 利用資格 → 資料の範囲（503）→ {id} の検査（422）→ 見えるグループが 0 件なら 403 → DB（503）→ 404
//   - 見えないグループの資料は、存在しないのと同じ 404（本文・PDF・画像は返さない）
//   - 成功した /chunks だけ Cache-Control: no-store。/file と /hero は no-store と nosniff を付ける。エラー（HTTPException）には付けない
//   - original_pdf は、取り込み側が DB に記録した original_verified_at を先に見る。記録が無ければ、その場で確かめる（pdfstore.go の available）

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math/big"
	"net/http"
	"strings"
	"time"
	"unicode"
)

const (
	r2HandlerTimeout = 30 * time.Second // /file と /hero の全体（R2 の列挙・取得を含む）

	maxChunks     = 2000                  // 暴走した件数を返さないための上限（Python の _MAX_CHUNKS と同じ）
	maxDocumentID = "9223372036854775807" // bigint の上限。これを超える id は、DB に渡す前に 422 にする

	detailDocNotConfigured    = "資料のデータベースが設定されていません。"
	detailDocNotFound         = "指定された資料が見つかりません。"
	detailNoOriginal          = "PDF は利用できません。"
	detailOriginalMismatch    = "PDF が、DB に保存された資料と一致しません。"
	detailOriginalUnavailable = "PDF を取得できませんでした。しばらく待ってから再試行してください。"
	detailNoHero              = "この資料の画像はありません。"
	detailThreadsNotAvailable = "会話の一覧は、ログイン（AUTH_MODE=required）のときだけ使えます。"
	detailThreadsUnavailable  = "会話履歴を操作できませんでした。しばらく待ってから再試行してください。"
)

var heroMimeTypes = map[string]bool{"image/png": true, "image/jpeg": true, "image/webp": true}

// validationError は FastAPI の 422（{id} の検査の失敗）。detail は配列で、Python の応答と同じ形・同じ文。
type validationError struct {
	typ   string
	msg   string
	input string
	ctx   string // 空でなければ ctx（{"ge":1} など）の JSON
}

func (v validationError) body() []byte {
	var b strings.Builder
	b.WriteString(`{"detail":[{"type":`)
	b.WriteString(jsonString(v.typ))
	b.WriteString(`,"loc":["path","document_id"],"msg":`)
	b.WriteString(jsonString(v.msg))
	b.WriteString(`,"input":`)
	b.WriteString(jsonString(v.input))
	if v.ctx != "" {
		b.WriteString(`,"ctx":` + v.ctx)
	}
	b.WriteString(`}]}`)
	return []byte(b.String())
}

// parseDocumentID は pydantic v2 の int（lax）の読み方と同じ: 前後の空白は無視、符号は +/-、数字は 0-9 だけ（全角・アラビア数字は不可）、
// 桁の間の _ は可（1_000）、小数点以下がすべて 0 なら可（1.0）、指数・16 進は不可。範囲は 1 以上 bigint の上限以下。
func parseDocumentID(raw string) (int64, *validationError) {
	invalid := &validationError{typ: "int_parsing", msg: "Input should be a valid integer, unable to parse string as an integer", input: raw}
	text := strings.TrimFunc(raw, unicode.IsSpace)
	if text == "" {
		return 0, invalid
	}
	negative := false
	if text[0] == '+' || text[0] == '-' {
		negative = text[0] == '-'
		text = text[1:]
	}
	intPart, frac, hasFrac := strings.Cut(text, ".")
	if !digitsWithUnderscores(intPart) {
		return 0, invalid
	}
	if hasFrac && (frac == "" || strings.Trim(frac, "0") != "") {
		return 0, invalid
	}
	n, ok := new(big.Int).SetString(strings.ReplaceAll(intPart, "_", ""), 10)
	if !ok {
		return 0, invalid
	}
	if negative {
		n.Neg(n)
	}
	if n.Sign() < 1 {
		return 0, &validationError{typ: "greater_than_equal", msg: "Input should be greater than or equal to 1", input: raw, ctx: `{"ge":1}`}
	}
	if n.Cmp(mustBig(maxDocumentID)) > 0 {
		return 0, &validationError{typ: "less_than_equal", msg: "Input should be less than or equal to " + maxDocumentID, input: raw, ctx: `{"le":` + maxDocumentID + `}`}
	}
	return n.Int64(), nil
}

func mustBig(s string) *big.Int {
	n, _ := new(big.Int).SetString(s, 10)
	return n
}

// digitsWithUnderscores は、0-9 の並び。桁と桁の間に 1 個ずつの _ を許す（先頭・末尾・連続は不可）。
func digitsWithUnderscores(s string) bool {
	if s == "" {
		return false
	}
	previousUnderscore := true // 先頭の _ を拒否する
	for _, r := range s {
		switch {
		case r >= '0' && r <= '9':
			previousUnderscore = false
		case r == '_' && !previousUnderscore:
			previousUnderscore = true
		default:
			return false
		}
	}
	return !previousUnderscore
}

func jsonString(s string) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(s)
	return strings.TrimRight(buf.String(), "\n")
}

func writeValidation(w http.ResponseWriter, v *validationError) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnprocessableEntity)
	_, _ = w.Write(v.body())
}

type chunkItem struct {
	ChunkIndex     int64   `json:"chunk_index"`
	Page           *int32  `json:"page"`
	PageEnd        *int32  `json:"page_end"`
	PageLabel      *string `json:"page_label"`
	PageEndLabel   *string `json:"page_end_label"`
	Kind           *string `json:"kind"`
	Heading        *string `json:"heading"`
	Content        string  `json:"content"`
	Embedded       bool    `json:"embedded"`
	EmbeddingModel *string `json:"embedding_model"`
}

type documentDetail struct {
	Document    documentSummary `json:"document"`
	Chunks      []chunkItem     `json:"chunks"`
	OriginalPDF bool            `json:"original_pdf"`
}

// documentRoute は、/documents/{id}/chunks|file|hero の経路を分ける。当てはまらなければ ok=false。
func documentRoute(path string) (rawID, leaf string, ok bool) {
	rest, found := strings.CutPrefix(path, "/documents/")
	if !found {
		return "", "", false
	}
	rawID, leaf, found = strings.Cut(rest, "/")
	if !found || rawID == "" || (leaf != "chunks" && leaf != "file" && leaf != "hero") {
		return "", "", false
	}
	return rawID, leaf, true
}

// documentRequest は、3 つの経路に共通の前半: 認証・資格・範囲 → {id} の検査 → 見えるグループが 0 件なら 403 → DB が無ければ 503。
func (s *server) documentRequest(ctx context.Context, w http.ResponseWriter, r *http.Request, rawID string) (access, int64, bool, *httpError) {
	acc, e := s.authorize(ctx, r)
	if e != nil {
		return access{}, 0, false, e
	}
	id, bad := parseDocumentID(rawID)
	if bad != nil {
		writeValidation(w, bad)
		return access{}, 0, true, nil // 422 は書いた
	}
	if e := acc.requireAny(); e != nil {
		return access{}, 0, false, e
	}
	if s.db == nil {
		return access{}, 0, false, newHTTPError(http.StatusServiceUnavailable, detailDocNotConfigured)
	}
	return acc, id, false, nil
}

func (s *server) chunks(w http.ResponseWriter, r *http.Request, rawID string) *httpError {
	ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
	defer cancel()
	acc, id, written, e := s.documentRequest(ctx, w, r, rawID)
	if e != nil || written {
		return e
	}
	row, chunks, err := s.db.documentWithChunks(ctx, id, maxChunks)
	if err != nil {
		s.logf("資料を取得できませんでした (%s)", errorType(err))
		return newHTTPError(http.StatusServiceUnavailable, detailDocumentsUnavailable)
	}
	if row == nil {
		return newHTTPError(http.StatusNotFound, detailDocNotFound)
	}
	// 見えないグループの資料は、存在しないのと同じ 404（本文は返さない）
	collection := collectionOrEmpty(row.Collection)
	s.checker.logShadow(acc.scope, "資料の本文", []string{collection})
	if !acc.scope.allows(collection) {
		return newHTTPError(http.StatusNotFound, detailDocNotFound)
	}
	summary := summarize(row.documentRow)
	summary.HeroImage = false // 一覧の SQL にだけある項目（この経路では持たない）
	summary.ChunkCount = int64(len(chunks))
	summary.EmbeddedCount = 0
	items := make([]chunkItem, 0, len(chunks))
	for _, c := range chunks {
		if c.Embedded {
			summary.EmbeddedCount++
		}
		items = append(items, chunkItem{
			ChunkIndex: c.ChunkIndex, Page: c.Page, PageEnd: c.PageEnd, PageLabel: c.PageLabel, PageEndLabel: c.PageEndLabel,
			Kind: c.Kind, Heading: c.Heading, Content: c.Content, Embedded: c.Embedded, EmbeddingModel: c.EmbeddingModel,
		})
	}
	// PDF を返せるかだけが要る。取り込み側が確かめて DB に記録済みなら、R2 には聞かない。未記録なら、その場で確かめる
	original := row.OriginalVerified || s.pdfs.available(ctx, row.Source, row.ContentHash)
	writeJSON(w, http.StatusOK, documentDetail{Document: summary, Chunks: items, OriginalPDF: original}, noStore)
	return nil
}

// contentDisposition: filename は ASCII の代わりの名前、filename* が本来の名前（日本語のファイル名は RFC 5987 の形で渡す）。
func contentDisposition(id int64, source string) string {
	return fmt.Sprintf(`inline; filename="document-%d.pdf"; filename*=UTF-8''%s`, id, pythonQuote(source))
}

// pythonQuote は urllib.parse.quote(s, safe="") と同じ: 英数字と `_.-~` 以外を、UTF-8 のバイトごとに %XX（大文字）にする。
func pythonQuote(s string) string { return uriEncode(s, true) }

func (s *server) file(w http.ResponseWriter, r *http.Request, rawID string) *httpError {
	ctx, cancel := context.WithTimeout(r.Context(), r2HandlerTimeout)
	defer cancel()
	acc, id, written, e := s.documentRequest(ctx, w, r, rawID)
	if e != nil || written {
		return e
	}
	row, err := s.db.document(ctx, id)
	if err != nil {
		s.logf("資料を取得できませんでした (%s)", errorType(err))
		return newHTTPError(http.StatusServiceUnavailable, detailDocumentsUnavailable)
	}
	if row == nil {
		return newHTTPError(http.StatusNotFound, detailDocNotFound)
	}
	collection := collectionOrEmpty(row.Collection)
	s.checker.logShadow(acc.scope, "PDF", []string{collection})
	if !acc.scope.allows(collection) {
		return newHTTPError(http.StatusNotFound, detailDocNotFound)
	}
	found := s.pdfs.find(ctx, row.Source, row.ContentHash)
	if !found.ok || found.data == nil {
		switch found.reason {
		case originalMismatch:
			return newHTTPError(http.StatusConflict, detailOriginalMismatch)
		case originalUnavailableCode:
			return newHTTPError(http.StatusServiceUnavailable, detailOriginalUnavailable)
		}
		return newHTTPError(http.StatusNotFound, detailNoOriginal)
	}
	h := w.Header()
	h.Set("Content-Type", "application/pdf")
	h.Set("Cache-Control", "no-store")
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Content-Disposition", contentDisposition(id, row.Source))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(found.data)
	return nil
}

func (s *server) hero(w http.ResponseWriter, r *http.Request, rawID string) *httpError {
	ctx, cancel := context.WithTimeout(r.Context(), r2HandlerTimeout)
	defer cancel()
	acc, id, written, e := s.documentRequest(ctx, w, r, rawID)
	if e != nil || written {
		return e
	}
	row, err := s.db.hero(ctx, id)
	if err != nil {
		s.logf("資料を取得できませんでした (%s)", errorType(err))
		return newHTTPError(http.StatusServiceUnavailable, detailDocumentsUnavailable)
	}
	if row == nil {
		return newHTTPError(http.StatusNotFound, detailNoHero)
	}
	collection := collectionOrEmpty(row.Collection)
	s.checker.logShadow(acc.scope, "ヒーロー画像", []string{collection})
	if !acc.scope.allows(collection) {
		return newHTTPError(http.StatusNotFound, detailNoHero)
	}
	if row.MimeType == nil || !heroMimeTypes[*row.MimeType] || row.R2Key == nil {
		return newHTTPError(http.StatusNotFound, detailNoHero)
	}
	data, found, err := s.pdfs.hero(ctx, *row.R2Key)
	if err != nil {
		s.logf("画像を取得できませんでした (%s)", errorType(err))
		return newHTTPError(http.StatusServiceUnavailable, detailDocumentsUnavailable)
	}
	if !found {
		return newHTTPError(http.StatusNotFound, detailNoHero)
	}
	h := w.Header()
	h.Set("Content-Type", *row.MimeType)
	h.Set("Cache-Control", "no-store")
	h.Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
	return nil
}
