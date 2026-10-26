# ロール別の資料グループの管理画面（Django Admin。ロードマップ ⑧ のフェーズ 4a）

運用者だけが使う管理画面。利用者へのロールの割り当て（`chatbot.user_roles`）を、SQL ではなく画面で行う。実装は [ops/access_admin/](../ops/access_admin/)。
アクセス制御そのものの仕様（`ACCESS_MODE`、10 経路、`default` ロール）は [configuration.md](configuration.md) の「資料グループのアクセス制御」。

**状態（2026-10-26）:** ステージ R0〜R9 まで完了。Supabase に migration を適用し、非公開の Cloud Run `docunavi-admin`（リビジョン `docunavi-admin-00003-cvr`、イメージ `docunavi-admin:799de59`）に配備済み。
運用者 1 人が、プロキシ経由でパスワードと TOTP でログインできる。最初の限定ロール（`cafe-staff`）の作成、一時的な割り当て、解除、`default` への復帰も確かめた。
ローカルの一時 Postgres での確認と、本番で確かめたこと・未確認は、下の「確かめたこと / 未確認」。

## 何をするか（4a の範囲）

| やること | やらないこと（4a の対象外） |
|---|---|
| 利用者の一覧（メール検索、割り当ての有無で絞り込み）。現在のロールと、見えるグループを表示 | ロール（`roles`）の作成・編集、ロールが持つグループ（`role_collection_access`）の編集。SQL で行う |
| 利用者ごとのロールの割り当て・解除（確認の画面で、変更前後の見えるグループを見せる） | `user_roles` の UPDATE。資料（`documents` `chunks`）への一切の操作 |
| ロールの一覧（持つグループ、割り当て済みの利用者の数。どのロールにもないグループの警告） | 利用者向けの機能、独自のデザイン（標準の Django Admin の枠のまま） |
| 変更の記録（`chatbot.access_audit`）と、Django の操作履歴の閲覧 | |

## 守りの層

1. **非公開の Cloud Run**（`--no-allow-unauthenticated`）。呼べるのは `roles/run.invoker` を持つ運用者だけ。
2. **Django のパスワード**（スーパーユーザー 1 人）。ログイン後 1 時間で切れる。Cookie は `HttpOnly` / `SameSite=Lax`（`Secure` は環境変数で切り替え）。
3. **TOTP**（認証アプリのワンタイムコード。`django-otp` 本体の `otp_totp`）。メール（個人情報）を扱うため、IAM とパスワードに加えて必須にした。
   TOTP の確認が済んでいないセッションは、管理画面のどの URL も開けない（`AccessAdminSite.has_permission`）。
4. **DB ロール `access_admin` の最小権限**（下）。画面や Django に不具合があっても、`documents` / `chunks` / 検索の表には届かない。

## DB の分担

| 対象 | 誰が変えるか |
|---|---|
| `chatbot` スキーマの表・ビュー | `supabase/migrations/` だけ。`access_admin` には `chatbot` への CREATE を付けていない。Django は `chatbot` の表を写すモデルを `managed = False` にしてあり、`sqlmigrate` が DDL を出さないことをテストで固定している |
| `saas` スキーマ（Django の表: ログイン、セッション、操作履歴、TOTP） | Django の `migrate`（`access_admin` として実行）。スキーマの所有者は `postgres` のまま、`access_admin` に USAGE と CREATE を付けた（計画では `authorization access_admin` だったが、Supabase の `postgres` ロールが `access_admin` の所有にできるか未確認のため、メンバーシップが要らないこの形にした） |
| `chatbot.admin_users`（ビュー） | `supabase/migrations/20261026070500_access_admin.sql`。`auth.users` の `id` `email` `last_sign_in_at` だけ。`public` `anon` `authenticated` `chatbot_app` には開けない |

`access_admin` の権限（テストが、許す操作が通ることと、許さない操作が `permission denied` になることの両方を確かめる）:

- `chatbot.user_roles`: SELECT / INSERT / DELETE（UPDATE と TRUNCATE は無し）
- `chatbot.roles` `role_collection_access` `access_audit` `document_collections` `admin_users`: SELECT だけ
- `chatbot.documents` `chunks` とチェックポイントの表: 何も無い。`auth` スキーマにも届かない（ビュー経由だけ）
- ロール自体: `login`、パスワードは SQL で決めない。`nosuperuser nocreaterole nocreatedb noinherit nobypassrls`。`search_path = saas, chatbot`

`chatbot_app`（API のロール）の権限は変えていない。

## 割り当ての画面の安全装置

- **保存の前に、変更前後の見えるグループを見せる**（`services.plan_change`。規則は `app/access.py` と同じで、実際の Postgres での契約テストが突き合わせる）。
- **見えるグループが減る変更は、確認のチェックボックスが無ければ保存しない。** 画面から送られた値のうち使うのは、選んだロール名・開いた時点の元の割り当て・チェックだけで、前後の計算はサーバーが保存の直前に DB から計算し直す。
- **開いてから保存するまでに割り当てが変わっていたら保存しない**（元の割り当てとの比較。利用者ごとの advisory lock の下で）。
- **ロールが 0 件の利用者は `default` 扱い**。最初のロールを足すと `default` は使われなくなるので、画面に警告を出し、「`default` も同時に割り当てる」を選べる。
- 書き込みはスーパーユーザーだけ。GET では書かない。CSRF を検査する。存在しないロール名は、名前を返さずに拒否する。
- 記録は 2 つ: DB のトリガー（`access_audit`。`actor` は接続した DB ロール = `access_admin`）と、Django の操作履歴（`LogEntry`。誰が、何を、見えるグループの前後。**利用者は ID だけで、メールは載せない**）。

## TOTP の運用

- **初回の登録:** パスワードでログインすると、登録の画面に送られる。QR コードを認証アプリで読み取り、6 桁のコードを入れて確定する。
  QR は**サーバー側で SVG にして**、この画面にだけ出す（文字のシークレットは出さない。ログにも操作履歴にも残さない。`no-store`）。
  確認済みの機器が 1 つでもある利用者には、画面からの再登録はできない。
- **2 回目以降:** パスワードのあとに、コード入力の画面。誤ったコード、使用済みのコード、連続の失敗中は拒否される（`django-otp` の待ち時間と再利用の拒否）。
- **認証アプリを失くしたとき:** 運用者が `python manage.py totp_reset <ユーザー名>` を実行する（機器を消すだけ。消した台数だけを表示）。次のログインで登録をやり直す。
  実行の仕方は、`migrate` と同じ Cloud Run Job（下の R6）。
- **標準の「TOTP devices」管理画面は登録しない**（QR を表示できるため）。

## 環境変数と Secret

| 名前 | 内容 |
|---|---|
| `ADMIN_SECRET_KEY` | Django の `SECRET_KEY`（32 文字以上。Secret Manager） |
| `ADMIN_DATABASE_URL` | `access_admin` の接続 URL（Secret Manager。既存の `DATABASE_URL` と同じホスト・ポートで、ユーザー名とパスワードだけ `access_admin` に替える） |
| `ADMIN_ALLOWED_HOSTS` | 受け付けるホスト名（カンマ区切り） |
| `ADMIN_CSRF_TRUSTED_ORIGINS` | 信頼するオリジン（`gcloud run services proxy` なら `http://localhost:8080`） |
| `ADMIN_SECURE_COOKIES` | 既定 `1`。`0` にするのは、プロキシ経由の `http://localhost` のときだけ |
| `ADMIN_TRUST_PROXY_SSL` | 既定 `1`（Cloud Run の `X-Forwarded-Proto: https` を信じる） |

足りない・形式が正しくないと、起動時の `manage.py check` で落ちる（エラーの文にパスワードは出さない）。

## ローカルでの確認

[ops/access_admin/README.md](../ops/access_admin/README.md)。テストは `cd ops/access_admin && ../../.venv-django/bin/python manage.py test`
（一時 Postgres が要る。`chatbot` / `auth` の身代わりと実際の migration の SQL を入れたテンプレート DB から作り、各テストは `SET LOCAL ROLE access_admin` で動く）。
契約テストは `tests/test_access_admin_contract.py`（アプリ本体の `visible_collections` の SQL と、管理画面の計算を、ロールのあらゆる組み合わせで突き合わせる）。

## 本番への手順（R0〜R9 は 2026-10-26 に実施済み。再現・復旧の参照用。各ステージは、承認を取ってから）

書き込みはユーザーが実行する（[deploy.md](deploy.md) の `as_me` の書き方）。`<…>` は実際の値に置き換える。
実際に通した手順から変えた点は、コードブロックのあとの「実施で分かったこと」に書いた。

| ステージ | 内容 | 誰が |
|---|---|---|
| R0 | ローカルでの実装・テスト・画面確認 | 完了 |
| R1 | `git push`（`ops/access_admin` と migration を含む。承認後） | ユーザーの承認 → 私 |
| R2 | CI の `admin` ジョブと既存ジョブが緑であることを確かめる | 私（`gh run list`） |
| R3 | 本番 DB に migration を適用 | ユーザー |
| R4 | `access_admin` のパスワード設定、Secret、実行用サービスアカウント、イメージのビルド | ユーザー |
| R5 | 初回のデプロイ（非公開・最小インスタンス 0・最大 1） | ユーザー |
| R6 | `migrate`（`saas` に Django の表を作る）を Cloud Run Job で実行 | ユーザー |
| R7 | スーパーユーザーの作成（Cloud Run Job） | ユーザー |
| R8 | プロキシでつなぎ、ログイン → TOTP 登録 → 一覧を確かめる | ユーザー（私は画面を見ない） |
| R9 | 最初の限定ロールを SQL で作る → 自分のアカウントへ一時的に割り当てて確かめる → 解除する | ユーザー（`user_roles` の書き込みは、その時点で別に承認） |

```bash
# R3: 適用前に、流れるのがこの 1 ファイルだけかを見る（dry-run は書き込まない）
supabase db push --linked --dry-run
supabase db push --linked      # 20261026070500_access_admin.sql だけが適用される想定
# 確認（読み取り）は、migration 末尾の「検証」の SELECT。期待値もそこに書いてある
```

```bash
# R4（例。パスワードを端末に出さず、Secret Manager に直接渡す）
PROJECT_ID=docunavi-app; REGION=asia-northeast1; SERVICE=docunavi-admin
# 1) DB 側: postgres ロールで接続できる手段で、次を実行する（<生成したパスワード> は手元で生成）
#      alter role access_admin password '<生成したパスワード>';
# 2) 接続 URL を Secret に入れる（ユーザー名は既存の DATABASE_URL と同じ規則。プーラー経由なら access_admin.<プロジェクト参照>）
printf '%s' "postgresql://<ユーザー>:<パスワード>@<ホスト>:<ポート>/<DB>?sslmode=require" \
  | as_me gcloud secrets create admin-database-url --data-file=- --project "$PROJECT_ID"
python3 -c 'import secrets; print(secrets.token_urlsafe(50), end="")' \
  | as_me gcloud secrets create admin-secret-key --data-file=- --project "$PROJECT_ID"
# 3) 実行用のサービスアカウント（このサービス専用）と、2 つの Secret だけの参照権限
as_me gcloud iam service-accounts create docunavi-admin-run --project "$PROJECT_ID"
for S in admin-database-url admin-secret-key; do
  as_me gcloud secrets add-iam-policy-binding "$S" --project "$PROJECT_ID" \
    --member "serviceAccount:docunavi-admin-run@${PROJECT_ID}.iam.gserviceaccount.com" --role roles/secretmanager.secretAccessor
done
# 4) イメージ（cloudbuild.yaml。_IMAGE は Artifact Registry の名前）
as_me gcloud builds submit --config ops/access_admin/cloudbuild.yaml --substitutions "_IMAGE=${IMAGE}" --project "$PROJECT_ID" --billing-project "$PROJECT_ID" .
```

```bash
# R5: 初回のデプロイ（認証必須。最小 0・最大 1）。<run.app ホスト> は、プロキシが Host に書き換える値（下の「実施で分かったこと」）
as_me gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
  --service-account "docunavi-admin-run@${PROJECT_ID}.iam.gserviceaccount.com" \
  --no-allow-unauthenticated --min-instances 0 --max-instances 1 \
  --set-secrets "ADMIN_DATABASE_URL=admin-database-url:latest,ADMIN_SECRET_KEY=admin-secret-key:latest" \
  --set-env-vars "^@^ADMIN_ALLOWED_HOSTS=localhost,127.0.0.1,<run.app ホスト>@ADMIN_CSRF_TRUSTED_ORIGINS=http://localhost:8080@ADMIN_SECURE_COOKIES=0"

# R6: migrate（同じイメージの Job。saas に Django の表だけを作る。実行後に実行結果とログを確かめる）
as_me gcloud run jobs create docunavi-admin-migrate --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
  --service-account "docunavi-admin-run@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-secrets "ADMIN_DATABASE_URL=admin-database-url:latest,ADMIN_SECRET_KEY=admin-secret-key:latest" \
  --set-env-vars "^@^ADMIN_ALLOWED_HOSTS=localhost,127.0.0.1,<run.app ホスト>@ADMIN_CSRF_TRUSTED_ORIGINS=http://localhost:8080@ADMIN_SECURE_COOKIES=0" \
  --max-retries 0 --task-timeout 300s --command python --args "manage.py,migrate,--noinput"
as_me gcloud run jobs execute docunavi-admin-migrate --project "$PROJECT_ID" --region "$REGION" --wait

# R8: つなぐ（運用者の権限。ブラウザーで http://localhost:8080/admin/）。要 cloud-run-proxy コンポーネント
as_me gcloud components install cloud-run-proxy      # 初回だけ（ホストの Mac）
as_me gcloud run services proxy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --port 8080
```

R7（スーパーユーザー）は、R6 の Job の `--args` を `manage.py,createsuperuser,--noinput,--username,<名前>,--email,<メール>` にして、
`DJANGO_SUPERUSER_PASSWORD` を Secret（`--set-secrets`）で渡す。パスワードはコマンドに書かない。実行したら、その Secret と Job を消す。
`totp_reset` も同じ形（`--args manage.py,totp_reset,<名前>`。実行したら Job を消す）。

イメージだけを差し替える再デプロイ（2026-10-26 に QR とボタンの修正で実施）。環境変数・Secret・サービスアカウント・非公開の設定は変わらない。`migrate` は、DB の変更が無ければ要らない:

```bash
as_me gcloud builds submit --config ops/access_admin/cloudbuild.yaml --substitutions "_IMAGE=${IMAGE}" --project "$PROJECT_ID" --billing-project "$PROJECT_ID" .
as_me gcloud run services update "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE"
# 戻す: 直前のリビジョンへ
as_me gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --to-revisions <直前のリビジョン>=100
```

ビルドは作業ツリーをそのまま送る。コミット済みで `git status` が空であることを確かめてから行う。

**実施で分かったこと（2026-10-26）:**
- Cloud Run は、新しいサービスを `--no-traffic` では作れない（最初のリビジョンが 100% を受ける）。非公開なので、この形で安全だった。
- `gcloud run services proxy` は、`Host` をサービスの `run.app` ホスト（`<サービス名>-<識別子>-an.a.run.app`）に書き換える。`ADMIN_ALLOWED_HOSTS` にそのホストが要る（`localhost` だけでは受け付けない）。
  ブラウザーのオリジンは `http://localhost:8080` のままなので、`ADMIN_CSRF_TRUSTED_ORIGINS` はそれでよい（ログインの POST が通ることを確かめた）。
  開発コンテナからは、ホストのプロキシに `host.docker.internal:8080` で届く（`Origin` ヘッダーは `http://localhost:8080` にする）。
- `--set-env-vars` はカンマで区切られるので、値にカンマを含むときは `^@^` の形にする（上のとおり）。
- zsh では `read -p` が使えない。パスワードは `read -rs "PW?プロンプト: "`。貼り付けた複数行（`\` の継続と `#` の行）が崩れるので、1 行で渡す。
- Secret に版が入る前に作った Job は、版を足したあとも Ready にならない（`SecretsAccessCheckFailed`）。Job を消して作り直す。
- プロキシを閉じるとき、ラッパーを止めても子の `cloud-run-proxy` が残ることがある。子だけを止めるとラッパーが作り直す。ホストで `ps -axo pid,ppid,command | grep '[r]un services proxy'` で親を先に止め、次に子を止め、`lsof -nP -iTCP:8080 -sTCP:LISTEN` が空であることを確かめる。
- `/healthz` は、Cloud Run の入口（Google 側）が 404 を返し、コンテナに届かない（リクエストログも残らない。予約された経路のためと推測。公式の文書では確認していない）。`/readyz` と存在しない経路は Django に届く。確認には `/readyz` を使う。コンテナの起動プローブは TCP で、動いている。
- gunicorn が起動のたびに `[ERROR] Control server error: [Errno 13] Permission denied: '/home/app'` を出す（ユーザー `app` にホームディレクトリが無いため）。動作に支障は無い。重大度は付かないため、重大度の絞り込みでは見えない。
- 認証アプリ（Google Authenticator）の登録時、iOS のカメラで QR を読むと Apple のパスワード管理への追加を勧められる。要素を分けるため、認証アプリ内の「コードをスキャン」で読んだ。
- 回復用コードは無い。認証アプリを失くしたときの回復は、`totp_reset`（Job）。

**R9 の流れ（2026-10-26 に実施。割り当てと解除は、利用者の選択で画面ではなく SQL で行った）:**
1. 運用者が SQL で、限定ロールを作る（`insert into chatbot.roles …`、`insert into chatbot.role_collection_access …`。例は [configuration.md](configuration.md)）。
   `access_audit` に記録されることを確かめる。
2. **承認を取ってから**、管理画面で自分のアカウントに、そのロールを割り当てる（画面の確認で、前後の見えるグループを見る）。
3. 本番の画面で、見えるグループが限られていることを確かめる（資料一覧・グループ一覧。実 API の質問は、別に承認）。
4. 割り当てを解除し、`access_audit`（INSERT と DELETE の両方）と Django の操作履歴に残っていることを確かめる。

**戻し方:** サービスを止める（Cloud Run の `docunavi-admin` を削除）→ migration 末尾の「元に戻す手順」→ Secret とサービスアカウントを削除。
利用者のロールの割り当て（`user_roles`）は消えない。アプリ本体（`docunavi`）には影響しない。

## 確かめたこと / 未確認

**確かめた（ローカルの一時 Postgres 17、証拠等級 A）:**
- Django のテスト 104 件（許す操作・許さない操作、TOTP の関門、割り当ての安全装置）。契約テスト 3 件（ロール 4 つの全組み合わせ）。
  契約テストと TOTP の関門は、規則・関門をわざと崩して、テストが落ちることを確かめた。
- `access_admin` ロールそのものでの `migrate`（`saas` に 11 表、`chatbot` は変わらない）、本番の設定（`config.settings.prod`）と gunicorn・WhiteNoise での起動、
  `/readyz`（`search_path` は DB ロールの設定だけで効く）。
- ブラウザー（Playwright、幅 1280）での通し: パスワード → TOTP 登録 → 管理画面、ログアウト → 再ログイン → 誤コード拒否 → 正コード、
  割り当て（確認なしの保存は 400 で拒否 → チェックして保存）、ロール一覧の警告。保存後、`access_audit` の `actor` が `access_admin`、操作履歴にメールが載らないこと。

**本番で確かめた（2026-10-26、読み取りの確認と、利用者の報告）:**
- migration の適用（R3。`supabase db push --linked`）。`access_admin` ロール・`chatbot.admin_users`・権限（`has_table_privilege` で確認。`documents` に SELECT 無し、`user_roles` に INSERT あり・UPDATE 無し）。
- イメージのビルド（Cloud Build。CI の `admin` ジョブでも Docker のビルドを確認）、非公開の Cloud Run への配備（`run.invoker` は運用者本人だけ）、Job での `migrate`（22 件、`saas` に 11 表、`chatbot` は変わらない）と `createsuperuser`。
- 接続プーラー経由で `access_admin` が接続でき、`/readyz` が 200（ロールの `search_path` が効く）。
- プロキシ経由（ホストのブラウザー、開発コンテナの curl の両方）でのログイン。パスワード → TOTP の登録（Google Authenticator）→ 再ログインでコードを要求 → 誤コードの拒否。
- R9: 限定ロール `cafe-staff`（`cafe` だけ）の作成、運用者本人への一時的な割り当て、`/collections` と `/documents` が `cafe` だけになること、過去の会話の伏せ字、解除、`default`（3 グループ）への復帰。割り当てと解除は SQL で行った（監査の `actor` は `postgres`）。
- 再デプロイ（`docunavi-admin-00003-cvr`）。コード入力画面の CSS にダークモードの主ボタンの規則が載っていること、ダークモードの見え方（利用者の報告）。
- `totp_reset`（Cloud Run Job。機器 1 台を削除。実行後に Job は削除）と再登録（同日 18:45 JST）。ダークモードの登録画面で、QR が白地に黒で出ること、「登録する」が見分けられること（利用者のスクリーンショット）。白背景に直した QR を、Google Authenticator で読み取れたこと（利用者の報告）。登録後、確認済みの機器が 1 台になったこと（読み取り）。

**未確認:**
- Django の画面から割り当て・解除をしたときの `access_audit` の `actor`（`access_admin`）と、Django の操作履歴。本番の割り当てと解除は SQL で行った。
- Django の画面の「見えるグループ」の表示と、`admin_users` 経由の利用者一覧・`last_sign_in_at` の値。
- 割り当て後に、検索が `cafe` 以外のグループを返さないこと（実際の Gemini の埋め込みが要る）。
- 認証のない実リクエストが拒否されること（`run.invoker` の設定を読んだだけで、試してはいない）。
- 誤コードの直後に正しいコードでログインできること（失敗回数のリセット）。
- 割り当てを Django の画面から行ったあとの、会話の伏せ字の挙動。

## 運用の注意

- 新しい資料グループが取り込まれると、`default` に割り当てるまで、`complete` が外れて古い会話が全員で伏せられる（[configuration.md](configuration.md)）。
  ロール一覧の警告は、登録済み（`document_collections`）のグループだけを見る。資料にだけあるグループは出ない。
- 利用者の削除（`auth.users`）では `user_roles` は消えない（外部キーを張っていない）。残った行は無害で、画面の一覧には出ない。
- 運用者のアカウントを増やすときは、Django の「ユーザー」画面で作る。作られたアカウントも、最初のログインで TOTP の登録が要る（パスワードだけで入れた直後に、登録の画面に送られる）。
