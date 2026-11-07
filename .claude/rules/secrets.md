# 秘密情報の扱い

2026-11-02 にユーザーが決めた方針。

- 保管先は Infisical に一本化する。新しい秘密を `.env` やリポジトリに置かない。
- 種類ごとの扱い:
  - OAuth のリフレッシュトークン: IdP の方針に従う。使用時に更新された場合のみ、新しい値を Infisical に書き戻す。
  - API キー: 60〜90 日、または自動ローテーション。
  - マシン ID の Client Secret: 短い TTL（30 日）。
  - 一時的なアクセストークン: 保存しない。1 回だけ使う。
- 可能な限り、動的シークレットや短命のトークンに寄せる。
- 開発コンテナでは、マシン ID（権限は必要なプロジェクトの最小限）を使う。人間の認証情報（利用者のログイン）は、
  利用者の判断で残してよい（2026-11-07 に変更。毎回ログアウトしない）。残す場合は、再ビルドで消えないよう
  `.devcontainer/persist-infisical-login.sh` がボリュームに置く。暗号化のパスフレーズも同じボリュームにあり、保管時の保護ではない。
  外すかどうかは利用者が決める（外すときは `infisical logout`）。エージェントは、ログアウトを促さない。
- 無料プランには、バージョン履歴・監査ログ・復元が無い。編集・削除は元に戻せないので、プロジェクトを分けて影響を限る。
- 秘密の値は表示・ログ・コミットしない。Infisical CLI の `secrets set KEY=@file` はファイルを読まない（パス文字列が保存される）。
  値は base64 にして `--file` で渡す。
- 実ユーザーのセッション（Playwright の保存状態など）は、リフレッシュトークンの再利用検知でユーザーが全端末で
  ログアウトされる恐れがある。保存済みの値は使わず、毎回新しく書き出してもらう。
- playwright-auth の Client Secret は 2026-12-02 に期限切れ。期限前にローテーションする。

## GitHub Actions のデプロイ用（2026-11-08 に決定）

`.github/workflows/deploy.yml` が、GitHub の OIDC で Infisical にログインし、Cloudflare の API トークンを実行時に取る。GitHub には Cloudflare の秘密を置かない。

- Infisical: プロジェクト `docunavi-deploy`（環境 `prod`）。秘密は `CLOUDFLARE_API_TOKEN` と `CLOUDFLARE_ACCOUNT_ID` だけ。ほかの用途の秘密を入れない
  （無料プランは復元ができないので、プロジェクトを分けて影響を限る）。`docunavi-devcontainer` は、このプロジェクトに入れない（MCP から届くため）。
- マシン ID `github-actions-deploy`（OIDC Auth、プロジェクトの Viewer）:
  - Discovery URL・Issuer: `https://token.actions.githubusercontent.com`
  - Subject: `repo:Starlight-Tree@154563471/docunavi@892933857:environment:production`（数値 ID つき。リポジトリの改名・移管で変わる）
  - Audience: `https://github.com/Starlight-Tree`、Access Token TTL: 600 秒
- Cloudflare の API トークン: アカウントに限定。権限は Workers Scripts: Edit と D1: Edit から始め、足りなければ足す。有効期限は 90 日。
  期限前に作り直し、Infisical の値を更新する。IP 制限は、GitHub のランナーの IP が不定なので使えない。
  現在のトークン `docunavi-github-actions-deploy` は 2026-11-08 に作成し、**期限は 2027-02-06**（作り直しは 2027-01 中）。
- GitHub: Environment `production` に必須レビュアー（ユーザー）、Variable `INFISICAL_IDENTITY_ID`（公開してよい ID）。
- 無料プランで OIDC Auth が使えることは、ユーザーが判断した（公式の文書では確認できていない。2026-11-08）。使えなくなったら、この経路は止まる。
