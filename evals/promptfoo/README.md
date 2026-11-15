# evals/promptfoo/（Promptfoo の試験導入）

2026-11-16 に始めた**試験**。複数ターンの会話を Promptfoo の YAML で書き、採点は既存の golden set の runner と評価器（`evals/golden/`）に任せる。
`evals/golden/` は変えていない。アプリ・`frontend/`・本番イメージ・CI には入っていない（npm の依存はこのフォルダーの中だけ）。

| ファイル | 役割 |
|---|---|
| `promptfooconfig.yaml` | 会話 8 本（17 ターン。`pf01`-`pf17`。`pf09` 以降は「分かる範囲を添える」型で、2026-11-16 に追加）。期待は結果を見る前に、資料と追質問の仕様から決めた。**結果を見て緩めない** |
| `provider.py` | プロバイダー。`evals.golden.runner.run_golden` を呼ぶ（**実 API・課金**）。安全確認・指標・評価器は runner のものを再利用 |
| `assert_golden.py` | 判定。8 つの評価器が全ターンで通れば合格（LLM 採点なし） |
| `mock_provider.py` | API を呼ばない模擬。設定と判定の流れだけを確かめる（回答の品質は測れない） |

## 実行

準備: `cd evals/promptfoo && npm ci`（`promptfoo@0.125.1`、MIT）。外部への送信を避けるため、次の環境変数を付けて実行する
（`PROMPTFOO_DISABLE_TELEMETRY` は、効き目をネットワークで確かめていない）。

```bash
export PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 PROMPTFOO_DISABLE_SHARING=1 PROMPTFOO_DISABLE_REMOTE_GENERATION=1
export PROMPTFOO_PYTHON=/workspaces/docunavi/.venv/bin/python

# API を呼ばない確認（設定の検証 + 模擬）
npx promptfoo validate config -c promptfooconfig.yaml
npx promptfoo eval -c promptfooconfig.yaml --providers file://mock_provider.py --no-cache --no-share

# 実 API（課金。質問文・回数・費用を示して承認を得てから）。LLM・埋め込みは Vertex AI、検索はローカルの Postgres
GOLDEN_REAL=1 GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=docunavi-app EMBEDDING_USE_VERTEXAI=true \
  DB_BACKEND=pg DATABASE_URL=<ローカルの Postgres> PROMPTFOO_GOLDEN_MAX_LLM_CALLS=30 \
  npx promptfoo eval -c promptfooconfig.yaml --no-cache --no-share -o results.json
```

- `GOLDEN_REAL=1` が無ければ、プロバイダーは API を呼ばずに `error` を返す。本番の DB・有料キー・LangSmith への送信は、runner と同じく受け付けない。
- 各ターンの後に `PROMPTFOO_GOLDEN_PACE_S` 秒（既定 15）待つ。429（`rate_limited`）が出ると、再試行で LLM のステップが増え、軌跡と時間の判定が崩れるため（2026-11-16 の初回で 2 回出た）。
- LLM 呼び出しの全体の上限は `PROMPTFOO_GOLDEN_MAX_LLM_CALLS`（既定 30）。会話は 1 本ずつ流す（`maxConcurrency: 1`）。
- **使わないもの:** `promptfoo validate target`（設定と応答を Promptfoo の遠隔の検証に送りうる）、`promptfoo share`、`promptfoo view` の共有。結果は、質問・回答の本文を含むので、外へ出さない。
- 試験の記録（初回の実測など）は、`evals/golden/README.md` ではなく、ここに追記する。

## 合格の線（2026-11-16 にユーザーが決定）

「資料にない詳細」を聞かれたときに、素っ気ない返答（「資料には記載がありません。」だけ）を、分かる範囲を添える返答に直す件の成功の条件。

1. Vertex 経由のこの評価で、`pf09`-`pf17` が合格し、`evals/golden/`（15 ターン）に回帰がない（`compare.py`）。数回繰り返して通る。
2. **最終ゲート: `localhost:3000` で、同じ会話（酸素濃度 → 昼夜の気温差 → 夕方）をやり直し、「夕方」の回答が改善されている。** これを満たすまで、成功と認めない（ユーザーの決定）。1 は 2 の代わりにならない（経路が Vertex と無料キーで違い、画面の表示は評価に含まれない）。
3. 完了かどうかは、ユーザーが決める。

## 測定条件の固定（2026-11-16 の反省）

プロンプトを比べる実行は、条件を固定してから流す。段階 A で、`.env` のフォールバック設定により、遅い回だけ別モデルが答えていた（3 回。`provider.py` が `GEMINI_FALLBACK_MODEL` を空にして、今は切ってある）。

- 固定するもの: 主モデル、フォールバック（無効）、認証・API 経路、各層（Promptfoo・アプリ・SDK）の再試行、ヘッジ。**ヘッジは、provider ではまだ切っていない**（本番と同じ）。
- 本番の回数の前に、同時実行 1 の少数（追質問を含む 2〜3 会話）で予備実行し、採用モデル・呼び出し数・429・タイムアウトを確かめる。
- 回数は「実行済み」「やり直しの予定」「ヘッジ・再試行を含む総呼び出しの上限」「費用の上限」に分けて報告する。件数と API 呼び出し数は一致しない。
- 混入した会話は、ターン単位でなく会話単位で除く（履歴が次のターンに影響する）。混入した実行は診断用で、正式な比較は新しい実行 ID で行う。
- ターンごとの採用モデルは、まだ記録していない（時間の一致による推定しかできない）。

## 注意

- `promptfooconfig.yaml` の `must_include` の各要素は、言い換えの**リスト**（`[["罠"]]`）。文字列を直接書くと 1 文字ずつ照合されるので、`provider.py` が止める。
- Promptfoo の結果は `~/.promptfoo/`（ローカルの DB）にも残る。
- `npm install` は、`esbuild` の postinstall が許可制で動かない警告を出す。今の使い方（YAML・Python）では要らない。
- 評価の観点が増えすぎたら、まず `evals/golden/chat_golden.yaml` に足すほうが小さい。この試験の残す・戻す条件は、golden set の評価器で足りない会話の観点を、設定の量を増やさずに書けるか。

## 戻す（削除）

削除は、対象と参照を示して承認を得てから。対象は `evals/promptfoo/` と、`evals/CLAUDE.md` の接頭辞 `pf` の行、`docs/development.md` のこの節への言及。
