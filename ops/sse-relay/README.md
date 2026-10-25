# SSE 中継

Go（標準ライブラリだけ）で作った、FastAPI の前に置く SSE の中継。**本番・Cloud Run・Compose・CI には、まだ入れていない**（導入は段階ごとに利用者の承認を取る）。
目的と測定の結果: [docs/target-architecture.md](../../docs/target-architecture.md) の「Go の SSE 中継の測定」。目的は 22〜31 秒の短縮ではない
（原因は検索と Gemini の初期処理で、中継では縮まらない）。長い接続の安定、切断時の上流の取り消し、同時接続が増えたときの備え。

- 中継するのは `POST /chat/stream`（と CORS の `OPTIONS`）だけ。ほかのパスは 404、ほかのメソッドは 405
- 本文は届いた分だけすぐ流す。応答に `X-Accel-Buffering: no` を付ける
- 本文が止まっている間は、`-heartbeat`（既定 25 秒）ごとに `: ping` を送る。イベントの途中には挟まない。0 で送らない
- クライアントが切れたら、上流（LLM 呼び出し）も取り消す。待ち受けは HTTP/1.1 と h2c の両方、上流へは `-upstream-h2c` で h2c
- `Server.WriteTimeout` は付けない。HTTP/1.1 では `EnableFullDuplex` を有効にする（上流が本文を読み終えてすぐ応答すると、中継から上流への接続が切られ、
  応答が途中で終わることがあった。約 5〜10%。`TestRelayDoesNotAbortWhenUpstreamRespondsEarly` が再現する）

```bash
.devcontainer/install-go.sh                      # Go 1.26.10（公式の SHA-256 を照合。devcontainer の cli-tools からも呼ばれる）
cd ops/sse-relay && go vet ./... && go test -race ./...
go build -o /tmp/sse-relay . && /tmp/sse-relay -listen 127.0.0.1:8200 -upstream http://127.0.0.1:8100 [-upstream-h2c] [-heartbeat 25s]
```

`bench/` は、`/chat/stream` に絞る前の中継（`GET /stream`）で測ったもの。いまの中継には、そのままでは使えない（測定の記録は target-architecture.md に残っている）。
戻し方: `ops/sse-relay/` と `.devcontainer/install-go.sh` を消し、`sudo rm -rf /usr/local/go /usr/local/bin/go /usr/local/bin/gofmt`。
