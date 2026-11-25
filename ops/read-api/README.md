# ops/read-api/（読み取り API。Go）

資料グループ・対象・資料種別の一覧（`GET /collections` `/targets` `/doc-types`）と `GET /health` だけを返す、読み取り専用の Go のサービス。
画面が直接呼ぶ（gateway にしない）。**未配備**（2026-11-25 時点。Cloud Run には無く、画面もまだ向けていない）。決定と範囲は [ADR 0017](../../docs/decisions/0017-go-read-api-for-speed.md)。

チャット（`/chat` `/chat/stream`）・会話の発言・書き込み・資料の本文（`/documents` ほか）は、まだ Python の API。次の段で `/documents` 系を移す（ADR の段 A の残り）。

## 守ること

- **読み取り専用。** DB への書き込みを足さない。接続は読み取り専用のロールにする（本番の配備のとき）。
- **Python の API と同じ契約。** 応答の JSON（項目の名前・順序・値）・状態・エラーの固定文・`Cache-Control`・`WWW-Authenticate` は `app/api/collections.py` `classifications.py` `access.py` `auth.py` と同じ。変えるときは、両方を直して、下の比較を通す。
  意図して違うのは、`/health`（`{"status":"ok"}` だけ。Python は `mode` `history` も返す）と、CORS（完全一致の許可だけ・`*` を許さない・`Access-Control-Max-Age` は 7200 秒〔Python は 600〕）。
- **認証は自分で検証する**（JWKS の ES256 / RS256。`exp` `iss` `aud` `sub` 必須。`alg=none`・HS256 は受け付けない）。Python 側の検証は外さない。トークン・Authorization ヘッダーはログに出さない。
- **権限は fail-closed。** `ACCESS_MODE=enforce` で引けなければ 503（制限なしには倒れない）。見える範囲が 0 件なら 403。範囲の外のグループは、SQL で絞ったうえで、返す前にもう一度確かめる。
- **DB は PgBouncer（トランザクションモード）越し**（本番のアプリはプール側 `pooled.db.prisma.io`）なので、`pgx` は `QueryExecModeExec`（準備済み文なし）。Python の `prepare_threshold=None` と同じ理由。
- **エラー本文は固定メッセージ。** 例外の文字列・接続先・SQL を応答とログに出さない（型の名前だけ）。
- 依存は `github.com/jackc/pgx/v5`（v5.13.0。MIT）だけ。ビルドに入るのは、その間接の `pgpassfile` `pgservicefile` `puddle/v2` `golang.org/x/sync` `golang.org/x/text`。足すときは承認を得る。

## 環境変数

| 変数 | 既定 | 意味 |
|---|---|---|
| `PORT` | `8080` | 待ち受け（Cloud Run が渡す） |
| `DATABASE_URL` | なし | 無ければ `configured:false` を返す（DB を読まない）。`ACCESS_MODE` / `MEMBERSHIP_MODE` が off 以外なら必須 |
| `DATABASE_POOL_MAX_SIZE` | `2` | 接続プールの上限。台数 × この値を、[ADR 0011](../../docs/decisions/0011-db-connection-pool-limits.md) の枠（プール側 10）に収める |
| `AUTH_MODE` | `off` | `required` で JWT を要求。`AUTH_BASE_URL`（必須）・`AUTH_AUDIENCE`（既定 `docunavi`）・`AUTH_JWKS_URL`（既定 `{AUTH_BASE_URL}/api/auth/jwks`）。起動時に JWKS を取り、取れなければ起動を失敗させる |
| `ACCESS_MODE` | `off` | `off` / `shadow` / `enforce`（資料グループの制御。[app/access.py](../../app/access.py) と同じ） |
| `MEMBERSHIP_MODE` | `off` | 利用資格（`app_members`）。`AUTH_MODE=required` が必要 |
| `ACCESS_CACHE_TTL_SECONDS` | `60` | 見えるグループと資格のプロセス内の控え。0 で無効 |
| `CORS_ORIGINS` | なし | 許可する画面のオリジン（カンマ区切り・完全一致）。`*` は起動を失敗させる |

## 検査

```bash
cd ops/read-api && go vet ./... && go test -race -count=1 ./...      # 単体（偽の DB・偽の JWKS）。CI の go ジョブも同じ
```

実 DB と Python との比較は、使い捨てのローカル DB で行う（本番・開発用の DB には流さない。`setup_db.py` はローカルのホスト以外を拒否する）:

```bash
SETUP_ADMIN_URL=postgresql://…@host.docker.internal:5433/postgres .venv/bin/python ops/read-api/testdata/setup_db.py readapi_cmp
# 実 DB の検査
READ_API_TEST_DATABASE_URL=postgresql://…/readapi_cmp go test -count=1 -run Postgres ./...
# Python と Go に同じ要求を送って、応答を比べる（Go は go build -o /tmp/readapi-bin . で作る）
READ_API_CMP_DATABASE_URL=postgresql://…/readapi_cmp .venv/bin/python ops/read-api/testdata/compare_auth.py /tmp/readapi-bin
READ_API_CMP_DATABASE_URL=postgresql://…/readapi_cmp .venv/bin/python ops/read-api/testdata/bench.py /tmp/readapi-bin   # 速さの比較（ローカル）
.venv/bin/python ops/read-api/testdata/setup_db.py readapi_cmp --drop                                                  # 後片付け
```

## 状態（2026-11-25）

- 実装: `/health` `/collections` `/targets` `/doc-types`、JWT 検証、`ACCESS_MODE` / `MEMBERSHIP_MODE`、CORS、`pgx` の接続プール。Dockerfile・Cloud Build・配備は未作成。
- 検証（証拠等級 A。ローカル）: 単体テスト（`-race`）・実 DB の検査 3 件が通る。Python と Go の応答は、`ACCESS_MODE` 3 通り（認証 off）と、資格・権限 4 通り × トークン 9 種類 × 3 経路（認証 on）で、本文・ヘッダー・状態まで一致した。
- **未確認:** 本番（Cloud Run・Prisma Postgres のプール越し）での動作と速さ。PgBouncer 越しでの `QueryExecModeExec` の動作。画面の切り替え（`NEXT_PUBLIC_*` の向き先）。`/documents` 系。
