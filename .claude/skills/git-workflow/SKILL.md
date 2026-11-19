---
name: git-workflow
description: コミット・amend・プッシュの承認依頼・CI の確認・deploy.yml の扱いをするときの詳細な手順。「コミットして」「amend して」「プッシュしていい？」「CI を見て」「デプロイして」と頼まれたとき、またはプッシュの承認を求める前に使う。禁止事項と承認の条件は、ルートの CLAUDE.md の「Git」にある（ここは、その手順の詳細）。
---

# Git の手順（詳細）

ルートの [CLAUDE.md](../../../CLAUDE.md) の「Git」が、禁止事項と承認の条件の正本。ここは、その実行の手順。

## コミット

- 作者名: `git config user.name` は `Starlight-Tree`（GitHub の登録名。メールは変えない）。2026-11-14 に、全履歴の作者・コミッターを `Starlight-Tree` に書き換えた（[docs/history-rewrite.md](../../../docs/history-rewrite.md)）。
- メッセージ: Conventional Commits（`type(scope): 英語の命令形の要約`）。type は `feat` `fix` `docs` `refactor` `test` `chore` `build` `ci` `perf`。
  件名は英語（日本語は本文に）・72 文字以内・小文字始まり・末尾ピリオドなし。本文に「なぜ」と検証結果（限界も）を書く。1 コミット 1 論理変更。
  `scripts/check_commit_messages.py` が検査し、CI（`commit-messages`）と Claude のフック（`guard-git-write.py`）が使う（過去の履歴は対象外）。
  件名の事前の検査: `python3 scripts/check_commit_messages.py --subject "<件名>"`。
- 変更が完結し、触れた範囲のテストが通ったら（文書だけの変更はテスト不要）、意味のある単位でコミットしてよい。
  ステージはパスを指定する（作業ツリーを並行セッションと共有しているので、`git add -A` と `git add .` は使わない）。コミット前に `git diff --staged` で意図しない変更が無いかを確かめる。`--no-verify` は使わない。

## amend（直前のコミットが未プッシュのときだけ、承認なしで可）

1. `git fetch` を行う。
2. `git status -sb` が `ahead` を示し、`git log --oneline origin/main..HEAD` に直前のコミットがあることを確かめる。
3. ステージの確認は amend でも行い、amend の後は検査をやり直す。

プッシュ済みのコミットは amend しない。直す必要があるときは、新しいコミットで直すか、ユーザーに相談する。

## プッシュの承認を求めるとき

コミットのたびには聞かず、まとまりが完結した時点で 1 回、次を示して承認を求める（前の承認は次に及ばない）:

- `git status --short`、`git log --oneline origin/main..HEAD`、`git diff --stat origin/main..HEAD`
- 検査の結果: 追加行の秘密情報のパターンと、登録語（`scripts/check-sensitive-terms.sh origin/main..HEAD`。語は Git の管理外の `.git/sensitive-terms` に置く。引数なしはステージ分で、`--staged` は無い）
- テストを実行したか
- `workers/` `frontend/` の変更が含まれるか（＝ユーザーが `deploy.yml` を実行したときに本番へ出る）。プッシュだけでは Worker は本番に出ない。Worker の秘密（`wrangler secret put`）はこの経路に含めない。

テストが失敗しているとき、作業の途中のときは、提案しない。

## CI とデプロイ

- `.github/workflows/ci.yml` が、`main` へのプッシュと PR でテスト・ビルド・静的検査を行う（内訳は [docs/testing.md](../../../docs/testing.md)）。**CI 自体にデプロイは含まない。**
- `.github/workflows/deploy.yml` は**手動の実行だけ**（`workflow_dispatch`。自動では起動しない）。`main` の CI の成功を確かめたうえで、利用者の明示の承認を得て実行し（GitHub MCP で私が起動してもよい。承認は会話で取る）、
  Cloudflare の Worker（`workers/api` = `docunavi-api`、`frontend` = `docunavi-web`）を本番へデプロイする。
  Cloudflare の資格情報は GitHub に置かず、OIDC で Infisical（`docunavi-deploy`）から取る（詳細は [docs/deploy.md](../../../docs/deploy.md)、方針は [.claude/rules/secrets.md](../../rules/secrets.md)）。Cloud Run（FastAPI・中継）は自動デプロイしない。
- CI の結果の扱い: プッシュのたびに待たない。設定・文書・拡張だけの変更は、見にも行かない。アプリ本体・テスト・Docker・`ci.yml`・`frontend/` を変えたら、
  プッシュ後に一度だけ `gh run list` で確認し、未確認でも次の独立した作業へ進む。次の作業が前のコミットの CI に依存するとき、デプロイや公開の前、
  ユーザーが結果を求めたときは、完了まで見て報告する。失敗したら、失敗ステップとログの要点を出す。
  待ちはバックグラウンドで行う（`.claude/rules/parallel-work.md`。前景の `gh run watch` はフックが止める）。
