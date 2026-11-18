# テスト用の架空資料（PDF）の一括生成

資料の検索・回答を評価するための、**テスト用の架空資料**を大量に作る道具。本文は、配置したオープンモデルが書く。この道具は、
生成の依頼を作り、結果を検証して、PDF と分類マニフェストにする。**アプリの検索対象ではない**（`storage/pdf/` にも `evals/corpora/` にも置かない）。

**状態（2026-11-04）:** ローカルの実装とテストだけ。GPU・vLLM・Cloud Run は一度も動かしていない（`gpu_worker.py` の `VllmEngine` は未実行）。
reportlab での PDF 作成も、この環境には reportlab が無く、未実行（描画のテストはスキップされる）。

## 流れ（第一候補: Cloud Run GPU Jobs + vLLM のオフライン一括推論）

```
bibles/*.yaml ─ plan ─▶ prepare ─▶ requests/round-NN.jsonl ─▶ 【GPU ジョブ: gpu_worker.py】─▶ results/round-NN.jsonl + .meta.json
 (共通設定)    (CPU)    (CPU)                                  モデルを 1 回読み込み、全依頼を生成して終了                │
                                                                                                          ingest（CPU: 検証・重複判定・受け入れ）
                                          落ちた分だけ次の回の依頼へ ◀──────────────────────────────────────────────────┤
                                                                                                          render（CPU: PDF 化）─▶ manifest.jsonl ─▶ verify
```

- **GPU に載せるのは生成だけ。** 検証・重複判定・PDF 化・マニフェストは、すべて CPU 側（ローカル、または GPU の無い小さな実行環境）。
- **GPU は 1 台・1 タスク。** 全依頼を 1 回で vLLM に渡し、vLLM が同時数（`--max-num-seqs`）まで走らせ、1 件終わるたびに待っている次を入れる。
- 逐次の経路（`run`）もある。`FakeBackend`（配線の確認）と、OpenAI 互換のエンドポイント（常設の vLLM や、Gemini の互換口）を 1 件ずつ呼ぶ。

## 使い方（ローカル。外部に接続しない）

```bash
python -m evals.synth plan                                  # 依頼票の計画。各対象 20 件（既存を含む）→ 新規 74 件
python -m evals.synth prepare --sample-per-target 2         # 8 件分の依頼 → out/requests/round-01.jsonl
python -m evals.synth.gpu_worker --engine fake --requests out/requests/round-01.jsonl \
    --out out/results/round-01.jsonl --meta out/results/round-01.meta.json   # 偽のエンジンで GPU ジョブの代わり
python -m evals.synth ingest --sample-per-target 2 --round 1
python -m evals.synth.make_fonts   # 1 回だけ: Noto Sans JP の Regular/Bold（静的な TTF）を fonts/ に作る（fonttools が要る。Git 管理外）
python -m evals.synth render                                # reportlab が要る。fonts/ があれば Noto Sans JP、無ければ IPA ゴシック（太字なし）
python -m evals.synth verify                                # 全ページの「テスト用の架空資料」・ハッシュ・マニフェストの整合
python -m evals.synth report --usd-per-hour <時間単価 USD>  # 実測の集計と、80 件・1,000 件への見積もりの補正
```

表紙: `out/cover/<対象>/<資料の id>.jpg`（2:1・1200×600 の JPEG）があれば、`render` が先頭に表紙を 1 ページ足す（題名・画像・「初版｜2026年11月」。表紙にもページ番号以外の帯を入れる）。
画像は Vertex の `gemini-3.1-flash-lite-image` で 1 枚ずつ作る（2026-11-18 に minato-coffee の 19 件。画像の生成は、この道具には入れていない）。既に PDF がある資料に表紙を足すときは、その PDF を消してから `render` する（本文が変わらないと作り直されないため）。
出力先は `evals/synth/out/`（Git 管理外）。`--out-dir` で変えられるが、`storage/` `evals/corpora/` `evals/fixtures/` `app/` などの中は拒否される。
reportlab は `requirements.txt` のとおり、一時的な仮想環境に入れる（アプリの依存には入れない）。

## 同時数の比較（4 → 8 → 16）

同じ依頼・同じ出力上限で、同時数だけを変えた GPU ジョブを別々に流し、全件完了までの時間・不合格・エラー・**合格文書あたりの費用**を比べる。
台帳には触れない。同時数 16 を意味のある比較にするには、依頼が 16 件以上要る（各対象 4 件 = 16 件）。

```bash
python -m evals.synth bench-prepare --sample-per-target 4 --max-tokens-per-doc 3000     # 全試行で共有する依頼（違う入力は拒否）
# GPU ジョブを、同時数を変えて 3 回: --max-num-seqs 4 / 8 / 16、出力は bench/results.c04.jsonl など（gpu/README.md）
python -m evals.synth bench-ingest --sample-per-target 4 --label c04 --usd-per-hour <時間単価 USD> --job-seconds <Cloud Run の実行の秒数>
python -m evals.synth bench-report                       # 並べて、推奨の同時数を出す
```

推奨の規則は、事前に決めてある（`bench.py` の冒頭）: エラー・欠落のある試行（メモリ不足など）は除き、不合格が最小の試行のうち、
合格 1 件あたりの費用が最小のものから 5% 以内で、同時数が最も小さいものを選ぶ。つまり、メモリ不足や効率悪化があれば、自動的に低い同時数が選ばれる。
`--job-seconds` を渡さないと、全体の時間はプロセス内の経過時間になり、**コンテナの起動・イメージの取得を含まない**（表に印が付く）。

## 上限（生成量・GPU・再試行）

| 層 | 上限 | 場所 |
|---|---|---|
| GPU の台数 | 1 台、1 タスク、`--parallelism=1`、`--max-retries=0` | ジョブの設定（`gpu/README.md`） |
| 同時数 | `--max-num-seqs`（既定 4。4・8・16 を比較して決める） | `gpu_worker.py` |
| 1 回のジョブの依頼数 | `--max-requests`（既定 16。超えたら何も生成せず止まる） | `gpu_worker.py` / `prepare --max-requests` |
| 実行時間 | `--deadline-seconds`（既定 0 = 無効。正の値のときだけ、`--chunk-size` の切れ目で止まる。読み込みの時間も数える）と、ジョブの `--task-timeout`（GPU のジョブの最大 12 時間。無効にした今は、これだけが上限） | 両方 |
| 再試行 | 1 つの資料の試行は `--max-attempts`（既定 2）まで。落ちた分だけ次の回へ。上限に達したら `failed` で止まり、`--retry-failed` を付けるまで作らない | `prepare` / `ingest` |
| 費用 | `prepare --max-total-cost-usd`（累計。見積もりが収まる件数まで依頼を減らす）、逐次の `run` は `--max-docs --max-tokens --max-cost-usd`（fake 以外は 3 つとも必須）、連続の失敗で全体を止める | `batch.py` `budget.py` `pipeline.py` |
| 重複 | 同じ `key`（bible の版・種類・変化・指示の版）は作り直さない。依頼済み（`queued`）は、`--requeue-queued` まで出し直さない。本文は、既存の資料と受け入れ済みの本文との類似度（5 文字の連続の Jaccard）0.5 以上を落とす | `plan.py` `pipeline.py` |
| 再開 | 台帳（追記だけの JSONL。書きかけの行は無視）と、出力先のロック | `ledger.py` |

## 生成後の仕上げ（第二段階。ユーザーが決定、2026-11-04）

目的はアプリ用のサンプルデータなので、生成した本文は、**Claude が読んで整える**（プロンプトでの抑止や、厳しい検証機構の追加はしない）。

- 直すのは、シナリオ内・資料間（既存の資料を含む）の矛盾、誤字、表示上の問題（PDF の字体に無い文字など）だけ。
- 創作、文字数の過不足、太字・`###` などの装飾は、サンプルとして問題なければ許容する。
- 元の生成結果（`results.<label>.jsonl`）は変えず、仕上げた版は別名（例: `results.<label>.final.jsonl`）で置く。
- `validate.py` の長さ・書式の規則と、数値の突合は、不合格の目安として出るが、仕上げの合否には使わない（合計や列挙から出した正しい数値も落とす）。

## 共通設定（bible）と矛盾の防ぎ方

`bibles/<対象>.yaml` に、対象ごとの事実（id・文・タグ）と、資料の種類（種類 × 変化 = 1 件ずつ）を置く。既存の資料
（`evals/corpora/*/source/`、`evals/fixtures/zahard_txt/`）の事実と矛盾しないように起こした。生成の指示には、種類のタグに合う事実だけを渡す。

検証は LLM を使わない規則だけ（`validate.py`）。**数値の突合**: 本文の数量・日付・時刻は、事実の文字列に含まれていなければ落ちる。
連絡先（電話・メール・URL・郵便番号）、実在の企業名、Markdown の小集合の外の記法、前置き、途中で終わった文、繰り返し、日本語の少なさも落とす。
**限界:** 数値を伴わない矛盾（言い換えで事実を変える、固有名の取り違え）は、見つからない。必要なら、あとから LLM の照合を足す（今は無い）。
既存の資料には、bible に書いていない事実（例: 店舗一覧の住所）もある。bible と既存の資料の食い違いは、人が目で確かめる（未実施）。

全 PDF に「テスト用の架空資料」の帯を全ページに入れ、メタデータにも `DOCUNAVI-SYNTHETIC-TEST` を入れる（`render.py`。`verify` が確かめる）。
帯は各ページの文字として抽出されるので、取り込むと検索のチャンクにも入る（評価の資料としては許容。気になるなら、取り込み側で除く）。

## 本番の検索に混ざらない分離

- 既定では、**本番に入れない。** 出力は `out/`（Git 管理外）。検索の評価は、ローカルの `db`（Postgres）か別の DB に入れて行う。
- 入れる場合は、承認を取ってから: 専用の collection `synthetic-test`（マニフェストの `collection`）を作り、どのロールにも付けない
  （`ACCESS_MODE=enforce` では見えない）。ただし MCP は運用者向けに全資料を検索できるので、これだけでは混ざる。
  取り込み側（`app/ingestion/`）に「PDF のメタデータに `DOCUNAVI-SYNTHETIC-TEST` があるものは、明示のフラグなしでは拒否」を足すことを提案する（`app/` の変更なので、承認後）。
- **ローカルの画面で PDF を開くとき:** `out/pdf/` を `storage/pdf/synthetic-test/` に写す（`cp -r evals/synth/out/pdf/. storage/pdf/synthetic-test/`）。
  アプリは `storage/pdf/<資料群>/<source>` を探し、DB の `source` が `minato-coffee/xxx.pdf` の形なので、この入れ子で見つかる。
  `storage/pdf/synthetic-test/` は `.gitignore` で除外している（大きく、作り直すたびに履歴が膨らむため）。
  検索の基準線のテスト（`test_bm25_*`・`test_eval_retrieval.py`）は、承認済みの 3 資料群（`cafe` `guides` `zahard`）の写しだけを使うので、置いても数字は動かない
  （`tests/bm25_fixtures.py` の `approved_pdfs`）。本番の R2 へ上げるのは、承認を取って、リリースのときに別作業で行う。
- 資料の分類（`target` `doc_type`）は、マニフェストに書いてある。`document_targets` `document_types` の語彙（4 + 4）の内側にしてある。

## 費用の見積もり（実測前の仮定）

実測は `report` / `bench-report` が出す。仮定の式（`measure.py`）: 回ごとの固定時間（起動 + モデルの読み込み + 書き出し）+ 1 件あたりの生成時間 × 件数。
再試行は、落ちた分だけ次の回に回し、合格率は 1 回目と同じと仮定する。**8〜16 件の測定は、バッチが小さい**ので、1,000 件の見積もりは高めに出る
（`--gain` で倍率を変えられるが、実際の倍率は、件数を増やして測らないと分からない）。時間単価は、GPU + CPU + メモリの合計を入れる
（GPU 単価は公式の価格表。CPU・メモリの単価は未確認のため、実際の請求で確かめる）。Gemini バッチとの比較は、`--gemini-in` `--gemini-out` を渡すと、同じトークン数の費用が並ぶ（品質・再試行の差は含まない）。

## 未確認・承認待ち

- GPU 実機（vLLM、`LLM.chat` の引数、モデルの日本語品質、メモリ、同時数ごとの速度）。モデルの候補: Qwen3.6-35B-A3B の BF16 版（RTX PRO 6000）、Gemma 4 26B-A4B。
- Cloud Run Jobs の GPU で、Cloud Storage のボリュームが使えるか（GPU の文書には記載がない）。
- reportlab での PDF の見た目（描画のテストはスキップされた）。
- GCP のリソース作成・GPU の実行・IAM・本番への取り込み・push・デプロイ・Gemini の実 API は、対象・費用上限・コマンドを示して承認を得る。
