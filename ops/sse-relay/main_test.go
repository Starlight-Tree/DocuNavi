package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func relayTo(t *testing.T, upstream http.Handler, h2c bool) *httptest.Server {
	t.Helper()
	return relayWithHeartbeat(t, upstream, h2c, 0)
}

func relayWithHeartbeat(t *testing.T, upstream http.Handler, h2c bool, heartbeat time.Duration) *httptest.Server {
	t.Helper()
	up := httptest.NewUnstartedServer(upstream)
	if h2c {
		up.Config.Protocols = new(http.Protocols)
		up.Config.Protocols.SetHTTP1(true)
		up.Config.Protocols.SetUnencryptedHTTP2(true)
	}
	up.Start()
	t.Cleanup(up.Close)
	u, _ := url.Parse(up.URL)
	relay := httptest.NewServer(newRelay(u, h2c, heartbeat, newStreamLimiter(0, t.Logf)))
	t.Cleanup(relay.Close)
	return relay
}

// 上流の本文が、まとめず届いた分から順に届く。Authorization と本文も上流へ渡る。
func TestRelayStreamsIncrementallyAndForwardsHeaders(t *testing.T) {
	for _, h2c := range []bool{false, true} {
		name := "h1"
		if h2c {
			name = "h2c"
		}
		t.Run(name, func(t *testing.T) {
			gotAuth := make(chan string, 1)
			relay := relayTo(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				gotAuth <- r.Header.Get("Authorization")
				w.Header().Set("Content-Type", "text/event-stream")
				f := w.(http.Flusher)
				w.Write([]byte("data: 1\n\n"))
				f.Flush()
				time.Sleep(400 * time.Millisecond)
				w.Write([]byte("data: 2\n\n"))
				f.Flush()
			}), h2c)

			req, _ := http.NewRequest("POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
			req.Header.Set("Authorization", "Bearer test")
			start := time.Now()
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			r := bufio.NewReader(resp.Body)
			line, _ := r.ReadString('\n')
			first := time.Since(start)
			if line != "data: 1\n" {
				t.Fatalf("1 行目 = %q", line)
			}
			if first > 300*time.Millisecond {
				t.Fatalf("最初のイベントが遅い（バッファされている）: %v", first)
			}
			if auth := <-gotAuth; auth != "Bearer test" {
				t.Fatalf("Authorization が渡っていない: %q", auth)
			}
		})
	}
}

// クライアントが切れると、上流のリクエストの context が取り消される。
func TestRelayPropagatesClientDisconnect(t *testing.T) {
	for _, h2c := range []bool{false, true} {
		name := "h1"
		if h2c {
			name = "h2c"
		}
		t.Run(name, func(t *testing.T) {
			cancelled := make(chan struct{})
			relay := relayTo(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				w.(http.Flusher).Flush()
				select {
				case <-r.Context().Done():
					close(cancelled)
				case <-time.After(5 * time.Second):
				}
			}), h2c)

			ctx, cancel := context.WithCancel(context.Background())
			req, _ := http.NewRequestWithContext(ctx, "POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			cut := time.Now()
			cancel()
			select {
			case <-cancelled:
				t.Logf("切断が上流に届くまで: %v", time.Since(cut))
			case <-time.After(3 * time.Second):
				t.Fatal("切断が上流に伝わらなかった")
			}
		})
	}
}

func sseHeaders(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
}

// 中継するのは POST /chat/stream（と CORS の OPTIONS）だけ。ほかは上流へ届かない。
func TestRelayOnlyServesChatStream(t *testing.T) {
	reached := make(chan string, 8)
	relay := relayTo(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached <- r.Method + " " + r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	}), false)
	cases := []struct {
		method, path string
		want         int
	}{
		{"POST", "/chat/stream", http.StatusNoContent},
		{"OPTIONS", "/chat/stream", http.StatusNoContent},
		{"GET", "/chat/stream", http.StatusMethodNotAllowed},
		{"POST", "/chat", http.StatusNotFound},
		{"GET", "/health", http.StatusNotFound},
		{"POST", "/chat/stream/extra", http.StatusNotFound},
		{"GET", "/threads", http.StatusNotFound},
	}
	for _, c := range cases {
		req, _ := http.NewRequest(c.method, relay.URL+c.path, strings.NewReader("{}"))
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != c.want {
			t.Errorf("%s %s = %d, want %d", c.method, c.path, resp.StatusCode, c.want)
		}
	}
	close(reached)
	var got []string
	for r := range reached {
		got = append(got, r)
	}
	if len(got) != 2 || got[0] != "POST /chat/stream" || got[1] != "OPTIONS /chat/stream" {
		t.Errorf("上流に届いたもの = %v", got)
	}
}

// SSE の応答には X-Accel-Buffering: no が付く。SSE でない応答には付けない。
func TestRelaySetsAccelBufferingOnlyForEventStream(t *testing.T) {
	relay := relayTo(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Json") == "1" {
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte("{}"))
			return
		}
		sseHeaders(w)
		w.Write([]byte("data: 1\n\n"))
	}), false)
	for _, json := range []bool{false, true} {
		req, _ := http.NewRequest("POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
		if json {
			req.Header.Set("X-Json", "1")
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		got := resp.Header.Get("X-Accel-Buffering")
		if json && got != "" || !json && got != "no" {
			t.Errorf("json=%v: X-Accel-Buffering = %q", json, got)
		}
	}
}

// 上流が黙っている間、一定の間隔で ": ping" が届く。上流の本文はそのまま届く。
func TestRelaySendsHeartbeatWhileUpstreamIsSilent(t *testing.T) {
	for _, h2c := range []bool{false, true} {
		name := "h1"
		if h2c {
			name = "h2c"
		}
		t.Run(name, func(t *testing.T) {
			release := make(chan struct{})
			relay := relayWithHeartbeat(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				sseHeaders(w)
				w.Write([]byte("data: first\n\n"))
				w.(http.Flusher).Flush()
				<-release
				w.Write([]byte("data: last\n\n"))
			}), h2c, 60*time.Millisecond)

			req, _ := http.NewRequest("POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			r := bufio.NewReader(resp.Body)
			readLine := func() string {
				line, err := r.ReadString('\n')
				if err != nil {
					t.Fatalf("読み取り: %v (%q)", err, line)
				}
				return line
			}
			if l := readLine(); l != "data: first\n" {
				t.Fatalf("1 行目 = %q", l)
			}
			readLine() // 空行
			pings := 0
			for pings < 2 {
				if l := readLine(); l != ": ping\n" {
					t.Fatalf("ping のはずの行 = %q", l)
				}
				readLine() // 空行
				pings++
			}
			close(release)
			// 以後、本文が届く（ping が混じってもよい）
			for {
				l := readLine()
				if l == "data: last\n" {
					break
				}
				if l != ": ping\n" && l != "\n" {
					t.Fatalf("想定外の行 = %q", l)
				}
			}
		})
	}
}

// ping は、イベントの途中（空行の前）には挟まない。
func TestRelayHeartbeatNeverSplitsAnEvent(t *testing.T) {
	relay := relayWithHeartbeat(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sseHeaders(w)
		f := w.(http.Flusher)
		w.Write([]byte("data: {\"a\":"))
		f.Flush()
		time.Sleep(300 * time.Millisecond) // heartbeat（40ms）の何回分も、イベントの途中で止まる
		w.Write([]byte("1}\n\n"))
		f.Flush()
		time.Sleep(150 * time.Millisecond)
	}), false, 40*time.Millisecond)

	req, _ := http.NewRequest("POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(string(body), "data: {\"a\":1}\n\n") {
		t.Fatalf("イベントの途中に挟まった、または欠けた: %q", body)
	}
	if !strings.Contains(string(body), ": ping\n\n") {
		t.Fatalf("イベントのあとに ping が来ていない: %q", body)
	}
}

// heartbeat を 0 にすると、ping は送らない。SSE でない応答にも送らない。
func TestRelayHeartbeatOffAndNotForPlainResponses(t *testing.T) {
	cases := map[string]struct {
		heartbeat time.Duration
		sse       bool
	}{
		"heartbeat 0": {0, true},
		"not an sse":  {20 * time.Millisecond, false},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			relay := relayWithHeartbeat(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if c.sse {
					sseHeaders(w)
				} else {
					w.Header().Set("Content-Type", "application/json")
				}
				w.Write([]byte("x\n\n"))
				w.(http.Flusher).Flush()
				time.Sleep(200 * time.Millisecond)
			}), false, c.heartbeat)
			req, _ := http.NewRequest("POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			body, _ := io.ReadAll(resp.Body)
			if string(body) != "x\n\n" {
				t.Fatalf("本文 = %q", body)
			}
		})
	}
}

// 上流に届かないときは 502。クライアントが先に切れたときと区別する。
func TestRelayReturns502WhenUpstreamIsDown(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close() // 誰も待ち受けていないアドレス
	u, _ := url.Parse("http://" + addr)
	relay := httptest.NewServer(newRelay(u, false, 0, newStreamLimiter(0, t.Logf)))
	defer relay.Close()
	resp, err := http.Post(relay.URL+"/chat/stream", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

// 待ち受けは h2c でも受ける（Cloud Run の --use-http2）。クライアントが切れたら、h2c のときも上流が取り消される。
func TestRelayAcceptsH2CAndPropagatesCancel(t *testing.T) {
	cancelled := make(chan struct{})
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sseHeaders(w)
		w.(http.Flusher).Flush()
		select {
		case <-r.Context().Done():
			close(cancelled)
		case <-time.After(5 * time.Second):
		}
	}))
	defer up.Close()
	u, _ := url.Parse(up.URL)
	srv := httptest.NewUnstartedServer(newRelay(u, false, 0, newStreamLimiter(0, t.Logf)))
	srv.Config.Protocols = new(http.Protocols)
	srv.Config.Protocols.SetHTTP1(true)
	srv.Config.Protocols.SetUnencryptedHTTP2(true)
	srv.Start()
	defer srv.Close()

	tr := &http.Transport{Protocols: new(http.Protocols)}
	tr.Protocols.SetUnencryptedHTTP2(true)
	client := &http.Client{Transport: tr}
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "POST", srv.URL+"/chat/stream", strings.NewReader("{}"))
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.ProtoMajor != 2 {
		t.Fatalf("プロトコル = %s（h2c で届いていない）", resp.Proto)
	}
	cancel()
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("h2c の切断が上流に伝わらなかった")
	}
}

// 上流がリクエスト本文を読み終えてすぐ応答し、少し続けて終わるとき、応答が途中で切れない。
// HTTP/1.1 のサーバーが、本文を読み切る前に応答を書き始めると本文を閉じ、中継から上流への接続が切れていた（EnableFullDuplex で直す。以前は約 5〜10% で落ちた）。
func TestRelayDoesNotAbortWhenUpstreamRespondsEarly(t *testing.T) {
	relay := relayTo(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		sseHeaders(w)
		w.Write([]byte("data: 1\n\n"))
		w.(http.Flusher).Flush()
		time.Sleep(5 * time.Millisecond)
	}), false)
	for i := 0; i < 150; i++ {
		resp, err := http.Post(relay.URL+"/chat/stream", "application/json", strings.NewReader("{}"))
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil || string(body) != "data: 1\n\n" {
			t.Fatalf("%d 回目: body=%q err=%v", i, body, err)
		}
	}
}

// 上流へ渡す Host は、中継の Host ではなく上流の URL のもの（Cloud Run は Host で配送先を決める）。
func TestRelaySendsUpstreamHostNotRelayHost(t *testing.T) {
	gotHost := make(chan string, 1)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHost <- r.Host
		sseHeaders(w)
		w.Write([]byte("data: 1\n\n"))
	}))
	t.Cleanup(up.Close)
	u, _ := url.Parse(up.URL)
	relay := httptest.NewServer(newRelay(u, false, 0, newStreamLimiter(0, t.Logf)))
	t.Cleanup(relay.Close)

	req, _ := http.NewRequest("POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
	req.Host = "relay.example.test"
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if got := <-gotHost; got != u.Host {
		t.Fatalf("上流が受けた Host = %q, 想定 %q", got, u.Host)
	}
}

// ---- 同時接続の上限 ----

func relayWithLimiter(t *testing.T, upstream http.Handler, limiter *streamLimiter) *httptest.Server {
	t.Helper()
	up := httptest.NewServer(upstream)
	t.Cleanup(up.Close)
	u, _ := url.Parse(up.URL)
	relay := httptest.NewServer(newRelay(u, false, 0, limiter))
	t.Cleanup(relay.Close)
	return relay
}

// logCapture は、limiter のログを集める（並行に書かれるので mutex で守る）。
type logCapture struct {
	mu    sync.Mutex
	lines []string
}

func (c *logCapture) logf(format string, args ...any) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lines = append(c.lines, fmt.Sprintf(format, args...))
}

func (c *logCapture) all() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]string(nil), c.lines...)
}

// holdStreams は、接続を開いたまま保つ上流。クライアントが切れる（context が取り消される）まで返らない。
func holdStreams(started chan<- struct{}) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sseHeaders(w)
		w.(http.Flusher).Flush()
		if started != nil {
			started <- struct{}{}
		}
		<-r.Context().Done()
	})
}

// openStream は、POST /chat/stream を送り、ヘッダーが届くまで待つ。呼び出し側が cancel で切る。
func openStream(t *testing.T, relayURL string) (*http.Response, context.CancelFunc) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	req, _ := http.NewRequestWithContext(ctx, "POST", relayURL+"/chat/stream", strings.NewReader("{}"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp, cancel
}

func waitActive(t *testing.T, l *streamLimiter, want int64) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if l.active.Load() == want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("active = %d, want %d", l.active.Load(), want)
}

// 多数の goroutine が同時に枠を取りに来ても、上限を超えて受け付けない。全部返せば 0 に戻る。
func TestStreamLimiterNeverExceedsLimitUnderContention(t *testing.T) {
	const limit, workers = 100, 1000
	l := newStreamLimiter(limit, t.Logf)
	var granted atomic.Int64
	var peakSeen atomic.Int64
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			if l.tryAcquire() {
				granted.Add(1)
				if cur := l.active.Load(); cur > peakSeen.Load() {
					peakSeen.Store(cur)
				}
			}
		}()
	}
	close(start)
	wg.Wait()
	if granted.Load() != limit {
		t.Fatalf("取れた枠 = %d, want %d", granted.Load(), limit)
	}
	if peakSeen.Load() > limit {
		t.Fatalf("同時数の最大 = %d, 上限 %d を超えた", peakSeen.Load(), limit)
	}
	for i := 0; i < limit; i++ {
		l.release()
	}
	if l.active.Load() != 0 {
		t.Fatalf("全部返したあとの active = %d, want 0", l.active.Load())
	}
}

// 上限内は受け付け、超えた要求は待たせずに 503 で断る。切断すると枠が戻り、次の要求が通る。
func TestRelayRejectsBeyondLimitAndFreesSlotOnDisconnect(t *testing.T) {
	const limit = 3
	logs := &logCapture{}
	l := newStreamLimiter(limit, logs.logf)
	relay := relayWithLimiter(t, holdStreams(nil), l)

	cancels := make([]context.CancelFunc, 0, limit)
	for i := 0; i < limit; i++ {
		resp, cancel := openStream(t, relay.URL)
		if resp.StatusCode != 200 {
			t.Fatalf("%d 本目 = %d, want 200", i+1, resp.StatusCode)
		}
		cancels = append(cancels, cancel)
	}
	waitActive(t, l, limit)

	req, _ := http.NewRequest("POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
	req.Header.Set("Origin", "https://docunavi-web.wataru-apps.workers.dev")
	start := time.Now()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	took := time.Since(start)
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("上限超過 = %d, want 503", resp.StatusCode)
	}
	if took > 500*time.Millisecond {
		t.Fatalf("断るのが遅い（待たせた）: %v", took)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "application/json" {
		t.Fatalf("Content-Type = %q", ct)
	}
	if string(body) != rejectBody {
		t.Fatalf("本文 = %q", body)
	}
	if resp.Header.Get("Retry-After") != rejectRetryAfter {
		t.Fatalf("Retry-After = %q", resp.Header.Get("Retry-After"))
	}
	if resp.Header.Get("Access-Control-Allow-Origin") != "https://docunavi-web.wataru-apps.workers.dev" {
		t.Fatalf("許可リストのオリジンには、ブラウザーが読めるよう値を返す: %q", resp.Header.Get("Access-Control-Allow-Origin"))
	}
	if resp.Header.Get("Access-Control-Allow-Credentials") != "" {
		t.Fatalf("Allow-Credentials は付けない: %q", resp.Header.Get("Access-Control-Allow-Credentials"))
	}
	if got := l.rejected.Load(); got != 1 {
		t.Fatalf("rejected = %d, want 1", got)
	}
	if got := l.active.Load(); got != limit {
		t.Fatalf("断っても active は変わらない: %d", got)
	}
	lines := logs.all()
	if len(lines) != 1 || !strings.Contains(lines[0], "stream_limit_rejected active_streams=3 limit=3") {
		t.Fatalf("ログ = %q", lines)
	}

	// 1 本切ると枠が戻り、次の要求が通る
	cancels[0]()
	waitActive(t, l, limit-1)
	resp2, _ := openStream(t, relay.URL)
	if resp2.StatusCode != 200 {
		t.Fatalf("枠が戻った後 = %d, want 200", resp2.StatusCode)
	}
	waitActive(t, l, limit)
}

// 試験の目標どおり、100 本は受け付け、101 本目を断る。
func TestRelayHolds100AndRejects101st(t *testing.T) {
	const limit = 100
	l := newStreamLimiter(limit, t.Logf)
	started := make(chan struct{}, limit+1)
	relay := relayWithLimiter(t, holdStreams(started), l)

	var wg sync.WaitGroup
	statuses := make(chan int, limit)
	for i := 0; i < limit; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithCancel(context.Background())
			t.Cleanup(cancel)
			req, _ := http.NewRequestWithContext(ctx, "POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				statuses <- -1
				return
			}
			t.Cleanup(func() { resp.Body.Close() })
			statuses <- resp.StatusCode
		}()
	}
	wg.Wait()
	close(statuses)
	for s := range statuses {
		if s != 200 {
			t.Fatalf("上限内の接続が 200 でない: %d", s)
		}
	}
	waitActive(t, l, limit)

	resp, err := http.Post(relay.URL+"/chat/stream", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("101 本目 = %d, want 503", resp.StatusCode)
	}
	if l.rejected.Load() != 1 || l.active.Load() != limit {
		t.Fatalf("rejected=%d active=%d", l.rejected.Load(), l.active.Load())
	}
}

// 上流が落ちて 502 になった要求も、枠を返す（返さないと、失敗が続いたときに枠が尽きる）。
func TestRelayReleasesSlotWhenUpstreamFails(t *testing.T) {
	l := newStreamLimiter(1, t.Logf)
	dead := httptest.NewServer(http.NotFoundHandler())
	u, _ := url.Parse(dead.URL)
	dead.Close()
	relay := httptest.NewServer(newRelay(u, false, 0, l))
	t.Cleanup(relay.Close)

	// 接続を使い回さない。上流に届かず 502 を返した HTTP/1.1 の接続は、リクエスト本文を読まないまま閉じられ、
	// 使い回した次のリクエストが reset されることがある（上限とは無関係。枠の返却だけを確かめたいので、避ける）
	client := &http.Client{Transport: &http.Transport{DisableKeepAlives: true}}
	for i := 0; i < 5; i++ {
		resp, err := client.Post(relay.URL+"/chat/stream", "application/json", strings.NewReader("{}"))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusBadGateway {
			t.Fatalf("%d 回目 = %d, want 502（503 なら枠が戻っていない）", i+1, resp.StatusCode)
		}
	}
	waitActive(t, l, 0)
	if l.rejected.Load() != 0 {
		t.Fatalf("rejected = %d, want 0", l.rejected.Load())
	}
}

// CORS の予備リクエスト（OPTIONS）は数えず、上限いっぱいでも上流へ届く。
func TestRelayDoesNotCountOrRejectPreflight(t *testing.T) {
	l := newStreamLimiter(1, t.Logf)
	var preflight atomic.Int64
	relay := relayWithLimiter(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodOptions {
			preflight.Add(1)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		holdStreams(nil).ServeHTTP(w, r)
	}), l)

	resp, _ := openStream(t, relay.URL)
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	waitActive(t, l, 1)

	req, _ := http.NewRequest("OPTIONS", relay.URL+"/chat/stream", nil)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusNoContent || preflight.Load() != 1 {
		t.Fatalf("OPTIONS = %d, 上流に届いた回数 = %d", res.StatusCode, preflight.Load())
	}
	if l.active.Load() != 1 || l.rejected.Load() != 0 {
		t.Fatalf("OPTIONS が数えられた: active=%d rejected=%d", l.active.Load(), l.rejected.Load())
	}
}

// 上限 0 は無制限（数えるだけで、断らない）。
func TestRelayLimitZeroIsUnlimited(t *testing.T) {
	l := newStreamLimiter(0, t.Logf)
	relay := relayWithLimiter(t, holdStreams(nil), l)
	for i := 0; i < 30; i++ {
		if resp, _ := openStream(t, relay.URL); resp.StatusCode != 200 {
			t.Fatalf("%d 本目 = %d", i+1, resp.StatusCode)
		}
	}
	waitActive(t, l, 30)
	if l.rejected.Load() != 0 {
		t.Fatalf("rejected = %d", l.rejected.Load())
	}
}

func TestMaxStreamsFromEnv(t *testing.T) {
	cases := []struct {
		raw     string
		want    int
		wantErr bool
	}{
		{"", defaultMaxStreams, false},
		{"  ", defaultMaxStreams, false},
		{"100", 100, false},
		{" 7 ", 7, false},
		{"0", 0, false},
		{"-1", 0, true},
		{"abc", 0, true},
		{"1.5", 0, true},
	}
	for _, c := range cases {
		got, err := maxStreamsFromEnv(func(string) string { return c.raw })
		if (err != nil) != c.wantErr || (!c.wantErr && got != c.want) {
			t.Errorf("%q: got (%d, %v), want (%d, err=%v)", c.raw, got, err, c.want, c.wantErr)
		}
	}
}

// 断る応答の CORS は許可リスト方式。リストのオリジンだけ値を返し、許可外・Origin なしには CORS ヘッダーを返さない。"*" は返さない。
func TestRejectCORSAllowlist(t *testing.T) {
	l := newStreamLimiter(1, t.Logf)
	l.setAllowedOrigins([]string{"https://docunavi-eosin.vercel.app", "http://localhost:3000"})
	relay := relayWithLimiter(t, holdStreams(nil), l)
	resp, cancel := openStream(t, relay.URL)
	defer cancel()
	if resp.StatusCode != 200 {
		t.Fatalf("1 本目 = %d", resp.StatusCode)
	}
	waitActive(t, l, 1)

	cases := []struct {
		origin string
		want   string // 空なら Access-Control-Allow-Origin が無いこと
	}{
		{"https://docunavi-eosin.vercel.app", "https://docunavi-eosin.vercel.app"},
		{"http://localhost:3000", "http://localhost:3000"},
		{"https://evil.example", ""},
		{"https://docunavi-eosin.vercel.app.evil.example", ""}, // 前方一致で通さない
		{"http://docunavi-eosin.vercel.app", ""},               // スキームが違う
		{"null", ""},
		{"", ""}, // Origin なし（curl など）
	}
	for _, c := range cases {
		req, _ := http.NewRequest("POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
		if c.origin != "" {
			req.Header.Set("Origin", c.origin)
		}
		r, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, r.Body)
		r.Body.Close()
		if r.StatusCode != http.StatusServiceUnavailable {
			t.Fatalf("origin=%q: %d, want 503", c.origin, r.StatusCode)
		}
		got, present := r.Header["Access-Control-Allow-Origin"]
		if c.want == "" && present {
			t.Errorf("origin=%q: 許可外なのに CORS ヘッダーを返した: %q", c.origin, got)
		}
		if c.want != "" && (len(got) != 1 || got[0] != c.want) {
			t.Errorf("origin=%q: Allow-Origin = %q, want %q", c.origin, got, c.want)
		}
		if got := r.Header.Get("Access-Control-Allow-Origin"); got == "*" {
			t.Errorf("origin=%q: * を返してはいけない", c.origin)
		}
		if r.Header.Get("Access-Control-Allow-Credentials") != "" {
			t.Errorf("origin=%q: Allow-Credentials は付けない", c.origin)
		}
		if !strings.Contains(r.Header.Get("Vary"), "Origin") {
			t.Errorf("origin=%q: Vary: Origin が無い: %q", c.origin, r.Header.Get("Vary"))
		}
	}
}

func TestCORSOriginsFromEnv(t *testing.T) {
	cases := []struct {
		raw     string
		want    []string
		wantErr bool
	}{
		{"", defaultAllowedOrigins, false},
		{"  ", defaultAllowedOrigins, false},
		{" , ", defaultAllowedOrigins, false},
		{"https://a.example", []string{"https://a.example"}, false},
		{" https://a.example/ , http://localhost:3000 ", []string{"https://a.example", "http://localhost:3000"}, false},
		{"*", nil, true},
		{"https://a.example,*", nil, true},
		{"a.example", nil, true},
		{"https://a.example/path", nil, true},
		{"ftp://a.example", nil, true},
	}
	for _, c := range cases {
		got, err := corsOriginsFromEnv(func(string) string { return c.raw })
		if (err != nil) != c.wantErr {
			t.Errorf("%q: err = %v, wantErr %v", c.raw, err, c.wantErr)
			continue
		}
		if !c.wantErr && strings.Join(got, "|") != strings.Join(c.want, "|") {
			t.Errorf("%q: got %q, want %q", c.raw, got, c.want)
		}
	}
}

// ---- 本文の上限・レート制限・下流の詰まり（2026-11-09） ----

// relayWithOptions は、options を整えてから中継を起こす（上流は h1）。
func relayWithOptions(t *testing.T, upstream http.Handler, tune func(*relayOptions)) *httptest.Server {
	t.Helper()
	up := httptest.NewServer(upstream)
	t.Cleanup(up.Close)
	u, _ := url.Parse(up.URL)
	o := relayOptions{limiter: newStreamLimiter(0, t.Logf), writeStall: defaultWriteStall}
	if tune != nil {
		tune(&o)
	}
	relay := httptest.NewServer(newRelayWith(u, o))
	t.Cleanup(relay.Close)
	return relay
}

type hitCounter struct {
	hits atomic.Int64
	body atomic.Value // 最後に届いた本文（string）
}

func (c *hitCounter) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c.hits.Add(1)
		b, _ := io.ReadAll(r.Body)
		c.body.Store(string(b))
		sseHeaders(w)
		w.Write([]byte("data: ok\n\n"))
	})
}

func post(t *testing.T, url string, body io.Reader, hdr map[string]string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest("POST", url+"/chat/stream", body)
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

func TestRelayRejectsBodyOverLimitByContentLengthWithoutForwarding(t *testing.T) {
	up := &hitCounter{}
	relay := relayWithOptions(t, up.handler(), func(o *relayOptions) { o.maxBody = 1024 })
	resp := post(t, relay.URL, strings.NewReader(strings.Repeat("a", 2048)), nil)
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", resp.StatusCode)
	}
	if !strings.Contains(string(body), `"detail"`) || !strings.Contains(string(body), "大きさ") {
		t.Fatalf("本文 = %q", body)
	}
	if up.hits.Load() != 0 {
		t.Fatal("上限を超えた本文が上流へ転送された")
	}
}

// Content-Length の無い（chunked の）本文は、読み進めて超えたら 413。上流へは何も送らない。上限ちょうどは通る。
func TestRelayRejectsChunkedBodyOverLimitAndPassesBodyWithinLimit(t *testing.T) {
	up := &hitCounter{}
	relay := relayWithOptions(t, up.handler(), func(o *relayOptions) { o.maxBody = 1024 })
	chunked := func(s string) io.Reader { return io.MultiReader(strings.NewReader(s)) } // 長さが分からない（ContentLength = -1）

	resp := post(t, relay.URL, chunked(strings.Repeat("a", 1025)), nil)
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", resp.StatusCode)
	}
	if up.hits.Load() != 0 {
		t.Fatal("上限を超えた chunked の本文が上流へ転送された")
	}

	exact := strings.Repeat("b", 1024)
	resp = post(t, relay.URL, chunked(exact), nil)
	io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK || up.hits.Load() != 1 {
		t.Fatalf("上限ちょうどの本文: status = %d, hits = %d", resp.StatusCode, up.hits.Load())
	}
	if got, _ := up.body.Load().(string); got != exact {
		t.Fatalf("上流に届いた本文が違う（%d バイト）", len(got))
	}
}

func TestRelayBodyLimitZeroIsUnlimited(t *testing.T) {
	up := &hitCounter{}
	relay := relayWithOptions(t, up.handler(), func(o *relayOptions) { o.maxBody = 0 })
	resp := post(t, relay.URL, strings.NewReader(strings.Repeat("a", 3<<20)), nil)
	io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestMaxBodyAndRateFromEnv(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	if n, err := maxBodyFromEnv(env(nil)); err != nil || n != 1<<20 {
		t.Fatalf("既定 = %d, %v", n, err)
	}
	if n, err := maxBodyFromEnv(env(map[string]string{maxBodyEnv: "2048"})); err != nil || n != 2048 {
		t.Fatalf("上書き = %d, %v", n, err)
	}
	for _, bad := range []string{"-1", "abc", "1.5"} {
		if _, err := maxBodyFromEnv(env(map[string]string{maxBodyEnv: bad})); err == nil {
			t.Fatalf("%q はエラーのはず", bad)
		}
	}
	cfg, err := rateFromEnv(env(nil))
	if err != nil || cfg != (rateConfig{PerMin: 60, Burst: 10}) {
		t.Fatalf("既定 = %+v, %v", cfg, err)
	}
	cfg, err = rateFromEnv(env(map[string]string{rateLimitPerMinEnv: "120", rateBurstEnv: "20"}))
	if err != nil || cfg != (rateConfig{PerMin: 120, Burst: 20}) {
		t.Fatalf("上書き = %+v, %v", cfg, err)
	}
	if cfg, err := rateFromEnv(env(map[string]string{rateTrustXFFEnv: "true"})); err != nil || !cfg.TrustXFF {
		t.Fatalf("TRUST_XFF=true = %+v, %v", cfg, err)
	}
	if cfg, err := rateFromEnv(env(map[string]string{rateLimitPerMinEnv: "0"})); err != nil || cfg.PerMin != 0 {
		t.Fatalf("0 は無効 = %+v, %v", cfg, err)
	}
	for _, m := range []map[string]string{{rateLimitPerMinEnv: "-1"}, {rateLimitPerMinEnv: "x"}, {rateBurstEnv: "0"}, {rateBurstEnv: "x"}, {rateTrustXFFEnv: "maybe"}} {
		if _, err := rateFromEnv(env(m)); err == nil {
			t.Fatalf("%v はエラーのはず", m)
		}
	}
}

// 時計を手で進められるレート制限つきの中継。
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

func relayWithRate(t *testing.T, upstream http.Handler, cfg rateConfig, clock *fakeClock, tune func(*relayOptions)) *httptest.Server {
	return relayWithOptions(t, upstream, func(o *relayOptions) {
		o.rate = newRateLimiter(cfg, clock.now)
		if tune != nil {
			tune(o)
		}
	})
}

func TestRelayRateLimitsNewConnectionsPerClientIPWith429(t *testing.T) {
	up := &hitCounter{}
	clock := &fakeClock{t: time.Unix(1_000_000, 0)}
	relay := relayWithRate(t, up.handler(), rateConfig{PerMin: 60, Burst: 3, TrustXFF: true}, clock, nil)
	ip := map[string]string{"X-Forwarded-For": "10.0.0.1"}
	for i := 0; i < 3; i++ { // バースト 3
		resp := post(t, relay.URL, strings.NewReader("{}"), ip)
		io.Copy(io.Discard, resp.Body)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("%d 回目: status = %d", i+1, resp.StatusCode)
		}
	}
	resp := post(t, relay.URL, strings.NewReader("{}"), ip)
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("4 回目: status = %d, want 429", resp.StatusCode)
	}
	if ra, _ := strconv.Atoi(resp.Header.Get("Retry-After")); ra < 1 {
		t.Fatalf("Retry-After = %q", resp.Header.Get("Retry-After"))
	}
	if !strings.Contains(string(body), "多すぎ") {
		t.Fatalf("本文 = %q", body)
	}
	if up.hits.Load() != 3 {
		t.Fatalf("上流に届いた数 = %d, want 3（断った要求は届かない）", up.hits.Load())
	}
	// 別の IP は影響を受けない
	if resp := post(t, relay.URL, strings.NewReader("{}"), map[string]string{"X-Forwarded-For": "10.0.0.2"}); resp.StatusCode != http.StatusOK {
		t.Fatalf("別の IP: status = %d", resp.StatusCode)
	}
	// 60 回/分 = 1 秒に 1 つ戻る
	clock.advance(1100 * time.Millisecond)
	resp = post(t, relay.URL, strings.NewReader("{}"), ip)
	io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("1 秒後: status = %d", resp.StatusCode)
	}
}

// TrustXFF が true のとき、X-Forwarded-For は最後の値だけを使う（それより前は、クライアントが書ける）。
func TestRelayRateLimitIgnoresClientWrittenForwardedFor(t *testing.T) {
	up := &hitCounter{}
	clock := &fakeClock{t: time.Unix(1_000_000, 0)}
	relay := relayWithRate(t, up.handler(), rateConfig{PerMin: 60, Burst: 1, TrustXFF: true}, clock, nil)
	first := post(t, relay.URL, strings.NewReader("{}"), map[string]string{"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
	io.Copy(io.Discard, first.Body)
	second := post(t, relay.URL, strings.NewReader("{}"), map[string]string{"X-Forwarded-For": "2.2.2.2, 9.9.9.9"})
	if first.StatusCode != http.StatusOK || second.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("status = %d, %d（同じ最後の値は同じ相手）", first.StatusCode, second.StatusCode)
	}
}

// 既定（TrustXFF なし）は接続元だけで数える。X-Forwarded-For を書き換えても枠は増えない。
func TestRelayRateLimitDefaultsToRemoteAddrAndIgnoresForwardedFor(t *testing.T) {
	up := &hitCounter{}
	clock := &fakeClock{t: time.Unix(1_000_000, 0)}
	relay := relayWithRate(t, up.handler(), rateConfig{PerMin: 60, Burst: 2}, clock, nil)
	status := func(h map[string]string) int {
		resp := post(t, relay.URL, strings.NewReader("{}"), h)
		io.Copy(io.Discard, resp.Body)
		return resp.StatusCode
	}
	if status(map[string]string{"X-Forwarded-For": "1.1.1.1"}) != 200 || status(map[string]string{"X-Forwarded-For": "2.2.2.2"}) != 200 {
		t.Fatal("バースト 2 までは通るはず")
	}
	if got := status(map[string]string{"X-Forwarded-For": "3.3.3.3"}); got != http.StatusTooManyRequests {
		t.Fatalf("X-Forwarded-For を変えても同じ接続元の枠のはず: status = %d", got)
	}
}

// Authorization は見ない。認証済みかどうかも、ヘッダーの値の違いも、枠に影響しない（そのまま上流へ渡すだけ）。
func TestRelayRateLimitDoesNotLookAtAuthorization(t *testing.T) {
	var seen []string
	var mu sync.Mutex
	up := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen = append(seen, r.Header.Get("Authorization"))
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	})
	clock := &fakeClock{t: time.Unix(1_000_000, 0)}
	relay := relayWithRate(t, up, rateConfig{PerMin: 60, Burst: 2}, clock, nil)
	status := func(auth string) int {
		resp := post(t, relay.URL, strings.NewReader("{}"), map[string]string{"Authorization": auth})
		io.Copy(io.Discard, resp.Body)
		return resp.StatusCode
	}
	if status("Bearer a.b.c") != 200 || status("Bearer x.y.z") != 200 || status("") != 429 {
		t.Fatal("Authorization が違っても、同じ接続元は同じ枠のはず")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 2 || seen[0] != "Bearer a.b.c" || seen[1] != "Bearer x.y.z" {
		t.Fatalf("上流に渡った Authorization = %q", seen)
	}
}

func TestRelayRateLimitDisabledWhenNil(t *testing.T) {
	up := &hitCounter{}
	relay := relayWithOptions(t, up.handler(), nil)
	for i := 0; i < 50; i++ {
		resp := post(t, relay.URL, strings.NewReader("{}"), nil)
		io.Copy(io.Discard, resp.Body)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("%d 回目: status = %d", i+1, resp.StatusCode)
		}
	}
}

// 開いている接続の ping と本文は、レート制限で止まらない。制限は接続の「開始」だけに効く。
func TestRelayHeartbeatIsNotRateLimited(t *testing.T) {
	clock := &fakeClock{t: time.Unix(1_000_000, 0)}
	release := make(chan struct{})
	relay := relayWithRate(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sseHeaders(w)
		w.Write([]byte("data: first\n\n"))
		w.(http.Flusher).Flush()
		<-release
		w.Write([]byte("data: last\n\n"))
	}), rateConfig{PerMin: 1, Burst: 1}, clock, func(o *relayOptions) { o.heartbeat = 30 * time.Millisecond })
	ip := map[string]string{"X-Forwarded-For": "10.0.0.1"}

	resp := post(t, relay.URL, strings.NewReader("{}"), ip) // 枠を使い切る
	r := bufio.NewReader(resp.Body)
	read := func() string {
		l, err := r.ReadString('\n')
		if err != nil {
			t.Fatalf("読み取り: %v", err)
		}
		return l
	}
	if l := read(); l != "data: first\n" {
		t.Fatalf("1 行目 = %q", l)
	}
	read()
	if second := post(t, relay.URL, strings.NewReader("{}"), ip); second.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("2 本目: status = %d, want 429", second.StatusCode)
	}
	// 制限に当たった後も、1 本目の ping は届き続け、本文も最後まで届く
	for pings := 0; pings < 3; {
		if l := read(); l == ": ping\n" {
			pings++
			read()
		}
	}
	close(release)
	for {
		l := read()
		if l == "data: last\n" {
			break
		}
		if l != ": ping\n" && l != "\n" {
			t.Fatalf("想定外の行 = %q", l)
		}
	}
}

func TestRateLimiterBoundsTheNumberOfKeysAndSweepsIdleOnes(t *testing.T) {
	clock := &fakeClock{t: time.Unix(1_000_000, 0)}
	l := newRateLimiter(rateConfig{PerMin: 60, Burst: 2}, clock.now)
	for i := 0; i < maxRateKeys+500; i++ {
		l.take(fmt.Sprintf("ip:%d", i))
	}
	l.mu.Lock()
	n := len(l.buckets)
	l.mu.Unlock()
	if n > maxRateKeys+1 { // +1 は、溢れた分をまとめる共通の枠
		t.Fatalf("キーの数 = %d（上限 %d）", n, maxRateKeys)
	}
	clock.advance(5 * time.Minute) // 全部満杯に戻る
	l.take("ip:fresh")
	l.mu.Lock()
	n = len(l.buckets)
	l.mu.Unlock()
	if n > 2 {
		t.Fatalf("掃除の後のキーの数 = %d", n)
	}
}

// 上流のイベントは、溜めず、受け取ってすぐ下流へ届く（上流がまだ生成中でも）。
func TestRelayForwardsEachEventImmediatelyWhileUpstreamIsStillRunning(t *testing.T) {
	release := make(chan struct{})
	relay := relayWithOptions(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sseHeaders(w)
		for i := 0; i < 3; i++ {
			fmt.Fprintf(w, "data: %d\n\n", i)
			w.(http.Flusher).Flush()
			time.Sleep(50 * time.Millisecond)
		}
		<-release // 以後は、止めたまま（まとめて流す実装なら、ここまでの 3 件が届かない）
	}), func(o *relayOptions) { o.maxBody = defaultMaxBodyBytes })
	t.Cleanup(func() { close(release) }) // relay.Close は処理中の要求を待つので、それより先に解く（後入れ先出し）
	resp := post(t, relay.URL, strings.NewReader("{}"), nil)
	r := bufio.NewReader(resp.Body)
	start := time.Now()
	for i := 0; i < 3; i++ {
		line, err := r.ReadString('\n')
		if err != nil || line != fmt.Sprintf("data: %d\n", i) {
			t.Fatalf("%d 件目 = %q, %v", i, line, err)
		}
		r.ReadString('\n')
		if got := time.Since(start); got > time.Duration(i+1)*50*time.Millisecond+400*time.Millisecond {
			t.Fatalf("%d 件目が %v 後に届いた（遅い）", i, got)
		}
	}
}

// 下流が切れたら、本文の上限・レート制限を入れた状態でも、上流のリクエストはすぐ取り消される。
func TestRelayCancelsUpstreamWhenDownstreamDisconnectsWithLimitsOn(t *testing.T) {
	cancelled := make(chan struct{})
	clock := &fakeClock{t: time.Unix(1_000_000, 0)}
	relay := relayWithRate(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sseHeaders(w)
		w.Write([]byte("data: first\n\n"))
		w.(http.Flusher).Flush()
		select {
		case <-r.Context().Done():
			close(cancelled)
		case <-time.After(5 * time.Second):
		}
	}), rateConfig{PerMin: 60, Burst: 10}, clock, func(o *relayOptions) { o.maxBody = defaultMaxBodyBytes })
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "POST", relay.URL+"/chat/stream", strings.NewReader("{}"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	bufio.NewReader(resp.Body).ReadString('\n')
	cancel()
	select {
	case <-cancelled:
	case <-time.After(2 * time.Second):
		t.Fatal("下流の切断が上流に伝わらなかった")
	}
}

// 読まない下流: 中継のメモリは増え続けない（上流の読み取りが止まる）。書き込みの期限を超えたら接続を切り、上流も取り消す。
func TestRelayDropsAStalledDownstreamWithoutGrowingMemory(t *testing.T) {
	var sent atomic.Int64
	upstreamDone := make(chan struct{})
	relay := relayWithOptions(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(upstreamDone)
		sseHeaders(w)
		chunk := []byte("data: " + strings.Repeat("x", 1000) + "\n\n")
		for r.Context().Err() == nil {
			n, err := w.Write(chunk)
			sent.Add(int64(n))
			if err != nil {
				return
			}
			w.(http.Flusher).Flush()
		}
	}), func(o *relayOptions) { o.writeStall = 300 * time.Millisecond })

	conn, err := net.Dial("tcp", strings.TrimPrefix(relay.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.(*net.TCPConn).SetReadBuffer(1024) // 受け取りを小さくして、読まない
	fmt.Fprintf(conn, "POST /chat/stream HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")

	runtime.GC()
	var before runtime.MemStats
	runtime.ReadMemStats(&before)

	select { // 期限（300ms）の後、中継が接続を切り、上流の書き込みが止まる
	case <-upstreamDone:
	case <-time.After(5 * time.Second):
		t.Fatalf("詰まった下流が切られず、上流が取り消されなかった（上流が送った量 %d バイト）", sent.Load())
	}
	runtime.GC()
	var after runtime.MemStats
	runtime.ReadMemStats(&after)

	const bound = 64 << 20
	if sent.Load() > bound {
		t.Fatalf("上流が %d バイト送れた（背圧が効いていない。上限 %d）", sent.Load(), bound)
	}
	if grown := int64(after.HeapAlloc) - int64(before.HeapAlloc); grown > bound {
		t.Fatalf("ヒープが %d バイト増えた", grown)
	}
	t.Logf("上流が送れた量 = %d バイト、ヒープの増加 = %d バイト", sent.Load(), int64(after.HeapAlloc)-int64(before.HeapAlloc))
}
