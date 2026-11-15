# evals/promptfoo/（Promptfoo の試験導入）

2026-11-16 に始めた**試験**。複数ターンの会話を Promptfoo の YAML で書き、採点は既存の golden set の runner と評価器（`evals/golden/`）に任せる。
`evals/golden/` は変えていない。アプリ・`frontend/`・本番イメージ・CI には入っていない（npm の依存はこのフォルダーの中だけ）。

| ファイル | 役割 |
|---|---|
| `promptfooconfig.yaml` | 会話 4 本（8 ターン。`pf01`-`pf08`）。期待は結果を見る前に、資料と追質問の仕様から決めた。**結果を見て緩めない** |
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
- LLM 呼び出しの全体の上限は `PROMPTFOO_GOLDEN_MAX_LLM_CALLS`（既定 30）。会話は 1 本ずつ流す（`maxConcurrency: 1`）。
- **使わないもの:** `promptfoo validate target`（設定と応答を Promptfoo の遠隔の検証に送りうる）、`promptfoo share`、`promptfoo view` の共有。結果は、質問・回答の本文を含むので、外へ出さない。
- 試験の記録（初回の実測など）は、`evals/golden/README.md` ではなく、ここに追記する。

## 注意

- `promptfooconfig.yaml` の `must_include` の各要素は、言い換えの**リスト**（`[["罠"]]`）。文字列を直接書くと 1 文字ずつ照合されるので、`provider.py` が止める。
- Promptfoo の結果は `~/.promptfoo/`（ローカルの DB）にも残る。
- `npm install` は、`esbuild` の postinstall が許可制で動かない警告を出す。今の使い方（YAML・Python）では要らない。
- 評価の観点が増えすぎたら、まず `evals/golden/chat_golden.yaml` に足すほうが小さい。この試験の残す・戻す条件は、golden set の評価器で足りない会話の観点を、設定の量を増やさずに書けるか。

## 戻す（削除）

削除は、対象と参照を示して承認を得てから。対象は `evals/promptfoo/` と、`evals/CLAUDE.md` の接頭辞 `pf` の行、`docs/development.md` のこの節への言及。
