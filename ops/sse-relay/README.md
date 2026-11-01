# SSE 中継

Go（標準ライブラリだけ）で作った、FastAPI の前に置く SSE の中継。**Cloud Run（`docunavi-sse-relay`）に配備済みで、本番の画面（Vercel）の `/chat/stream` はこの中継を通る**（2026-10-25 から。環境変数 `NEXT_PUBLIC_STREAM_BASE_URL`）。Compose・CI には、まだ入れていない。
本番で確かめたのは、実質問の `POST /chat/stream` が 200（約 3.0 秒）、CORS の `OPTIONS` が 200、回答と引用の表示まで（2026-10-25、各 1 回）。
**切断の伝播（1 回の実測）:** 回答本文の最初のチャンク（`[DONE]` 前）を受け取った直後にブラウザーから切断すると、21 ミリ秒後に API が
「クライアントが切断したため LLM 呼び出しを取り消しました」を記録した。切断は生成の終盤だった可能性があり、生成の途中を長く切った試験ではない。
API が取り消し処理を実行したことまでの確認で、モデル提供元で生成・課金が止まったことは確認していない。
**同時接続:** 短い質問 2 件の同時送信で、どちらも 200 と `[DONE]` まで受信した（疎通の確認で、負荷の上限ではない）。
**未確認:** 中継自身の `: ping`（API が 10 秒ごとに送る `: ping` と区別できず、試していない）、同時接続の負荷の上限、本文サイズの上限（付けていない）、レート制限（無い）。
**同時接続の上限と、本番の状態（2026-10-25）:** 中継には同時に処理する `/chat/stream` の上限があり（`-max-streams` / 環境変数 `MAX_CONCURRENT_STREAMS`。コードの既定は 100、0 で無制限）、超えると 503（日本語の `detail`、`Retry-After: 5`、許可した Origin の CORS ヘッダー）を返し、上流には届かない。
**本番の中継（`docunavi-sse-relay`）は、イメージ `ac95150`（この上限のコードを含まない）のままで、`MAX_CONCURRENT_STREAMS` は適用していない**（利用者の決定。本番は Cloud Run の同時実行数 20 × 最大 2 台 = 40 本で、既定の 100 は届かず効果がない。理由と再検討の条件は [docs/go-architecture-plan.md](../../docs/go-architecture-plan.md) の §8）。このコードを入れた中継を本番へ配備する前に、上の判断を見直す。
**100 同時の試験（Cloud Run の試験用サービス、疑似の上流、各 1 回。2026-10-25）:** 100 本を保持でき、101 本目が 0.11 秒で 503 になり上流に届かず、25 本の切断が約 46 ms で上流に伝わった。メモリ・CPU、100 本の切断、実 API を通した試験は未実施。結果の詳細は [docs/go-architecture-plan.md](../../docs/go-architecture-plan.md) の §8。
**試験の道具:** 負荷ツール `bench/trial_load.py`（asyncio + httpx の HTTP/2。接続先は、ホスト名に `docunavi-sse-trial-` を含む試験用サービスかループバックだけ。URL は引数か `TRIAL_RELAY_URL` / `TRIAL_FAKE_URL`）と、LLM・DB・認証を持たない疑似の上流 `ops/sse-fake-upstream/`（`POST /chat/stream` と `/stats`）。使い方はツール先頭の説明にある。試験用のサービス・イメージ・サービスアカウントは試験後に削除したので、再試験には作り直しが要る。
**再試験・再配備の前提:** 試験用の中継は本番とは別名にし、最大台数 1・同時実行数は Go の上限より大きくする（台数が 2 以上だと 101 本目が別の台に回る。同じ値だと、101 本目は Go に届く前に Cloud Run が待たせる見込みだが、挙動は未確認）。本番の `docunavi-sse-relay` に負荷をかけない。作成・削除・デプロイは承認を取り、書き込みはユーザーが実行する。本番に上限を入れるなら、同時実行数と上限の関係（台ごとの上限になる点）、メモリの実測、503 のあとの画面の挙動を先に決める。
**ファイルの構成と規約（Claude Code 向け）:** [CLAUDE.md](CLAUDE.md)。実装は `main.go` ひとつ（設定・同時数の上限・ハンドラー・ping の書き込み・起動の区切りは CLAUDE.md の表）、テストは `main_test.go`（20 件）。疑似の上流 `ops/sse-fake-upstream/` は 11 件。どちらも CI には含まれない。
目的と測定の結果: [docs/target-architecture.md](../../docs/target-architecture.md) の「Go の SSE 中継の測定」。目的は 22〜31 秒の短縮ではない
（原因は検索と Gemini の初期処理で、中継では縮まらない）。長い接続の安定、切断時の上流の取り消し、同時接続が増えたときの備え。

- 中継するのは `POST /chat/stream`（と CORS の `OPTIONS`）だけ。ほかのパスは 404、ほかのメソッドは 405
- 本文は届いた分だけすぐ流す。応答に `X-Accel-Buffering: no` を付ける
- 本文が止まっている間は、`-heartbeat`（既定 25 秒）ごとに `: ping` を送る。イベントの途中には挟まない。0 で送らない
- クライアントが切れたら、上流（LLM 呼び出し）も取り消す。待ち受けは HTTP/1.1 と h2c の両方、上流へは `-upstream-h2c` で h2c
- `Server.WriteTimeout` は付けない。HTTP/1.1 では `EnableFullDuplex` を有効にする（上流が本文を読み終えてすぐ応答すると、中継から上流への接続が切られ、
  応答が途中で終わることがあった。約 5〜10%。`TestRelayDoesNotAbortWhenUpstreamRespondsEarly` が再現する）

```bash
.devcontainer/install-go.sh                      # Go 1.26.10（公式の SHA-256 を照合）。開発コンテナでは Dockerfile が入れ済みなので、手で入れ直すときだけ（版とハッシュは Dockerfile と同じ）
cd ops/sse-relay && go vet ./... && go test -race ./...
go build -o /tmp/sse-relay . && /tmp/sse-relay -listen 127.0.0.1:8200 -upstream http://127.0.0.1:8100 [-upstream-h2c] [-heartbeat 25s]
```

`bench/` は、`/chat/stream` に絞る前の中継（`GET /stream`）で測ったもの。いまの中継には、そのままでは使えない（測定の記録は target-architecture.md に残っている）。
戻し方: `ops/sse-relay/` と `.devcontainer/install-go.sh` を消し、Dockerfile の Go の節（`GO_VERSION`・`GO_SHA256_ARM64` を含む `RUN`）も消して、再ビルドする。
