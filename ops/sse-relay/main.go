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
package main

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	streamPath       = "/chat/stream"
	defaultHeartbeat = 25 * time.Second
)

var heartbeatComment = []byte(": ping\n\n")

// newRelay は、upstream の /chat/stream へ中継するハンドラーを返す。h2c が true なら、上流へは h2c で話す。
// heartbeat が 0 以下なら、ping は送らない。
func newRelay(upstream *url.URL, h2c bool, heartbeat time.Duration) http.Handler {
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
	flag.Parse()

	upstream, err := url.Parse(*upstreamRaw)
	if err != nil || upstream.Scheme == "" || upstream.Host == "" {
		log.Fatalf("上流の URL が正しくありません: %q", *upstreamRaw)
	}
	server := &http.Server{
		Addr:              *listen,
		Handler:           newRelay(upstream, *h2c, *heartbeat),
		ReadHeaderTimeout: 10 * time.Second,
		// WriteTimeout は付けない（SSE を途中で切るため）
	}
	// 待ち受けは HTTP/1.1 と h2c の両方（Cloud Run に --use-http2 を付けたとき、コンテナまで HTTP/2 で届く）
	server.Protocols = new(http.Protocols)
	server.Protocols.SetHTTP1(true)
	server.Protocols.SetUnencryptedHTTP2(true)
	log.Printf("中継を開始します: %s -> %s (上流 h2c=%v, heartbeat=%v)", *listen, upstream, *h2c, *heartbeat)
	log.Fatal(server.ListenAndServe())
}
