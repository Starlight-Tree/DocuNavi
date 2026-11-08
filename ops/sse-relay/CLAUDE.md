# ops/sse-relay/（Go の SSE 中継）

全体の方針・禁止事項は、ルートの [CLAUDE.md](../../CLAUDE.md)。状態・本番の測定・再配備の前提は [README.md](README.md)。Go を採った理由と条件は `docs/target-architecture.md` の「Go の SSE 中継」と `docs/go-architecture-plan.md`。

画面（Worker `docunavi-web`）の `POST /chat/stream` を FastAPI へ中継するだけの小さなサービス。標準ライブラリだけ。**Cloud Run `docunavi-sse-relay` に配備済みで、本番の画面はここを通る。** 新しいパス・認証・ビジネスロジックを足さない。

## 構成（`main.go` ひとつ。分けない）

`Dockerfile` が `go.mod` と `main.go` だけを `COPY` する。ファイルを増やすと配備物が変わるので、分割は事前に承認を得る（Dockerfile の `COPY` も同じ変更で直す）。境界は次の区切りで保つ。

| 区切り | 場所（`main.go`） | 責務 |
|---|---|---|
| 設定 | 定数、`maxStreamsFromEnv` `corsOriginsFromEnv` `maxBodyFromEnv` `rateFromEnv`、`main` のフラグ | フラグ・環境変数（`MAX_CONCURRENT_STREAMS` `CORS_ORIGINS` `SSE_MAX_BODY_BYTES` `SSE_RATE_LIMIT_PER_MIN` `SSE_RATE_LIMIT_BURST` `SSE_RATE_LIMIT_TRUST_XFF`）の読み取りと検証。**`get func(string) string` を受け取り、`os.Getenv` を直接呼ばない**（テストで差し替える） |
| 同時数の上限 | `streamLimiter`（`tryAcquire` `release` `reject`） | 原子的な加算で上限を守る。断る 503 の本文・`Retry-After`・CORS。枠は必ず戻す |
| レート制限 | `rateLimiter`（`admit` `take`）、`clientIP` | 新しい接続の開始だけを数える（ping・開いている接続は対象外）。キーはクライアント IP だけ（既定は接続元 `RemoteAddr`。`SSE_RATE_LIMIT_TRUST_XFF=true` のときだけ `X-Forwarded-For` の最後の値。Authorization は見ない）。超えたら 429（日本語の固定文・`Retry-After`）。キー数の上限と掃除がある |
| ハンドラー | `newRelayWith`（`newRelay` は上限なしの薄い包み） | 経路（`POST`/`OPTIONS /chat/stream` だけ）、`ReverseProxy`、切断の伝播、`EnableFullDuplex`。POST の順序は、レート制限（429）→ `Content-Length` 超過（413）→ 同時数（503）→ 本文を上限つきで読み切る（超過は 413。上流には何も送らない） |
| 書き込み | `heartbeatWriter` | 本文が止まっている間の `: ping`。**イベントの途中には挟まない**。書き込みと Flush のたびに期限（定数 `defaultWriteStall` = 30 秒。フラグ・環境変数は無い）を付け、下流が読まずに詰まったら接続を切る（上流も取り消される。中継のバッファーは増やさない） |
| 起動 | `main` | 待ち受け（HTTP/1.1 と h2c）、`ReadHeaderTimeout`。`WriteTimeout` は付けない |
| ログ | `log.New(os.Stderr, "relay: ", …)` と `streamLimiter.logf` | **リクエストの本文・Authorization・質問文を出さない**。件数と原因の種類だけ。クライアントの切断（`context.Canceled`）はログに出さない |
| テスト | `main_test.go`（34 件） | 偽の上流（`httptest`）で、流れ方・切断・ping・同時数の上限・CORS・設定・本文の上限・レート制限・即時の転送・詰まった下流を確かめる |

## 守ること

- **中継するのは `POST /chat/stream` と、その `OPTIONS` だけ。** ほかのパスは 404、ほかのメソッドは 405。
- **認証を持たない。** `Authorization` はそのまま渡し、検証は上流（FastAPI）が行う。ここでヘッダーを見て分岐しない（レート制限の `X-Forwarded-For` は、接続元の IP を得るためだけで、既定では読まない）。
- **本文は届いた分だけすぐ流す**（`FlushInterval = -1`）。バッファーしない。`Server.WriteTimeout` を付けない（代わりに、書き込みごとの期限 `defaultWriteStall`）。
- **要求の本文は 1 MiB まで**（`SSE_MAX_BODY_BYTES`。0 で無制限）。**レート制限は新しい接続の開始だけ**（既定 60 回/分・バースト 10。`SSE_RATE_LIMIT_PER_MIN=0` で無効）。断る 413・429 の `detail` は日本語の固定文（画面が見せる）。
- **クライアントが切れたら上流も取り消す。** この伝播を壊さない（`TestRelayPropagatesClientDisconnect`、`TestRelayAcceptsH2CAndPropagatesCancel`）。
- **CORS の許可は `CORS_ORIGINS` の完全一致だけ。** `*` を入れない（入れさせない検証がある）。`Allow-Credentials` を付けない。
- **上限の 503 の `detail` は日本語の固定文。** 画面（`frontend/src/api/chat.ts` の `SERVER_DETAIL_STATUSES`）がそのまま見せる。文面や状態を変えるときは画面も確認する。
- 本番の中継は、上限のコードを含まない古いイメージのまま（README）。コードを変えても、**デプロイしない**。本文の上限・レート制限・書き込みの期限（④）も、承認前は未配備。配備は承認を得て、ユーザーが実行する。
- `bench/` は、絞り込む前の中継（`GET /stream`）で測ったもの。いまの中継にはそのままでは使えない。
- Go の版は 1.26.10 で揃えている（開発コンテナ `.devcontainer/Dockerfile` と `install-go.sh`、`go.mod` 2 つ、`Dockerfile` 2 つの `golang:1.26.10`。CI は `go.mod` から読む。2026-11-01 に、Dockerfile だけ `golang:1.27.2` だった食い違いを解消）。上げ下げするときは、承認を得て、これらを一度に合わせる（開発コンテナのハッシュと再ビルドはホストの作業）。

## 疑似の上流（`ops/sse-fake-upstream/`）

LLM・DB・認証を持たない負荷試験用の上流。`POST /chat/stream`（シナリオはクエリで指定）と `/stats` `/health` だけ。製品の経路には入らない。使い方は `main.go` の先頭のコメント。テスト 11 件。

## テスト

CI（`.github/workflows/ci.yml` の `go` ジョブ）が、両方で `go vet` と `go test -race` を実行する。変更したら、それぞれのディレクトリーでも実行する。実 API・外部通信は使わない（`httptest` とループバックだけ）。

```bash
cd ops/sse-relay && go vet ./... && go test -race -count=1 ./...
cd ../sse-fake-upstream && go vet ./... && go test -race -count=1 ./...
```

2026-11-01 に上の 2 組が通ることを確かめた（Go 1.26.10）。
