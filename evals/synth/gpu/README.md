# GPU ジョブ（Cloud Run Jobs + vLLM）の案

**この文書のコマンドは、どれも実行していない。** 承認を得てから、ユーザーがホストで実行する（リソース作成・有料実行・IAM は承認が要る）。
既存の本番（`docunavi-app` の Cloud Run・DB・権限）には触れない。別のプロジェクトを使う案（下の `$PROJECT`）。

確認できたこと（[GPU の文書（jobs）](https://docs.cloud.google.com/run/docs/configuring/jobs/gpu)）: Cloud Run Jobs は GPU を使え、1 インスタンスに GPU は 1 枚。
RTX PRO 6000（96GB。20 CPU・80GiB 以上）は asia-southeast1・asia-south2・europe-west4・us-central1、L4（24GB。4 CPU・16GiB 以上）は
asia-southeast1・europe-west1・europe-west4・us-central1・us-east4 ほか。東京は無い。`--no-gpu-zonal-redundancy`（冗長なし。安い）で作る。
新しいプロジェクトの初回のデプロイで、リージョンごとに GPU 3 枚分の割り当てが付く。並列数は割り当てより小さくする
（使う GPU = タスクあたりの GPU × `--parallelism`）。文書に無かったこと: `--tasks` と `--task-timeout` の上限、Cloud Storage のボリュームが使えるか。

## 1 台に抑える設定

| 設定 | 値 | 目的 |
|---|---|---|
| `--tasks` | 1 | 1 つの実行 = 1 つのタスク |
| `--parallelism` | 1 | GPU の同時使用を 1 台に（割り当て 3 のうち 1 だけ使う） |
| `--max-retries` | 0 | 失敗時の自動の再実行をしない（再試行は、検証のあとに CPU 側が「次の回」として出す） |
| `--task-timeout` | `43200s`（GPU のジョブの最大 12 時間。[クォータの表](https://cloud.google.com/run/quotas)。独自の短い上限は設けない、ユーザーの指示 2026-11-04） | 時間の唯一の上限（`--deadline-seconds` は既定で無効）。超えると、結果の書き出しの前に止まる。12 時間 = 約 $46（$3.82/h の概算） |
| 実行できる ID | 運用者の ID と専用のサービスアカウントだけ | ジョブに HTTP の入口は無い。実行はジョブの実行権限（IAM）だけ |

予算: プロジェクトに予算アラートを付け（承認が要る）、`prepare --max-total-cost-usd` で依頼の数を抑える。

## 案（`$PROJECT` は新しく作るプロジェクト。請求先のひも付けは承認が要る）

```bash
# 1. イメージ（Artifact Registry に置く。ビルド・push は承認が要る）。重みはイメージに入れず、Cloud Storage に置く案
gcloud builds submit evals/synth --config <cloudbuild.yaml> --project $PROJECT      # Dockerfile は gpu/Dockerfile

# 2. ジョブ（RTX PRO 6000 の例。L4 なら --gpu-type nvidia-l4 --cpu 8 --memory 32Gi）
gcloud run jobs create synth-gen --project $PROJECT --region asia-southeast1 \
  --image $IMAGE --gpu 1 --gpu-type nvidia-rtx-pro-6000 --no-gpu-zonal-redundancy \
  --cpu 20 --memory 80Gi --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 43200s \
  --service-account synth-runner@$PROJECT.iam.gserviceaccount.com

# 3. 実行（同時数ごとに 1 回。引数だけを変える）
gcloud run jobs execute synth-gen --project $PROJECT --region asia-southeast1 --wait \
  --args=--engine,vllm,--model,/models/qwen3.6-35b-a3b,--requests,/work/bench/requests.jsonl,\
--out,/work/bench/results.c04.jsonl,--meta,/work/bench/results.c04.meta.json,\
--max-num-seqs,4,--max-requests,16,--max-model-len,8192,\
--load-format,runai_streamer,--model-loader-extra-config,{"concurrency":16,"memory_limit":17179869184}

# 4. 実行の時間（起動込み）を読む → bench-ingest の --job-seconds へ
gcloud run jobs executions describe <execution> --project $PROJECT --region asia-southeast1 \
  --format='value(status.startTime,status.completionTime)'
```

- 依頼・結果・モデルの重みの置き場所は、Cloud Storage（バケットを専用に作る。承認が要る）をボリュームとして `/work` `/models` に載せる案。
  **未確認:** GPU のジョブでボリュームが使えるか。使えなければ、重みはイメージに入れる（イメージが大きくなり、起動が遅くなる）、
  依頼と結果は、起動時・終了時に `gcloud storage cp` 相当で出し入れする（その分、ワーカーに足す）。
- Qwen3.6 は、思考の出力を止める引数（`--chat-template-kwargs '{"enable_thinking": false}'`）が要るかもしれない。**未確認。** 8〜16 件の測定で、出力に思考が混ざらないかを見る。
- 重みは元の BF16 版 `Qwen/Qwen3.6-35B-A3B`（約 72GB、リビジョン `995ad96`）をそのまま使う。事前 FP8 版も `--quantization fp8` も使わない（ユーザーの方針）。メモリは、40 層のうち全注意は 10 層だけ（KV は 1 トークン約 20KiB）なので、重み約 67GiB + KV・状態で 96GB に収まる見込み。**机上の見積もりで、vLLM での実測はまだ。**

## 重みの並列読み込み（2026-11-04 に採用。効果は未測定）

初回試験は、読み込みだけで 2191 秒（約 33 分）かかった。現行のベースイメージには `runai-model-streamer[s3,gcs,azure]>=0.15.7` が入っているので、
ワーカーの `--load-format runai_streamer --model-loader-extra-config '{"concurrency":16,"memory_limit":17179869184}'` で使う（`gpu_worker.py` の引数。
既定は無指定で、従来どおり）。読み先は、既存のボリューム `/data/models/qwen3.6-35b-a3b` のまま。BF16・量子化なしは維持。

- 値は文書の例（concurrency 16）と、80GiB のコンテナに収まる CPU バッファ 16GiB。測って決めた値ではない。
- **未確認:** 2191 秒のうち重みの読み込みが占める割合（`import`・コンパイル・CUDA グラフの初期化も入っている。vLLM のログの `Loading weights took …` で分かる）、
  ナイトリーの vLLM が Qwen3.6 をストリーマーで読めるか、FUSE 越しで並列が効くか。
- 効かなかったときの候補: `gs://` を直接読む（ADC の確認が要る）、`concurrency` の変更。

## 測定の手順（承認後）

1. `bench-prepare --sample-per-target 4`（16 件）。
2. 同時数 4 → 8 → 16 の順に、同じ依頼を別々の実行で流す（モデルの読み込みは実行ごとに 1 回）。1 つずつ結果を見て、メモリ不足・エラーが出たら、次へ進む前に止まる。
3. `bench-ingest` ×3 → `bench-report`。推奨の同時数を決める。
4. 推奨の同時数で、8 件（各対象 2 件）の `prepare` → 実行 → `ingest` → `report`。合格 PDF あたりの費用と、起動込みの所要時間を実測し、80 件・1,000 件の見積もりを補正する。
