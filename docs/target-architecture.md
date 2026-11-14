# 目標構成

この文書は、将来の構成の方向と、それぞれを採用する条件をまとめたものです。**実装の契約ではありません。** 今動いている構成は [architecture.md](architecture.md)、
段階の順序は [roadmap.md](roadmap.md) にあります。構成の判断は、測定か要件が出たとき、または利用者が学習のために採用すると決めたときに行い、先に終着点として固定しません。

## 目的

ドキュナビは、今の製品を作るだけでなく、**大規模なマルチテナントの AI SaaS に要る構成を、実際に足して動かして学ぶ練習も兼ねる**（利用者の位置づけ、2026-10）。
Go の SSE 中継、Django、gRPC、組織・権限・課金は、いまの製品には要らないが、「いつか足すかもしれない」で終わらせず、将来の終着点として学習と検証の対象にする。

- **今すぐ全部を実装するのではなく**、順番を決めて 1 つずつ足す。足す前に、何を学ぶか、どう測るか、戻し方を決める。
- 学ぶこと: SSE の接続の分離、AI・文書処理の分離、SaaS 管理の分離、サービス間の認証、スキーマの持ち主とマイグレーションの境界、マルチテナントのデータ分離、監視とトレース。
- 学習のために足すときも、本番の挙動と運用の負担は変わる。足す前に、利用者の承認を取る（CLAUDE.md）。

## 状態の区分

| 区分 | 意味 |
|---|---|
| 実装済み | 今のコードと本番にある |
| 決定（未実装） | 利用者が方針を決めたが、まだ入っていない |
| 候補 | 有望だが、採用していない |
| 条件付き | 下の「採用条件」を満たしたときだけ採用する |

証拠等級: A = 実機・実測で確認 / B = 公式の文書 / C = 第三者の記事・別の AI の回答 / 不明。

## 目標構成（2026-11-09 に改訂）

```
ブラウザー → Cloudflare Worker `docunavi-web`（Next.js の画面）
          → Cloud Run（FastAPI。`*.run.app`。`/chat/stream` は Go の中継 `docunavi-sse-relay` 経由）→ Prisma Postgres / R2
          → Worker `docunavi-api`（認証。Better Auth）
```

2026-10 に決めた「独自ドメインの `api.` を Cloudflare のプロキシにし、Worker を前段に置く」形は、2026-11-09 に廃止した（[decisions/0009](decisions/0009-drop-custom-domain-and-worker-front.md)）。上の図は、廃止後の、いま動いている入口の形。roadmap の段階 7 の「Vite のまま静的配信か」は、この決定に合わせて直した（2026-10-24）。

## 要素ごとの状態

| 要素 | 状態 | 根拠 | 採用の条件・備考 |
|---|---|---|---|
| Cloud Run の FastAPI | 実装済み | 本番で稼働 | - |
| Cloudflare R2（原本） | 実装済み | 本番で稼働 | - |
| Next.js（Worker `docunavi-web`） | 実装済み（2026-11-08 から） | 本番で稼働。Cloud Run は 2026-10-25 に API のみのリビジョンを promote 済み | 確認の結果と未確認の点は roadmap の① |
| Cloudflare Worker + プロキシ（`api.` の前段） | **廃止**（2026-11-09、利用者の決定。[decisions/0009](decisions/0009-drop-custom-domain-and-worker-front.md)） | 廃止前の測定: SSE の通過は測定済み（A、プローブ。下記） | 独自ドメインを使わないので置かない。画面の Worker `docunavi-web` と、認証の Worker `docunavi-api` は別物で、残る |
| Cloud Run の `--use-http2` + h2c 対応サーバー（hypercorn） | **本番に適用済み**（2026-10-25） | 切断の伝わりをプローブで測定（A）。本番（`docunavi-00016-ces`）で、ブラウザーの停止ボタンによる取り消しを確認済み（A。roadmap ②の追記） | 適用済み。旧リビジョン（HTTP/1.1）へ戻すと取り消しは効かなくなる |
| Hono | 候補 | 不明 | Worker のルートが増えたとき。今のプローブは素の Worker |
| Go の SSE 中継 | **本番に配備済み**（2026-10-25） | 開発コンテナ内の疑似 SSE 測定（A）を経て、Cloud Run（`docunavi-sse-relay`、h2c）へ配備。`/chat/stream` を中継 | 画面の `NEXT_PUBLIC_STREAM_BASE_URL` 経由で稼働中 |
| gRPC のサーバーストリーミング | 条件付き | 未測定 | 下記 |
| Django（組織・課金・管理画面） | **管理画面は 2026-10-26 に配備し、2026-11-08 に廃止した** | 学習用の試作（`ops/django-saas/`）を経て、運用者向けロール割り当て画面（`ops/access_admin/`）を非公開 Cloud Run（`docunavi-admin`）に配備 | 本番の SaaS 組織・課金管理機能としては未着手・条件待ち。運用者用管理画面は廃止済み（ロールの割り当ては CLI `python -m app.access_admin`） |
| Celery + Redis | 条件付き | 不明 | 下記。今の取り込みは Cloud Run の Job + Scheduler で足りている（A、本番で稼働） |
| LLM ゲートウェイ（LiteLLM Proxy、Bifrost、claude-code-router / CCR） | 非推奨・廃止（削除済み）。LiteLLM gateway is removed and no longer used. | アプリ内実装（ヘッジ・フォールバック・有料キー）で充足。開発時の AI 支援は Claude Code（VS Code 拡張）。Gemini CLI is not part of the docunavi runtime | 採用しない。外部プロキシやルーティング層は置かず、必要な耐障害性はアプリ（`app/agents/resilience.py`）で担保する |

## 測定済みのこと（プローブ。LLM・DB・秘密なし。2026-10-22〜24。証拠等級 A）

- SSE（最初の本文を 60 秒遅らせる）と 5 MB のバイナリは、Worker を通しても順に届く。Cloud Run の 90 秒で切れるまで、Free プランの Worker の制限に当たらなかった。
  Cloudflare の 125 秒（524）は、先に Cloud Run が切るので未測定。
- 切断: Cloud Run の既定（コンテナまで HTTP/1.1。uvicorn）では、クライアントを 3 秒で切っても、コンテナの処理は最後まで走った（4/4。直接も Worker 経由も同じ）。
  `--use-http2`（ポート名 `h2c`）+ hypercorn では、6/6 で伝わった（直接も Worker 経由も）。
- 含意: 本番の `docunavi` は、HTTP/1.1 のリビジョンでは、停止やタブを閉じたときに LLM 呼び出しを取り消していなかった（2026-10-24 の実測。roadmap ②）。その後、本番を h2c + hypercorn（`docunavi-00016-ces`）にして、取り消しを本番で確認した（2026-10-25。証拠等級 A）。
- 測定のコードは測定後に削除した（Git には入れていない）。

## Go の SSE 中継の測定（学習用。2026-10-25。証拠等級 A）

利用者の決定（学習目的。「目的」の節）で、`ops/sse-relay/` に Go（1.26.10、標準ライブラリだけ）の SSE 中継を作って測った。製品のコード・本番・Cloud Run・Compose・CI は変えていない。
Go は `.devcontainer/Dockerfile` のイメージに入れてある（公式の SHA-256 を照合。以前は `.devcontainer/install-go.sh` を `postCreateCommand` から呼んでいて、作り直すたびに入れ直していた。`install-go.sh` は残してあり、版とハッシュが Dockerfile と重複する）。単体テスト 4 件（逐次の中継、`Authorization` の受け渡し、切断の伝播。上流が HTTP/1.1 と h2c の両方）が通る。

- **条件:** 開発コンテナの 127.0.0.1 だけ（外部通信なし・課金なし）。上流は `ops/sse-relay/bench/sse_upstream.py`（FastAPI の `StreamingResponse` で、1 秒ごとに 20 秒間送る疑似 SSE。LLM・DB なし）。
  クライアントは 1 つの Python プロセス（HTTP/1.1、`Connection: close`）。4 構成: 直接の uvicorn、中継 → uvicorn（HTTP/1.1）、直接の hypercorn、中継 → hypercorn（h2c）。再現: `ops/sse-relay/bench/bench.py`。
- **同時接続（1000 本、全構成でエラー 0・全本完了）:**

| 構成 | 最初のバイトまで p50 / p99 | イベントの遅延 p99 | 上流のメモリ（最大）/ CPU | 中継のメモリ（最大）/ CPU |
|---|---|---|---|---|
| 直接 uvicorn | 102 / 112 ms | 3.9 ms | 80 MB / 0.70 s | - |
| 中継 → uvicorn | 99 / 159 ms | 4.7 ms | 79 MB / 0.79 s | 94 MB / 0.58 s |
| 直接 hypercorn | 1062 / 1536 ms | 3.4 ms | 125 MB / 1.31 s | - |
| 中継 → hypercorn（h2c） | 152 / 188 ms | 15.7 ms | 122 MB / 1.62 s | 85 MB / 0.50 s |

- **切断が上流に届くまで**（20 本を 2 秒で RST で切る。全構成 20/20 本が届いた）: 直接 uvicorn 1.7 ms、中継 → uvicorn 2.4〜2.5 ms、直接 hypercorn 0.9 ms、中継 → hypercorn（h2c）2.7〜2.9 ms。中継は約 1〜2 ms 足す。HTTP/1.1 でも h2c でも伝わる。
- **低負荷（20 本）の遅延:** イベントの遅延は p99 で、直接 1.4〜1.9 ms、中継 1.9〜3.9 ms。最初のバイトまでは、直接 9〜20 ms、中継 34〜35 ms。
- **本物のアプリ:** `tests/e2e_server.py`（偽の LLM）の `/chat/stream` は、中継越しで `start` から `[DONE]` まで順に届いた（モックは即答なので、逐次の効果までは見えない）。本物のアプリの切断の取り消しは、中継越しでは測っていない（偽の LLM が待たないため）。
- **読み取り:** この測定では、Go の中継は SSE の遅延にはほとんど足されない（ミリ秒）。切断も確実に伝わる。直接の hypercorn は 500 本を超えると接続の受け付けが遅くなったが（最初のバイトまで 1 秒前後）、中継が上流への接続を h2c の多重化にまとめると 150 ms 台に収まった。一方で中継自体が 85〜94 MB を使う（上流と同じ桁）ので、メモリの節約にはならず、プロセスが 1 つ増える。
- **限界（結論を広げない）:** 1 台のマシンの loopback で、クライアントも同じ CPU を使う（1000 本のとき、遅延と最初のバイトまでにはクライアントの影響が含まれる）。メモリは各プロセスの最大値で、同じ上流を 100 → 500 → 1000 本と続けて測っている（段階ごとの純粋な値ではない）。1 回ずつの測定で、分散は見ていない。
  疑似 SSE で、本物の LLM・DB・認証・Cloud Run の前段（HTTP/2 → HTTP/1.1 の変換）・Cloudflare Worker は含まない。クライアント → 中継は HTTP/1.1 で、本番のブラウザー（HTTP/2）とは違う。1000 本を超える数・長時間（数分以上）・障害時（上流が落ちたとき）は未測定。

## Django の測定（学習用。2026-10-25。証拠等級 A）

製品には入れていない。組織・メンバー・役割・プランの管理の土台を Django で作ると何が楽で何が変わらないかを、開発コンテナの中だけで測った。コードと再現手順は `ops/django-saas/`（README）。
条件: Django 5.2.18（LTS）、PyJWT 2.15.1、psycopg 3.3.6、一時の Postgres 17。JWT は使い捨ての鍵で署名した疑似のもので（当時の認証は Supabase。実鍵・実ログイン・外部通信なし）、`chatbot` スキーマは本番を簡略にした身代わり。1 台・ループバック・各 1 回（JWT の検証だけ 3 回）。

| 測ったこと | 結果 |
|---|---|
| 作る手間 | API の層は、Django と FastAPI + 手書き SQL でほとんど変わらない（FastAPI 側は**見積もりで、実装していない。証拠等級 C**）。差が出るのは、**管理画面（`admin.py` 20 行で一式）とマイグレーションの自動生成**。管理画面が要らなければ、Django を足す理由は薄い |
| マイグレーションの境界 | `saas` スキーマだけの所有者で `migrate` すると、`chatbot` の列・制約・索引・権限・行数は完全に不変。`chatbot` と `public` への操作は権限エラー（42501）で拒否された |
| JWT の検証 | 2 つとも約 70 µs（Django 69.0〜69.4 µs、`app/auth.py` 72.8〜74.1 µs。p50）。ほとんど ES256 の署名検証で、差は実質ない。拒否の 8 種は同じ |
| `sub` から Membership を引く 1 リクエスト | プロセス内で 0.41〜0.65 ms（p50）。検証はその 2 割弱で、残りは DB とフレームワーク |

見つけたこと:
- **Django の標準のユーザー表は消せない。** 管理画面は `django.contrib.auth` に依存するので、運用者のログインは利用者の認証と別になる。
- **Django のテストは `public` 以外のスキーマを知らない。** テスト用 DB の作成にテンプレート DB と DB を作れるロールが要り、権限が `saas` だけの本番用ロールではテストが動かない。
- 入っていない組織は、存在を漏らさないため 403 ではなく 404 にした。

測っていないこと: 実際の認証基盤の鍵（JWKS）の取得・更新、課金、マルチテナントの分け方の比較、`chatbot` の実物のスキーマ（pgvector / pgroonga）との同居、本番の Cloud Run・WSGI/ASGI サーバーでの性能、大量のデータ、長期の運用の負担。

## 採用条件

次の条件は、**製品の必要から採用する場合**の基準です。「目的」にあるとおり、**学習のために採用する場合**は、利用者が決めたときに採用できます（上の条件を満たさなくてもよい）。
その場合も、足す前に、学ぶこと・測ること・戻し方を決めて、承認を取ります。

### Go の SSE 中継

次のどれかを満たしたとき。
- 同時の SSE 接続数が、FastAPI の測定した容量を超える。
- SSE の p99 の遅延が、製品の要件になる。
- SSE の接続のせいで、Cloud Run のメモリやインスタンス数が増える。
- AI 処理の失敗が、SSE の可用性に影響し始める。

### gRPC のサーバーストリーミング

Go の SSE 中継を採用し、かつ Go と Python の間でサーバーストリーミングが要るとき。

### Django

（学習用の測定の結果は、上の「Django の測定」。）

次のどれかが実際の要件になったとき。組織、課金・有料プラン、役割ごとの権限、運用の管理画面（SQL と DB の標準ツールで足りなくなったとき）。

入れるときの前提:
- 認証の窓口は Better Auth（Worker `docunavi-api`）のまま。Django は JWT の `sub` を外部 ID として扱い、自前のユーザー表・セッション認証は使わない（二重管理になる）。
- 同じ Postgres を共有するときは、スキーマごとにマイグレーションの持ち主を分ける（`chatbot` は今のリポジトリ、新しい `saas` などは Django 側）。

例外（2026-10-26 に決定）: **運用者 1 人専用の管理画面に限り、Django 自身の運用者ログイン（`django.contrib.auth`）を使う。**
- 認証基盤との連携はしない（管理画面を利用者のログインに繋がない）。運用者のログインは利用者の認証と別になることを承知の上での例外。
- 利用者向けの画面・チャット・検索は、Django に依存させない。利用者は今までどおり Better Auth だけで入る。
- Django が止まっても、利用者向けの機能は動き続ける。
- 運用者が 2 人以上になる、または利用者が管理画面に入る場合は、この例外の対象外。そのときは、上の前提（認証の窓口は Better Auth）に戻って決め直す。

### Celery + Redis（または Pub/Sub）

取り込みや埋め込みが長時間になり、再試行・優先度・並行数の制御が要るか、API と別にスケールさせたいとき。

### LLM ゲートウェイ（LiteLLM Proxy、Bifrost、claude-code-router / CCR）

**非推奨・廃止（削除済み）。LiteLLM gateway is removed and no longer used.**
以前検討された LiteLLM Proxy、Bifrost、claude-code-router（CCR）等の外部 LLM ゲートウェイやプロキシレイヤーは採用せず、非推奨・廃止（削除済み）とした。
- アプリ本体側で、ヘッジ付き再試行・フォールバック（`gemini-3.5-flash-lite`）・有料キー切り替え（`app/agents/resilience.py`）を内製・完結しており、外部ゲートウェイを挟むオーバーヘッドや障害点を排除する。
- 開発環境でも、中間ルーティングプロキシは使わない。開発時の AI 支援は Claude Code（VS Code 拡張）で、Gemini CLI is not part of the docunavi runtime（開発コンテナにも入れない）。
- Cloud Code・Gemini Code Assist・Data Agent Kit・Jupyter 系の VS Code 拡張は導入しない。GCP の操作は原則 gcloud CLI で行う（[README.md](../README.md) の「開発環境の方針」）。
- 画像生成の Cloud Run Job は、星庭ジョブ（`HoshiniwaJob`）のリポジトリで管理する。このリポジトリに `job/` は置かない。

## やらないこと

- 新しいというだけで、gRPC・Celery・Redis・LiteLLM / Bifrost / CCR を入れない（LiteLLM Proxy・Bifrost・claude-code-router などの外部 LLM ゲートウェイは廃止・非推奨）。入れるのは、製品の必要か、利用者が決めた学習の目的があるときだけ。
- 測定・要件・学習の目的のどれも無いまま、サービスを分けない。
- Better Auth と並べて、Django の認証を入れない（運用者 1 人専用の管理画面だけが例外。上の「Django」の「例外」）。
- `chatbot` スキーマのマイグレーションの持ち主を二重にしない。
