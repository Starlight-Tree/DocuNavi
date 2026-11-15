# evals/（検索・回答の評価）

全体の方針は、ルートの [CLAUDE.md](../CLAUDE.md)。道具・指標・実測の経緯は [README.md](README.md)。ここには、取り違えやすい点だけを置く。

## corpora と fixtures

| 場所 | アプリの検索対象 | 評価での扱い |
|---|---|---|
| `corpora/<名前>/`（`cafe` `guides`） | 対象。同じ PDF を `storage/pdf/<同じ名前>/` にも置いている（一致はテストが確かめる） | `--app` が `corpora/*/questions.yaml` を全部読む |
| `fixtures/<名前>/`（`exact` `menu` `gym` `two_column` ほか） | **対象外**。`storage/pdf/` にも本番 DB にも入れない | `--corpus evals/fixtures/<名前>` で個別に測る |

- `corpora/` に PDF や質問を足す・作り直すときは、`storage/pdf/` の写しも同じにする。`--app` は `storage/pdf/` に無い PDF を探して止まる。
- アプリの検索対象に入れるつもりのない資料は、`corpora/` ではなく `fixtures/` に置く（`exact` は `corpora/` から移した経緯がある。README の `fixtures/exact/` の行）。
- `storage/pdf/zahard/` は `fixtures/zahard_pdf/` の `make_pdf.py` で作る。質問は `evals/questions.yaml`。
- `cache/` は Git 管理外（実 API で作った埋め込みの保存先）。コミットしない。

## synth/（テスト用の架空資料の一括生成）

アプリの検索対象ではない。出力は `synth/out/`（Git 管理外）で、`storage/` `corpora/` `fixtures/` には置かない。GPU・有料の手順は承認が要る（`synth/README.md`、`synth/gpu/README.md`）。

## PDF の前提

- 質問の `evidence` は PDF の見た目から書かれている。抽出結果に無ければ、`--allow-missing-evidence` を付けない限り、終了コード 2 で止まる（黙って無効にしない）。
- 承認済みの質問の期待値（`expected_doc_ids` `evidence`）は変えない。PDF 化や抽出でずれたら、質問側を直さずにずれを報告する。
- PDF は同じ入力から同じバイト列になるように作る（`source/make_pdf*.py`）。作り直したら、PDF と質問の対応が崩れていないかを `pytest tests/test_eval_retrieval.py` で確かめる。

## 質問 ID の衝突防止

`--app` は全部の評価セットを読み、**ID が重なると結果の対応が付かないので止まる**（`tests/test_eval_retrieval.py::test_app_mode_rejects_ids_shared_between_question_sets`）。
今使っている接頭辞は、次のとおり。新しい評価セットは、ここに無い接頭辞を選び、この表に足す。

| 接頭辞 | 評価セット |
|---|---|
| `q` | `questions.yaml`（ザーハード） |
| `c` | `corpora/cafe` |
| `a` `t` | `corpora/guides` |
| `e` | `fixtures/exact`（`e001`-`e050`） |
| `m` `p` `x` | `fixtures/menu` |
| `xc` `xs` `xg` | `fixtures/cross_collection` `cross_collection_short` `gym` |
| `h` | `fixtures/two_column` |
| `gr` | `fixtures/graph_rules`（`gr001`-`gr022`。GraphRAG の評価専用。`python -m scripts.eval_graph`） |
| `gx` | `fixtures/graph_rules_ext`（`gx001`-`gx062`。遠い参照・3 条またぎを増やした GraphRAG の評価専用。`python -m scripts.eval_graph --set graph_rules_ext`。最初のセット `graph_rules` の質問・計画・結果は変えない） |
| `n` | `answers/questions_research.yaml` |
| `gc` | `golden/chat_golden.yaml`（グラフ全体の golden set。`gc01`-`gc15`。検索の評価セットではなく、`--app` は読まない。`evals/golden/README.md`） |
| `pf` | `promptfoo/promptfooconfig.yaml`（Promptfoo の試験導入の会話。`pf01`-`pf08`。`--app` は読まない。`evals/promptfoo/README.md`） |

`fixtures/menu` の `x001`-`x004` があるので、`x` 始まりの新しい ID は使わない（`exact` が `x001`-`x050` だった時代に衝突した）。

## 実 API と結果ファイル

- 実 API を呼ぶのは、質問の埋め込み（`scripts/eval_vector.py` `scripts/eval_pipeline.py embed --apply`）と、回答の評価（`scripts/eval_answers.py`。LLM も呼ぶ）。始める前に、質問文・回数・概算費用を示して承認を得る。手順は `.claude/skills/search-eval/SKILL.md`。
- `results/` の既存の実測ファイルを上書きしない。取り直すときは `--out` で別名にする。記録として残すものがある（README の「ザーハードが .txt だった時代の実測」）。
- 評価の数字を出したら、質問の数・上位何件・どの構成かを添える。確認していない構成の数字を「実測」と書かない。
