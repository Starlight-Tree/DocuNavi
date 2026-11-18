# 秘密情報の扱い

2026-11-02 にユーザーが決めた方針。

- 保管先は Infisical に一本化する。新しい秘密を `.env` やリポジトリに置かない。
- 種類ごとの扱い:
  - OAuth のリフレッシュトークン: IdP の方針に従う。使用時に更新された場合のみ、新しい値を Infisical に書き戻す。
  - API キー: 60〜90 日、または自動ローテーション。
  - マシン ID の Client Secret: TTL は 180 日（2026-11-18 に 30 日から変更。使用回数の上限は無し）。
  - 一時的なアクセストークン: 保存しない。1 回だけ使う。
- 可能な限り、動的シークレットや短命のトークンに寄せる。
- 開発コンテナでは、マシン ID（読み取り専用の Viewer。権限は必要なプロジェクトの最小限）を使う。人間の認証情報（利用者のログイン）は、
  2026-11-18 にコンテナから外した（`infisical logout`）。書き込み（`register` 系）・マシン ID の作り直しは、利用者がそのときだけ
  `infisical login` で入る。再ビルドで残す仕組みとして `.devcontainer/persist-infisical-login.sh` はあるが、
  暗号化のパスフレーズも同じボリュームにあり、保管時の保護ではない。ログインを残すかどうかは利用者が決める。エージェントは、ログアウトを促さない。
- 秘密の平文のバックアップ（`~/.infisical/secrets-backup` など）は、エージェントが動くコンテナの中に置かない。
  置くならホストの `~/Backups/`（権限 700）。2026-11-18 にホストの `~/Backups/infisical-20261118/` へ移した（数週間後に不要なら削除を相談）。
- 無料プランには、バージョン履歴・監査ログ・復元が無い。編集・削除は元に戻せないので、プロジェクトを分けて影響を限る。
- 秘密の値は表示・ログ・コミットしない。Infisical CLI の `secrets set KEY=@file` はファイルを読まない（パス文字列が保存される）。
  値は base64 にして `--file` で渡す。
- 実ユーザーのセッション（Playwright の保存状態など）は、リフレッシュトークンの再利用検知でユーザーが全端末で
  ログアウトされる恐れがある。保存済みの値は使わず、毎回新しく書き出してもらう。
- playwright-auth の Client Secret は 2026-12-02 に期限切れ。期限前にローテーションする。

## GitHub Actions のデプロイ用（2026-11-08 に決定。詳細は移した）

`.github/workflows/deploy.yml` は、GitHub の OIDC で Infisical（`docunavi-deploy`）にログインし、Cloudflare の API トークンを実行時に取る。
マシン ID・Subject・Audience・トークンの権限と期限・Environment の設定は、[docs/deploy.md](../../docs/deploy.md) の「GitHub Actions のデプロイ用の秘密（OIDC・Infisical・Cloudflare）」（2026-11-18 に、ここから移した）。守ること:

- GitHub には Cloudflare の秘密を置かない。
- `docunavi-deploy` には `CLOUDFLARE_API_TOKEN` と `CLOUDFLARE_ACCOUNT_ID` 以外の秘密を入れない（無料プランは復元ができないので、プロジェクトを分けて影響を限る）。`docunavi-devcontainer` は入れない。
- Cloudflare の API トークン `docunavi-github-actions-deploy` の期限は **2027-02-06**。期限前（2027-01 中）に作り直し、Infisical の値を更新する。
