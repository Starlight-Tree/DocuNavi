# SSE 中継（学習用）

Go（標準ライブラリだけ）で作った、FastAPI の前に置く SSE の中継。製品には入れていない（本番・Cloud Run・Compose・CI は変えていない）。
目的と測定の結果: [docs/target-architecture.md](../../docs/target-architecture.md) の「Go の SSE 中継の測定」。

```bash
.devcontainer/install-go.sh                      # Go 1.26.10（公式の SHA-256 を照合。コンテナを作り直すと消える）
cd ops/sse-relay && go test ./...                # 単体テスト（逐次の中継・ヘッダーの受け渡し・切断の伝播。上流 h1 と h2c）
go build -o /tmp/sse-relay . && /tmp/sse-relay -listen 127.0.0.1:8200 -upstream http://127.0.0.1:8100 [-upstream-h2c]
../../.venv/bin/python bench/bench.py --relay-bin /tmp/sse-relay   # 測定（約 6 分。127.0.0.1 だけ、外部通信なし）
```

`bench/` の測定は `hypercorn`（`requirements.txt` と同じ 0.18.0）を使う。戻し方: `ops/sse-relay/` と `.devcontainer/install-go.sh` を消し、`sudo rm -rf /usr/local/go /usr/local/bin/go /usr/local/bin/gofmt`。
