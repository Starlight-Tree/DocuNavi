# ドキュメントの索引

文書は、読む目的で 4 つに分けています。**状態**の見方: 「現状」は今の実装・運用に合わせて更新している文書、「設計メモ」「計画」は設計の検討と判断の材料（実装の契約ではない）、「記録」は当時の経緯や作業の結果（更新しない）。
エージェント（Claude Code）向けの、どの文書が何の正本かの表は [CLAUDE.md](CLAUDE.md)。

## まず読む（設計の中心）

| 文書 | 状態 | 内容 |
|---|---|---|
| [architecture.md](architecture.md) | 現状 | 質問の処理の流れ（LangGraph）、ディレクトリ構成、本番とローカルの環境構成（図）、ログインと画面の配信 |
| [search-design.md](search-design.md) | 現状 | 検索の設計（PDF のチャンク化、pgvector、ハイブリッド・リランク・GraphRAG）と、測定の結果、見直す条件 |
| [guardrails.md](guardrails.md) | 現状 | 入出力ガードレール（拒否の条件、ストリーミング中の逐次検査）と限界 |
| [configuration.md](configuration.md) | 現状 | 環境変数、LLM の遅延・失敗・切断への対処（ヘッジ・フォールバック・有料キーへの切り替え）、資料グループのアクセス制御 |
| [decisions/](decisions/README.md) | 記録 | 重要な決定と、その理由（1 件 1 ファイル） |
| [api.md](api.md) | 現状 | `/health`・`/chat`・`/chat/stream`・資料一覧などの仕様とエラー |

## 開発・運用

| 文書 | 状態 | 内容 |
|---|---|---|
| [development.md](development.md) | 現状 | ローカルの起動、テスト、E2E、CI、依存の更新方針 |
| [deploy.md](deploy.md) | 現状＋記録 | Cloud Run・Cloudflare へのデプロイ手順と、本番運用の記録 |
| [pg-backup.md](pg-backup.md) | 現状 | 本番 Postgres のバックアップ（週 1 回、R2 へ）と復元の確認 |
| [r2-key-issuer.md](r2-key-issuer.md) | 現状 | R2 のキーを発行・取り消す Job |
| [access-separation.md](access-separation.md) | 現状 | プロジェクトごとの権限・資格情報の分離の方針と、DocuNavi の対応表（GCP・Infisical・MCP）、整理の候補 |
| [roadmap.md](roadmap.md) | 現状 | 順序と状態（何が済み、何が待ちか） |

## 設計メモ・計画

| 文書 | 状態 | 内容 |
|---|---|---|
| [target-architecture.md](target-architecture.md) | 設計メモ | 目標構成と、Go・Django などを採用する条件 |
| [go-architecture-plan.md](go-architecture-plan.md) | 設計メモ | Go と Python の役割分担の計画（Go の SSE 中継は、学習と検証が目的） |
| [r2-ingest-design.md](r2-ingest-design.md) | 設計メモ | PDF の取り込みの自動化（R2 の通知と Queue） |
| [ingestion-stage4-design.md](ingestion-stage4-design.md) | 設計メモ | 取り込みの構成案（段階 4） |
| [mcp-tool-design.md](mcp-tool-design.md) | 設計メモ | 検索を MCP のツールにする設計 |
| [access-control-plan.md](access-control-plan.md) | 設計メモ | ロール別の資料グループ制御の計画 |
| [image-generation-design.md](image-generation-design.md) | 設計メモ | 資料のヒーロー画像の自動生成 |
| [backlog.md](backlog.md) | 計画 | 画面・情報設計の、まだ着手していない改善（2026-10-18 のレビュー） |

## 記録

| 文書 | 状態 | 内容 |
|---|---|---|
| [r2-rollout.md](r2-rollout.md) | 記録 | 元 PDF を R2 から読む本番への切り替え |
| [history-rewrite.md](history-rewrite.md) | 記録 | Git の履歴の書き換え（2026-10-25、2026-11-14）と、旧ハッシュの対応表 |
| [ux-research.md](ux-research.md) | 記録 | UI/UX リサーチの記録 |

文書には日付を入れ、確認したことと未確認のことを分けて書いています。測定の数字には、日付・件数・証拠等級（A: 実機・実測 / B: 公式の文書 / C: 第三者の記事）を添えます。
