# DB 認証情報（`chatbot_app`）の更新手順

2026-11-05 に、`chatbot_app` の接続文字列（パスワード付き）が、2026-10-19 の Cloud Logging に残っていたことが分かった
（接続文字列の先頭が欠けた不正な値を `psycopg_pool` が WARNING で出した。原因は 11 月 4 日の `docs/deploy.md` の「ostgresql」の記録と同じ）。
**更新はユーザーが後で行う（2026-11-05 に「後回し」と決定）。** この文書は手順の保存で、実行済みではない。
ログの出力側の対策（秘密の形を伏せる）は `app/log_redaction.py`（コミット 50518a0、プッシュ済み。Cloud Run へは、ビルドとデプロイのあと）。

## 使っているもの

Secret Manager の `database-url`（利用側はすべて `latest` を参照。起動中のリビジョンは、起動時の版を持ち続ける）。

| 利用側 | 反映 |
|---|---|
| Cloud Run `docunavi`（API） | 新しいリビジョンが要る |
| ジョブ `docunavi-ingest-sync` `docunavi-ingest-queue` `docunavi-history-cleanup` `r2-verify` | 実行のたびに最新版を読む。作業は不要（次の実行の成否で確かめる） |

対象外: `docunavi-admin` `docunavi-admin-migrate`（`admin-database-url`）、`docunavi-mcp`（`database-url-readonly`）。
別の保管先（開発コンテナの `.env`、Infisical、手元のメモ）に写しがあれば、更新後に直す。

## 手順（ホストのターミナル。パスワードは画面に出さない）

```bash
cd ~/projects/docunavi
PROJECT_ID=docunavi-app; REGION=asia-northeast1
as_me() { env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT "$@"; }

# 0. いまの版を控える（値は出ない）
as_me gcloud secrets versions list database-url --project $PROJECT_ID

# 1. 新パスワードを作る（クリップボードにだけ入れる）
openssl rand -hex 24 | tr -d '\n' | pbcopy

# 2. DB 側を更新する（psql の \password はクライアント側でハッシュ化して送る）
psql "<postgres ロールの session pooler 接続文字列（ポート 5432）>"
#   \password chatbot_app      ← ⌘V を 2 回
#   \q

# 3. Secret に新バージョンを追加する（いまの形のまま、パスワードだけ差し替える）
as_me gcloud secrets versions access latest --secret database-url --project $PROJECT_ID \
 | NEWPW="$(pbpaste)" python3 -c '
import os, re, sys
u = sys.stdin.read().strip()
new = re.sub(r"(://[^:@/]+:)[^/]*(@)", lambda m: m.group(1) + os.environ["NEWPW"] + m.group(2), u, count=1)
assert new != u and new.startswith("postgresql://")
sys.stdout.write(new)' \
 | as_me gcloud secrets versions add database-url --data-file=- --project $PROJECT_ID
pbcopy < /dev/null

# 4. 本番に反映する（新バージョンの番号を明示して、新リビジョンを作る。番号は 0 の一覧の次）
as_me gcloud run services update docunavi --project $PROJECT_ID --region $REGION \
  --update-secrets=DATABASE_URL=database-url:<新しい番号>
```

- 手順 2 から 4 のあいだは、本番の新しい DB 接続が失敗する（既存の接続は、プールにある間は生きる）。続けて実行して数分に収める。
- 新リビジョンは起動時に DB を検査するので、パスワードが違えば Ready にならず、旧リビジョンが残る（トラフィックは奪わない）。
  直すときは、正しい値で版を足して手順 4 をやり直す。
- psql が無いときの代替は SQL エディターの `alter role chatbot_app with password '…'`（平文の文が実行履歴に残る）。

## 確認と旧版の無効化

1. 新リビジョンが Ready、`/health` が 200、起動ログに「会話履歴の保存先: Postgres」があり、プールのエラーが無い。
2. ジョブの次の実行が成功する。
3. 旧パスワードが拒否される（接続文字列は変数に入るだけで表示しない）:
   `OLD=$(as_me gcloud secrets versions access <旧版> --secret database-url --project $PROJECT_ID); psql "$OLD" -c 'select 1' 2>&1 | grep -c -i 'authentication\|password'; unset OLD`
4. 確認が済んでから、旧版（v1・v2）を無効化する（元に戻せる。完全な破棄は数日後）:
   `for v in 1 2; do as_me gcloud secrets versions disable $v --secret database-url --project $PROJECT_ID; done`

## ログの閲覧範囲について（確認できなかったこと）

プロジェクトの IAM と、ログの保持（`_Default` は 30 日）のうち、閲覧権限は閲覧専用の権限では読めなかった。誰が過去のログを読めたかは未確認。
漏れた値の行は、保持期間が過ぎれば消えるが、それまでと、このセッションの記録には残る。更新するまで、値は有効なものとして扱う。
