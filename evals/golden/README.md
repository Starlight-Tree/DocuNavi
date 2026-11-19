# golden set（グラフ全体の回答の評価）

> **実 API の前に:** ローカルの実 API は、2026-11-15 から無料キーだけ（[dev-environment.md](../../.claude/rules/dev-environment.md)）。この評価（`runner.py`）は Vertex AI が要るため、実行するときは、質問文・回数・費用を示して事前に承認を得る。

検索の評価（`evals/README.md`）とは別に、**検索 → 回答のグラフ全体**を、ターンごとの期待で採点する。2026-11-09 に、手動で確かめた 15 ターンを `chat_golden.yaml` にした。

| ファイル | 役割 |
|---|---|
| `chat_golden.yaml` | golden set（質問・期待する `search_documents` の有無・必要な事実・禁止事実・期待する出典・軌跡・上限）。ID は `gc01`-`gc15` |
| `evaluators.py` | 評価器 8 つ（API なし）。`accuracy` `grounding` `conciseness` `search_need` `trajectory` `latency` `cost` `no_irrelevant_mention` |
| `runner.py` | グラフを `/chat/stream` と同じ経路（ストリーミング）で流して結果の JSON を書く（**実 API・課金**）。Vertex AI + ローカルの Postgres だけを受け付ける。指標は本番と同じ `app/metrics.py` の `ChatMetrics` |
| `compare.py` | 変更前後の 2 つの結果を比べ、回帰があれば終了コード 1（API なし） |
| `trace_check.py` | 結果と LangSmith のトレースを突き合わせる（LangSmith を読むだけ）。`POST /api/v2/runs/query` で一括取得（cursor で全件、429 は Retry-After と指数バックオフ + ジッターで再試行） |
| `results/` | 結果の JSON。既存のファイルは上書きしない |

## LangSmith の評価の型との対応

LLM を呼ばない規則ベースの版にしてある（判定の LLM を足すと、呼び出しと費用が増え、判定自体が揺れる）。

| 型 | 評価器 | 見るもの |
|---|---|---|
| hallucination | `grounding` + `accuracy` の禁止事実 | 回答の数字が検索結果にあるか、期待した出典が当たったか、資料に無い具体を作っていないか |
| tool-selection | `search_need` | `search_documents` を呼んだか（挨拶は呼ばない期待）、回数が上限内か |
| trajectory | `trajectory` | LLM と検索のステップの並び（追質問は「補完 → 検索 → 回答」） |

## 測る指標（本番のログ `chat_metrics` と同じ数え方）

総時間（`latency_s`）の p50・p95・p99、TTFT（最初の本文まで）の p50・p95・p99、LLM 呼び出し回数、入力・出力トークン、費用（単価を渡したとき）、`search_documents` の回数、無関係な結果への言及。
TTFT は、出力ガードが末尾 32 文字を保持するので、短い回答では総時間に近くなる。**2026-11-09 の最初の baseline は非ストリーム（TTFT なし）。ストリームで測った結果とは別物として比べる**（`compare.py` は、片方に TTFT が無ければその比較を省く）。

## 実行

```bash
# 1. 実 API で流す（課金。質問・回数・費用を示して承認を得てから）。LLM・埋め込みは Vertex AI、検索はローカルの Postgres
GOLDEN_REAL=1 GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=docunavi-app EMBEDDING_USE_VERTEXAI=true \
  DB_BACKEND=pg DATABASE_URL=<ローカルの Postgres> \
  python -m evals.golden.runner --label baseline --repeat 3 --trace
# 2. 変更の後にもう一度流して、比べる
python -m evals.golden.compare evals/golden/results/baseline.json evals/golden/results/after.json
# 3. トレースと突き合わせる（--trace で流した結果）
python -m evals.golden.trace_check evals/golden/results/baseline.json
# 4. 保存済みの結果を、今の golden set・評価器で採点し直す（API なし）
GOLDEN_RESULTS=evals/golden/results/baseline.json pytest tests/test_golden_chat_real.py
```

- `known_issue` は、今は満たせないと分かっている評価器（pytest では xfail、`compare.py` では回帰に数えない）。
- 費用は、確かめた単価を `GOLDEN_PRICE_IN_PER_MTOK` / `GOLDEN_PRICE_OUT_PER_MTOK`（USD / 100 万トークン）で渡したときだけ出す。未設定ならトークンだけ。
- **CI では実 API を流さない**（CI に Vertex AI の資格情報が無い）。CI が確かめるのは、golden set の形・評価器・比較・安全確認（`tests/test_golden_evaluators.py`）。実測の前後比較は、ここに書いた手順で手元で行う。
