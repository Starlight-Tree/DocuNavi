# R2 キーの発行 Job（`r2-key-issuer`）

R2 のバケット `docunavi-documents` を読み書きするキー（S3 互換の Access Key ID と Secret Access Key）を、
**値を会話・ログ・Claude Code に出さずに**発行して、Secret Manager に保存する仕組み。コードは `ops/r2_key_issuer/`、
土台の作成は `scripts/setup-r2-key-issuer.sh`、テストは `tests/test_r2_key_issuer.py`。

**状態（2026-11-20、gcloud の読み取り。Job の実行履歴・Scheduler のログ・`r2-check` のログによる。証拠等級 A）:**

- Job は、下の表の 7 つ（`r2-revoke-all` を含む）がすべて配備されている。ほかに、DB バックアップ用の `r2-issue-backup-rw`（`r2-backup-rw` に発行。2026-11-08 に 1 回実行して成功。[pg-backup.md](pg-backup.md)）がある。`r2-issue-ingest-rw` は 2026-10-21 03:29 JST に 1 回実行して成功した。`r2-revoke-all` と手動の `r2-rotate-app-read` `r2-issue-app-read` は、実行履歴が無い。
- 2026-11-16 05:00 JST の `r2-check` のログは、`app-read: ok`、`ingest-rw: ok`、`managed tokens: 2 (not recorded: 0)`、親トークンは有効で期限は **2027-01-19 23:59 JST**（ログの時点、2026-11-15 20:00 UTC で、残り 64 日）。
- Scheduler `r2-sched-revoke-stale`（月曜 04:30）と `r2-sched-check`（月曜 05:00）は、2026-10-26・11-02・11-09・11-16 に起動し、Job は 4 回とも成功した。
- **月次の入れ替え `r2-sched-rotate-app-read`（毎月 1 日 04:00 JST。名前と cron は `scripts/setup-r2-key-issuer.sh` の定義）は、起動の記録が無い。** 2026-11-01 04:00 JST の起動が、Scheduler のログにも、Job `r2-rotate-if-due-app-read` の実行履歴（2026-10-21 の手動 1 回だけ）にも無い。Scheduler が無いのか、起動に失敗したのかは、閲覧用の権限では Scheduler を読めず、**原因は未確認**。`r2-app-read` の版 2 は 2026-10-21 の発行なので、60 日の期限は 2026-12-20 ごろ（発行日からの計算）。2026-12-01 の起動では入れ替わらず、入れ替わるのは早くて 2027-01-01 の起動。それまでに Scheduler の状態を確かめるか、手動の入れ替え（下の「失敗・再実行・更新・取り消し」）を使う。
- 本番のアプリは、2026-10-21 のうちに `r2-app-read` を使う構成に切り替わった（下の手順 6・7。[r2-rollout.md](r2-rollout.md)）。配信中のリビジョン `docunavi-00091-juj` は、Secret `r2-app-read` のボリュームを `/secrets/r2` に持ち、`PDF_STORE=r2`・`R2_CREDENTIALS_FILE=/secrets/r2/credentials.json` を設定している。

以下の本文は、特記が無い限り、2026-10-21 時点の記述です。

状態（2026-10-21、当時の記述）: **配備済みで、1 回通しで動かした**（親トークン、SA 3 つ、Secret 3 つ、Job 6 つ、Scheduler 3 つ）。`r2-issue-ingest-rw`（7 つ目）はスクリプトに足した分で、配備はあなたが `setup-r2-key-issuer.sh` を再実行したとき。
最初の `rotate-if-due-app-read` で `r2-app-read` の版 2 を発行し、`r2-verify` で R2 の PDF 6 件を新しい鍵で読めることを確かめた（本物の Cloudflare・Secret Manager・R2）。
作った範囲は、**アプリ用（`r2-app-read`）の自動ローテーションだけ**（2026-10-21 にあなたが決めた）。
取り込み用（`r2-ingest-rw`）は、Secret の入れ物と SA `r2-uploader`、発行の Job `r2-issue-ingest-rw`（コードは対応済み。**本人だけが実行する**。PDF を R2 に置くための読み書きのキーを、値を画面に出さずに Secret へ入れる）まで。入れ替えの Job と Scheduler は作らない。使い方は下の「PDF を R2 に置く」。
通知（メール・Cloud Monitoring のアラート）は作らない。警告は `r2-check` のログで読む。

## 何を作るか

発行するキーは 2 つ。どちらも `docunavi-documents` 1 つだけに限定したバケット単位のトークン。

| 用途 | 権限（Cloudflare の権限グループ） | 保存先の Secret | 使う側 |
|---|---|---|---|
| アプリ用 | 読み取り（`Workers R2 Storage Bucket Item Read`） | `r2-app-read` | Cloud Run のサービス（`GET /documents/{id}/file`） |
| 取り込み用 | 読み書き（`Workers R2 Storage Bucket Item Write`） | `r2-ingest-rw`（**`r2-issue-ingest-rw` を実行するまで空**） | PDF を R2 に置く操作（`python -m app.r2_store upload`）。取り込み（`app.ingestion.sync`）は読み取りだけなので、`r2-app-read` を使う |

Secret の中身は、`{"access_key_id": …, "secret_access_key": …}` の JSON 1 つ。**1 つの版に組で書く**ので、
「ID だけ新しい」という食い違いは起きない。Secret Access Key は、トークンの値の SHA-256（Cloudflare の公式の説明どおり）。

## 「Claude が変更できるスクリプトに強い認証情報を渡す」のではない

| | 強い認証情報を、Claude が変更できるスクリプトに渡す | **独立した処理側で権限を絞る（これを採る）** |
|---|---|---|
| 親トークンの置き場 | コンテナの中。Claude が読める | Secret Manager。読めるのは Job 用の SA だけ |
| 処理の中身 | Claude が書き換えて、そのまま動かせる | あなたがデプロイしたイメージ（digest 固定）だけが動く |
| Claude にできること | 何でも（スクリプトの中身次第） | 固定の 5 つの Job を「実行する」ことだけ。引数も上書きできない |
| 値の行き先 | Claude の目に入りうる | Job の中で Secret Manager に書かれて終わり |

リポジトリのコードは Claude が編集できる。ただし、**編集しても、あなたが `setup-r2-key-issuer.sh` で配備し直すまで、動いている Job は変わらない**。
配備のたびに、ソースのハッシュ（ラベル `source-sha`）が付き、`--verify` で手元のコードと照合できる。

## 構成と権限

```
Claude Code ──(gcloud run jobs execute r2-<モード>。フックが毎回「確認」)──▶ Cloud Run Job（SA: r2-key-issuer）
                                                                              │  ① 親トークンを Secret Manager から読む
                                                                              │  ② Cloudflare API でバケット限定のトークンを作る
                                                                              │  ③ R2 に実際につないで動作を確かめる
                                                                              └─ ④ Secret Manager の 1 つの版に保存（値はログに出さない）
```

| 主体 | 持つ権限 | 持たない権限 |
|---|---|---|
| `r2-key-issuer`（Job の SA） | 親トークンの Secret と `r2-ingest-rw` への `secretAccessor`、`r2-app-read` と `r2-ingest-rw` への `secretAccessor` と `secretVersionAdder`（Secret 単位。`r2-*` のような接頭辞の一括付与はしない） | プロジェクト全体のロール、他の Secret、Secret の作成、Job の変更 |
| `docunavi-viewer`（Claude の日常の ID） | Job 5 つ（`revoke-all` を除く）への `roles/run.invoker`（実行だけ） | `run.jobs.runWithOverrides`（引数・環境変数の上書き）、Secret の値、IAM の変更 |
| `agent-deployer`（アプリのデプロイ用） | 変更なし | この SA の `serviceAccountUser`（Job の更新に必要。**付けない**） |
| あなた | 配備、IAM、親トークンの保存、`revoke-all` | — |

Job は、下の表の 7 つに加えて、2026-11-08 に加わった DB バックアップ用の `r2-issue-backup-rw`（冒頭の状態）がある。どれも**用途ごとに引数を固定**し、実行時の上書きは使わない（`--args` の上書きには `runWithOverrides` が別に要り、`run.invoker` だけでは足りない。
Claude に上書き権限は与えない）。

| Job | 何をするか | Claude が実行 |
|---|---|---|
| `r2-issue-app-read` | 有効なキーが無いときだけ発行（あれば何もしない） | できる（毎回確認） |
| `r2-issue-ingest-rw` | 読み書き用のキーを発行して `r2-ingest-rw` に入れる（有効なキーがあれば何もしない）。PDF を R2 に置く前に 1 回実行する | **できない（あなただけ）**。書き込める鍵を作るため |
| `r2-rotate-app-read` | 新しいキーを発行して差し替える（古いトークンは残す） | できる（毎回確認） |
| `r2-rotate-if-due-app-read` | 記録したキーの発行から 60 日たっていれば、`rotate` と同じことをする。そうでなければ、キーが動くことを確かめるだけで終わる。**Cloud Scheduler が月 1 回起動する** | できる（毎回確認） |
| `r2-revoke-stale` | 記録されていない古い・失敗の残りのトークンを取り消す。入れ替えで置き換えられた古いキーは、**新しいキーの発行から 7 日たつまで残す**。失敗の残り（記録されたキーより新しいもの）は、作って 15 分たてば取り消す | できる（毎回確認） |
| `r2-check` | 点検だけ（何も作らない・変えない）。**Cloud Scheduler が週 1 回起動する** | できる（毎回確認） |
| `r2-revoke-all` | このバケット用のトークンを全部取り消す（漏れたとき） | **できない（あなただけ）** |

## 値を出さない仕組み

- 親トークンは `scripts/store-cloudflare-parent-token.sh` が、画面に出ない入力で受け取り、標準入力で Secret Manager に入れる。
  引数・ファイル・履歴に残らない。古い版は無効にする。会話に貼らない。
- Job は、トークンの名前・版の番号・件数・固定の文言だけをログに出す。例外はクラス名と HTTP ステータスとエラー番号だけ（本文は出さない）。
  テストが、成功・失敗・取り消しの全経路で、親トークンの値・新しい値・Secret Access Key がログと標準出力に出ないことを確かめる。
- Claude が受け取るのは、成否、トークンの名前、版の番号だけ。保存した値を読み戻して確かめることはしない。
  保存の確認は、書き込み時の CRC32C による検査と、**そのキーで R2 に実際につなぐ動作確認**（読み取り用は「読める・書けない」、読み書き用は「書く・読む・消す」）で行う。
- 閲覧専用の ID は、Secret のメタデータも読めない（2026-10-20 に `secrets describe` が拒否されるのを確認）。`gcloud secrets … access` はフックが拒否する。

## 初回の手順：誰が何をするか

| # | 内容 | 担当 |
|---|---|---|
| 1 | Cloudflare の画面で親トークンを作る（アカウント API トークン。権限は `Account API Tokens: Edit` から。**有効期限を必ず付ける**） | **あなた**（本人の認可が要る。値が画面に出るのはここだけ） |
| 2 | `./scripts/setup-r2-key-issuer.sh <プロジェクト ID> --cloudflare-account <ID>`（SA・Secret の入れ物・イメージ・Job・権限） | **あなた**（IAM を作る権限があるのは本人だけ。Claude に届く経路にしない） |
| 3 | `./scripts/store-cloudflare-parent-token.sh <プロジェクト ID>`（親トークンを保存） | **あなた** |
| 4 | `… --verify`（権限・ソースの一致・否定テスト）の出力を貼る | あなたが実行、Claude が読んで判定 |
| 5 | `r2-check` → `r2-rotate-if-due-app-read`（最初の 1 回は必ず入れ替わる。下の「自動ローテーション」）→ `r2-verify` → `r2-check` の実行と、ログの確認 | **Claude**（毎回あなたが承認） |
| 6 | アプリのサービスに `r2-app-read` をボリュームで渡し、`PDF_STORE=r2` に切り替える（`docunavi-run` に `secretAccessor` は付与済み。配線の Job `r2-wire-*` による段階的な切り替え。手順は [r2-rollout.md](r2-rollout.md)） | あなたが実行（結果は Claude が読む） |
| 7 | 動作の確認（R2 に PDF を置き、`/file` で取得） | Claude（読み取り）、置く作業は取り込みの Job |

1〜3 は一度きり（コードを直したときの 2 のやり直しを除く）。2 を Claude が実行できない理由は、**Claude が使える ID に IAM を作る権限を渡すと、この仕組み全体が意味を失う**から。
Claude は、スクリプトを書き、模擬で試し、配備後に読み取りで検証する側。

## 自動ローテーション（2026-10-21 に採用・配備。通知は作らない）

親トークンを Secret Manager に置いたままにする運用（下の表の B、有効期間は最長 90 日）を採る。決めたこと:

| 項目 | 内容 |
|---|---|
| 入れ替え | Cloud Scheduler が毎月 1 日 04:00（JST）に `rotate-if-due-app-read` を起動。記録したキーが 60 日より古い（古さが分からない・動かない場合も）ときだけ入れ替える。`r2-ingest-rw` は対象外 |
| 古いキー | 入れ替えでは取り消さない。`revoke-stale`（毎週月曜 04:30）が、**置き換えた新しいキーの発行から 7 日**たってから取り消す（古いキー自身の古さではない）。記録より新しい残りは 15 分たてば取り消す |
| 失敗したとき | 取り消すのは、その実行で作った新しいトークンだけ。記録された有効なキーには触れない（終了コード 2〜5。**通知は無い**ので、Job の失敗はログか `gcloud run jobs executions list` で見る） |
| 親トークン | R2 のキーの作成・取り消し専用、最小権限（`Account API Tokens: Edit` から。IP 制限なし）、有効期間 90 日。`check`（毎週月曜 05:00）が、残り 21 日を切ると `PARENT_TOKEN_EXPIRING` を WARNING で出す（使えないときも出す。終了コードは変えない）。作り直しは人の作業（年 4 回ほど） |
| 記録の形 | Secret の JSON に `issued_at`（入れ替え時期の判定に使う。親トークンが使えなくても読める）を足した。アプリは無視する |
| アプリ側 | `R2_CREDENTIALS_FILE`（Secret をボリュームで渡す。配線は [r2-rollout.md](r2-rollout.md) の Job が行い、`/secrets/r2/credentials.json` に `latest` をマウントする）を使うと、R2 が認証エラー（401/403）を返したとき、ファイルを読み直し、**鍵の ID が変わっていれば** 1 回だけやり直す。再配備は要らない（`app/r2_store.py`） |

通知は作らない（2026-10-21、あなたが決めた）。`PARENT_TOKEN_EXPIRING` と `KEY_ROTATION_OVERDUE` は、`r2-check` のログに出るだけで、誰にも飛ばない。
見るのは、Claude に `r2-check` のログを読ませるか、あなたが Cloud Run のログを開いたとき。

**最初の入れ替えで起きること（想定内）:**

- `r2-app-read` の v1 は、ダッシュボードで作った鍵（`docunavi-r2-read`）で、`issued_at` がない。最初の `rotate-if-due-app-read` は古さが分からないので、**必ずその場で入れ替える**（v2）。
- `docunavi-r2-read` は、管理する名前（`docunavi-r2-app-read-…`）に一致しないので、`revoke-stale` は取り消さない。入れ替えと `r2-verify`（最新の版を読む）の成功を確かめてから、**あなたが画面で取り消す**。
- 入れ替えの前の `r2-check` は、`app-read` で「記録したキーに対応するトークンがない」と出て、終了コード 1 になる（v1 が管理外のため）。入れ替えのあとは 0 になる。
- 本番のアプリは、まだ `r2-app-read` を使っていない（`PDF_STORE=r2` とボリュームの追加は別の承認）。入れ替えは本番に影響しない。（2026-11-20 の注記: 2026-10-21 のうちに切り替え済み。冒頭の状態。）

確認済み（2026-10-21、手動実行）: 親トークンの検証、権限グループ、トークンの作成、R2 での動作確認、Secret への保存、`r2-verify`。
未確認: Scheduler からの実際の起動（初回は 10/26 月曜 04:30 `revoke-stale`・05:00 `r2-check`、11/1 04:00 入れ替え）、`revoke-stale` が旧い管理キーを取り消すこと（次の入れ替え後）、
ボリュームの `latest` が再配備なしで新しい版に変わること、動作確認の失敗・保存の失敗の経路（本物では起きていない。模擬のテストのみ）。
`PDF_STORE=r2` への切り替えと、アプリへのボリュームの追加は別の承認。（2026-11-20 の注記: 切り替えは済み。冒頭の状態。上の「Scheduler からの実際の起動」のうち、`revoke-stale` と `r2-check` の起動は確認した〔冒頭の状態〕。11/1 04:00 の入れ替えの起動は、記録が無い。）

## 以降に残る操作と、親トークンの期限

日常の操作は、Claude が Job を実行して、あなたが承認するだけ。親トークンは、**発行・入れ替え・取り消しのときだけ**使う（アプリと取り込みのキーは、親トークンが失効しても動き続ける）。

| 親トークンの運用 | 期限が切れた後にあなたがすること | 頻度と手間 | 漏れたときの影響 |
|---|---|---|---|
| A. 都度（有効 1 日） | 発行・入れ替え・取り消しの前に、画面で作る → `store-…sh` → Claude が Job を実行 | キーの入れ替えのたび（年 0〜2 回を想定）。1 回およそ 3 分 | 待機中は失効済みで無害 |
| **B. 90 日（採用）** | 期限が来る前に同じ手順で作り直す（`r2-check` が残り日数を出し、21 日を切ると警告する） | 年 4 回ほど。入れ替えの要否に関係なく発生 | 最大 90 日間、トークンを作れる |
| C. 1 年 | 年 1 回 | 年 1 回 | 最大 1 年間 |

期限の無いトークンは作らない。期限切れは異常ではなく、`r2-check` は警告として出す（キーの点検は続ける）。
Cloudflare に GCP のような「長期の秘密を持たない認証」があるかは、調べた範囲では見つけていない（未確認）。

## 失敗・再実行・更新・取り消し

| 状況 | Job の動き | 次にすること |
|---|---|---|
| 再実行（有効なキーがある） | `issue-*` は何も変えず 0 で終わる | — |
| 記録されたキーのトークンが無い・動かない | `issue-*` が新しいキーを発行して差し替える | — |
| 作成が明確に拒否された（4xx） | 何も作らず終了コード 2 | 権限・設定を直す |
| 作成の結果が不明（タイムアウト・5xx・429） | トークン名で探し、あれば取り消す。終了コード 3 | 同じ Job をもう一度。残りは `r2-revoke-stale` が片付ける |
| 作ったキーが R2 で動かない | 少し待って 6 回まで再試行 → 駄目ならトークンを取り消す。終了コード 4 | `r2-check`、親トークンの権限を確認 |
| 保存に失敗 | 最新の版の ID が今作った ID か確かめる。違えばトークンを取り消す。終了コード 5 | Secret の権限を確認 |
| 保存は成功したが応答が届かなかった | 最新の版が新しい ID なら成功として扱う | — |
| 取り消しにも失敗 | 残ったトークンの名前を `ORPHAN` として出す | `r2-revoke-stale`（15 分後）、または画面で取り消す |
| 2 つの実行が重なった | 各版は組で書かれるので食い違わない。後から書いた方が有効。負けた方のトークンが残る | `r2-revoke-stale` |
| キーの入れ替え（手動。自動化が止まったときの代替） | ① 親トークンを作って保存（`store-cloudflare-parent-token.sh`）→ `r2-check` ② `r2-rotate-<用途>` ③ アプリの `/documents/{id}/file` で取得できることを確かめる ④ **7 日後に** `r2-revoke-stale`（古いキーが取り消される。7 日前の実行では何も取り消さない） | ④の前に古いキーを画面で取り消さない（動いているインスタンスが止まる）。アプリがボリューム（`R2_CREDENTIALS_FILE`）でなく環境変数で鍵を受けている場合は、②のあと再配備する |
| 漏れた疑い | あなたが `r2-revoke-all` → 親トークンを作り直し → `r2-issue-*` → 再配備 | 取り消すとアプリの `/file` と取り込みは止まる |

## 限界（隠さずに書く）

- **フックは「速度制限」で、強い境界ではない。** コンテナの中には、あなた自身の gcloud ログインが残る。リポジトリのスクリプトを経由すると、
  フックはスクリプトの中身を見ずに通す。2026-10-20 の設計の検証中に、私が書いたセットアップスクリプトを、偽の gcloud で試走させるつもりで、
  偽物を作る手順を取りこぼしたまま実行し、**本物の gcloud があなたのログインで `-y` 付きで走った**（対象は実在しない `proj-x`。最初のコマンドの API の有効化が
  権限不足で拒否され、`set -e` で止まった。何も作られていない）。つまり、**この設計が守るのは通常の経路**で、誤った・悪意のある経路が
  コンテナ内のログインを使うのは塞げていない。対策は 2 層で、どちらも実行前の承認が要る:
  1. （実装済み）スクリプト自身の安全装置。対話端末でなければ gcloud を呼ばずに終了する（終了コード 3）。テスト用の差し替え（`GCLOUD_BIN`）は、絶対パス・実行可能・
     `# STUB-GCLOUD` の行を持つ偽物だけ。PATH には頼らない。`tests/test_r2_scripts.py` が、本物に届かない環境（空の環境・gcloud の無い PATH）で確かめる。
     ただし、これは事故を防ぐ装置で、悪意には破られる（端末の偽装など）。
  2. （提案中。あなたの承認待ち）コンテナのログインを、強いアカウントから、専用の最小権限のアカウントに替える。強い認証は、コンテナの外だけに置く。
- 閲覧専用の ID に付ける `run.invoker` は、Job の実行を許す。悪用されても、起きるのは「キーの入れ替え」と「古いトークンの取り消し」まで（`revoke-all` は付けない）。値は読めない。
- Job をアプリと同じプロジェクト `docunavi-app` に置くので、同じプロジェクトの別ロール（Owner・Editor・`iam.serviceAccountUser` を持つ主体）は迂回できる。
  `--verify` が一覧を出す。`agent-deployer` が Job を更新できないことは、`--verify` の否定テストで確かめる（Cloud Run が更新に `actAs` を要求する、という前提で、**未確認**）。
  通ってしまう場合は、Job を別のプロジェクトに分ける。
- 親トークンに R2 の権限グループを足す必要があるか（トークンは自分の権限の範囲でしか他のトークンに権限を渡せない、という公式の説明がメンバーについてあり、トークンについては確認できていない）は、
  `r2-check` の「permission group ok」で分かる。足りなければ、`Workers R2 Storage Write`（全バケットの管理を含む広い権限）を親に足すことになる。**足す前に相談する**。
- R2 との実際のやり取り（トークンの伝播の遅れ、読み取り用トークンの書き込みが 403 になること、`ListObjectsV2` が読み取り権限で通ること）は、模擬（moto）でしか試していない。**本物では未確認**。（2026-11-20 の注記: 2026-10-21 の実機の実行で、Job の動作確認〔R2 に実際につなぐ確認〕は通り、以後の `r2-check` も `ok` を出している。個々の挙動〔伝播の遅れの有無など〕の記録は無く、未確認のまま。）

## 確認の状況

**2026-11-20 の注記:** 次の表は 2026-10-21 時点です。その後、Job の起動・イメージ・Cloudflare の API・Secret Manager・R2 への実際の呼び出しは、実機で実行されて成功しています（2026-10-21 以降の Job の実行履歴と、`r2-check` のログ。冒頭の状態）。表の「未確認」のうち、セットアップスクリプトの `--verify` の出力の書式、`run.invoker` が引数の上書きを許さないこと、`agent-deployer` が Job を更新できないことは、確認の記録が無く、未確認のままです。

| 項目 | 状況 |
|---|---|
| Job のロジック（発行・再実行・途中失敗・取り消し・点検・ログに値が出ない） | テストで確認（`tests/test_r2_key_issuer.py`、模擬）。変異（値を出す・バケット限定を外す・親を除外しない）でテストが落ちることも確認 |
| Cloudflare への HTTP（パス・認証ヘッダー・4xx と 5xx の区別・ページ送り） | 模擬の HTTP で確認 |
| Secret Manager・R2 のクライアントの呼び方 | 実ライブラリ（google-cloud-secret-manager 2.31、boto3 1.43）に、偽の下位クライアント・moto をつないで確認 |
| イメージのビルドと Job の起動 | **未確認**（開発コンテナに Docker が無い。初回の配備で確認する） |
| セットアップスクリプトの流れ | 偽の gcloud で、コマンドの順序と引数（付けない権限を含む）を確認（`tests/test_r2_scripts.py`）。本物の gcloud では**未確認**（`--verify` の出力の書式も） |
| `gcloud run jobs execute r2-…` が毎回「確認」になる | 確認済み（フックを実際に通した。変更不要） |
| `run.invoker` が引数の上書きを許さない | **未確認**（`--verify` の否定テストで確かめる） |
| Cloudflare の API（権限グループの名前、リソース名、`Account API Tokens Write` でトークン作成、`value` の返却） | 公式の文書と OpenAPI で確認。実際の呼び出しは未実施 |

## PDF を R2 に置く（`r2-issue-ingest-rw` を配備したあと）

ダッシュボードでキーを作って写す必要はない。あなたが端末で、次の順に実行する。キーの値は画面にもシェルの履歴にも残らず、そのコマンドの環境変数にだけ入る。

```bash
env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud run jobs execute r2-issue-ingest-rw --project docunavi-app --region asia-northeast1 --wait
```

発行は最初の 1 回だけ（有効なキーがあれば、2 回目以降は何もしない）。置くときは次の 1 行（`--apply` なしは計画の表示だけ）。

```bash
R2_ACCOUNT_ID=<アカウント ID> R2_BUCKET=docunavi-documents R2_CREDENTIALS_JSON="$(env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud secrets versions access latest --secret r2-ingest-rw --project docunavi-app)" .venv/bin/python -m app.r2_store upload --input <置き場>
```

- キーの有効期間は、親トークンの期限と、手動の入れ替え（`rotate-ingest-rw` のモードは、コードにあるが Job は作っていない）に従う。自動では入れ替わらない。使わなくなったら、`r2-revoke-all` で取り消す。
- 読み書きのキーは、R2 の資料を書き換えられる。端末の履歴やログに値が残る書き方（`echo` や `export` での表示）にしない。
