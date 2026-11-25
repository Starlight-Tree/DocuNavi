package main

// GET /threads（会話の一覧。app/api/threads.py の list_threads の移植）。本人の会話だけを、新しい順（最大 50 件）に返す。
//   - AUTH_MODE=required のときだけ。匿名（off）の会話には持ち主がなく、一覧にすると他人の会話が見えるので、404（認証 → 利用資格の順に確かめたあと）
//   - 資料の範囲（ACCESS_MODE）は見ない。見えるグループが 0 件でも 403 にしない（Python の current_member と同じ）
//   - 題名は、保存時に checkpoints.metadata の title へ入れた値（app/thread_title.py）。Go はチェックポイント本体（msgpack）を復号しない。
//     title が無い古い会話は「（題名なし）」として返す。**画面を Go へ向ける前に、python -m app.history_admin --backfill-titles で埋める**
//   - 会話の発言（/threads/{id}/messages）と削除（DELETE）は、Python の API のまま（この経路では 404）

import (
	"context"
	"net/http"
	"time"
)

const (
	maxThreads = 50 // Python の MAX_THREADS と同じ
	untitled   = "（題名なし）"
)

type threadSummary struct {
	ThreadID  string  `json:"thread_id"`
	Title     string  `json:"title"`
	CreatedAt *pyTime `json:"created_at"`
	UpdatedAt *pyTime `json:"updated_at"`
}

type threadList struct {
	Threads []threadSummary `json:"threads"`
}

func toPyTime(t *time.Time) *pyTime {
	if t == nil {
		return nil
	}
	p := pyTime(*t)
	return &p
}

func (s *server) threads(w http.ResponseWriter, r *http.Request) *httpError {
	ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
	defer cancel()
	user, e := s.currentUser(ctx, r)
	if e != nil {
		return e
	}
	if e := s.checker.enforceMembership(ctx, user, r.URL.Path); e != nil {
		return e
	}
	if user == nil {
		return newHTTPError(http.StatusNotFound, detailThreadsNotAvailable)
	}
	out := []threadSummary{}
	if s.db != nil {
		prefix := *user + ":"
		rows, err := s.db.threads(ctx, prefix, maxThreads)
		if err != nil {
			s.logf("会話履歴を操作できませんでした (%s)", errorType(err))
			return newHTTPError(http.StatusServiceUnavailable, detailThreadsUnavailable)
		}
		for _, row := range rows {
			title := untitled
			if row.Title != nil {
				title = *row.Title
			}
			out = append(out, threadSummary{
				ThreadID: row.ThreadID[len(prefix):], Title: title, CreatedAt: toPyTime(row.CreatedAt), UpdatedAt: toPyTime(row.UpdatedAt),
			})
		}
	}
	writeJSON(w, http.StatusOK, threadList{Threads: out}, nil)
	return nil
}
