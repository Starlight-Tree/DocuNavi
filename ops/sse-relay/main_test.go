package main

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
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
	relay := httptest.NewServer(newRelay(u, h2c, heartbeat))
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
	relay := httptest.NewServer(newRelay(u, false, 0))
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
	srv := httptest.NewUnstartedServer(newRelay(u, false, 0))
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
	relay := httptest.NewServer(newRelay(u, false, 0))
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
