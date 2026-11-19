# ドキュメントの索引

文書は、読む目的で 4 つに分けています。**状態**の見方: 「現状」は今の実装・運用に合わせて更新している文書、「設計メモ」「計画」は設計の検討と判断の材料（実装の契約ではない）、「記録」は当時の経緯や作業の結果（過去の記述は、現状に合わせて書き換えない）。「＋記録」は、その状態の文書に、日付つきの経緯や測定が同居していること。
エージェント（Claude Code）向けの、どの文書が何の正本かの表は [CLAUDE.md](CLAUDE.md)。

## まず読む（設計の中心）

| 文書 | 状態 | 内容 |
|---|---|---|
| [features.md](features.md) | 現状 | 利用者の操作ごとの機能一覧と、条件・制限・提供の状態 |
| [architecture.md](architecture.md) | 現状 | 質問の処理の流れ（LangGraph）、ディレクトリ構成、本番とローカルの環境構成（図）、ログインと画面の配信 |
| [search-design.md](search-design.md) | 現状＋記録 | 検索の設計の現状（pgvector のベクトル検索に、ハイブリッド・リランカー・GraphRAG を重ねる）と、ベクトル単独に決めた 2026-10-18 の根拠・測定の結果、見直す条件 |
| [guardrails.md](guardrails.md) | 現状 | 入出力ガードレール（拒否の条件、ストリーミング中の逐次検査）と限界 |
| [configuration.md](configuration.md) | 現状 | 環境変数、LLM の遅延・失敗・切断への対処（ヘッジ・フォールバック・有料キーへの切り替え）、資料グループのアクセス制御 |
| [decisions/](decisions/README.md) | 記録 | 重要な決定と、その理由（1 件 1 ファイル） |
| [api.md](api.md) | 現状 | `/health`・`/chat`・`/chat/stream`・資料一覧などの仕様とエラー |

## 開発・運用

| 文書 | 状態 | 内容 |
|---|---|---|
| [development.md](development.md) | 現状 | ローカルの起動、依存の更新方針、LangSmith Studio・Promptfoo（試験導入）の使い方 |
| [testing.md](testing.md) | 現状 | テストの種類と実行方法（pytest・Vitest・E2E・実 API）、CI、本番の `/chat` の自動テスト、確認できていないこと |
| [deploy.md](deploy.md) | 現状＋記録 | Cloud Run・Cloudflare へのデプロイ手順と、本番運用の記録 |
| [pg-backup.md](pg-backup.md) | 現状 | 本番 Postgres のバックアップ（週 1 回、R2 へ）と復元の確認 |
| [r2-key-issuer.md](r2-key-issuer.md) | 現状＋記録 | R2 のキーを発行・取り消す Job と、キーの期限・入れ替えの運用。冒頭の「状態」は 2026-11-20 の読み取り（gcloud、等級 A）で、本文は特記が無い限り 2026-10-21 時点。DB バックアップ用の `r2-issue-backup-rw` の詳細は [pg-backup.md](pg-backup.md) |
| [alloydb-test-env.md](alloydb-test-env.md) | 現状 | 検証環境（AlloyDB Express）の再作成の名前・手順・落とし穴（Preview の製品。本番の移行ではない） |
| [access-separation.md](access-separation.md) | 現状 | プロジェクトごとの権限・資格情報の分離の方針と、DocuNavi の対応表（GCP・Infisical・MCP）、整理の候補 |
| [roadmap.md](roadmap.md) | 現状 | 順序と状態（何が済み、何が待ちか） |

## 設計メモ・計画

| 文書 | 状態 | 内容 |
|---|---|---|
| [target-architecture.md](target-architecture.md) | 設計メモ | 目標構成と、Go・Django などを採用する条件 |
| [go-architecture-plan.md](go-architecture-plan.md) | 設計メモ＋記録 | Go と Python の役割分担の計画（Go の SSE 中継は、学習と検証が目的）。中継は 2026-10-25 から本番に配備済みで、本文上限・レート制限などを足した版を 2026-11-09 に配備（[decisions/0005](decisions/0005-go-sse-relay-without-prod-limit.md)）。それ以外の Go 化は未着手。冒頭の「現状」は 2026-11-20 に更新 |
| [r2-ingest-design.md](r2-ingest-design.md) | 設計メモ＋記録 | PDF の取り込みの自動化（R2 の通知と Queue）。第 1 版（突き合わせ）と第 2 版（Queue）の両方が実装・配備済みで、冒頭の「状態」は 2026-11-20 の読み取り。Scheduler は週 1 回で、5 分おきは一時停止（2026-11-20 にホストの Claude が Scheduler を読んだ。ホストの報告で、コンテナは未再確認。[roadmap.md](roadmap.md)）。本文の叩き台と 2026-10-26 の切り替えは、当時の記録 |
| [mcp-tool-design.md](mcp-tool-design.md) | 設計メモ＋記録 | 検索を MCP のツールにする設計。`docunavi-mcp` として 2026-10-21 に配備し、確認したのは接続と 1 回の検索まで。現在の状態は同文書の「配備と確認」の「現在の状態（2026-11-09 時点）」、順序は [roadmap.md](roadmap.md) |
| [access-control-plan.md](access-control-plan.md) | 設計メモ＋記録 | ロール別の資料グループ制御の計画。フェーズ 1〜3 は実装済みで、本番は 2026-10-26 から `enforce`（配信中のリビジョンの環境変数でも `enforce`、2026-11-20、gcloud の読み取り、等級 A。仕様は [configuration.md](configuration.md)、状態は [roadmap.md](roadmap.md) の ⑧）。フェーズ 4（管理画面）は条件付き |
| [image-generation-design.md](image-generation-design.md) | 設計メモ | 資料のヒーロー画像の自動生成。基盤を続けるか止めるかは未決定（[backlog.md](backlog.md)） |
| [backlog.md](backlog.md) | 計画 | 画面・情報設計の改善案と、判断待ちの運用・基盤の項目、測ってから決める案の一覧。決定後に残った作業の行を含む（元は 2026-10-18 の画面レビュー） |

## 記録

| 文書 | 状態 | 内容 |
|---|---|---|
| [r2-rollout.md](r2-rollout.md) | 記録 | PDF を R2 から読む本番への切り替え |
| [ingestion-stage4-design.md](ingestion-stage4-design.md) | 記録 | 段階 4 の取り込みの構成案（2026-10-14）と確認の記録。4a・4b は済み（[roadmap.md](roadmap.md)）で、Supabase・Cloud SQL の比較は当時のまま |
| [history-rewrite.md](history-rewrite.md) | 記録 | Git の履歴の書き換え（2026-10-25、2026-11-14）と、旧ハッシュの対応表 |
| [ux-research.md](ux-research.md) | 記録 | UI/UX リサーチの記録 |

文書には日付を入れ、確認したことと未確認のことを分けて書いています。測定の数字には、日付・件数・証拠等級（A: 実機・実測 / B: 公式の文書 / C: 第三者の記事）を添えます。
