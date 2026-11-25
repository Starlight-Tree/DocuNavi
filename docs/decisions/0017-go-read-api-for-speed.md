# 0017 読み取り API を Go のサービスに移す（速度を最優先にする）

- 状態: 提案（2026-11-25 起案。コード・設定には何も入っていない。「決定済み」は利用者の承認があるもの、「決定（案）」は推奨を 1 つ示したもので利用者の判断待ち、「未決」は利用者の判断か計測が要るもの）
- 日付と決めた人: 2026-11-25。利用者が、(1) 目的は速度で、最優先（学習・ポートフォリオの目的より上。「十分速くても、限界まで速くする」）、(2) API を Go へ移す、を決めた。**「決定（案）」の各項は、利用者がまだ承認していない。**
- 関係: [go-architecture-plan.md](../go-architecture-plan.md)（役割ごとの分解。この ADR は、その §3 の「今は決めない」を、速度の目的で決めに進めるもの）、[0005](0005-go-sse-relay-without-prod-limit.md)（Go の SSE 中継）、[0011](0011-db-connection-pool-limits.md)（接続プール）、[0012](0012-accept-prisma-free-limit-and-cut-operations.md)（Prisma の操作数）、[0016](0016-auth-bff-in-web-worker.md)（認証。この ADR は認証の方式を変えない）

## 背景

- 計画書 §0 は「Go の方が速いとは言えない」と書いていた（この製品で測っていないため）。今回の測定（下の「測ったこと」）で、**読み取り系の応答は、Python の処理そのものではなく、DB への往復・転送量・コールドスタートで決まっている**ことが分かった。Go に移して削れるのは、プロセスの起動と 1 要求ごとの処理の部分で、DB への往復は変わらない。
- それでも、利用者の方針は「限界まで速く」。そのため、順序を「効く順」にし、Go 化は**測って比べながら 1 経路ずつ**進める。

## 測ったこと（日付・条件つき）

| 何を | 結果 | 等級 |
|---|---|---|
| 本番の API のリクエスト遅延（Cloud Run のログ。2026-11-04〜16、直近 1,500 件の標本。コールドスタートを含む） | `/threads` p50 782 ms・p90 999 ms（n=260）。`/collections` `/targets` `/doc-types` p50 約 58 ms・p90 約 500 ms（各 n=130）。`/documents` p50 64 ms・p90 313 ms（n=99）。`/documents/{id}/file` p50 308 ms（n=57）。`/health` と `OPTIONS` の最大が 10〜20 秒。`/chat/stream` p50 9.3 s・p90 36.6 s（n=56） | A（読み取り。標本は偏りうる） |
| Cloud Run の設定（2026-11-25、`gcloud run services describe`。読み取り） | `docunavi`: 最小台数 0、最大 2、同時実行 20、CPU スロットリングあり、1 vCPU / 512 MiB、起動 CPU ブースト有効 | A |
| ローカルの Python API（loopback、ローカルの Postgres、認証 off、直列、各 300 回。2026-11-25） | `/health` 0.09 ms、`/targets` `/doc-types` `/collections` 約 0.9 ms、`/documents`（33 KB）2.2 ms、`/documents/1/chunks`（14 KB）2.0 ms（p50） | A（ローカル。本番の往復・JWT・ネットワークを含まない） |
| `/threads` の一覧のローカル（26 会話。同日） | SQL 2.2 ms＋全チェックポイントの復号 1.3 ms＝3.5 ms（p50）。本番の 782 ms は、この復号の CPU では説明できない（転送量・往復・SQL の全件走査が候補。**推定**） | A（ローカル）／推定（本番） |
| 認証の Worker の要求ごとのスキーマ検査（同日。Node、数える偽の DB） | 新しいインスタンスごとに DB 問い合わせ 2 本 → `validateSchema: false` で 0 本（`8053bd8`）。`betterAuth()` の組み立て自体は 0.05 ms | A（実 DB・Workers 上は未確認） |

限界: 本番の測定は 2026-11-04〜16 のもので、本番は Prisma の上限で止まっている（12 月まで）ので、いまは測り直せない。ローカルの測定は、本番より速い条件（同じ機械の DB、認証 off）。

## 決定（案）

### 1. 先にやること（言語に依らない。Go 化の前に効く）

1. `/threads` の題名を保存時に `checkpoints.metadata` へ入れ、一覧が本体を取らない形にする（**実装済み・`59074e2`**）。
2. 認証 Worker のスキーマ検査を止める（**実装済み・`8053bd8`**）。
3. 最小台数 1（`docunavi`）と、起動時に DB の接続・JWKS を温める。**本番の書き込みなので、利用者の実行・別承認。** 本番再開後に、前後を測る。

### 2. Go のサービス（案）

- **新しい Cloud Run サービス**（仮の名前 `docunavi-read`。リージョン `asia-northeast1`）。`ops/` 配下の Go（標準ライブラリを先に使う。[ops/sse-relay/CLAUDE.md](../../ops/sse-relay/CLAUDE.md) の流儀）。**新しいサービス・言語を足す件は、CLAUDE.md の決まりどおり、利用者の承認を得る**（この ADR の承認が、それを兼ねる）。
- **入口は gateway にしない。** 画面が読み取り系だけを、この Go のサービスへ直接呼ぶ（`/chat/stream` を中継へ向ける `NEXT_PUBLIC_STREAM_BASE_URL` と同じ、経路ごとの向き先）。理由: gateway にすると、1 回の中継が足され（測定で 1〜2 ms）、止まったときに全部が止まる（計画書 §5 の 2）。**未確認:** 画面の API 呼び出しが、経路ごとに向き先を切り替えられる形か（`frontend/src/api/` を読んで確かめる）。
- **認証は Go でも検証する。** Go のサービスは公開（Cloud Run の `*.run.app`）なので、JWT を自分で検証する（JWKS の ES256/RS256、`exp` `iss` `aud` `sub` 必須。`app/auth.py` と同じ規則）。Python 側の検証は外さない（直接来る経路が残るため。計画書 §5 の 1）。標準ライブラリ（`crypto/ecdsa` `crypto/rsa`）で足りる見込み。1 回の検証は約 70 µs（Django 試作の測定。計画書 §5）。
- **DB は同じ Postgres。** 読み取り専用のロール（`database-url-readonly` と同じ考え）で接続し、接続数は [0011](0011-db-connection-pool-limits.md) の枠に収める。**Prisma の操作数（[0012](0012-accept-prisma-free-limit-and-cut-operations.md)）を増やさない**（同じ問い合わせを移すだけ。資料の範囲の確認と一覧を 1 本にまとめられれば減る）。
- **新しい依存は Postgres のドライバーだけ**（`github.com/jackc/pgx/v5` が候補。標準ライブラリには無い）。**追加は、実装の前に別の承認を得る。** R2 の読み出し（段 C）は、署名（SigV4）を標準ライブラリで書くか、依存を足すかを、その段で決める。

### 3. 移す順（1 経路ずつ。旧実装と並べて比べる）

| 段 | 経路 | 前提・注意 |
|---|---|---|
| A | `/health` `/collections` `/targets` `/doc-types` `/documents` `/documents/{id}/chunks` | SQL とアクセス制御（`app/access.py` 499 行・`app/scope.py`）の移植。**アクセス制御は認可の要で、本番は `enforce`。** Python と同じ入力に同じ出力が出ることを、契約テストで確かめる（下の「検証」）。これが最大のリスク |
| B | `/threads`（一覧だけ） | 題名が `metadata` にある行だけを返せる状態にする。**前提:** 古い行の題名を埋める（`python -m app.history_admin` に題名の補完を足す。本番の DB への書き込みなので、別承認）。Go は msgpack のチェックポイントを復号しない |
| C | `/documents/{id}/file` `/documents/{id}/hero`（R2 の素通し） | 現状 p50 308 ms（n=57）。範囲（Range）・`Content-Disposition`・ETag の扱いを Python と同じにする |
| 移さない | `/chat` `/chat/stream`、`/threads/{id}/messages`、`DELETE /threads/{id}`、`/documents/{id}/graph` `/impact`、取り込み | LangGraph・Gemini・検索・PDF 解析・msgpack の復号は Python に残す（計画書 §3）。`DELETE` は書き込みなので、読み取り専用のロールの Go には載せない |

### 4. 採否の基準（速度が目的なので、測って決める）

- 各段で、**同じ DB・同じ入力**で Python と Go を並べて測る（p50・p90・p99、同時 1 と同時 20、コールドスタートを別に）。**Go が同等以上に速いことを確かめてから**、画面の向き先を切り替える。
- 切り替えは経路単位で、画面の向き先の値を戻すだけで元に戻せる（Python のルートは、一致が確かめられるまで消さない）。
- 本番の効果は、再開後に、本番ログの同じ指標（Cloud Run のリクエスト遅延）で前後を比べる。

## 検証（案）

- **契約テスト:** `tests/test_documents.py` `test_collections.py` `test_threads.py` `test_access.py` などが仕様。同じ HTTP の契約を、Go のサービスにも流す（言語に依らない形にして、両方に当てる）。応答の JSON・ステータス・ヘッダー・エラーの固定文が一致すること。
- **Go:** `go vet` と `go test -race -count=1`（CI の `go` ジョブに足す）。
- **アクセス制御:** ロール・グループ・対象・`document_ids` の組み合わせを、Python と Go の両方に流して、一致を確かめる（`ACCESS_MODE=off` / `shadow` / `enforce`）。

## 結果と限界（未確認）

- Go にした読み取りの速さ。**1 本も測っていない。** ローカルの Python が 0.9〜2.2 ms なので、削れる量は小さい見込み。効くとすれば、コールドスタート（Go は数百 ms 以下の見込みだが**未測定**）と、1 要求ごとの余計な処理。
- 本番の遅さの主因。`/threads` の 782 ms の原因は推定のまま（`59074e2` の効果は、本番再開後に測る）。
- 画面が経路ごとに向き先を切り替えられるか。Cloud Run のサービスを足したときの課金・同時実行数・最大台数の設計。R2 の読み出しの実装方法。

## 未決（利用者の判断）

1. この案（読み取り系だけを、画面から直接 Go へ。チャットと解析は Python）でよいか。
2. 段 A の前に、`pgx` の追加を承認するか。
3. 古い会話の題名の補完（段 B の前提。本番の DB への書き込み）を、本番再開後にやるか。
4. サービス名と、Cloud Run の設定（最小台数 0 か 1 か。Go の起動を測ってから決めてよいか）。

## 出典

[go-architecture-plan.md](../go-architecture-plan.md) の §2・§3・§5・§6、[target-architecture.md](../target-architecture.md)、[0005](0005-go-sse-relay-without-prod-limit.md)、`app/api/threads.py`・`app/thread_title.py`・`app/d1_saver.py`、`workers/api/src/auth.ts`。測定は 2026-11-25（本番ログの読み取り、`gcloud run services describe` の読み取り、開発コンテナのローカル測定）。
