---
paths:
  - "ops/sse-relay/**"
  - "ops/sse-fake-upstream/**"
---

# Go の SSE 中継（ops/sse-relay/）を触るとき

詳細は [ops/sse-relay/CLAUDE.md](../../ops/sse-relay/CLAUDE.md)。本番の画面の `/chat/stream` が通る。標準ライブラリだけ。

- 中継するのは `POST /chat/stream` と `OPTIONS` だけ。パス・メソッド・認証・ビジネスロジックを足さない。`Authorization` は渡すだけで、ここで検証しない
- 本文はバッファーしない（`FlushInterval = -1`）。`Server.WriteTimeout` を付けない。`: ping` はイベントの途中に挟まない
- クライアントの切断を上流へ伝える動作と、`EnableFullDuplex` を壊さない
- 環境変数は `get func(string) string` 経由で読む。ログに本文・`Authorization`・質問文を出さない
- CORS は `CORS_ORIGINS` の完全一致だけ。`*` を許さない。上限の 503 の日本語の `detail` を変えるときは、画面（`frontend/src/api/chat.ts`）も確かめる
- ファイルを増やさない（`Dockerfile` は `main.go` だけを `COPY` する）。分割・Go の版の変更は承認を得る。外部モジュールを足さない
- デプロイしない（本番の中継は古いイメージのまま。配備は承認を得てユーザーが実行する）。`gcloud`・`gh` の書き込みはしない
- 触れたら、`ops/sse-relay` と `ops/sse-fake-upstream` のそれぞれで `go vet ./... && go test -race -count=1 ./...`。CI には含まれない
