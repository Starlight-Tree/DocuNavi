# 検索の評価（evals）

資料の検索（`app/agents/tools.py` の `search_chunks`、BM25）だけを、質問と正解の組で採点します。
**LLM も DB も呼びません**（無料で、毎回同じ結果になります）。検索を変える前後の差を数字で見るための道具です。

## 実行

```bash
.venv/bin/python -m scripts.eval_retrieval --mode bm25 --k 5            # evals/results/baseline_bm25.json
.venv/bin/python -m scripts.eval_retrieval --mode bm25 --k 2 --out evals/results/baseline_bm25_k2.json
```

| 引数 | 意味 |
|---|---|
| `--mode` | 検索の方式。今は `bm25` だけ（`scripts/eval_retrieval.py` の `MODES` に足す） |
| `--k` | 上位何ファイルまでを評価するか（既定 5） |
| `--questions` | 評価セット（既定 `evals/questions.yaml`） |
| `--out` | 出力先（既定 `evals/results/baseline_<mode>.json`） |

評価セットが資料と合っていない（期待ファイルや根拠の文が無い）ときは、何も書かずに終了コード 2 で止まります。

## ファイル

| パス | 内容 |
|---|---|
| `questions.yaml` | 質問と正解。項目の意味はファイル先頭のコメント |
| `results/baseline_bm25.json` | 現行 BM25 の実測（k=5）。質問ごとの返した本文と失敗の種類を含む |
| `results/baseline_bm25_k2.json` | 同じ評価を k=2 で。**アプリが LLM に渡す件数（`TOP_K`）はこちら** |
| `../scripts/eval_retrieval.py` | 採点の本体 |
| `../tests/test_eval_retrieval.py` | 採点の道具のテストと、baseline からの退行の検知 |

## 指標

答えが資料にある質問（`factual` / `paraphrase` / `multi_hop`）について:

| 指標 | 意味 |
|---|---|
| `doc_recall_at_k` | 期待ファイルが上位 k 件に入った割合。`multi_hop` は期待ファイルが全部入ること |
| `keyword_hit_rate_at_k` | 期待ファイルごとに、そのファイルの検索結果の本文に `evidence` のどれか 1 つが含まれた割合。ファイル単位では測れない「答えの文が返ったか」を見る |
| `mrr` | 期待ファイルが最初に現れた順位の逆数の平均。入らなければ 0 |

資料に答えが無い質問（`out_of_scope`）について:

| 指標 | 意味 |
|---|---|
| `empty_answer_accuracy` | 何も返さなかった割合。`scope_kind` ごとの内訳も出す |

`scope_kind` は 2 種類です。`off_topic` は資料と無関係（天気など）、`on_topic_absent` はザーハードの話だが資料に書かれていない
（人口・首都など）。後者は、文章の中の語だけで一致する検索には本質的に難しく、**「空を返す」を期待値にするのが厳しすぎる**可能性があります。
LLM が「資料に無い」と答えられるかは、この評価では測れません（回答の評価は別の段階）。

## 失敗の種類（`failure_category`）

| 種類 | 意味 |
|---|---|
| `no_hit` | 何も返らなかった |
| `wrong_file` | 何か返ったが、期待ファイルが（全部は）入っていない |
| `wrong_rank` | 期待ファイルは返ったが、先頭ではない |
| `keyword_miss` | 期待ファイルは先頭だが、根拠の文が返っていない |
| `paraphrase_miss` | `paraphrase` の質問での `no_hit` / `wrong_file` / `keyword_miss`（言い換えが原因かを見るため、まとめている） |
| `unexpected_result` | `out_of_scope` なのに何か返った |

## baseline の見方

2026-10-15 時点の実測（資料 3 ファイル、質問 36 件。回答可能 24、資料に無い 12）:

| | k=5 | k=2 |
|---|---|---|
| doc_recall | 21/24 | 20/24 |
| keyword_hit | 21/24 | 20/24 |
| mrr | 0.8958 | 0.8958 |
| empty_answer（off_topic） | 8/8 | 8/8 |
| empty_answer（on_topic_absent） | 0/4 | 0/4 |

- **factual 16/16、paraphrase 3/5、multi_hop 2/3（k=5）。** 資料の言葉で聞けば拾え、資料に無い語での言い換えは拾えません。
- **失敗 8 件の内訳（k=5）。** `paraphrase_miss` 2（気温差・毒ガス。資料は「温度差」「有害なガス」）、`wrong_rank` 1、
  `wrong_file` 1（`q030`。2 つ目のファイルが返らない）、`unexpected_result` 4（`on_topic_absent` の全部）。
- **見る順番。** (1) 失敗した質問の `retrieved`（何が返ったか）を読む。(2) `failure_category` で偏りを見る。
  (3) 偏りが「言い換え」なら、語の一致では原理的に拾えないので、意味で拾う手段（同義語、埋め込み）の候補になる。
  偏りが「資料に無いのに返る」なら、空を返す条件（スコアのしきい値）の問題で、検索方式を変えても残る。

## 限界

- **件数が少ない。** 回答可能 24 件なので、1 件の増減が約 4 ポイント。割合ではなく件数で比べる。
- **資料が 3 ファイルしかない。** `k` が 3 以上なら結果は同じ（`k=3` と `k=5` は同一）。ファイル単位の指標は甘く出る。
  資料が増えてから意味が出る。
- **質問は資料を読んで書いた。** 実際の利用者の質問ではない。実装に合わせ込まないように、q030 以降は検索の結果を見る前に書いた。
  質問や期待値を結果に合わせて直さない（直すなら、理由をコミットに書く）。
- **`keyword_hit` は部分文字列の有無。** 返った文が答えとして正しいか、LLM が正しく答えるかは測らない。
- **回答の質（根拠への忠実さ、拒否の仕方）は測らない。** LLM を呼ぶ別の評価になる。

## 更新のしかた

- 質問を足す、または検索を変えたら、上の 2 コマンドで baseline を取り直し、数字が変わったときだけコミットする
  （`generated_at` だけが変わる差分は、コミットしない）。
- `tests/test_eval_retrieval.py` は、現在の検索が baseline より該当数を減らしたら失敗する（CI の `backend` ジョブで動く）。
  資料が変わったときは skip になるので、baseline を取り直す。
- 質問の元は `tests/rag_eval_cases.py` と同じもの（q001-q029）を含む。両方を変えるときは同期テストが落ちる。

## 次に見るもの

1. **PDF を増やしたあと**（段階 1・2）: 同じコマンドで取り直し、`corpus.sha256` と `search_params` を突き合わせる。
   `GENERIC_TERM_RATIO` と `MIN_SHARED_TERMS` は 3 ファイルに合わせた値なので、資料量が増えたときの挙動を記録する。
2. **`on_topic_absent` の扱い**: 検索で空を返すのか、LLM に「資料に無い」と答えさせるのかを決める。決めてから、ベクトル検索の
   類似度のしきい値を評価する（ベクトル検索は常に近傍を返すので、しきい値が無いと全部 `unexpected_result` になる）。
3. **`wrong_file`（q030）**: 2 つ目のファイルが返らない原因（件数の絞り込み `RELATIVE_SCORE_CUTOFF`、文の選び方）は未調査。
