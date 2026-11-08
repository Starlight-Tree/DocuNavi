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
//   - リクエスト本文の上限（SSE_MAX_BODY_BYTES。既定 1 MiB、0 で無制限）。Content-Length が超えていれば上流へ送らず 413。
//     chunked などで読み進めて超えたときも 413。上流へ送るのは、上限内で読み切った本文だけ（読み切るまで上流には何も送らない）
//   - 新しい接続（POST）の開始のレート制限（SSE_RATE_LIMIT_PER_MIN 既定 60、SSE_RATE_LIMIT_BURST 既定 10。PER_MIN=0 で無効）。
//     超えたら 429。判定キーはクライアント IP だけ（既定は接続元。SSE_RATE_LIMIT_TRUST_XFF=true のときだけ X-Forwarded-For の最後の値）。Authorization は見ない。
//     送信済みの接続の ping・本文には適用しない（接続の開始だけを数える）
//   - 下流への書き込みは 1 回ごとに期限（defaultWriteStall）を付ける。読まない・遅すぎる下流は、期限で切る（上流も取り消される）。
//     中継が溜めるのは、ReverseProxy の 1 回分のコピーバッファ（32 KiB）だけで、下流が詰まれば上流の読み取りも止まる（背圧）
package main

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"net"
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

	// リクエスト本文の上限（バイト）。質問は短い JSON なので 1 MiB で十分
	defaultMaxBodyBytes = 1 << 20
	maxBodyEnv          = "SSE_MAX_BODY_BYTES"
	tooLargeBody        = `{"detail":"送れる大きさを超えています。短くして、もう一度お試しください。"}`

	// 新しい接続の開始のレート制限（1 分あたりの回数と、バースト）
	rateLimitPerMinEnv = "SSE_RATE_LIMIT_PER_MIN"
	rateBurstEnv       = "SSE_RATE_LIMIT_BURST"
	defaultRatePerMin  = 60
	defaultRateBurst   = 10
	rateLimitedBody    = `{"detail":"リクエストが多すぎます。しばらく待ってから、もう一度お試しください。"}`
	// true にすると、判定キーに X-Forwarded-For の最後の値を使う（Cloud Run が直接受けると確認できた環境だけ）。既定は接続元（RemoteAddr）
	rateTrustXFFEnv = "SSE_RATE_LIMIT_TRUST_XFF"
	// 数える相手の数の上限（超えたら、共通の 1 つの枠にまとめる。メモリを無限に増やさない）
	maxRateKeys = 10000

	// 下流への 1 回の書き込み（と flush）に許す時間。超えたら、その接続を切る
	defaultWriteStall = 30 * time.Second
)

var heartbeatComment = []byte(": ping\n\n")

// 環境変数 CORS_ORIGINS が未設定のときに、断る応答の CORS を許すオリジン（本番の画面と、ローカルの開発）
var defaultAllowedOrigins = []string{
	"https://docunavi-web.wataru-apps.workers.dev",
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
	l.respond(w, r, http.StatusServiceUnavailable, rejectBody, rejectRetryAfter)
}

// respond は、上流へ届く前に断る応答（503・413・429）を書く。本文は固定。CORS は reject と同じ扱い。
func (l *streamLimiter) respond(w http.ResponseWriter, r *http.Request, status int, body, retryAfter string) {
	h := w.Header()
	h.Add("Vary", "Origin")
	if origin := r.Header.Get("Origin"); origin != "" {
		if _, ok := l.allowedOrigins[origin]; ok {
			h.Set("Access-Control-Allow-Origin", origin)
		}
	}
	h.Set("Content-Type", "application/json")
	h.Set("Cache-Control", "no-store")
	if retryAfter != "" {
		h.Set("Retry-After", retryAfter)
	}
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
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

// maxBodyFromEnv は、環境変数 SSE_MAX_BODY_BYTES（リクエスト本文の上限）の値を返す。未設定なら既定。0 は無制限。
func maxBodyFromEnv(get func(string) string) (int64, error) {
	raw := strings.TrimSpace(get(maxBodyEnv))
	if raw == "" {
		return defaultMaxBodyBytes, nil
	}
	n, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("%s は 0 以上の整数にしてください（0 は無制限）", maxBodyEnv)
	}
	return n, nil
}

// rateConfig は、新しい接続の開始のレート制限。PerMin が 0 なら制限しない。
type rateConfig struct {
	PerMin int
	Burst  int
	// TrustXFF が true のときだけ、X-Forwarded-For の最後の値をキーにする。false は接続元（RemoteAddr）
	TrustXFF bool
}

// rateFromEnv は、環境変数 SSE_RATE_LIMIT_PER_MIN（0 で無効）、SSE_RATE_LIMIT_BURST（1 以上）、SSE_RATE_LIMIT_TRUST_XFF（既定 false）の値を返す。未設定なら既定。
func rateFromEnv(get func(string) string) (rateConfig, error) {
	cfg := rateConfig{PerMin: defaultRatePerMin, Burst: defaultRateBurst}
	if raw := strings.TrimSpace(get(rateLimitPerMinEnv)); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 0 {
			return cfg, fmt.Errorf("%s は 0 以上の整数にしてください（0 は無効）", rateLimitPerMinEnv)
		}
		cfg.PerMin = n
	}
	if raw := strings.TrimSpace(get(rateBurstEnv)); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 {
			return cfg, fmt.Errorf("%s は 1 以上の整数にしてください", rateBurstEnv)
		}
		cfg.Burst = n
	}
	if raw := strings.TrimSpace(get(rateTrustXFFEnv)); raw != "" {
		v, err := strconv.ParseBool(raw)
		if err != nil {
			return cfg, fmt.Errorf("%s は true か false にしてください", rateTrustXFFEnv)
		}
		cfg.TrustXFF = v
	}
	return cfg, nil
}

// rateLimiter は、キーごとのトークンバケット。新しい接続の開始だけを数える（開いた後の ping・本文は数えない）。
// 数えるキーの数には上限があり（maxRateKeys）、満杯の（＝しばらく使われていない）バケットは掃除する。
type rateLimiter struct {
	cfg rateConfig
	now func() time.Time

	mu        sync.Mutex
	buckets   map[string]*bucket
	lastSweep time.Time
	rejected  atomic.Int64
}

type bucket struct {
	tokens float64
	last   time.Time
	perSec float64
	burst  float64
}

func newRateLimiter(cfg rateConfig, now func() time.Time) *rateLimiter {
	if now == nil {
		now = time.Now
	}
	return &rateLimiter{cfg: cfg, now: now, buckets: make(map[string]*bucket), lastSweep: now()}
}

// take は、key の枠から 1 つ取る。取れなければ false と、取れるまでの目安。
func (l *rateLimiter) take(key string) (bool, time.Duration) {
	perSec := float64(l.cfg.PerMin) / 60
	burst := float64(l.cfg.Burst)
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()
	l.sweepLocked(now)
	b, ok := l.buckets[key]
	if !ok {
		if len(l.buckets) >= maxRateKeys {
			key = "overflow"
			b = l.buckets[key]
		}
		if b == nil {
			b = &bucket{tokens: burst, last: now, perSec: perSec, burst: burst}
			l.buckets[key] = b
		}
	}
	b.tokens = math.Min(b.burst, b.tokens+now.Sub(b.last).Seconds()*b.perSec)
	b.last = now
	if b.tokens >= 1 {
		b.tokens--
		return true, 0
	}
	return false, time.Duration((1 - b.tokens) / b.perSec * float64(time.Second))
}

// sweepLocked は、1 分に 1 回、満杯に戻ったバケット（＝最近使われていない相手）を消す。
func (l *rateLimiter) sweepLocked(now time.Time) {
	if now.Sub(l.lastSweep) < time.Minute {
		return
	}
	l.lastSweep = now
	for k, b := range l.buckets {
		if b.tokens+now.Sub(b.last).Seconds()*b.perSec >= b.burst {
			delete(l.buckets, k)
		}
	}
}

// admit は、新しい接続を受けてよいかを返す。断るときは、取れるまでの目安も返す。キーはクライアント IP だけ
// （Authorization は見ない。認証済みかどうかも、ここでは判定しない）。
func (l *rateLimiter) admit(r *http.Request) (ok bool, retry time.Duration) {
	return l.take("ip:" + clientIP(r, l.cfg.TrustXFF))
}

// clientIP は、判定キーにするクライアントの IP。既定は接続元（RemoteAddr）。
// trustXFF が true（Cloud Run が直接受けると確認できた環境）のときだけ、X-Forwarded-For の最後の値（Google のフロントが足した、
// 直接つないだ相手）を使う。最後より前の値は、クライアントが書けるので使わない。前段にプロキシ（Cloudflare など）があれば、その IP になる。
// 接続元は、Cloud Run では Google の内側のアドレスで、全員が同じキーになりうる（その間は全員で 1 つの枠）。
func clientIP(r *http.Request, trustXFF bool) string {
	if trustXFF {
		if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
			parts := strings.Split(xff, ",")
			if last := strings.TrimSpace(parts[len(parts)-1]); last != "" {
				return last
			}
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// relayOptions は、newRelayWith の設定。ゼロ値の項目は無効（maxBody = 0 は無制限、rate = nil は制限なし、writeStall = 0 は期限なし）。
type relayOptions struct {
	h2c        bool
	heartbeat  time.Duration
	limiter    *streamLimiter
	maxBody    int64
	rate       *rateLimiter
	writeStall time.Duration
}

// newRelay は、本文の上限とレート制限なし（書き込みの期限だけ既定）の中継。
func newRelay(upstream *url.URL, h2c bool, heartbeat time.Duration, limiter *streamLimiter) http.Handler {
	return newRelayWith(upstream, relayOptions{h2c: h2c, heartbeat: heartbeat, limiter: limiter, writeStall: defaultWriteStall})
}

// newRelayWith は、upstream の /chat/stream へ中継するハンドラーを返す。h2c が true なら、上流へは h2c で話す。
// heartbeat が 0 以下なら、ping は送らない。limiter は POST の同時数を制限する（OPTIONS の予備リクエストは数えない）。
func newRelayWith(upstream *url.URL, o relayOptions) http.Handler {
	h2c, heartbeat, limiter := o.h2c, o.heartbeat, o.limiter
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
			// 1. レート制限（接続の開始だけ。安い判定を先にして、濫用を早く断る）
			if o.rate != nil {
				if ok, retry := o.rate.admit(r); !ok {
					total := o.rate.rejected.Add(1)
					limiter.logf("rate_limited rejected_total=%d", total) // キー（IP）は出さない
					limiter.respond(w, r, http.StatusTooManyRequests, rateLimitedBody, strconv.Itoa(max(1, int(math.Ceil(retry.Seconds())))))
					return
				}
			}
			// 2. 本文の上限（Content-Length が分かるときは、読む前に断る）
			if o.maxBody > 0 && r.ContentLength > o.maxBody {
				limiter.logf("body_too_large content_length_over_limit=true")
				limiter.respond(w, r, http.StatusRequestEntityTooLarge, tooLargeBody, "")
				return
			}
			// 3. 同時数の上限
			if !limiter.tryAcquire() {
				limiter.reject(w, r)
				return
			}
			defer limiter.release() // 切断・エラー・正常終了のどれでも、ハンドラーが戻るときに枠を返す
			// 4. 本文を上限まで読み切る（chunked などで Content-Length が無くても、超えたら 413）。読み切ってから上流へ送る
			if o.maxBody > 0 {
				body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, o.maxBody))
				if err != nil {
					var tooLarge *http.MaxBytesError
					if errors.As(err, &tooLarge) {
						limiter.logf("body_too_large content_length_over_limit=false")
						limiter.respond(w, r, http.StatusRequestEntityTooLarge, tooLargeBody, "")
					}
					return // 本文の途中で切れた（相手がいない）ときは、何も返さない
				}
				r.Body = io.NopCloser(bytes.NewReader(body))
				r.ContentLength = int64(len(body))
				r.Header.Del("Transfer-Encoding")
			}
		}
		rc := http.NewResponseController(w)
		// HTTP/1.1 のサーバーは、リクエスト本文を読み切る前に応答を書き始めると、本文を閉じてしまう。上流が本文を読み終えてすぐ応答すると、
		// 中継から上流へ本文を送っている最中の接続が切られ、応答の途中で終わることがある（約 5〜10%。単体テストで再現）。同時に読み書きできるようにする
		_ = rc.EnableFullDuplex()
		// 長い SSE を、サーバー全体の期限で切らない（Server.WriteTimeout は付けていないが、付けられても外れるように）
		_ = rc.SetWriteDeadline(time.Time{})
		hw := newHeartbeatWriter(w, heartbeat)
		hw.stall = o.writeStall
		hw.onStall = func() { limiter.logf("downstream_stalled") } // 読まない・遅すぎる下流を切った（件数だけ）
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

	stall   time.Duration // 1 回の書き込みに許す時間（0 で期限なし）
	onStall func()
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
	n, err := h.writeWithDeadlineLocked(p)
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
	h.withDeadlineLocked(func() error { return http.NewResponseController(h.w).Flush() })
}

// withDeadlineLocked は、f（下流への書き込み・flush）に期限を付ける。期限を超えたら、下流が詰まっているとみなして切る
// （書き込みがエラーになり、ReverseProxy が中断し、リクエストの context が取り消されて上流も止まる）。終わったら期限を外す
// （イベントの間隔が空いても、接続自体は切らない）。
func (h *heartbeatWriter) withDeadlineLocked(f func() error) error {
	if h.stall <= 0 {
		return f()
	}
	rc := http.NewResponseController(h.w)
	_ = rc.SetWriteDeadline(time.Now().Add(h.stall))
	err := f()
	_ = rc.SetWriteDeadline(time.Time{})
	if errors.Is(err, os.ErrDeadlineExceeded) && h.onStall != nil {
		h.onStall()
	}
	return err
}

func (h *heartbeatWriter) writeWithDeadlineLocked(p []byte) (int, error) {
	var n int
	err := h.withDeadlineLocked(func() error {
		var err error
		n, err = h.w.Write(p)
		return err
	})
	return n, err
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
				if _, err := h.writeWithDeadlineLocked(heartbeatComment); err != nil {
					h.closed = true
					h.mu.Unlock()
					return
				}
				_ = h.withDeadlineLocked(func() error { return http.NewResponseController(h.w).Flush() })
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
	maxBody, err := maxBodyFromEnv(os.Getenv)
	if err != nil {
		log.Fatal(err)
	}
	rateCfg, err := rateFromEnv(os.Getenv)
	if err != nil {
		log.Fatal(err)
	}
	var rate *rateLimiter
	if rateCfg.PerMin > 0 {
		rate = newRateLimiter(rateCfg, nil)
	}
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
		Addr: *listen,
		Handler: newRelayWith(upstream, relayOptions{
			h2c: *h2c, heartbeat: *heartbeat, limiter: limiter, maxBody: maxBody, rate: rate, writeStall: defaultWriteStall,
		}),
		ReadHeaderTimeout: 10 * time.Second,
		// WriteTimeout は付けない（SSE を途中で切るため）
	}
	// 待ち受けは HTTP/1.1 と h2c の両方（Cloud Run に --use-http2 を付けたとき、コンテナまで HTTP/2 で届く）
	server.Protocols = new(http.Protocols)
	server.Protocols.SetHTTP1(true)
	server.Protocols.SetUnencryptedHTTP2(true)
	log.Printf("中継を開始します: %s -> %s (上流 h2c=%v, heartbeat=%v, max_streams=%d, cors_origins=%d件, max_body=%d, rate=%d/分 burst=%d trust_xff=%v)", *listen, upstream, *h2c, *heartbeat, *maxStreams, len(origins), maxBody, rateCfg.PerMin, rateCfg.Burst, rateCfg.TrustXFF)
	log.Fatal(server.ListenAndServe())
}
