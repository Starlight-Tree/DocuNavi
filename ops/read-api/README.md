# ops/read-api/（読み取り API。Go）

読み取り専用の Go のサービス。返す経路は、`GET /health`、資料グループ・対象・資料種別・資料の一覧（`/collections` `/targets` `/doc-types` `/documents`）、
資料 1 件の本文・PDF・画像（`/documents/{id}/chunks` `/file` `/hero`）、会話の一覧（`/threads`）。
画面が直接呼ぶ（gateway にしない）。**未配備**（2026-11-26 時点。実装・ローカルの検証は済み。Cloud Run には無く、画面もまだ向けていない。本番は Prisma の上限で止まっていて、再開は 2026-12 以降）。決定と範囲は [ADR 0017](../../docs/decisions/0017-go-read-api-for-speed.md)。

チャット（`/chat` `/chat/stream`）・会話の発言（`/threads/{id}/messages`）と削除・書き込み・関係図（`/documents/{id}/graph` `/impact`）は、Python の API のまま（Go では 404）。

## 守ること

- **読み取り専用。** DB への書き込みを足さない。接続は読み取り専用のロールにする（本番の配備のとき）。
- **Python の API と同じ契約。** 応答の JSON（項目の名前・順序・値）・状態・エラーの固定文・`Cache-Control`・`WWW-Authenticate` は `app/api/collections.py` `classifications.py` `access.py` `auth.py` と同じ。変えるときは、両方を直して、下の比較を通す。
  意図して違うのは、`/health`（`{"status":"ok"}` だけ。Python は `mode` `history` も返す）と、CORS（完全一致の許可だけ・`*` を許さない・`Access-Control-Max-Age` は 7200 秒〔Python は 600〕）。
- **認証は自分で検証する**（JWKS の ES256 / RS256。`exp` `iss` `aud` `sub` 必須。`alg=none`・HS256 は受け付けない）。Python 側の検証は外さない。トークン・Authorization ヘッダーはログに出さない。
- **権限は fail-closed。** `ACCESS_MODE=enforce` で引けなければ 503（制限なしには倒れない）。見える範囲が 0 件なら 403。範囲の外のグループは、SQL で絞ったうえで、返す前にもう一度確かめる。
- **DB は PgBouncer（トランザクションモード）越し**（本番のアプリはプール側 `pooled.db.prisma.io`）なので、`pgx` は `QueryExecModeExec`（準備済み文なし）。Python の `prepare_threshold=None` と同じ理由。
- **エラー本文は固定メッセージ。** 例外の文字列・接続先・SQL を応答とログに出さない（型の名前だけ）。
- **見えないものは「無い」のと同じ 404。** 見えないグループの資料の本文・PDF・画像は、存在しない資料と同じ 404 と同じ文。`{id}` が不正なら FastAPI と同じ 422（pydantic の整数の読み方まで合わせた）。順序も同じ: 認証 → 利用資格 → 範囲（503）→ `{id}` の検査（422）→ 見えるグループが 0 件なら 403 → DB（503）→ 404。
- **PDF は、名前とハッシュが DB と合うものだけを返す**（`pdfstore.go`。`/file` は毎回確かめる。`/chunks` の `original_pdf` は、DB の `original_verified_at` を先に見て、無ければその場で確かめる〔r2 は結果を 600 秒 / 60 秒だけ控える。読めなかった結果は控えない〕）。R2 を読めないときは、ローカルへ切り替えず 503。
- **会話の一覧（`/threads`）は、題名を `checkpoints.metadata` の `title` からだけ読む。** チェックポイント本体（msgpack）は復号しない。`title` が無い古い会話は「（題名なし）」になる。**画面を向ける前に、`python -m app.history_admin --backfill-titles --apply` で補完する**（既定は件数だけ。本番の DB への書き込みなので、実行の前に承認を得る）。
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
| `PDF_STORE` | `local` | PDF の置き場。`r2`（本番）以外は `local`（Python の `app/pdf_originals.py` と同じ） |
| `PDF_DIR` | `storage/pdf` | `local` のときの置き場（直下が資料群のディレクトリ）。シンボリックリンクはたどらない |
| `R2_ACCOUNT_ID` `R2_BUCKET` | なし | R2 に接続する値（`PDF_STORE=r2` の PDF と、**`PDF_STORE` に関わらず**ヒーロー画像に使う）。足りなければ、PDF・画像は 503 の固定文 |
| `R2_CREDENTIALS_JSON` / `R2_CREDENTIALS_FILE` / `R2_ACCESS_KEY_ID` + `R2_SECRET_ACCESS_KEY` | なし | R2 の**読み取り**の鍵（`{"access_key_id":…,"secret_access_key":…}`。Secret `r2-app-read` を `R2_CREDENTIALS_FILE` のボリュームで渡す形を想定）。R2 が 401/403 を返したら鍵を読み直し、**鍵の ID が変わっていれば** 1 回だけやり直す（再配備は要らない） |

## 検査

```bash
cd ops/read-api && go vet ./... && go test -race -count=1 ./...      # 単体（偽の DB・偽の JWKS）。CI の go ジョブも同じ
```

実 DB と Python との比較は、使い捨てのローカル DB で行う（本番・開発用の DB には流さない。`setup_db.py` はローカルのホスト以外を拒否する）:

```bash
SETUP_ADMIN_URL=postgresql://…@host.docker.internal:5433/postgres .venv/bin/python ops/read-api/testdata/setup_db.py readapi_cmp
# 実 DB の検査
READ_API_TEST_DATABASE_URL=postgresql://…/readapi_cmp go test -count=1 -run Postgres ./...
# Python と Go に同じ要求を送って、応答を比べる（Go は go build -o /tmp/readapi-bin . で作る。compare_auth.py は /file の比較のために、使い捨ての DB の資料の行と会話を書き換え、題名の補完も流す）
READ_API_CMP_DATABASE_URL=postgresql://…/readapi_cmp .venv/bin/python ops/read-api/testdata/compare_auth.py /tmp/readapi-bin
READ_API_CMP_DATABASE_URL=postgresql://…/readapi_cmp .venv/bin/python ops/read-api/testdata/bench.py /tmp/readapi-bin   # 速さの比較（ローカル）
.venv/bin/python ops/read-api/testdata/setup_db.py readapi_cmp --drop                                                  # 後片付け
```

## 状態（2026-11-26）

- 実装: 上の全経路、JWT 検証、`ACCESS_MODE` / `MEMBERSHIP_MODE`、CORS、`pgx` の接続プール、R2 の読み取り（SigV4 を標準ライブラリで署名）。`Dockerfile` と `cloudbuild.yaml`（イメージのビルドだけ。配備はしない）。画面の切り替えは `frontend/src/api/chat.ts` の `readEndpoint` / `readThreadsEndpoint`（`NEXT_PUBLIC_READ_BASE_URL` / `NEXT_PUBLIC_READ_THREADS_BASE_URL`。空なら Python の API のまま）。
- 検証（証拠等級 A。ローカル）: 単体テスト（`-race`）、実 DB の検査、Python と Go の応答の比較（`testdata/compare.py`〔認証 off。`ACCESS_MODE` 3 通り〕・`compare_auth.py`〔資格・権限 4 通り × トークン 9 種類 × 33 経路。実在の PDF での 200 / 409 / 404 を含む〕）で、本文・ヘッダー・状態まで一致した。**SigV4 の署名は、botocore が同じ要求に付ける値と一致する**（`r2_test.go`）。題名の補完（`--backfill-titles`）は、実 Postgres（jsonb）で流して、Python（本体を復号）と Go（`title` だけ）の一覧が一致することを確かめた。イメージは、ローカルでビルドして起動し、`/health` が 200 を返すことを確かめた（その後に削除）。
- **未確認:** 本番（Cloud Run・Prisma Postgres のプール越し）での動作と速さ。PgBouncer 越しでの `QueryExecModeExec` の動作。**実際の R2 への接続**（偽の S3 サーバーと、署名の照合だけ。実 R2 には接続していない）。Cloud Run のイメージの配備。`updated_at` は、DB の時刻をミリ秒に切り捨てて出す（Python と同じ。ミリ秒の境目の値だけ、Python は 1 ms ずれうる）。
- 意図して違う点: `/threads` は題名が未保存の会話を「（題名なし）」にする（上）。`/threads` で DB が無いとき、Python はメモリの履歴を返すが、Go は空の一覧。`/documents/` のように末尾が `/` のパスは、Python は 307 で転送するが、Go は 404。`Access-Control-Max-Age` は 7200 秒。
