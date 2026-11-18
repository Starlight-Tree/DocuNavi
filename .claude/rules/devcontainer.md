---
paths:
  - ".devcontainer/**"
---

# 開発コンテナの定義（.devcontainer/）を触るとき

方針の全体は [dev-environment.md](dev-environment.md)（2026-11-18 に、再ビルドとツールの入れ場所の節をそこから移した。本文は変えていない。承認の要る 2 点の要約は dev-environment.md にも残してある）。
gcloud・gh・Playwright・Docker プロキシなどの手順と限界は [.devcontainer/README.md](../../.devcontainer/README.md)。

## 再ビルドの可能性

- 開発コンテナの定義は、このリポジトリのファイルだけで完結させる。星庭ジョブなど他のプロジェクトのフォルダーは、載せない（権限と資格情報をプロジェクトごとに分けるため。docs/access-separation.md）。
- `devcontainer.json`・`.devcontainer/docker-compose.yml`・`.devcontainer/Dockerfile` に、ホストの絶対パス・別プロジェクトのファイル・外部のアカウント（GCP プロジェクト ID など）を足す変更は、事前にユーザーに確認する
- 再ビルドはホスト側の作業。承認なしに実行しない

## ツールの入れ場所（2026-10-31）

- gcloud・gh・Go・docker-cli・Playwright のライブラリーは、`.devcontainer/Dockerfile`（イメージのビルド時）に入れる。`postCreateCommand` に apt の導入を足さない（作成のたびに毎回走り、キャッシュされない）
- Chromium の本体は、名前付きボリューム `playwright-browsers` のまま（`install-playwright.sh` が取得と起動確認を行う）
- Node の feature はイメージの後に入るので、npm で入れるものは Dockerfile に移せない。Vercel の CLI・MCP・プラグインは入れない（本番は Cloudflare）
- `install-gh.sh`・`install-go.sh` は残してある（`postCreateCommand` からは呼ばない）。Go の版とハッシュは `install-go.sh` と Dockerfile の両方にあるので、上げるときは両方を合わせる
