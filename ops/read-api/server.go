package main

// HTTP の入口。経路は GET /health /collections /targets /doc-types /documents /documents/{id}/chunks|file|hero /threads だけ（読み取り専用）。ほかのパスは 404、ほかのメソッドは 405
// （FastAPI と同じ {"detail": ...} の JSON）。応答の JSON は app/api/collections.py・classifications.py と同じ形・同じ順序の項目。
//
// ヘッダーの扱いも Python と同じ:
//   - 成功と、DB の失敗の 503 には Cache-Control: no-store を付ける
//   - 認証・資格・権限（403・401・503）のエラーには付けない（Python では、依存の中で起きるので、ハンドラーが付けたヘッダーが載らない）
//   - 401 には WWW-Authenticate: Bearer

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

const (
	detailUnauthorized     = "ログインが必要です。ログインし直してください。"
	detailAuthUnavailable  = "認証サービスに接続できませんでした。しばらく待ってから再試行してください。"
	detailCollectionsError = "資料グループを取得できませんでした。しばらく待ってから再試行してください。"
	detailClassifyError    = "資料の分類を取得できませんでした。しばらく待ってから再試行してください。"
	dbTimeout              = 10 * time.Second
	corsMaxAge             = "7200" // ブラウザーの上限（Chrome は 2 時間）。Python は 600 秒。事前確認（OPTIONS）の往復を減らす
)

type httpError struct {
	status int
	detail string
	code   string
	header map[string]string
}

func (e *httpError) Error() string { return e.detail }

func newHTTPError(status int, detail string) *httpError {
	return &httpError{status: status, detail: detail}
}

func errorType(err error) string { return fmt.Sprintf("%T", err) }

var noStore = map[string]string{"Cache-Control": "no-store"}

// writeJSON は、compact な JSON（FastAPI と同じ。非 ASCII もそのまま）を書く。
func writeJSON(w http.ResponseWriter, status int, body any, header map[string]string) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(body); err != nil {
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	out := bytes.TrimRight(buf.Bytes(), "\n")
	w.Header().Set("Content-Type", "application/json")
	for k, v := range header {
		w.Header().Set(k, v)
	}
	w.WriteHeader(status)
	_, _ = w.Write(out)
}

// writeError は {"detail": 固定文} を返す。利用資格がない 403 だけ、画面が資料の権限の 403 と区別できるよう、識別コードを添える（detail, code の順）。
func writeError(w http.ResponseWriter, e *httpError) {
	writeJSON(w, e.status, struct {
		Detail string `json:"detail"`
		Code   string `json:"code,omitempty"`
	}{e.detail, e.code}, e.header)
}

type collectionSummary struct {
	Name          string `json:"name"`
	Label         string `json:"label"`
	Description   string `json:"description"`
	SortOrder     *int32 `json:"sort_order"`
	Registered    bool   `json:"registered"`
	DocumentCount int64  `json:"document_count"`
	ReadyCount    int64  `json:"ready_count"`
}

type collectionList struct {
	Configured  bool                `json:"configured"`
	Collections []collectionSummary `json:"collections"`
}

type classificationSummary struct {
	Name          string `json:"name"`
	Label         string `json:"label"`
	Description   string `json:"description"`
	SortOrder     int32  `json:"sort_order"`
	DocumentCount int64  `json:"document_count"`
	ReadyCount    int64  `json:"ready_count"`
}

type targetList struct {
	Configured bool                    `json:"configured"`
	Targets    []classificationSummary `json:"targets"`
}

type docTypeList struct {
	Configured bool                    `json:"configured"`
	DocTypes   []classificationSummary `json:"doc_types"`
}

// labelOf は、表示名が空白だけなら name にする（Python の label.strip() if ... else name）。
func labelOf(label *string, name string) string {
	if label != nil {
		if trimmed := strings.TrimSpace(*label); trimmed != "" {
			return trimmed
		}
	}
	return name
}

type server struct {
	cfg      config
	verifier *tokenVerifier // AUTH_MODE=off のときは nil
	checker  *accessChecker
	db       store // nil = DB の接続が無い（configured:false を返す）
	pdfs     *pdfStore
	logf     func(string, ...any)
}

func (s *server) corsAllowed(origin string) bool {
	for _, o := range s.cfg.CORSOrigins {
		if o == origin {
			return true
		}
	}
	return false
}

// applyCORS は、許可したオリジン（CORS_ORIGINS と完全一致）にだけ、許可のヘッダーを付ける。* も Allow-Credentials も使わない。
func (s *server) applyCORS(w http.ResponseWriter, r *http.Request) (preflightHandled bool) {
	origin := r.Header.Get("Origin")
	if origin == "" {
		return false
	}
	w.Header().Add("Vary", "Origin")
	allowed := s.corsAllowed(origin)
	preflight := r.Method == http.MethodOptions && r.Header.Get("Access-Control-Request-Method") != ""
	if allowed {
		w.Header().Set("Access-Control-Allow-Origin", origin)
	}
	if !preflight {
		return false
	}
	if !allowed {
		http.Error(w, "Disallowed CORS origin", http.StatusBadRequest)
		return true
	}
	w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
	if requested := r.Header.Get("Access-Control-Request-Headers"); requested != "" {
		w.Header().Set("Access-Control-Allow-Headers", requested)
		w.Header().Add("Vary", "Access-Control-Request-Headers")
	}
	w.Header().Set("Access-Control-Max-Age", corsMaxAge)
	w.WriteHeader(http.StatusNoContent)
	return true
}

func bearerToken(r *http.Request) string {
	scheme, value, _ := strings.Cut(r.Header.Get("Authorization"), " ")
	token := strings.TrimSpace(value)
	if strings.EqualFold(scheme, "bearer") && token != "" {
		return token
	}
	return ""
}

// currentUser は、AUTH_MODE=off なら nil（匿名）、required なら検証済みの利用者 ID。
func (s *server) currentUser(ctx context.Context, r *http.Request) (*string, *httpError) {
	if s.verifier == nil {
		return nil, nil
	}
	unauthorized := newHTTPError(http.StatusUnauthorized, detailUnauthorized)
	unauthorized.header = map[string]string{"WWW-Authenticate": "Bearer"}
	token := bearerToken(r)
	if token == "" {
		return nil, unauthorized
	}
	sub, err := s.verifier.verify(ctx, token)
	if err != nil {
		var invalid invalidToken
		if errors.As(err, &invalid) {
			s.logf("認証に失敗しました (reason=%s)", invalid.reason)
			return nil, unauthorized
		}
		return nil, newHTTPError(http.StatusServiceUnavailable, detailAuthUnavailable)
	}
	return &sub, nil
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if s.applyCORS(w, r) {
		return
	}
	var handler func(http.ResponseWriter, *http.Request) *httpError
	switch r.URL.Path {
	case "/health":
		handler = s.health
	case "/collections":
		handler = s.collections
	case "/targets":
		handler = s.classifications(kindTargets)
	case "/doc-types":
		handler = s.classifications(kindDocTypes)
	case "/documents":
		handler = s.documents
	case "/threads":
		handler = s.threads
	default:
		rawID, leaf, ok := documentRoute(r.URL.Path)
		if !ok {
			writeError(w, newHTTPError(http.StatusNotFound, "Not Found"))
			return
		}
		handler = func(w http.ResponseWriter, r *http.Request) *httpError {
			switch leaf {
			case "chunks":
				return s.chunks(w, r, rawID)
			case "file":
				return s.file(w, r, rawID)
			}
			return s.hero(w, r, rawID)
		}
	}
	if r.Method != http.MethodGet {
		e := newHTTPError(http.StatusMethodNotAllowed, "Method Not Allowed")
		e.header = map[string]string{"Allow": "GET"}
		writeError(w, e)
		return
	}
	if e := handler(w, r); e != nil {
		writeError(w, e)
	}
}

func (s *server) health(w http.ResponseWriter, _ *http.Request) *httpError {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"}, nil)
	return nil
}

// authorize は、認証 → 利用資格 → 資料の範囲の順に決める。DB の接続が無い（モック・設定なし）ときも、モードが off なら通る。
func (s *server) authorize(ctx context.Context, r *http.Request) (access, *httpError) {
	user, e := s.currentUser(ctx, r)
	if e != nil {
		return access{}, e
	}
	return s.checker.request(ctx, user, r.URL.Path)
}

func (s *server) collections(w http.ResponseWriter, r *http.Request) *httpError {
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
		writeJSON(w, http.StatusOK, collectionList{Configured: false, Collections: []collectionSummary{}}, noStore)
		return nil
	}
	rows, err := s.db.collections(ctx, acc.scope.sqlNames(), maxListItems)
	if err != nil {
		s.logf("資料グループを取得できませんでした (%s)", errorType(err))
		failure := newHTTPError(http.StatusServiceUnavailable, detailCollectionsError)
		failure.header = noStore
		return failure
	}
	names := make([]string, 0, len(rows))
	for _, row := range rows {
		names = append(names, row.Name)
	}
	s.checker.logShadow(acc.scope, "資料グループの一覧", names)
	// SQL で絞っているが、返す前にもう一度確かめる（範囲の外のグループは返さない）
	out := make([]collectionSummary, 0, len(rows))
	for _, row := range rows {
		if !acc.scope.allows(row.Name) {
			continue
		}
		description := ""
		if row.Description != nil {
			description = *row.Description
		}
		out = append(out, collectionSummary{
			Name: row.Name, Label: labelOf(row.Label, row.Name), Description: description, SortOrder: row.SortOrder,
			Registered: row.Registered, DocumentCount: row.DocumentCount, ReadyCount: row.ReadyCount,
		})
	}
	writeJSON(w, http.StatusOK, collectionList{Configured: true, Collections: out}, noStore)
	return nil
}

func (s *server) classifications(kind classificationKind) func(http.ResponseWriter, *http.Request) *httpError {
	return func(w http.ResponseWriter, r *http.Request) *httpError {
		ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
		defer cancel()
		acc, e := s.authorize(ctx, r)
		if e != nil {
			return e
		}
		if e := acc.requireAny(); e != nil {
			return e
		}
		items := []classificationSummary{}
		configured := s.db != nil
		if configured {
			rows, err := s.db.classifications(ctx, kind, acc.scope.sqlNames(), maxListItems)
			if err != nil {
				s.logf("資料の分類を取得できませんでした (%s)", errorType(err))
				failure := newHTTPError(http.StatusServiceUnavailable, detailClassifyError)
				failure.header = noStore
				return failure
			}
			for _, row := range rows {
				items = append(items, classificationSummary{
					Name: row.Name, Label: labelOf(&row.Label, row.Name), Description: row.Description,
					SortOrder: row.SortOrder, DocumentCount: row.DocumentCount, ReadyCount: row.ReadyCount,
				})
			}
		}
		if kind == kindDocTypes {
			writeJSON(w, http.StatusOK, docTypeList{Configured: configured, DocTypes: items}, noStore)
		} else {
			writeJSON(w, http.StatusOK, targetList{Configured: configured, Targets: items}, noStore)
		}
		return nil
	}
}
