# 0005 Go の SSE 中継は配備し、本番には同時数の上限を入れない

- 状態: 配備済み（旧イメージ）。上限のコードは実装済み・未配備
- 日付と決めた人: 2026-10-25（利用者。学習のための採用と、上限を適用しない判断「案 C」）

## 背景
`/chat/stream` の長い SSE 接続の安定、切断時の上流の取り消し、同時接続が増えたときの備えを、Go の小さな中継で試す。製品の必要というより、学習と検証が目的（22〜31 秒かかる最初のチャンクまでの遅延は、中継では縮まらない）。

## 決定
- Go の SSE 中継（`ops/sse-relay/`、標準ライブラリだけ）を Cloud Run `docunavi-sse-relay` に配備し、本番の画面の `/chat/stream` はここを通す（`NEXT_PUBLIC_STREAM_BASE_URL`）。
- 中継するのは `POST /chat/stream` だけ。認証は持たず、検証は上流（FastAPI）が行う。
- 中継のコードには同時数の上限（`MAX_CONCURRENT_STREAMS`、既定 100）があるが、**本番には適用しない**。本番は Cloud Run の同時実行数 20 × 最大 2 台 = 40 本で、100 の上限は届かず効果がないため。
- 本番へ新しいコードを配備するときは、本文上限 1 MiB とレート制限 60/分・バースト 10 を既定で有効にする（2026-11-09、利用者の決定）。レート制限のキーはクライアント IP だけ（Authorization は見ない）。
- 配備は、別の承認で行う。

## 結果と限界
- 開発コンテナの疑似 SSE で 1000 本の同時接続がエラー 0、中継が足す遅延は数 ms（証拠等級 A、各 1 回）。Cloud Run の試験用で 100 本を保持し、101 本目が 503（各 1 回）。
- 本番で確認済み: 実質問が中継経由で 200（3.0 秒）、切断の 21 ms 後に API が取り消しを記録（各 1 回）。
- 未確認: 中継自身の `: ping`、Cloud Run での中継のメモリ・CPU、実 API を通した負荷。Go の効果（この製品で効くか）は測っていない。
- 本番へ上限のコードを入れた中継を配備する前に、この判断を見直す。

## 出典
[go-architecture-plan.md](../go-architecture-plan.md) の §8、[target-architecture.md](../target-architecture.md) の「Go の SSE 中継の測定」、[ops/sse-relay/README.md](../../ops/sse-relay/README.md)、[ops/sse-relay/CLAUDE.md](../../ops/sse-relay/CLAUDE.md)
