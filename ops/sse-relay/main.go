// SSE 中継（学習用。docs/target-architecture.md の「Go の SSE 中継」）。
//
// ブラウザー側の接続を受け、上流（FastAPI）へそのまま中継する。標準ライブラリだけで作る。
//   - 上流の本文は届いた分だけすぐ流す（FlushInterval = -1）
//   - Authorization などのヘッダーはそのまま渡す（検証は上流が行う。ここでは見ない）
//   - クライアントが切れたら、リクエストの context が取り消され、上流の接続も閉じる
//     （HTTP/1.1 は接続を閉じる、h2c は RST_STREAM を送る）
//   - 上流へ h2c（暗号化なしの HTTP/2）で話すことも選べる（-upstream-h2c。Go 1.24 以降の標準機能）
package main

import (
	"flag"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"time"
)

// newRelay は、upstream へ中継するハンドラーを返す。h2c が true なら、上流へは h2c で話す。
func newRelay(upstream *url.URL, h2c bool) http.Handler {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConnsPerHost = 1024
	if h2c {
		transport.Protocols = new(http.Protocols)
		transport.Protocols.SetUnencryptedHTTP2(true)
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(upstream)
			r.Out.Host = r.In.Host
		},
		Transport:     transport,
		FlushInterval: -1,
		ErrorLog:      log.New(os.Stderr, "relay: ", log.LstdFlags),
	}
	return proxy
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8200", "待ち受けるアドレス")
	upstreamRaw := flag.String("upstream", "http://127.0.0.1:8100", "中継先（FastAPI）の URL")
	h2c := flag.Bool("upstream-h2c", false, "上流へ h2c（暗号化なしの HTTP/2）で話す")
	flag.Parse()

	upstream, err := url.Parse(*upstreamRaw)
	if err != nil || upstream.Scheme == "" || upstream.Host == "" {
		log.Fatalf("上流の URL が正しくありません: %q", *upstreamRaw)
	}
	server := &http.Server{
		Addr:              *listen,
		Handler:           newRelay(upstream, *h2c),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("中継を開始します: %s -> %s (上流 h2c=%v)", *listen, upstream, *h2c)
	log.Fatal(server.ListenAndServe())
}
