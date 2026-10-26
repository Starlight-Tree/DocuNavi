// SSE 中継（docs/target-architecture.md の「Go の SSE 中継」）。
//
// ブラウザー側の接続を受け、上流（FastAPI）の /chat/stream だけを中継する。標準ライブラリだけで作る。
//   - 受けるのは POST /chat/stream と、その CORS の OPTIONS だけ。ほかのパスは 404（他のエンドポイントは、これまでどおり FastAPI へ直接）
//   - 上流の本文は届いた分だけすぐ流す（FlushInterval = -1）。X-Accel-Buffering: no を付ける（途中の中継に溜めさせない）
//   - 本文が止まっている間は、一定の間隔で SSE のコメント（": ping"）を送る。イベントの途中には挟まない
//   - Authorization などのヘッダーはそのまま渡す（検証は上流が行う。ここでは見ない）
//   - クライアントが切れたら、リクエストの context が取り消され、上流の接続も閉じる
//     （HTTP/1.1 は接続を閉じる、h2c は RST_STREAM を送る）
//   - 上流へ h2c（暗号化なしの HTTP/2）で話すことも選べる（-upstream-h2c）。待ち受けも h1 と h2c の両方を受ける（Cloud Run の --use-http2 用）
//   - Server.WriteTimeout は付けない（長い SSE を切らないため）。念のため、ハンドラーの書き込み期限も外す
//   - 同時に処理する /chat/stream の数に上限を持つ（-max-streams / 環境変数 MAX_CONCURRENT_STREAMS。既定 100、0 で無制限）。
//     超えた要求は待たせず、すぐ 503（固定の日本語の detail）で断る。切断・エラー・正常終了のどれでも枠は必ず戻る。
//     この 503 に CORS を付けるのは、許可リスト（CORS_ORIGINS。既定は本番の画面とローカル）にあるオリジンだけ
package main

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	streamPath       = "/chat/stream"
	defaultHeartbeat = 25 * time.Second
	// 同時に処理する /chat/stream の既定の上限。Cloud Run の containerConcurrency は、これより少し大きくする
	// （同じ値だと、上限を超えた要求が中継に届く前に Cloud Run の側で待たされ、ここでの拒否が起きない）
	defaultMaxStreams = 100
	maxStreamsEnv     = "MAX_CONCURRENT_STREAMS"
	// 上限で断る応答に CORS ヘッダーを付けてよい画面のオリジン（カンマ区切り。API の CORS_ORIGINS と同じ名前・同じ形）
	corsOriginsEnv = "CORS_ORIGINS"
	// 上限で断ったときの待ち時間の目安（秒）。Retry-After に入れる
	rejectRetryAfter = "5"
	// 画面は 503 の detail が日本語ならそのまま見せる（frontend/src/api/chat.ts の SERVER_DETAIL_STATUSES）
	rejectBody = `{"detail":"ただいま混み合っています。しばらく待ってから、もう一度お試しください。"}`
)

var heartbeatComment = []byte(": ping\n\n")

// 環境変数 CORS_ORIGINS が未設定のときに、断る応答の CORS を許すオリジン（本番の画面と、ローカルの開発）
var defaultAllowedOrigins = []string{
	"https://docunavi-eosin.vercel.app",
	"http://localhost:3000",
	"http://127.0.0.1:3000",
}

// streamLimiter は、処理中の /chat/stream の数を数え、上限を超えた要求をすぐ断る。
// 数えるのは原子的な加算と比較交換だけで、複数の goroutine から同時に呼んでも、上限を超えて受け付けない。
type streamLimiter struct {
	limit    int64 // 0 以下なら無制限（数えるだけ）
	active   atomic.Int64
	rejected atomic.Int64
	logf     func(format string, args ...any)
	// 断る応答に Access-Control-Allow-Origin を返してよいオリジン。ここに無いオリジンには CORS ヘッダーを返さない
	allowedOrigins map[string]struct{}
}

func newStreamLimiter(limit int, logf func(format string, args ...any)) *streamLimiter {
	l := &streamLimiter{limit: int64(limit), logf: logf}
	l.setAllowedOrigins(defaultAllowedOrigins)
	return l
}

func (l *streamLimiter) setAllowedOrigins(origins []string) {
	l.allowedOrigins = make(map[string]struct{}, len(origins))
	for _, o := range origins {
		l.allowedOrigins[o] = struct{}{}
	}
}

// tryAcquire は、枠が取れたら true。待たない。取れたら、呼び出し側が必ず release する。
func (l *streamLimiter) tryAcquire() bool {
	for {
		cur := l.active.Load()
		if l.limit > 0 && cur >= l.limit {
			return false
		}
		if l.active.CompareAndSwap(cur, cur+1) {
			return true
		}
	}
}

func (l *streamLimiter) release() { l.active.Add(-1) }

// reject は、上限で断る応答を書く。本文は固定（リクエストの内容は読まない・載せない）。
// ブラウザーが 503 を読めるよう、許可リストにあるオリジンだけ、その値を Access-Control-Allow-Origin に返す。
// 許可外のオリジンには CORS ヘッダーを返さない。値は常に特定のオリジンで、"*" は使わない。
// Access-Control-Allow-Credentials は付けない（画面は Authorization ヘッダーで認証し、Cookie を使わない）。
func (l *streamLimiter) reject(w http.ResponseWriter, r *http.Request) {
	total := l.rejected.Add(1)
	l.logf("stream_limit_rejected active_streams=%d limit=%d rejected_total=%d", l.active.Load(), l.limit, total)
	h := w.Header()
	h.Add("Vary", "Origin")
	if origin := r.Header.Get("Origin"); origin != "" {
		if _, ok := l.allowedOrigins[origin]; ok {
			h.Set("Access-Control-Allow-Origin", origin)
		}
	}
	h.Set("Content-Type", "application/json")
	h.Set("Cache-Control", "no-store")
	h.Set("Retry-After", rejectRetryAfter)
	w.WriteHeader(http.StatusServiceUnavailable)
	_, _ = w.Write([]byte(rejectBody))
}

// maxStreamsFromEnv は、環境変数 MAX_CONCURRENT_STREAMS の値を返す。未設定なら既定。正の整数か 0（無制限）以外はエラー。
func maxStreamsFromEnv(get func(string) string) (int, error) {
	raw := strings.TrimSpace(get(maxStreamsEnv))
	if raw == "" {
		return defaultMaxStreams, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("%s は 0 以上の整数にしてください（0 は無制限）", maxStreamsEnv)
	}
	return n, nil
}

// corsOriginsFromEnv は、環境変数 CORS_ORIGINS（カンマ区切り）の許可リストを返す。未設定・空なら既定。
// "*" と、スキームの無い値、パスを含む値はエラー（任意のオリジンを許す設定を入れさせない）。
func corsOriginsFromEnv(get func(string) string) ([]string, error) {
	raw := strings.TrimSpace(get(corsOriginsEnv))
	if raw == "" {
		return defaultAllowedOrigins, nil
	}
	var origins []string
	for _, part := range strings.Split(raw, ",") {
		o := strings.TrimRight(strings.TrimSpace(part), "/")
		if o == "" {
			continue
		}
		u, err := url.Parse(o)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.Path != "" || u.RawQuery != "" {
			return nil, fmt.Errorf("%s には https://example.com のようなオリジンだけを書いてください（\"*\" は使えません）", corsOriginsEnv)
		}
		origins = append(origins, o)
	}
	if len(origins) == 0 {
		return defaultAllowedOrigins, nil
	}
	return origins, nil
}

// newRelay は、upstream の /chat/stream へ中継するハンドラーを返す。h2c が true なら、上流へは h2c で話す。
// heartbeat が 0 以下なら、ping は送らない。limiter は POST の同時数を制限する（OPTIONS の予備リクエストは数えない）。
func newRelay(upstream *url.URL, h2c bool, heartbeat time.Duration, limiter *streamLimiter) http.Handler {
	logger := log.New(os.Stderr, "relay: ", log.LstdFlags)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConnsPerHost = 1024
	if h2c {
		transport.Protocols = new(http.Protocols)
		transport.Protocols.SetUnencryptedHTTP2(true)
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			// Host は上流の URL のものを使う（SetURL が Out.Host を空にする）。Cloud Run は Host で配送先を決めるので、
			// 中継の Host のまま渡すと、上流の Cloud Run のサービスに届かず 404 になる
			r.SetURL(upstream)
		},
		Transport:     transport,
		FlushInterval: -1,
		ErrorLog:      logger,
		ModifyResponse: func(resp *http.Response) error {
			if isEventStream(resp.Header) {
				resp.Header.Set("X-Accel-Buffering", "no")
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			if errors.Is(err, context.Canceled) {
				return // クライアントが切れた。返す相手がいないので、ログにも出さない
			}
			logger.Printf("上流への中継に失敗しました: %v", err)
			w.WriteHeader(http.StatusBadGateway)
		},
	}

	handle := func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			if !limiter.tryAcquire() {
				limiter.reject(w, r)
				return
			}
			defer limiter.release() // 切断・エラー・正常終了のどれでも、ハンドラーが戻るときに枠を返す
		}
		rc := http.NewResponseController(w)
		// HTTP/1.1 のサーバーは、リクエスト本文を読み切る前に応答を書き始めると、本文を閉じてしまう。上流が本文を読み終えてすぐ応答すると、
		// 中継から上流へ本文を送っている最中の接続が切られ、応答の途中で終わることがある（約 5〜10%。単体テストで再現）。同時に読み書きできるようにする
		_ = rc.EnableFullDuplex()
		// 長い SSE を、サーバー全体の期限で切らない（Server.WriteTimeout は付けていないが、付けられても外れるように）
		_ = rc.SetWriteDeadline(time.Time{})
		hw := newHeartbeatWriter(w, heartbeat)
		defer hw.close()
		proxy.ServeHTTP(hw, r)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST "+streamPath, handle)
	mux.HandleFunc("OPTIONS "+streamPath, handle)
	return mux
}

func isEventStream(h http.Header) bool {
	return strings.HasPrefix(strings.ToLower(h.Get("Content-Type")), "text/event-stream")
}

// heartbeatWriter は、本文が interval の間止まったら、SSE のコメントを書く ResponseWriter。
// 書き込みは 1 つの mutex で直列にし、イベントの区切り（空行）の直後にだけ ping を書く（イベントの途中に挟まない）。
type heartbeatWriter struct {
	w        http.ResponseWriter
	interval time.Duration

	mu        sync.Mutex
	started   bool // WriteHeader 済み
	closed    bool // ハンドラーが戻った、または書き込みに失敗した。以後は書かない
	tail      []byte
	lastWrite time.Time
	stop      chan struct{}
}

func newHeartbeatWriter(w http.ResponseWriter, interval time.Duration) *heartbeatWriter {
	return &heartbeatWriter{w: w, interval: interval, stop: make(chan struct{})}
}

func (h *heartbeatWriter) Header() http.Header { return h.w.Header() }

// Unwrap は、http.ResponseController が元の ResponseWriter の機能（期限の設定など）へ届くようにする。
func (h *heartbeatWriter) Unwrap() http.ResponseWriter { return h.w }

func (h *heartbeatWriter) WriteHeader(code int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.writeHeaderLocked(code)
}

func (h *heartbeatWriter) writeHeaderLocked(code int) {
	if h.started {
		return
	}
	h.started = true
	// 1xx（情報）は本文の始まりではない。ここでは最終のステータスだけを扱う
	sse := code == http.StatusOK && isEventStream(h.w.Header())
	h.w.WriteHeader(code)
	h.lastWrite = time.Now()
	if sse && h.interval > 0 && !h.closed {
		go h.pingLoop()
	}
}

func (h *heartbeatWriter) Write(p []byte) (int, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.writeHeaderLocked(http.StatusOK)
	n, err := h.w.Write(p)
	if n > 0 {
		h.tail = append(h.tail, p[:n]...)
		if len(h.tail) > 4 {
			h.tail = h.tail[len(h.tail)-4:]
		}
		h.lastWrite = time.Now()
	}
	return n, err
}

func (h *heartbeatWriter) Flush() {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return
	}
	_ = http.NewResponseController(h.w).Flush()
}

// atBoundaryLocked は、いま ping を挟んでよい（直前までのイベントが空行で終わっている）かを返す。
func (h *heartbeatWriter) atBoundaryLocked() bool {
	return len(h.tail) == 0 || bytes.HasSuffix(h.tail, []byte("\n\n")) || bytes.HasSuffix(h.tail, []byte("\r\n\r\n"))
}

func (h *heartbeatWriter) pingLoop() {
	retry := h.interval / 10
	if retry < 5*time.Millisecond {
		retry = 5 * time.Millisecond
	}
	timer := time.NewTimer(h.interval)
	defer timer.Stop()
	for {
		select {
		case <-h.stop:
			return
		case <-timer.C:
		}
		h.mu.Lock()
		if h.closed {
			h.mu.Unlock()
			return
		}
		wait := h.interval - time.Since(h.lastWrite)
		if wait <= 0 {
			if h.atBoundaryLocked() {
				if _, err := h.w.Write(heartbeatComment); err != nil {
					h.closed = true
					h.mu.Unlock()
					return
				}
				_ = http.NewResponseController(h.w).Flush()
				h.lastWrite = time.Now()
				wait = h.interval
			} else {
				wait = retry // イベントの途中。区切りまで少し待つ
			}
		}
		h.mu.Unlock()
		timer.Reset(wait)
	}
}

// close は、ハンドラーが戻る前に呼ぶ。以後は ping を書かない。
func (h *heartbeatWriter) close() {
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.closed {
		h.closed = true
		close(h.stop)
	}
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8200", "待ち受けるアドレス")
	upstreamRaw := flag.String("upstream", "http://127.0.0.1:8100", "中継先（FastAPI）の URL")
	h2c := flag.Bool("upstream-h2c", false, "上流へ h2c（暗号化なしの HTTP/2）で話す")
	heartbeat := flag.Duration("heartbeat", defaultHeartbeat, "本文が止まっている間に ': ping' を送る間隔（0 で送らない）")
	maxStreams := flag.Int("max-streams", -1, "同時に処理する /chat/stream の上限（0 で無制限）。未指定なら環境変数 "+maxStreamsEnv+"、それも無ければ既定")
	flag.Parse()
	if *maxStreams < 0 {
		n, err := maxStreamsFromEnv(os.Getenv)
		if err != nil {
			log.Fatal(err)
		}
		*maxStreams = n
	}

	origins, err := corsOriginsFromEnv(os.Getenv)
	if err != nil {
		log.Fatal(err)
	}
	limiter := newStreamLimiter(*maxStreams, log.New(os.Stderr, "relay: ", log.LstdFlags).Printf)
	limiter.setAllowedOrigins(origins)

	upstream, err := url.Parse(*upstreamRaw)
	if err != nil || upstream.Scheme == "" || upstream.Host == "" {
		log.Fatalf("上流の URL が正しくありません: %q", *upstreamRaw)
	}
	server := &http.Server{
		Addr:              *listen,
		Handler:           newRelay(upstream, *h2c, *heartbeat, limiter),
		ReadHeaderTimeout: 10 * time.Second,
		// WriteTimeout は付けない（SSE を途中で切るため）
	}
	// 待ち受けは HTTP/1.1 と h2c の両方（Cloud Run に --use-http2 を付けたとき、コンテナまで HTTP/2 で届く）
	server.Protocols = new(http.Protocols)
	server.Protocols.SetHTTP1(true)
	server.Protocols.SetUnencryptedHTTP2(true)
	log.Printf("中継を開始します: %s -> %s (上流 h2c=%v, heartbeat=%v, max_streams=%d, cors_origins=%d件)", *listen, upstream, *h2c, *heartbeat, *maxStreams, len(origins))
	log.Fatal(server.ListenAndServe())
}
