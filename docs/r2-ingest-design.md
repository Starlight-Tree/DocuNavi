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
**第 2 版は 2026-10-26 に本番へ切り替えた**（下の「本番への切り替え（2026-10-26）」）。

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

### 本番への切り替え（2026-10-26）

第 2 版を本番に切り替えた。時刻はすべて JST。確認は 1 回ずつの実測（証拠等級 A）。

**目標の言い方:** 「通常は数分。通知が遅れたときは最大 10 分前後。取りこぼした分は、毎時の突き合わせが拾う」。「常に数分以内」とは書かない（通知の遅れは、段 5 で約 6 分 15 秒を一度測った）。

**流れ:**

```text
R2 `docunavi-documents` → 通知の規則 → Queue `docunavi-ingest` → Cloud Scheduler（5 分おき）→ Cloud Run Job `docunavi-ingest-queue` → 取り込み → 埋め込み → DB
```

毎時の `docunavi-ingest-sync-hourly`（毎時 0 分）は、最後の砦としてそのまま残す。

**構成の記録:**

| 部品 | 内容 |
|---|---|
| 通知の規則 | id `5db36367-…`。接尾辞 `.pdf`。対象は PutObject / CopyObject / CompleteMultipartUpload。**DeleteObject は入れない**（消えた PDF は DB から消さない方針〔決めたこと 6〕のため）。作る前に、検証用の規則（接頭辞 `ingest-probe`）を先に削除した（重なる規則は拒否される、という公式の API 仕様〔証拠等級 B〕による） |
| Queue | `docunavi-ingest`。保持 86400 秒、配信の遅延 0 |
| consumer（http_pull） | `max_retries` 3、**DLQ なし**、`retry_delay` 0、`visibility_timeout_ms` 900000、`batch_size` 1。worker は呼び出しごとに、batch 5・可視時間 900 秒・再試行の待ち 60 秒を指定する |
| Job | `docunavi-ingest-queue`（asia-northeast1）。イメージ `docunavi-ingest:8a9ba33`。SA `docunavi-ingester`。コマンド `python -m app.ingestion.queue_worker --apply --max-chunks 60`。タスク 1、並列 1、`maxRetries` 0、タイムアウト 840 秒（可視時間 900 秒より短い）、CPU 1 / 512Mi |
| Job の Secret | `database-url`、`gemini-api-key`、`r2-app-read`（ファイルとして）、`ingest-queue-token`（**バージョン 2 に固定**。バージョン 1 は無効化） |
| Scheduler | `docunavi-ingest-queue-every-5min`（`*/5 * * * *`、Asia/Tokyo）。Cloud Run の Jobs API の `:run` を、SA `ingest-queue-scheduler` の **OAuth アクセストークン認証**で呼ぶ（OIDC ID トークンは `run.googleapis.com` の Jobs API では拒否された）。この SA に付けた権限は、Job への `roles/run.invoker` だけ（これだけで足りた。`actAs` の 403 は出なかった） |
| 権限 | `docunavi-ingester` は 4 つの Secret に対してだけ `secretAccessor`（有料キーは付けない）。SA の JSON キーは作らない |
| トークン | Queues の Read + Write、この口座全体。期限は **2027-01-19 23:59 JST**。それまでに作り直し、Secret のバージョンを増やして Job の固定を付け替える |

**確認した内容（手動の順に）:**

| 段 | 時刻 | 結果 |
|---|---|---|
| C1 | 00:18 | `--apply` なしで Job を実行。設定の確認が通り、終了コード 0（イメージに `queue_worker` があり、Secret を読めた） |
| C2 | 00:19 | 空の Queue で実行。取り込み 0 件、ack 0 件、終了コード 0（トークンと Queue に届く） |
| C3 | 00:21 | 9 バイトの `.txt` を置いた約 19 秒後に Job が pull。キーが不正として取り込まずに捨て、ack 1 件、終了コード 0。置いたオブジェクトは削除した |
| D1 | 00:26 | 本番の規則を作成（上の記録）。読み戻して、規則が 1 件だけであることを確かめた |
| C4 | 00:26〜 | 合成の PDF（英語 1 ページ、2 チャンク）を `ingest-probe/ingest-probe-c4-20261026.pdf` に置いた。キュー経由で取り込まれ、検索（`search_documents`）で合言葉が見つかった。**Job は計 3 回実行した。最初の通知は消えていた**（最初に置いたときのメッセージが届かなかった。原因は未特定。「起きたこと」は等級 A、理由の説明は等級 C）ので、1 回余計に実行した。後始末として、R2 のオブジェクトと DB の文書 1 件を、利用者の承認のうえで削除した |
| D2 | 01:15 | Scheduler が 01:15:00 に Job を起動。実行 `docunavi-ingest-queue-47ggh` が上の SA で動き、1/1 完了。空の実行は 10〜12 秒。01:20 の回は読んでいない |

**決めたこと（2026-10-26）:**

- DLQ は置かない。毎時の突き合わせが、失敗した PDF を毎回やり直すことを前提にする（`sync.py` を読んで確認した）。
- 毎時の突き合わせは残す。Queue 経路は、反映を早めるための追加であって、置き換えではない。
- 同時実行は許容する（利用者の判断）。15 分の可視時間は、**同じ Queue メッセージの二重処理を防ぐ**。ただし、毎時の突き合わせと Queue の worker の同時実行では、別の経路からの重複処理が起こりうる（許容）。Scheduler の実行どうしも、1 回が最長 14 分続くので重なりうる。保存の段階は documents の行ロックで直列になる（テストで確認）。埋め込みは直列にしていないので、同じチャンクで API を二重に呼びうる（費用だけの問題。`content_hash` で守られ、壊れない）。

**費用:** Cloud Scheduler の費用はジョブ数ベースで、実行回数には依存しない（利用者が貼った情報。公式の料金表は未確認）。Job の空の実行が Cloud Run の無料枠に収まるかは**未確認**。

**未確認:**

- worker が指定する再試行の待ち・batch・可視時間が、実際に効くか（C3 は再試行も batch の複数件も通っていない）。
- `max_retries` を超えたメッセージの扱い（DLQ なしだと破棄される、という貼り付けの情報は等級 C）。
- 可視時間切れによる再配信が `max_retries` に数えられるか。
- 通知が遅れたとき、何回の実行で拾えるか。
- 最初の通知が消えた原因。
- 埋め込みの同時実行は、実際には走らせていない。

## 決めたこと

2026-10-21、利用者の方針（Queue は後から・突き合わせから・埋め込みは取り込み成功時だけ）に沿って埋めた。
**確定** は承認済み、**仮承認** は私が置いた案を利用者が仮に承認したもの（後で変えてよい）。

| # | 項目 | 内容 | 状態 |
|---|---|---|---|
| 1 | 方式 | 第 1 版は突き合わせ方式。Queue は必要になってから足す。**着手するか自体は未決**（必要は小さい） | 方式は確定、着手は未決 |
| 2 | 実行間隔 | 1 時間おきから。突き合わせ方式では遅延が実害にならず、Scheduler の最短間隔の問題も出ない。資料を足した直後に反映したければ、Job を手動で実行する | 仮承認 |
| 3 | R2 の鍵 | 読み取りだけで足りる。`r2-app-read` を共用する（Job の SA に Secret 単位で `secretAccessor`）。`r2-ingest-rw` は使わない。共用で困るのは、アプリ用の鍵に問題があると取り込みも止まること。取り込み専用の権限分離は、将来の検討 | 仮承認 |
| 4 | Cloudflare の pull トークン | 第 1 版では**要らない**（Queue を使わない）。第 2 版では、利用者が画面で作った（`docunavi-ingest-queue-pull`。Queues の Read + Write、この口座全体、期限 2027-01-19 23:59 JST）。Secret `ingest-queue-token` のバージョン 2 を Job が参照する（「本番への切り替え（2026-10-26）」） | 確定・実施済み（期限前に更新が要る） |
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
  --set-env-vars "R2_ACCOUNT_ID=<CLOUDFLARE_ACCOUNT_ID>,R2_BUCKET=docunavi-documents,R2_CREDENTIALS_FILE=/secrets/r2/credentials.json" \
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
