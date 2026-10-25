# SSE 中継

Go（標準ライブラリだけ）で作った、FastAPI の前に置く SSE の中継。**Cloud Run（`docunavi-sse-relay`）に配備済みで、本番の画面（Vercel）の `/chat/stream` はこの中継を通る**（2026-10-25 から。環境変数 `NEXT_PUBLIC_STREAM_BASE_URL`）。Compose・CI には、まだ入れていない。
本番で確かめたのは、実質問の `POST /chat/stream` が 200（約 3.0 秒）、CORS の `OPTIONS` が 200、回答と引用の表示まで（2026-10-25、各 1 回）。
**切断の伝播（1 回の実測）:** 回答本文の最初のチャンク（`[DONE]` 前）を受け取った直後にブラウザーから切断すると、21 ミリ秒後に API が
「クライアントが切断したため LLM 呼び出しを取り消しました」を記録した。切断は生成の終盤だった可能性があり、生成の途中を長く切った試験ではない。
API が取り消し処理を実行したことまでの確認で、モデル提供元で生成・課金が止まったことは確認していない。
**同時接続:** 短い質問 2 件の同時送信で、どちらも 200 と `[DONE]` まで受信した（疎通の確認で、負荷の上限ではない）。
**未確認:** 中継自身の `: ping`（API が 10 秒ごとに送る `: ping` と区別できず、試していない）、同時接続の負荷の上限、本文サイズの上限（付けていない）、レート制限（無い）。
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
