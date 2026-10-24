package main

import (
	"bufio"
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func relayTo(t *testing.T, upstream http.Handler, h2c bool) *httptest.Server {
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
	relay := httptest.NewServer(newRelay(u, h2c))
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
