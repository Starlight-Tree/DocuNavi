# 4b 取り込みの自動化: 設計の叩き台

状態（2026-10-21）: **叩き台。コードも、Cloudflare・GCP 側の作成も、まだ無い。** 方針は決まった（末尾の「決めたこと」）: **第 1 版は突き合わせ方式、Queue は後から。**
着手するかどうか自体は、まだ決めていない。仮承認の項目（決めたこと 2・3・6）は後で変えてよい。
目的は、資料を R2 に置くだけで、検索に出るところまで進むようにすること。ただし実用上の必要は小さい（資料は運営者が置く共通のもので、頻度が低い。
手動の取り込みは 2 コマンド）。主な価値はイベント駆動・キュー・リトライの学習。

```text
R2 に PDF を置く → R2 のイベント通知 → Cloudflare Queue → Cloud Run Job が pull → 取り込み → 埋め込み → DB
```

## すでにあるもの（作り直さない）

- 取り込みは R2 から読める: `python -m app.ingestion --input r2://<資料群>/<ファイル名> --apply`（`app/ingestion/__main__.py`）。
  資料群は R2 のキーの先頭の名前。形の検査（`COLLECTION_PATTERN`）と、サイズの上限（`MAX_ORIGINAL_BYTES`）もある。
- 冪等: `documents` は内容のハッシュと状態、`chunks` は `UNIQUE (document_id, chunk_index)` の upsert。同じ PDF を 2 回処理しても結果は変わらない。
  読めない PDF は `documents.status = 'failed'` と `failure_reason` に残り、終了コードは 3。
- 埋め込みは別コマンド（`python -m app.ingestion.embed --apply`。実 API で課金。埋め込みが無いチャンクだけを対象にし、`--max-chunks` `--max-calls` で上限がある）。
- 読み取り用の R2 キー `r2-app-read`（自動ローテーション。[r2-key-issuer.md](r2-key-issuer.md)）。取り込み用の `r2-ingest-rw` は空の入れ物だけ。

## 公式で確かめたこと（Cloudflare、2026-10-21 に資料を検索）

| 項目 | 内容 |
|---|---|
| イベント | `object-create`（`PutObject` `CopyObject` `CompleteMultipartUpload`）と `object-delete`。メッセージは `account` `action` `bucket` `object.key` `object.size` `object.eTag` `eventTime`（`copySource` は `CopyObject` だけ） |
| 絞り込み | 規則に `prefix` / `suffix` を付けられる（正規表現は不可）。バケットあたり 100 規則まで。重なる規則は作れない |
| pull | HTTP の POST で pull と ack。短いポーリング（空なら空の応答がすぐ返る）。`batch_size` 既定 5・最大 100、`visibility_timeout` 既定 30 秒・最大 12 時間。ack しないと `visibility_timeout` 後に再配信。`retries` で明示的に戻せ、遅延も指定できる |
| 認証 | Queues の `read` と `write` の**両方**が要る（ack がキューの状態を書き換えるため）。公式の手順は「アカウントの Queues の編集」で、1 つのキューに絞れるかは確認できなかった |
| 再試行 | `max_retries`（既定 3）に達したメッセージは、DLQ が無ければ**消える**。DLQ は同じ管轄のキュー。消費者が無い DLQ のメッセージは 4 日で消える |
| 無料プラン | Queues は無料プランにも付き、1 日 10,000 操作（読み・書き・削除の合計）。**保持は最長 24 時間**（有料は 14 日） |
| 料金（有料時） | pull の操作は 100 万回あたり $0.40 |

**公式に書かれていない / 未確認:** 通知が少なくとも 1 回届くのか（重複や欠落）、順序の保証、`suffix` が大文字小文字を区別するか、
アカウントが Workers の無料・有料のどちらか、Cloud Scheduler の最短間隔（cron の書式は使えるが、1 分刻みが許されるか公式で確認できていない。
料金は 1 ジョブあたり月 $0.10、無料枠は請求先アカウントあたり 3 ジョブ。すでに 4 つあるので、足すと +$0.10/月の見込み）。

## 第 1 版: 突き合わせ方式（Queue なし）

```text
Cloud Scheduler（間隔は長めでよい）──▶ Cloud Run Job docunavi-ingest-sync（SA: docunavi-ingester）
  ① R2 の資料群ごとに r2://<資料群> を取り込む（既存の取り込み。--apply）
  ② 終了コードが 0 または 3 なら、埋め込む（既存の embed。--apply。--max-chunks で上限）
  ③ 1・2 なら埋め込まず、WARNING 以上のログで知らせる
```

- 新しい部品は、Job の入口（資料群を巡って上の順に呼ぶ小さなモジュール）だけ。取り込みと埋め込みの本体は書き換えない。
- 冪等なので、変わっていない PDF は「変わらない」で終わり、埋め込みも足りないチャンクだけ。何度起動しても結果は同じ。通知の取りこぼしという問題が、そもそも無い。
- 毎回、全 PDF を R2 から取得して解析する。数件のうちは問題にならない。増えたら、R2 の一覧（キーとサイズ・ETag）と DB を先に比べて、変わったものだけ取り込む形に絞る。
- 埋め込みの対象は「埋め込みが無いチャンク」だけなので、取り込みが終わった資料の分だけ課金される。
- **終了コード 3（壊れた PDF が混ざった）でも埋め込む理由:** 壊れた PDF は取り込みのたびに同じく失敗し、終了コードは 3 のまま変わらない。
  3 で埋め込みを止めると、壊れた PDF 1 件のせいで、他の資料が検索に出なくなる。壊れた文書にはチャンクが無く、埋め込みの対象にもならない。
  「成功したときだけ」を「埋め込める状態のときだけ」と解釈し、0 と 3 を含めた（決めたこと 5。承認済み）。1・2 は、まだ成功していない状態なので埋め込まない。
- R2 から消えた PDF（DB にはある）は、削除せず、ログで知らせるだけにする。

## 第 2 版（後で）: Queue を足す

遅延を減らしたくなったとき、または学習としてイベント駆動を試したいときに足す。第 1 版の Job は残し、通知の取りこぼしを拾う定期の突き合わせとして使い続ける。
以降の節は、Queue を足す場合の設計。

## 方式: pull を第一候補にする

| | push（Worker が Cloud Run を呼ぶ） | **pull（Cloud Run Job が Queue を引く）** |
|---|---|---|
| 認証の手間 | Worker で Google の ID トークンを作る。Cloud Run を認証必須のまま呼ぶ仕組みが増える | Cloudflare の API トークンを Secret Manager に置くだけ |
| 遅延 | 小さい | Scheduler の間隔（分単位）。低頻度の用途では実害が小さい |
| Cloudflare 側の部品 | Worker（consumer）が増える | Queue と通知の規則だけ |
| 失敗の扱い | Worker の再試行と DLQ | ack・retry・DLQ を Job が明示的に制御する |

pull の理由は、認証必須の方針を保てることと、Cloudflare 側に書くコードが無いこと。

**比較として: Queue を使わない案。** Scheduler が Job を起こし、Job が R2 の一覧と DB の `documents.content_hash` を比べて、足りない PDF だけを取り込む。
部品が少なく、通知の欠落（24 時間の保持切れ）にも強い。ただし、イベント駆動とキューを学ぶ目的には合わない。
**どちらを採っても、「通知が欠けたとき」の備えとして、この一覧の突き合わせ（`r2-verify` の考え方）は要る。**

## 構成

```text
R2 docunavi-documents ──(規則: object-create, suffix .pdf)──▶ Queue docunavi-ingest（HTTP pull を有効化）
Cloud Scheduler（数分おき）──▶ Cloud Run Job docunavi-ingest-poll（SA: docunavi-ingester）
  ① Queue から pull（batch 小さめ）
  ② メッセージごとに: bucket とキーを検査 → 取り込み → 埋め込み
  ③ 結果で ack / retry を決める → 空なら終了
```

- Job はアプリと同じイメージを使い、入口に小さなモジュール（`app/ingestion/queue_worker.py` の想定）を足す。取り込みと埋め込みの本体は呼ぶだけで、書き換えない。
- `parallelism` と `task count` は 1。同じ文書を同時に処理しない。
- `visibility_timeout` は処理時間より長く取る（15 分を仮置き。PDF と埋め込みの実測で決める）。

## メッセージの扱い（入力は信用しない）

キューの中身は、データとして検査する。命令としては扱わない。

1. `bucket` が `docunavi-documents` でなければ、ack して捨てる。
2. `object.key` が `<資料群>/<ファイル名>.pdf` の形で、資料群が `COLLECTION_PATTERN` に合い、`..` や余分な階層を含まないこと。合わなければ ack して捨てる（警告のログ。再試行しても直らない）。
3. `object.size` が `MAX_ORIGINAL_BYTES` を超えるなら、取り込まずに ack し、ログに残す。
4. 取り込みは `r2://<資料群>/<ファイル名>` で呼ぶ。キーの文字列をシェルやパスにそのまま渡さない。

## 失敗の扱い

| 状況 | 判断 | 動き |
|---|---|---|
| 取り込み成功（終了コード 0） | 完了 | 埋め込みへ進み、成功したら ack |
| PDF が読めない（終了コード 3。`documents.status = 'failed'`） | 何度やっても同じ | **ack する**（失敗は DB の `failure_reason` に残る） |
| DB・R2・Gemini の一時的な失敗（終了コード 1） | 直るかもしれない | retry（遅延付き）。上限に達したら DLQ |
| 設定の誤り（終了コード 2） | 直らない | ack せず、WARNING 以上のログで知らせる |
| R2 にオブジェクトが無い（削除済み・名前違い） | 再試行しても同じ | ack して捨てる |
| Job が途中で落ちた | 不明 | ack していないので、`visibility_timeout` 後に再配信される。冪等なので安全 |
| 同じ内容が重複して届いた | 想定内 | ハッシュが同じなので「変わらない」で終わる |
| 同じキーに別の内容が上書きされた | 更新 | ハッシュが変わり、チャンクが入れ替わる。埋め込みは新しいチャンクだけ作る |

通知は取りこぼしうる（無料プランの保持は 24 時間）ので、定期の突き合わせ（上記）を別に持つ。**状態の持ち主は DB の `documents` で、Queue ではない。**

`object-delete` は**扱わない**（規則を作らない）。R2 から消しても DB の文書は残る。消す運用が要るときに、別に決める。

## 権限（最小）

| 主体 | 持たせるもの | 持たせないもの |
|---|---|---|
| `docunavi-ingester`（Job の SA、新設） | DB の URL・Gemini キー・Queue の pull トークン・R2 の読み取りキーの各 Secret への `secretAccessor`（Secret 単位） | プロジェクト全体のロール、R2 への書き込み |
| Scheduler 用の SA | この Job への `run.invoker` だけ | — |
| Cloudflare の pull トークン | Queues の read + write（公式の手順では、キー単位に絞れるか未確認） | R2 や他の製品の権限 |

- **R2 の鍵は読み取りで足りる**（取り込みは R2 を読むだけ）。`r2-ingest-rw` を使わず、読み取り専用のキーを使う案が最小権限になる。決めること 3。
- Claude の日常の ID（`docunavi-viewer`）には、Job を**実行する**権限までを検討する。作成・更新・IAM は、これまでどおりあなたが実行する。
- Cloudflare 側（Queue と通知の規則の作成）は書き込みなので、あなたの権限で行う。

## 費用の見積り（いずれも公式で要再確認）

- Queues: 数分おきの pull は 1 日数百回。無料プランの 10,000 操作 / 日に収まる見込み。
- Cloud Scheduler: +$0.10/月の見込み（無料の 3 ジョブをすでに超えているため）。
- Cloud Run Job: 空の回は数秒。無料枠の範囲の見込み。
- Gemini の埋め込み: 資料を足したときだけ。上限は `embed` の `--max-chunks` `--max-calls` で抑える。

## 進め方（段階。各段で止まれる）

| 段 | 内容 | 課金・外部への書き込み |
|---|---|---|
| 0 | この文書。決めたこと（下）を埋める（済み。仮承認の3項目は後で変えてよい） | なし |
| 1 | **済み（2026-10-21）。** 第 1 版の入口 `app/ingestion/sync.py` を、偽の取り込みと偽の埋め込みで実装し、テストした（`tests/test_ingestion_sync.py`。終了コードごとの分岐、0・3 で埋め込む、1・2 で埋め込まない、を網羅） | なし |
| 2 | Job・SA・Secret を作る（あなたが実行）。Scheduler は作らず、Job を手動で 1 回実行して確かめる（PDF が変わっていなければ、何も保存されないことを確かめる） | Cloud Run Job が少々 |
| 3 | PDF を 1 件 R2 に足して Job を手動実行し、検索に出るまでを通しで確かめる。壊れた PDF の分岐も 1 回通す | 埋め込みの実 API（1 件分） |
| 4 | Scheduler を作って定期実行にする | Scheduler +$0.10/月の見込み |
| 5（後で） | 第 2 版: Queue と通知の規則を足す。まずテスト用のプレフィックス（例: `_ingest-test/`）で、メッセージの実物を 1 通だけ pull して形を確かめる | Queue の操作が少々（無料枠内） |

### 段 5 の実施メモ（2026-10-25）

- **テスト用のキーは、バケット直下・スラッシュなし（例: `ingest-probe-1.txt`）にする。** 上の表の `_ingest-test/` のようなプレフィックスは使わない。
  毎時の `docunavi-ingest-sync` は、バケット直下のプレフィックスをすべて「資料群」として扱う（`r2_store.collections()`）ので、`_ingest-test/x.pdf` は本番の検索に取り込まれて埋め込まれ、
  `_ingest-test/x.txt` は取り込みが終了コード 1 で終わって、その回の埋め込みを止める。直下のキーは資料群にならない。規則は `prefix=ingest-probe` で絞る。確認したら、テスト用のキーは消す。
- **Cloudflare MCP（`cloudflare`、ローカルスコープ）で Queues と R2 の通知を扱うには、認可のやり直しが要る。** 既定の Minimal では製品のスコープが無く、Queues・R2 は 403 になる（ゾーンも Workers も同じ）。
  `/mcp` の **Reconnect は保存済みのトークンを使い直す**ので効かない。手順: `/mcp` → `cloudflare` → **Clear authentication** → **Authenticate** → 最初の画面の **Advanced** → 「Edit base scopes」に
  `queues.write workers-r2.write`（スペース区切り。Minimal のまま）→ Continue → 次の画面の許可が 3 から 5 に増えたことを確かめて Authorize。
  次の画面の「Edit Permissions」には、最初の画面で要求した分しか出ないので、そこでは足せない。「Full access」は広すぎるので使わない。広げた認可を狭め直すかは、利用者が決める（R2 の write は広い。2026-10-25 は「また使うのでそのまま」と決めた。狭めるなら同じ手順の逆）。
  スコープ名は、403 のメッセージの「Reconnect and grant one of: …」に出る名前をそのまま使う。
- **実測した通知のメッセージ（2026-10-25、証拠等級 A）。** Cloudflare の API（`PUT /accounts/{id}/r2/buckets/{bucket}/objects/{key}`）で置いた約 6 秒後に、Queue（`http_pull`、`batch_size` 1）から 1 件取れた。
  本文は JSON の**文字列**（`body` を `JSON.parse` する）で、キーは `account` `bucket` `eventTime` `action` `object`（`key` `size` `eTag`）。例（`account` は伏せた）:
  `{"account":"<伏せた>","bucket":"docunavi-documents","eventTime":"2026-10-24T20:12:23.200Z","action":"PutObject","object":{"key":"ingest-probe-1.txt","size":15,"eTag":"…"}}`。
  メタデータは `CF-Content-Type: json` だけ（`CF-sourceMessageSource` は無い。API で直接入れたメッセージにだけ付く）。`attempts` は 0 から。時刻は UTC。
  設計への含意: キーは URL エンコードの扱いを実物で確かめてから使う（今回は ASCII のみ）。`eTag` と `size` は、突き合わせ（DB の登録済みの版との比較）に使える。
- **通知は遅れることがあり、順番も保証されない（実測）。** wrangler の `r2 object put --remote` で置いた分（`eventTime` 20:07:05Z、6 バイト）は、約 3 分待った pull 4 回がすべて空で、**約 6 分 15 秒後（20:13:20Z ごろ）にようやく Queue に入った**。
  その間に API で置いた分（20:12:23Z）は約 6 秒で届いていて、**後に置いた方が先に着いた**。wrangler の経路が通知の対象外ということではない（最初は「出なかった」と書いたが誤り）。
  遅れた理由は未特定（規則の作成直後の初回だけか、配信の揺れか）。設計への含意: 反映が数分遅れても壊れないこと（突き合わせの 1 時間おきの Job が最後の砦）、到着順に頼らず `eTag` や `eventTime` で新旧を決めること。
  ダッシュボードのアップロードは未試験。なお、Queue に直接入れたメッセージ（`POST .../messages`）の pull と ack は正常だった。

## 決めたこと

2026-10-21、利用者の方針（Queue は後から・突き合わせから・埋め込みは取り込み成功時だけ）に沿って埋めた。
**確定** は承認済み、**仮承認** は私が置いた案を利用者が仮に承認したもの（後で変えてよい）。

| # | 項目 | 内容 | 状態 |
|---|---|---|---|
| 1 | 方式 | 第 1 版は突き合わせ方式。Queue は必要になってから足す。**着手するか自体は未決**（必要は小さい） | 方式は確定、着手は未決 |
| 2 | 実行間隔 | 1 時間おきから。突き合わせ方式では遅延が実害にならず、Scheduler の最短間隔の問題も出ない。資料を足した直後に反映したければ、Job を手動で実行する | 仮承認 |
| 3 | R2 の鍵 | 読み取りだけで足りる。`r2-app-read` を共用する（Job の SA に Secret 単位で `secretAccessor`）。`r2-ingest-rw` は使わない。共用で困るのは、アプリ用の鍵に問題があると取り込みも止まること。取り込み専用の権限分離は、将来の検討 | 仮承認 |
| 4 | Cloudflare の pull トークン | 第 1 版では**要らない**（Queue を使わない）。第 2 版に進むときに、作り方（画面で作る / 発行 Job に足す）と期限を決める | 確定（第 2 版に先送り） |
| 5 | 埋め込みの自動実行 | 取り込みが成功したときだけ。**終了コード 0 に加えて 3 でも実行する**（理由は「第 1 版」の節。1・2 では実行しない）。上限は `--max-chunks` | 確定（0 と 3 で埋め込み、1 と 2 では埋め込まない。承認済み） |
| 6 | 削除の扱い | R2 から消えた PDF は、DB から消さず、ログで知らせるだけ。自動で消さない | 仮承認 |

## 段 1 の実装で決めたこと（2026-10-21）

- 入口は `python -m app.ingestion.sync`（既定は確認だけ。`--apply` で保存し、実 API で埋め込む。`--max-chunks` `--max-calls` は埋め込みへそのまま渡す）。
  R2 の資料群を `store.collections()` で列挙し、資料群ごとに既存の取り込みを呼び、全部終わってから埋め込みを 1 回だけ呼ぶ。
- 終了コード: 0 = 完了（**壊れた PDF が混ざっていても 0**。1 にすると Job の再試行が、同じ失敗を無駄に繰り返す。該当の資料群は標準エラーに出す）、
  1 = 取り込み・埋め込み・R2 の一時的な失敗（想定外のコードも 1）、2 = 設定・使い方の誤り。
- 名前の形が `COLLECTION_PATTERN` に合わないプレフィックスは、警告して飛ばす（取り込みが 2 で終わり、埋め込みを止めてしまうため）。名前そのものはログに出さない。
- 「R2 から消えた PDF」の検出は、sync には入れない。既存の `r2-verify`（`python -m app.r2_store verify`）が、DB の文書ごとに R2 の有無とハッシュを確かめる。決めたこと 6 の「ログで知らせる」は、これで足りる。
- 実行時に要る環境変数: `R2_ACCOUNT_ID` `R2_BUCKET` `R2_CREDENTIALS_FILE`（または `R2_CREDENTIALS_JSON`）、`DATABASE_URL`、`GEMINI_API_KEY`。

## 段 2 の手順（あなたが実行する書き込み）

前提: 配備中のイメージ（`docunavi:8154585`）には `sync.py` が入っていない。新しいコミットからイメージを作る（サービスの配備はしない）。
Job の SA は、まず `docunavi-run`（サービスと同じ）を使う案にする。必要な Secret（`database-url` `gemini-api-key` `r2-app-read`）にすでに届き、IAM の作業が増えない。
設計の表の `docunavi-ingester`（専用 SA）は、Job を定期実行にするとき（段 4）に作り直してもよい。

```bash
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/docunavi-ingest:$(git rev-parse --short HEAD)"

as_me gcloud builds submit . --project "$PROJECT_ID" --config cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_SUPABASE_URL=${SUPABASE_URL},_SUPABASE_PUBLISHABLE_KEY=${SUPABASE_PUBLISHABLE_KEY}"

as_me gcloud run jobs create docunavi-ingest-sync \
  --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
  --service-account "docunavi-run@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "R2_ACCOUNT_ID=ec39c9c73795d4ff060e7f807ec2ad8b,R2_BUCKET=docunavi-documents,R2_CREDENTIALS_FILE=/secrets/r2/credentials.json" \
  --set-secrets "DATABASE_URL=database-url:latest,GEMINI_API_KEY=gemini-api-key:latest,/secrets/r2/credentials.json=r2-app-read:latest" \
  --command python --args=-m,app.ingestion.sync \
  --max-retries 0 --task-timeout 900

as_me gcloud run jobs execute docunavi-ingest-sync --project "$PROJECT_ID" --region "$REGION" --wait
```

- コメントは付けていない（zsh に貼ると壊れるため）。`REGION=asia-northeast1`、`PROJECT_ID=docunavi-app`、`SUPABASE_URL` と `SUPABASE_PUBLISHABLE_KEY` は画面用の公開値で、[deploy.md](deploy.md) の F と同じ。
- Job は **`--apply` を付けずに** 作る。最初の実行は確認だけ（取り込みは何も保存せず、埋め込みは件数と費用の見積りだけ）。ログで、6 件が「変わらない」で、埋め込みの対象が 0 件なことを見る。
- 確認の結果が想定どおりなら、`as_me gcloud run jobs update docunavi-ingest-sync … --args=-m,app.ingestion.sync,--apply,--max-chunks,60` で `--apply` を足す。まだ Scheduler は作らない（段 4）。
- 埋め込みが使うのは無料のキー `GEMINI_API_KEY` だけ（有料キーは渡さない）。無料枠の範囲なので課金は出ないが、枠が尽きると 429 で止まり、埋め込みは終了コード 1 で終わる（次の実行が続きから進む）。
  GCP の無料クレジットは、Gemini API には使えない可能性が高い（公式の請求の説明を、利用者が AI Studio で確認した範囲。等級 B）。クレジットが払うとは考えない。
