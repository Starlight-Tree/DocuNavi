// 疑似の上流（負荷試験用）。本物の API（FastAPI の POST /chat/stream）の代わりに、固定の SSE を流す。
// 標準ライブラリだけ。LLM・DB・外部通信は使わない（課金なし）。docs/go-architecture-plan.md の「段階 1 の試験の設計」。
//
// 受けるのは POST /chat/stream（本文は読み捨て）と、GET /stats、GET /health だけ。
// 流れ方は、クエリ ?scenario=… と、数値のクエリで決める（未指定は -default-scenario）:
//
//	normal      start → data-status → text-start → text-delta … → text-end → message-metadata → finish → [DONE]（正常終了）
//	error       途中で data-failure + error + [DONE]（ヘッダー送信後の失敗。本物の API の error チャンクと同じ形）
//	abort       途中で接続を切る（上流が落ちた・RST）。[DONE] は送らない
//	hang        start を送った後、何も流さない（ping だけ）。クライアントが切るまで、または duration まで（タイムアウトの再現）
//	slow-start  first_byte 秒たってからヘッダーを返す（本物は最初のチャンクまで 22〜31 秒かかったことがある）。その後は normal
//	http-error  ストリームを始めず、HTTP の status（既定 500）と固定の JSON の detail を返す
//
// 数値のクエリ: duration=全体の秒数（既定 30）、interval=text-delta の間隔（既定 1）、first_byte=slow-start の待ち（既定 20）、
// ping=無通信のときの ": ping" の間隔（既定 10。0 で送らない）、after=error/abort までの text-delta の数（既定 3）、status=http-error の値。
// 範囲を外れた値・知らない scenario は 400。待ち時間の上限は -max-duration（既定 600 秒）。
//
// クライアントが切れたら、リクエストの context が取り消されて止まる。数えたものは GET /stats で見る
// （active=いま流している数、started、completed、errored、aborted、client_disconnected、rejected）。
// 待ち受けは HTTP/1.1 と h2c の両方（中継の -upstream-h2c と、Cloud Run の --use-http2 に合わせる）。
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"sync/atomic"
	"time"
)

const (
	streamPath         = "/chat/stream"
	defaultScenario    = "normal"
	defaultDuration    = 30 * time.Second
	defaultInterval    = time.Second
	defaultFirstByte   = 20 * time.Second
	defaultPing        = 10 * time.Second
	defaultAfter       = 3
	defaultHTTPStatus  = http.StatusInternalServerError
	maxAfter           = 1000
	failureText        = "疑似の上流の失敗です（試験用）。"
	httpErrorBody      = `{"detail":"疑似の上流が失敗を返しました（試験用）。"}`
	messageID          = "msg_fake"
	maxRequestBodyRead = 1 << 20 // 本文は読み捨てる。読むのは 1MB まで
)

type scenario string

const (
	scenarioNormal    scenario = "normal"
	scenarioError     scenario = "error"
	scenarioAbort     scenario = "abort"
	scenarioHang      scenario = "hang"
	scenarioSlowStart scenario = "slow-start"
	scenarioHTTPError scenario = "http-error"
)

var scenarios = map[scenario]bool{
	scenarioNormal: true, scenarioError: true, scenarioAbort: true,
	scenarioHang: true, scenarioSlowStart: true, scenarioHTTPError: true,
}

// params は、1 回のリクエストの再現のしかた。
type params struct {
	scenario  scenario
	duration  time.Duration
	interval  time.Duration
	firstByte time.Duration
	ping      time.Duration
	after     int
	status    int
}

type stats struct {
	active, started, completed, errored, aborted, clientDisconnected, rejected atomic.Int64
}

type fake struct {
	defaultScenario scenario
	maxDuration     time.Duration
	stats           stats
}

func newFake(def scenario, maxDuration time.Duration) *fake {
	return &fake{defaultScenario: def, maxDuration: maxDuration}
}

func (f *fake) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST "+streamPath, f.serveStream)
	mux.HandleFunc("GET /stats", f.serveStats)
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	return mux
}

func (f *fake) serveStats(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]int64{
		"active":              f.stats.active.Load(),
		"started":             f.stats.started.Load(),
		"completed":           f.stats.completed.Load(),
		"errored":             f.stats.errored.Load(),
		"aborted":             f.stats.aborted.Load(),
		"client_disconnected": f.stats.clientDisconnected.Load(),
		"rejected":            f.stats.rejected.Load(),
	})
}

func (f *fake) parse(r *http.Request) (params, error) {
	q := r.URL.Query()
	p := params{
		scenario: f.defaultScenario, duration: defaultDuration, interval: defaultInterval,
		firstByte: defaultFirstByte, ping: defaultPing, after: defaultAfter, status: defaultHTTPStatus,
	}
	if v := q.Get("scenario"); v != "" {
		p.scenario = scenario(v)
	}
	if !scenarios[p.scenario] {
		return p, fmt.Errorf("scenario が正しくありません")
	}
	secs := func(key string, dst *time.Duration, min, max time.Duration) error {
		v := q.Get(key)
		if v == "" {
			return nil
		}
		n, err := strconv.ParseFloat(v, 64)
		if err != nil || n != n { // NaN
			return fmt.Errorf("%s が正しくありません", key)
		}
		d := time.Duration(n * float64(time.Second))
		if n < 0 || n > max.Seconds() || d < min {
			return fmt.Errorf("%s が範囲外です", key)
		}
		*dst = d
		return nil
	}
	for _, c := range []struct {
		key      string
		dst      *time.Duration
		min, max time.Duration
	}{
		{"duration", &p.duration, 0, f.maxDuration},
		{"interval", &p.interval, time.Millisecond, f.maxDuration},
		{"first_byte", &p.firstByte, 0, f.maxDuration},
		{"ping", &p.ping, 0, f.maxDuration},
	} {
		if err := secs(c.key, c.dst, c.min, c.max); err != nil {
			return p, err
		}
	}
	if v := q.Get("after"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 || n > maxAfter {
			return p, fmt.Errorf("after が範囲外です")
		}
		p.after = n
	}
	if v := q.Get("status"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 400 || n > 599 {
			return p, fmt.Errorf("status は 400〜599 にしてください")
		}
		p.status = n
	}
	return p, nil
}

func (f *fake) serveStream(w http.ResponseWriter, r *http.Request) {
	p, err := f.parse(r)
	if err != nil {
		f.stats.rejected.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": err.Error()})
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(r.Body, maxRequestBodyRead))

	f.stats.started.Add(1)
	f.stats.active.Add(1)
	defer f.stats.active.Add(-1)

	if p.scenario == scenarioHTTPError {
		f.stats.errored.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(p.status)
		_, _ = w.Write([]byte(httpErrorBody))
		return
	}

	ctx := r.Context()
	if p.scenario == scenarioSlowStart && !sleep(ctx, p.firstByte) {
		f.stats.clientDisconnected.Add(1)
		return
	}

	rc := http.NewResponseController(w)
	_ = rc.EnableFullDuplex()
	_ = rc.SetWriteDeadline(time.Time{})
	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache, no-transform")
	h.Set("X-Accel-Buffering", "no")
	h.Set("x-vercel-ai-ui-message-stream", "v1")
	w.WriteHeader(http.StatusOK)

	send := func(s string) bool {
		if _, err := io.WriteString(w, s); err != nil {
			return false
		}
		return rc.Flush() == nil
	}
	chunk := func(v map[string]any) bool {
		b, _ := json.Marshal(v)
		return send("data: " + string(b) + "\n\n")
	}
	gone := func() { f.stats.clientDisconnected.Add(1) }

	if !chunk(map[string]any{"type": "start", "messageId": messageID, "messageMetadata": map[string]any{"thread_id": "fake", "mode": "fake"}}) ||
		!chunk(map[string]any{"type": "data-status", "data": map[string]any{"phase": "analyzing"}, "transient": true}) {
		gone()
		return
	}

	if p.scenario == scenarioHang {
		if f.idle(ctx, p, send, p.duration) {
			f.stats.completed.Add(1)
		} else {
			gone()
		}
		return
	}

	if !chunk(map[string]any{"type": "text-start", "id": "t0"}) {
		gone()
		return
	}
	deadline := time.Now().Add(p.duration)
	for i := 0; ; i++ {
		switch {
		case p.scenario == scenarioError && i == p.after:
			f.stats.errored.Add(1)
			chunk(map[string]any{"type": "data-failure", "data": map[string]any{"status": 502}, "transient": true})
			chunk(map[string]any{"type": "error", "errorText": failureText})
			send("data: [DONE]\n\n")
			return
		case p.scenario == scenarioAbort && i == p.after:
			f.stats.aborted.Add(1)
			panic(http.ErrAbortHandler) // 接続を切る（h1 は閉じ、h2 は RST_STREAM）。ログにも出ない
		}
		if !time.Now().Before(deadline) {
			break
		}
		if !chunk(map[string]any{"type": "text-delta", "id": "t0", "delta": fmt.Sprintf("断片%d ", i)}) {
			gone()
			return
		}
		// 間隔の途中でも、期限と切断で止める。無通信の間は ping を挟む
		wait := min(p.interval, time.Until(deadline))
		if !f.idle(ctx, p, send, wait) {
			gone()
			return
		}
	}
	if !chunk(map[string]any{"type": "text-end", "id": "t0"}) ||
		!chunk(map[string]any{"type": "message-metadata", "messageMetadata": map[string]any{"context": "", "sources": []any{}}}) ||
		!chunk(map[string]any{"type": "finish"}) ||
		!send("data: [DONE]\n\n") {
		gone()
		return
	}
	f.stats.completed.Add(1)
}

// idle は d の間待つ。p.ping ごとに ": ping" を送る。最後まで待てたら true、切断で止まったら false。
func (f *fake) idle(ctx context.Context, p params, send func(string) bool, d time.Duration) bool {
	end := time.Now().Add(d)
	for {
		left := time.Until(end)
		if left <= 0 {
			return ctx.Err() == nil
		}
		step := left
		if p.ping > 0 && p.ping < step {
			step = p.ping
		}
		if !sleep(ctx, step) {
			return false
		}
		if step < left { // ping の番
			if !send(": ping\n\n") {
				return false
			}
		}
	}
}

// sleep は d 待つ。ctx が先に取り消されたら false。
func sleep(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8100", "待ち受けるアドレス（Cloud Run では -listen=:8080）")
	def := flag.String("default-scenario", defaultScenario, "scenario を指定しないリクエストの流れ方")
	maxDur := flag.Duration("max-duration", 600*time.Second, "duration などの上限")
	flag.Parse()
	if !scenarios[scenario(*def)] {
		log.Fatalf("default-scenario が正しくありません: %q", *def)
	}
	srv := newFake(scenario(*def), *maxDur)
	server := &http.Server{Addr: *listen, Handler: srv.handler(), ReadHeaderTimeout: 10 * time.Second}
	server.Protocols = new(http.Protocols)
	server.Protocols.SetHTTP1(true)
	server.Protocols.SetUnencryptedHTTP2(true)
	log.Printf("疑似の上流を開始します: %s (default=%s, max-duration=%v)", *listen, *def, *maxDur)
	log.Fatal(server.ListenAndServe())
}
