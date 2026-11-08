# 0009 独自ドメインと、Cloudflare Worker の前段（段階 3・③）を廃止する

- 状態: 決定（文書の更新だけ。コードと本番の設定は変えていない）
- 日付と決めた人: 2026-11-09（利用者）

## 背景
段階 3「Cloudflare の前段（独自ドメイン・レート制限）」と、実装順序 ③「Cloudflare Worker（`api.` → Worker → Cloud Run）」は、独自ドメインの取得待ちのまま残っていた（[roadmap.md](../roadmap.md)）。[target-architecture.md](../target-architecture.md) では「決定（未実装）」だった。

## 決定
- 独自ドメインの取得待ちをやめる。独自ドメインは使わない。
- 段階 3 と ③ を廃止する。
- `api.` の前に Cloudflare の Worker とプロキシを置く計画を廃止する。
- 残るもの（廃止の対象ではない）: 画面の Worker `docunavi-web`、認証の Worker `docunavi-api`（Better Auth）。入口は `*.run.app` と Worker `docunavi-web` のまま（2026-11-08 の決定どおり）。

## 結果と限界
- 廃止の理由の詳細は、利用者から聞いていないので記録しない。
- Worker 前段で得るはずだったレート制限は、SSE 中継（`/chat/stream` だけ。[0005](0005-go-sse-relay-without-prod-limit.md)）にある分だけになる。`/chat` などほかの経路の制限は、今の設計には無い（この決定では足さない）。
- 前段の Worker の秘密ヘッダーで `*.run.app` への直接のアクセスを防ぐ案（[target-architecture.md](../target-architecture.md)）も、前段を置かない以上、この決定では実現しない。
- [target-architecture.md](../target-architecture.md)（構成図・「段階 7 の前に決めること」の当時の記録）と [go-architecture-plan.md](../go-architecture-plan.md)（タイムアウトの鎖）にも、廃止の注記を入れた（2026-11-09）。

## 出典
利用者の指示（2026-11-09）、[roadmap.md](../roadmap.md)、[target-architecture.md](../target-architecture.md)
