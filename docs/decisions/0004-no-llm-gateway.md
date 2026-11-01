# 0004 LLM ゲートウェイを使わず、耐障害性はアプリ内で持つ

- 状態: 実装済み
- 日付と決めた人: 2026-10-31（利用者。ルールの置き場所の整理と合わせて）

## 背景
LiteLLM Proxy・Bifrost・claude-code-router（CCR）などの外部ゲートウェイを検討していた。Claude Code のログ・メモリーが失われた件（2026-10-31）と、構成の整理を受けて見直した。

## 決定
- LLM ゲートウェイは採用せず、削除した。LLM の遅延・失敗への対処は `app/agents/resilience.py`（ヘッジ付き再試行、別モデルへのフォールバック、有料キーへの切り替え）に置く。
- Gemini CLI は docunavi の実行環境に入れない（開発コンテナにも入れない）。
- Cloud Code・Gemini Code Assist・Data Agent Kit・Jupyter 系の VS Code 拡張は入れない。GCP の操作は原則 gcloud CLI。
- 画像生成の Cloud Run Job は星庭ジョブ（`hoshiniwa-job`）のリポジトリで管理し、このリポジトリに `job/` を置かない。

## 結果と限界
- ゲートウェイを挟まないので、中間の障害点とオーバーヘッドがない。
- 別モデルへのフォールバックは任意で既定は無効（本番は 2026-10-21 から有効）。**未確認:** 実際に切り替わった回、取り消した試行の課金、切り替えた回の回答の品質（[configuration.md](../configuration.md)）。

## 出典
[.claude/rules/dev-environment.md](../../.claude/rules/dev-environment.md)、[target-architecture.md](../target-architecture.md) の「LLM ゲートウェイ」、[architecture.md](../architecture.md)、ルートの CLAUDE.md
