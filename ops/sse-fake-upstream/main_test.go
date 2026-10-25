package main

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newServer(t *testing.T) (*httptest.Server, *fake) {
	t.Helper()
	f := newFake(scenarioNormal, 60*time.Second)
	srv := httptest.NewServer(f.handler())
	t.Cleanup(srv.Close)
	return srv, f
}

func post(ctx context.Context, t *testing.T, base, query string) (*http.Response, error) {
	t.Helper()
	req, _ := http.NewRequestWithContext(ctx, "POST", base+"/chat/stream?"+query, strings.NewReader(`{"message":"x"}`))
	return http.DefaultClient.Do(req)
}

// readEvents は、"data: …" の行を [DONE] か EOF まで集める。": ping" は pings に数える。
func readEvents(r io.Reader) (events []string, pings int, err error) {
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		line := sc.Text()
		switch {
		case line == ": ping":
			pings++
		case strings.HasPrefix(line, "data: "):
			events = append(events, strings.TrimPrefix(line, "data: "))
		}
	}
	return events, pings, sc.Err()
}

func types(t *testing.T, events []string) []string {
	t.Helper()
	var out []string
	for _, e := range events {
		if e == "[DONE]" {
			out = append(out, "[DONE]")
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(e), &m); err != nil {
			t.Fatalf("JSON でない: %q", e)
		}
		out = append(out, m["type"].(string))
	}
	return out
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("待っても満たされない: %s", what)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestNormalStreamHasTheRealShapeAndEndsWithDone(t *testing.T) {
	srv, f := newServer(t)
	resp, err := post(context.Background(), t, srv.URL, "duration=0.3&interval=0.1")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 || resp.Header.Get("Content-Type") != "text/event-stream" ||
		resp.Header.Get("x-vercel-ai-ui-message-stream") != "v1" {
		t.Fatalf("status=%d headers=%v", resp.StatusCode, resp.Header)
	}
	events, _, err := readEvents(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	got := types(t, events)
	if got[0] != "start" || got[1] != "data-status" || got[2] != "text-start" || got[len(got)-1] != "[DONE]" {
		t.Fatalf("並び = %v", got)
	}
	tail := got[len(got)-4:]
	if strings.Join(tail, ",") != "text-end,message-metadata,finish,[DONE]" {
		t.Fatalf("末尾 = %v", tail)
	}
	deltas := 0
	for _, ty := range got {
		if ty == "text-delta" {
			deltas++
		}
	}
	if deltas < 2 || deltas > 4 {
		t.Fatalf("text-delta = %d（0.3 秒 / 0.1 秒間隔）", deltas)
	}
	waitFor(t, "completed=1 active=0", func() bool { return f.stats.completed.Load() == 1 && f.stats.active.Load() == 0 })
}

func TestErrorScenarioSendsDataFailureThenErrorThenDone(t *testing.T) {
	srv, f := newServer(t)
	resp, err := post(context.Background(), t, srv.URL, "scenario=error&after=2&interval=0.01&duration=5")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	events, _, _ := readEvents(resp.Body)
	got := types(t, events)
	tail := strings.Join(got[len(got)-3:], ",")
	if tail != "data-failure,error,[DONE]" {
		t.Fatalf("末尾 = %v", got)
	}
	if n := strings.Count(strings.Join(got, ","), "text-delta"); n != 2 {
		t.Fatalf("after=2 なのに text-delta = %d", n)
	}
	waitFor(t, "errored=1", func() bool { return f.stats.errored.Load() == 1 })
}

func TestAbortScenarioCutsTheConnectionWithoutDone(t *testing.T) {
	srv, f := newServer(t)
	resp, err := post(context.Background(), t, srv.URL, "scenario=abort&after=1&interval=0.01&duration=5")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	events, _, rerr := readEvents(resp.Body)
	if rerr == nil {
		t.Fatalf("途中で切れるはずが、エラーなく終わった: %v", events)
	}
	for _, e := range events {
		if e == "[DONE]" {
			t.Fatal("abort で [DONE] を送ってはいけない")
		}
	}
	waitFor(t, "aborted=1 active=0", func() bool { return f.stats.aborted.Load() == 1 && f.stats.active.Load() == 0 })
}

func TestHTTPErrorScenarioReturnsStatusBeforeStreaming(t *testing.T) {
	srv, _ := newServer(t)
	resp, err := post(context.Background(), t, srv.URL, "scenario=http-error&status=429")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 429 || resp.Header.Get("Content-Type") != "application/json" || string(body) != httpErrorBody {
		t.Fatalf("status=%d body=%q", resp.StatusCode, body)
	}
}

func TestSlowStartDelaysTheFirstByte(t *testing.T) {
	srv, _ := newServer(t)
	start := time.Now()
	resp, err := post(context.Background(), t, srv.URL, "scenario=slow-start&first_byte=0.4&duration=0.1&interval=0.05")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if took := time.Since(start); took < 400*time.Millisecond {
		t.Fatalf("ヘッダーが早すぎる: %v", took)
	}
	events, _, _ := readEvents(resp.Body)
	if got := types(t, events); got[len(got)-1] != "[DONE]" {
		t.Fatalf("並び = %v", got)
	}
}

// タイムアウトの再現: start の後は ping だけで、本文が進まない。切ると active が戻り、client_disconnected が増える。
func TestHangSendsOnlyPingsAndStopsWhenClientDisconnects(t *testing.T) {
	srv, f := newServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	resp, err := post(ctx, t, srv.URL, "scenario=hang&ping=0.05&duration=30")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	br := bufio.NewReader(resp.Body)
	seen := map[string]int{}
	for seen["ping"] < 3 {
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		line = strings.TrimSpace(line)
		switch {
		case line == ": ping":
			seen["ping"]++
		case strings.Contains(line, `"text-`):
			t.Fatalf("hang なのに本文が進んだ: %s", line)
		}
	}
	if f.stats.active.Load() != 1 {
		t.Fatalf("active = %d", f.stats.active.Load())
	}
	cancel()
	waitFor(t, "client_disconnected=1 active=0", func() bool {
		return f.stats.clientDisconnected.Load() == 1 && f.stats.active.Load() == 0
	})
}

func TestClientDisconnectDuringNormalStreamIsCounted(t *testing.T) {
	srv, f := newServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	resp, err := post(ctx, t, srv.URL, "duration=30&interval=0.05")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	br := bufio.NewReader(resp.Body)
	for i := 0; i < 5; i++ { // 少し読んでから切る
		if _, err := br.ReadString('\n'); err != nil {
			t.Fatal(err)
		}
	}
	cancel()
	waitFor(t, "client_disconnected=1 active=0 completed=0", func() bool {
		return f.stats.clientDisconnected.Load() == 1 && f.stats.active.Load() == 0 && f.stats.completed.Load() == 0
	})
}

func TestHundredConcurrentStreamsAreHeldAndAllReleased(t *testing.T) {
	srv, f := newServer(t)
	const n = 100
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	bodies := make([]io.Closer, 0, n)
	for i := 0; i < n; i++ {
		resp, err := post(ctx, t, srv.URL, "scenario=hang&duration=30")
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("%d 本目: %v", i, err)
		}
		bodies = append(bodies, resp.Body)
	}
	waitFor(t, "active=100", func() bool { return f.stats.active.Load() == n })
	cancel()
	for _, b := range bodies {
		b.Close()
	}
	waitFor(t, "active=0", func() bool { return f.stats.active.Load() == 0 })
	if got := f.stats.clientDisconnected.Load(); got != n {
		t.Fatalf("client_disconnected = %d, want %d", got, n)
	}
}

func TestBadParametersAreRejectedWith400(t *testing.T) {
	srv, f := newServer(t)
	bad := []string{
		"scenario=nope", "duration=-1", "duration=abc", "duration=NaN", "duration=61", // max-duration は 60 秒（このテストの設定）
		"interval=0", "after=-1", "after=1001", "status=200", "status=600", "first_byte=99999",
	}
	for _, q := range bad {
		resp, err := post(context.Background(), t, srv.URL, q)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 400 {
			t.Errorf("%q: %d, want 400", q, resp.StatusCode)
		}
		if strings.Contains(string(body), q) {
			t.Errorf("%q: 入力をそのまま返してはいけない: %s", q, body)
		}
	}
	if got := f.stats.rejected.Load(); got != int64(len(bad)) {
		t.Errorf("rejected = %d, want %d", got, len(bad))
	}
	if f.stats.started.Load() != 0 {
		t.Error("不正なリクエストを started に数えてはいけない")
	}
}

func TestOnlyPostChatStreamStatsAndHealthExist(t *testing.T) {
	srv, _ := newServer(t)
	for _, c := range []struct {
		method, path string
		want         int
	}{
		{"GET", "/chat/stream", 405}, {"POST", "/other", 404}, {"GET", "/health", 200}, {"GET", "/stats", 200},
	} {
		req, _ := http.NewRequest(c.method, srv.URL+c.path, nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != c.want {
			t.Errorf("%s %s = %d, want %d", c.method, c.path, resp.StatusCode, c.want)
		}
	}
}

func TestDefaultScenarioAppliesWhenNotSpecified(t *testing.T) {
	f := newFake(scenarioHTTPError, 60*time.Second)
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	resp, err := post(context.Background(), t, srv.URL, "")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 500 {
		t.Fatalf("既定の scenario が効いていない: %d", resp.StatusCode)
	}
}
