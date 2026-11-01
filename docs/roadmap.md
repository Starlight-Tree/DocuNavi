# ロードマップ

2026-10-13 時点の拡張計画です（段階 1・4 は 2026-10-14 に具体化し、段階 1 のコードは同日に実装しました）。DB を使うアプリへ育てることと、Cloudflare・R2・Vercel などの構成を実際に動かして学ぶことが目的です。
計画なので、段階に入るたびに見直します。

## 現状と優先順位（2026-10-22 更新。10-21 の完了分を反映。2026-10-25 に Next.js 化の完了と、本番の切断の確認を追記）

下の「段階」の表は 2026-10-13 の計画のままで、番号は変えていない。実装が進んだので、いまの状態と、これからの順序をここに置く。
順序は、「公開に要る守りを先に、検索の高度化は取りこぼしが出てから」。

**できていること**

- Cloud Run への公開（段階 0。2026-10-20。プロジェクト `docunavi-app`、サービス `docunavi`、リージョン `asia-northeast1`。ログインと質問は利用者が確認。Google の新しい OAuth クライアントでも同日に確認。正規の URL は `https://docunavi-114733761361.asia-northeast1.run.app` の 1 つ。通った手順と詰まった点は [deploy.md](deploy.md) の「実際に通った記録」）
- 履歴の削除コマンド（`python -m app.history_admin`）と、7 日の経過を模した Postgres のテスト（2026-10-20。`tests/test_history_postgres.py`）。**Cloud Run Job と Cloud Scheduler を作成済み**（2026-10-20。毎週日曜 03:00 JST に、最後の動きから 7 日たった会話を削除。Scheduler から Job が動いたことは確認。初回の定時（2026-10-25 03:00 JST）で「削除した会話: 10 件」がログに出た〔下の「受動確認」〕。方針の 90 日は変えない。記録は [deploy.md](deploy.md) の D）
- Postgres（Supabase）と、会話履歴の保存（段階 1。`--max-instances` × `DATABASE_POOL_MAX_SIZE` が接続数の上限に収まるかは未確認）
- 認証（段階 2。本物の Supabase・Google でのログインと質問は、ローカルで確認済み（2026-10-19）。トークンの期限切れ後の更新、Docker、公開環境は未確認）
- 会話の一覧と本人による削除（`GET /threads`、`DELETE /threads/{id}`、ログイン中の左サイドバー。API と画面の本物のログインでの動作は確認済み（2026-10-19、利用者の報告。確認した範囲の詳細は記録していない）。発言を返す API `GET /threads/{id}/messages` と、選んだ会話の過去のやり取りの表示を実装済み（テストとスクリーンショットでの確認。本物のログインでの確認は済み（2026-10-19、利用者の報告。8000 番の画面））
- PDF の取り込み・埋め込み・DB 検索（段階 4a のうち、ローカルの PDF から取り込む部分）
- 原本 PDF を R2 に置き、本番がそこから返す（段階 4a。2026-10-21。バケット `docunavi-documents` に 6 件。DB の内容ハッシュと一致することは `r2-verify` で確認し、段階的な切り替え〔`r2-wire-stage` → `r2-wire-promote`〕で本番を `PDF_STORE=r2` にした。資料一覧で原本を開けたことは利用者が確認し、切り替え後の新しい版のログに WARNING 以上は無い。アプリ用の読み取りキーは自動で入れ替わる。設計は [r2-key-issuer.md](r2-key-issuer.md)、手順は [r2-rollout.md](r2-rollout.md)。**未確認:** 実際のキーの入れ替えのあとも読めること、動いているインスタンスへの Secret の反映）
- 出典カードのページ番号と、PDF の該当ページへの遷移
- 資料の種別（`documents.collection` 列。`storage/pdf/<名前>/` のディレクトリ名）のバッジと、資料一覧の絞り込み
- チャットの画面（Markdown 表示、例題、「続けて聞く」）
- 画面の状態を URL に持たせる（タブ・資料・表示・ページ・種別。ログインの往復でも保つ。往復の動作は、公開環境の本物のログインで確認済み〔2026-10-20、利用者の報告〕）
- 本番で別モデルへのフォールバックを有効にした（2026-10-21。`GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite`、リビジョン `docunavi-00011-vuy`。実 API で、主モデルで始めた会話を両方向に続けられることを確認。**実際に切り替わった回は、まだ一度も見ていない**〔下の「受動確認」〕。検証の範囲は [configuration.md](configuration.md) の「検証の状況」）
- 画面の Next.js 化と Vercel での配信（2026-10-25 に完了。本番 URL `https://docunavi-eosin.vercel.app`、`main` へのプッシュで本番が更新される。最終確認の結果は下の「実装順序」の①）
- 開発・配備まわり（2026-10-21）: 読み取り専用の Docker ソケットプロキシ（ホストで検証して反映済み。[.devcontainer/README.md](../.devcontainer/README.md)）、Compose で画面の開発サーバー `frontend` を別コンテナに分ける変更（ホストで確認済み。[development.md](development.md) の「検証の記録」）、タグ付きプレビューの手順と `scripts/preview.sh`（[deploy.md](deploy.md) の「タグ付きプレビュー」）、gcloud のログインを名前付きボリュームに残す変更
- 開発コンテナの再ビルドを速くした（2026-10-31、利用者の決定）: gh・psql・Go・Playwright のライブラリー（Chromium の実行に要る共有ライブラリーとフォント）を `postCreateCommand` の導入から `.devcontainer/Dockerfile`（イメージのビルド時。apt と Go の tar は cache mount、Go は SHA-256 照合）へ移した。`postCreateCommand` からは apt の導入が無くなり、残るのはボリュームの所有者の修正、Chromium の取得と起動確認（本体は `playwright-browsers` ボリュームのまま）、ツールの存在確認、Vercel CLI の導入（Node は feature でイメージの後に入るため Dockerfile へは移せない）。`install-gh.sh`・`install-psql.sh`・`install-go.sh` は残してある（呼ばない。Go の版とハッシュは Dockerfile と重複）。Vercel CLI は今回の変更では残し、削除は別のコミットで行う予定（承認済み）。再ビルド後の確認（2026-10-31）: feature（Node・Python）、Chromium の起動、gh・psql・go・gcloud・docker、MCP の接続、バックエンドとフロントエンドのテスト（pytest 2477 件通過・48 件スキップ、vitest 326 件通過、型検査）が通った。`postCreateCommand` の所要時間は、コンテナ内のファイルと npm ログの時刻からの概算で、`cli-tools` が約 1〜2 分強（以前は約 7 分 23 秒の見積り。残りの大半は Vercel CLI）。正確な値はホストの Dev Containers のログにある。**未確認:** キャッシュを使わない再ビルド全体の所要時間、`search_documents` の実際の呼び出し（課金のため行っていない）、e2e テスト、`make test-db`。
- 開発コンテナから Vercel CLI を削除した（2026-10-31、利用者の承認。上の Dockerfile への移設とは別のコミット）: `install-vercel.sh` を消し、`postCreateCommand` の `cli-tools` から外した。必要なときは `npx --yes vercel@63.1.2`（`.devcontainer/README.md` の「開発環境の Vercel CLI」）。Vercel MCP とプラグインは変わらない。手順 7 の記述（下の ① の行）は当時の記録なので、そのまま残す。**確認済み（2026-10-31）:** `npx --yes vercel@63.1.2 --version` が成功する。初回は約 93 秒（npm から取得）、2 回目以降は約 1.1 秒（npm のキャッシュ）。削除後の再ビルドでの `postCreateCommand` の所要時間は、約 10 秒（2026-10-31 時点。コンテナ内のファイルと npm のログの時刻からの推定で、時間を計って測ったものではない）。**未確認:** `vercel login` と認証を伴う操作。
- Dependabot の対象に Docker と Go modules を足した（2026-10-25、`ff2081c`）。それまでは GitHub Actions だけだった。Docker は 8 つの Dockerfile のベースイメージ、Go modules は `ops/sse-relay` と `ops/sse-fake-upstream`（今は外部の依存なし）。更新の間隔は週 1 回、公開から 7 日の待機とコミットメッセージの規約は既存と同じ。**pip は対象外:** `requirements*.txt` は `uv pip compile` の生成物で、直接は編集しないため（`.in` を直す運用）。**未確認:** Dependabot がこの設定を受け付けたか、Go modules が外部の依存の無いモジュールに何を提案するか（GitHub の Insights → Dependency graph → Dependabot で見る）。`python:3.12-slim` の主版の更新の提案が来たら、`ignore` を足すか判断する

**これからの順序**

| 順 | 項目 | 中身 |
|---|---|---|
| 1 | 認証の残りの確認 | **済み（2026-10-20）。確認した内容と、その範囲の限界は次のとおり。** **ログイン後の URL の復元は、公開環境の本物のログインで確認済み（2026-10-20、利用者の報告。「復元できた」だけで、どの画面で試したかの詳細はない）。** **トークンの期限切れ後の更新も、通ったと利用者が報告した（2026-10-20。以前に行った確認で、日付・環境〔ローカルか公開か〕・待った時間の詳細はない。画面側の E2E は偽の認証）。**会話の一覧と本人による削除は、API（`GET /threads`、`DELETE /threads/{id}`）と左サイドバーの画面を実装済み。API の本物のログインでの動作は確認済み（2026-10-19、利用者の報告）。画面の本物のログインでの確認も済み（2026-10-19、利用者の報告）。過去のやり取りの表示（`GET /threads/{id}/messages`）は、サイドバーで選んだときまで実装済み（本物のログインでの確認は済み（2026-10-19、利用者の報告）。ページの再読み込みだけでは取得しない） |
| 2 | Cloud Run の公開後の残り | **①〜④は済み（2026-10-20）。残りは「未確認」の項目だけで、下の「受動確認」で見る。** 公開と、下の「公開前の守り」の設定は済み（2026-10-20）。残り: ① **済み（2026-10-20）:** 閲覧専用の ID を `docunavi-ops` の `docunavi-viewer` に置き換え、`docunavi-app` の Cloud Run・ログ・ビルドが読めることを確認した（コンテナの再ビルド後の日常の ID でも確認済み。旧 `agent-viewer` は削除、Default Gemini Project も削除。`wataru-sandbox` は無料の Gemini キーの置き場として残す） ② **配備は済み（2026-10-20）。** 有料キー `docunavi-paid` を `docunavi-app` に作り（制限は Gemini API のみ）、Secret `gemini-api-key-paid`（`docunavi-run` に `secretAccessor`）、環境変数 `GEMINI_API_KEY_PAID`、Cloud Run のリビジョン `docunavi-00003-b9t` で配備した。リビジョンの環境変数に `GEMINI_API_KEY` と `DATABASE_URL` が残ることと、`/health` が 200（`mode: gemini`、`history: postgres`）であることは、私が確認した。請求階層は、AI Studio の一覧で `docunavi-app` が **Tier 1**（`wataru-sandbox` は無料枠のまま）と表示された（利用者の報告）。Tier 1 にするため、前払い ¥800（最小 $5 相当）を購入した（利用者の報告。失効は購入から 12 か月、返金なし。自動チャージは設定していない）。有料キーで実際に Gemini を呼べることは確認した（同日、Secret の値で `gemini-3.1-flash-lite` を 1 回呼んで HTTP 200、キーは 39 文字。利用者が実行した結果の報告）。**未確認:** 本物の 429 での切り替えと 60 秒の保持（無料枠が尽きたときの `app.agents.resilience` の WARNING で確かめる。偽の 429 のテストは通っている）、前払い ¥800 が予算ガードの費用に数えられるか。前払いの残高が尽きると、有料キーは 402 を返す（切り替えの条件は 429 だけなので、その場合は無料キーの 429 がそのまま返る）③ **配線と設定は確認済み（2026-10-20）。** 予算（月 1000 円、クレジットを差し引かない費用で、50・90・100%）の対象は `projects/114733761361`（Cloud Run の URL の番号と一致。ID との対応は直接引いていない）、通知先は `budget-alerts` トピックで、`budget-guard` のトリガーと同じ。`budget-guard` は約 40 分おきに起動し、本番は `DRY_RUN=false`。デプロイ済みのソース（`main.py` と `requirements.txt`）は、`ops/budget_guard`（コミット `dd58ef1`）と SHA-256 が一致した（2026-10-20、利用者が実行した結果の報告。最終更新は 2026-10-20 03:39 JST）。**本物のしきい値超過で課金が外れることは、2026-11-01 に確認済み**（2026-10-31 14:24 UTC の `budget-guard` のログ。下の「受動確認」） ④ **済み（2026-10-20）:** Supabase の Site URL を公開の URL に変えた（本番とローカルのログインは、変更後に利用者が確認） |
| 3 | 4a の残り | **済み（2026-10-21。上の「できていること」）。**原本を R2 に置く。**資料は全員共通で進める**（2026-10-20、利用者の決定。運営者が置く共通の資料で、利用者のアップロードは作らない。持ち主の列・利用者ごとの絞り込みは入れない。アップロードを作る段になったら見直す） |
| 4 | 4b 自動化 | **最初の版は済み（2026-10-21）。** R2 を毎時に見て、新しい PDF を取り込み・埋め込む整合の Job（`docunavi-ingest-sync`）と Cloud Scheduler（`docunavi-ingest-sync-hourly`、毎時 0 分 JST）。Scheduler の自動実行、新しい PDF が検索に出ること、壊れた PDF が失敗として記録され他を止めないことを、本番で確認した。設計は [r2-ingest-design.md](r2-ingest-design.md)。R2 のイベント通知 → Queue にする第 2 版は、検証（設計の段 5）まで済み（2026-10-25。通知は数分遅れることがあり、順序も保証されない。測った内容と、片付けた検証用の資源は [r2-ingest-design.md](r2-ingest-design.md) の「段 5 の実施メモ」）。本番への切り替えは **2026-10-26 に済み**（通知の規則・Queue・取り込み用の Job・5 分おきの Scheduler。確認した内容と未確認は [r2-ingest-design.md](r2-ingest-design.md) の「本番への切り替え」）。目標は「通常は数分、通知が遅れたときは最大 10 分前後。取りこぼしは毎時の突き合わせが拾う」 |
| 6 | GraphRAG など | **最小版を実装した（2026-11-01、利用者の指示。条件を待たない）。** 「第N条」の参照をたどって、参照先のチャンクを追加の文脈にする。環境変数 `SEARCH_GRAPH` で切り替え。**既定は off**（実測で辺が 0 本、効果を測れなかったため。実装は残す）。範囲と測定の結果は下の「検索改善の実装」。汎用の実体グラフ（取り込み時に LLM で抽出して DB に保存）は未着手 |
| 7 | Vercel でフロント分離（段階 5）= Next.js 化 | **完了とする（2026-10-25、利用者の判断。下の「実装順序」の①）**（着手は 2026-10-24、利用者の指示。それまでは「最後」としていた） |
| 8 | MCP ツール化 | **完了とする（2026-10-22、利用者の判断。最初の版は 2026-10-21）。** 資料の検索 `search_documents`（読み取り専用。「資料への質問」ではなく検索）を、別の Cloud Run サービス `docunavi-mcp`（非公開、`run.invoker` は利用者本人だけ、読み取り専用ロールの DB URL）に置いた。Claude Code から `headersHelper`（`scripts/mcp-auth-header.sh`）でプロキシなしに直接つなぎ、実際の検索が通ることと、コンテナの再ビルド 1 回で手作業なしに再接続することを確かめた（無料キーで各 1 回）。設計・経緯・見方は [mcp-tool-design.md](mcp-tool-design.md)。再ビルドを重ねても手作業なしに再接続することは、2 回で確認した（19:31 JST の再ビルドと、22:39 JST の再ビルド。後者は Claude の VS Code のログで、`headersHelper` が 1 回走り 1 秒で接続）。
  **ID トークンの期限切れ後の復旧（2026-10-22）:** 01:30 JST に、接続から約 2 時間 51 分で、SSE の再接続が `Unauthorized` で失敗し、再接続を打ち切った（この時点では `headersHelper` は再実行されなかった）。01:36 JST に `search_documents` を 1 回呼ぶと（利用者の承認、無料キー）、最初の POST は 401 になり、Claude Code が `headersHelper` を再実行して再接続し（約 5.5 秒）、検索は成功した。つまり、**期限切れの後は、次の呼び出しの 1 回目で 401 → 自動で再接続して成功する**（待機中の SSE が切れても、その場では復旧しない）。
  **予算ガード（確認しないまま完了とした点）:** `docunavi-mcp` は予算の対象と同じプロジェクト `docunavi-app` にあるので、費用は数えられ、課金が外れれば止まる見込み（Cloud Run の一覧で確認。請求レポートで実際に数えられているかは未確認で、いまは確認できない）。Cloud Run 自体を MCP で操作する（デプロイ・ログ確認）のは、必要になってから |

いま着手を決めている項目は無い（下の「実装順序」の①②④⑤⑥は済み。残りの③は条件待ち、⑦は導入済みで、確認も済み。⑧〔ロール別 PDF グループ制御〕はフェーズ 1〜3 が済み〔本番は `enforce`〕、フェーズ 4a〔Django Admin〕は R0〜R9 まで済みで本番に配備済み。QR とボタンの修正の再配備も済み〔2026-10-26〕）。6（GraphRAG）と 4c（ハイブリッド検索）は、2026-11-01 に利用者の指示で実装した（下の「検索改善の実装」）。条件待ちで残るのは、段階 3（Cloudflare。自分のドメインが要る。ドメイン取得まで待つ、利用者の決定）だけ。

**実装順序（2026-10-24、利用者の指示。2026-10-25 時点: ①②⑥は完了、④⑤は学習用の試作と測定まで済み〔本番には入れていない〕、⑦は導入済みで確認も済み、③は未着手）**

| 番 | 項目 | 位置づけ |
|---|---|---|
| ① | Next.js 化 | （2026-10-26 に `frontend-next/` を `frontend/` へ改名した。以下は改名後の名前で書く。旧 Vite 版の `frontend/` は削除済みで、同名の別物。）**完了とする（2026-10-25、利用者の判断。確認の結果は下の「最終確認」）。** 既存の Vite の画面を Next.js（App Router、Vercel）に置き換える。API は今の FastAPI のまま。形は [target-architecture.md](target-architecture.md)。着手前の決定（2026-10-24）は同文書の「段階 7（Vercel）の前に決めること」。**手順 3 まで済み（2026-10-24）:** `frontend/` に App Router の雛形を置き（Next.js 16.4.3、TypeScript 7.0.2）、`src` を丸ごと移した（`VITE_*` → `NEXT_PUBLIC_*`）。画面はブラウザーだけで描く（`app/client-app.tsx` が `ssr: false` で `src/Root.tsx` を読む。Vite 版と同じ SPA。サーバー側の描画は移行のあとで別に検討する）。単体テスト 239 件が通り、Vite 版（239 件）と一致する。`next build` と `tsc --noEmit` も通る。実ブラウザー（Playwright）で、`next dev` と偽の LLM の API（`tests/e2e_server.py`）をつないで、質問 → SSE の回答 → 出典カードと、資料一覧への切り替え（URL の `?tab=documents`）まで確認した（コンソールのエラー 0 件）。**注意:** `@ai-sdk/react` と `ai` は Vite 版と同じ版に固定した（`4.0.132`、`7.0.129`）。`@ai-sdk/react` 4.0.156 では `App.test.tsx` の 1 件（出典の表示）が落ちる。原因は調べていない。**Dependabot などで `@ai-sdk/react` を上げると、Vite 版でも同じ 1 件が落ちる可能性がある**（Vite 版の lock は 4.0.132 のまま）ので、上げるときはこのテストを先に見る。旧 Vite 版のフォルダー（当時の名前も `frontend/`。のち削除）は残し（移行の間は `src` が二重になる）、CI・Dockerfile・配信は変えていない。**手順 4〜6 も済み（2026-10-24）:** 手順 4（本物の Google ログイン）は、Supabase の Redirect URLs に `http://localhost:3100` を足すとログインできた（利用者の確認）。手順 5: `next dev` の `rewrites` 越しで、偽の SSE が 1 秒刻みで届き、切断も約 2 秒でバックエンドの取り消しに伝わった。承認を得た実 API の 1 問も、Next → Vite → api で通った。`rewrites` は `API_PROXY_TARGET` を設定したときだけ有効（ローカルの確認用。本番では使わない）。手順 6: `frontend/playwright.next.config.ts` が `next build` + `next start` で E2E を実行し、4 件通る（`4e4c91e`）。`next start` の `rewrites` では、API が `cache-control: no-cache, no-transform` を返すので逐次に届く。この指定が無く `Accept-Encoding: gzip` を受けると、圧縮されてまとめて届く（偽の SSE で測定）。**Vercel の制約（公式の limits のページ、証拠等級 B。2026-10-24 に読んだ）:** 外部宛先への `rewrites` は最大 120 秒（超えると `ROUTER_EXTERNAL_TARGET_ERROR`）。本番は `rewrites` を使わず、ブラウザーから Cloud Run を直接呼ぶ。**Hobby の規約（公式の Hobby と Fair Use のページ、等級 B。利用規約の本文と Fair Use の「例」の節は未読）:** 非商用・個人利用のみ。誰かの金銭的利益のための使い方は商用に当たり、Pro が要る。今の使い方なら Hobby でよい。**手順 7（Vercel。手動デプロイのみ）は 2026-10-24 に着手し、2026-10-25 に完了:** 開発コンテナに Vercel CLI 63.1.2 を固定で入れた（`.devcontainer/install-vercel.sh`。2026-10-31 に削除した。上の記録を参照。ログイン情報は残さず、デプロイのたびに `vercel login` → 終わったら `vercel logout`）。Root Directory は設定せず、`frontend` で `vercel link` する（CLI は `frontend` だけをアップロードする）。環境変数は Production のみ（`NEXT_PUBLIC_SUPABASE_URL`・`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`・`NEXT_PUBLIC_API_BASE_URL`。`API_PROXY_TARGET` は入れない）。Supabase は Site URL を変えず（Cloud Run 単体の画面を退路に残す）、Redirect URLs に Vercel の固定 URL（`vercel deploy` の `Aliased:`）だけを足す。Cloud Run の `CORS_ORIGINS` をその URL に絞る（タグ付きプレビュー）。（この時点の未確認項目は、下の「最終確認」で確かめた。）CI の `frontend` ジョブ（単体テスト・型検査・ビルド）がある。**Vercel MCP は導入済み（2026-10-24、利用者の指示で保留を解除）:** ローカル登録（`.mcp.json` には入れない）、OAuth 済み。公式の文書（等級 B）に読み取り専用の接続は見当たらず、請求・ドメイン・DNS・鍵・ファイアウォール・デプロイ・環境変数の変更を含む 200 以上のツールが、利用者のアカウントと同じ権限で使える。gcloud・gh の「読み取りだけ」と違うので、書き込み系は権限の規則で止める（`.claude/settings.local.json`。許可 = 読み取り 12 件、毎回確認 = 9 件、拒否 = 請求・ドメイン・DNS・鍵・ファイアウォールなど 19 件。2026-10-24 に導入。拒否した 19 件がツールの一覧から消えたことは確認、確認が出る動きは未確認）。初回のデプロイは 2026-10-24 に CLI で成功（本番の URL は `https://docunavi-eosin.vercel.app`、認証なしで 200）。その後（2026-10-25）: Supabase の Redirect URLs に `https://docunavi-eosin.vercel.app/**` が入り（管理 API の読み取りで確認。Site URL は Cloud Run の URL のまま）、Cloud Run の `CORS_ORIGINS` はその URL だけに絞られた。 **Vite 版の削除（2026-10-24）:** E2E を `frontend` に移し（新旧とも 4 件通過）、`frontend/`・FastAPI の静的配信・CI の `frontend` ジョブ・イメージの Node ステージを削除した。Compose の `frontend` は next dev に置き換えた（ホストで起動を確認済み、2026-10-25。ホットリロード・停止の所要時間・Google ログインの戻りは未確認）。Cloud Run の本番は、API のみのイメージ（`docunavi-00016-ces`）を 2026-10-25 に promote 済みで、`/` は 404。promote 後の実 API の確認は 2 問: 長い要約の質問は、最初の Gemini 呼び出しが 16.9 秒かかり、アプリの 60 秒のストリーム待ちで 504 の固定文になった（原因は未調査。Gemini の遅さを疑っているだけ）。短い質問は成功（13.4 秒、回答・出典・会話一覧の反映まで確認）。**最終確認（2026-10-25 03:34〜03:37 JST、証拠等級 A。利用者のログイン状態を渡した Playwright で本番 URL `https://docunavi-eosin.vercel.app` を操作し、無料キーの実 API で質問 2 回。コンソールのエラーは 0 件）:** **Git 連携:** Vercel の API で、`main` への各プッシュが本番デプロイになっていることを確認した（最新は `bf480b1`、READY）。Root Directory の設定値は読めていない。リポジトリ直下に Next.js が無いのに `frontend` のビルドが通っているので、`frontend` のはず（推測）。 **ログイン:** Google ログイン済みの画面（会話の一覧・資料一覧）が出た。 **逐次表示:** 送信から 3.9 秒で回答が出て、会話欄は 65 → 100 文字（44 ミリ秒間隔）→ 出典カードの順に複数回更新された。回答が短く（約 100 文字）、0.1 秒で終わるので、目で見える逐次表示かは分からない。長い回答での逐次表示は未確認。 **停止:** 送信の約 1 秒後に「停止」を押すと、通信は `ERR_ABORTED` で中断し、エラー表示は無く、入力欄は有効に戻り、15 秒後までに回答は出なかった。読み直して開いた会話には質問だけが保存されていて、回答は無い。サーバー側は、本番ログに「クライアントが切断したため LLM 呼び出しを取り消しました (elapsed=1.0s)」が出た（停止した会話に質問だけが残ることと合う。下の②の追記）。 **関係図:** 入寮規程で表示された（ノード 28、エッジ 19）。 **PDF:** `GET /documents/3/file` が 200 で、中身は 94,203 バイトの PDF（先頭 `%PDF-`）。ヘッドレスではビューアーが描画されないので、私は画面を見ていない。画面での表示は、利用者が自分のブラウザーで開いて問題なしと報告した（2026-10-25）。 |
| ② | 本番での切断 → LLM 取り消しの確認 | **確認済み（2026-10-25。下の追記）。** ①と並行して行う確認タスクだった。本番ログで確認し、結果で⑥の要否を決める。**2026-10-24 の読み取り:** 過去 14 日のログに、切断の記録（499、「クライアントが切断したため…」のログ）は無い。`/chat` の要求は 13 件で、すべて最後まで走っている。切断された要求が無いので、これでは判断できない。確かめるには、本番で 1 回停止する操作が要る。**実測（2026-10-24 22:00〜22:04 JST、証拠等級 A。無料キーで 3 回、`curl --http2` で `/chat/stream` を呼んで切断。本番に会話 3 件。thread_id は `cancel-probe-1024-2200`・`-2201`・`-2203`）:** 最初の本文が来る前の切断（1.5 秒で切断。本文は約 33 秒後）では、「クライアントが切断したため…」のログも 499 も無く、リクエストは 200 で 35 秒、最後まで走った。切断の後にも、8 秒応答が無いためフォールバックのモデルへの追加の試行を投げており、無駄なトークンが出る。本文の直後の切断（2 回。本文の 0.45 秒後と、0.13 秒前）でも、取り消しのログは無かった。**判断:** 既定の HTTP/1.1（Cloud Run → コンテナ）では、切断が本番のアプリまで伝わらない（ステージ 2 の測定と同じ）。⑥（h2c + hypercorn）を検討する根拠になる（着手は利用者が決める）。**限界:** `curl` は接続ごと閉じる。ブラウザーの停止（ストリームだけの取り消し）で同じかは未確認。アプリ側が切断を検知できないのか、信号がコンテナに届かないのかは、この測定では切り分けていない（ステージ 2 は後者）。停止後の会話の保存内容は未確認。**【2026-10-25 の追記、証拠等級 A】** 上の測定は、本番が HTTP/1.1 のリビジョンだったときの結果。その後、本番は `docunavi-00016-ces`（ポート名 `h2c` = `--use-http2`、起動は hypercorn。`gcloud run revisions describe` で確認）になり、全リクエストのログのプロトコルが `H2C` になった。この本番で、ブラウザー（Playwright）の「停止」ボタンを送信の約 1 秒後に押すと（03:35 JST）、本番ログに「クライアントが切断したため LLM 呼び出しを取り消しました (thread_id=…, elapsed=1.0s)」が INFO で出た。停止した会話には質問だけが保存され、回答は無い。**注意:** 旧リビジョン `docunavi-00013-nuq`（HTTP/1.1）へロールバックすると、この取り消しは効かなくなる。 |
| ③ | Cloudflare Worker | 独自ドメインの取得後に実装する。`api.` → Worker → Cloud Run の経路を作る（段階 3） |
| ④ | Go の SSE 中継 | 条件付き。**学習用の中継を `ops/sse-relay/` に作り、開発コンテナ内で測定した（2026-10-25、利用者の決定。結果は [target-architecture.md](target-architecture.md) の「Go の SSE 中継の測定」。疑似 SSE で 1000 本まで、切断は 1〜3 ms で伝わる、遅延はミリ秒。その後、**本番に配備した（2026-10-25、利用者の決定。Cloud Run の `docunavi-sse-relay`、h2c、画面の `/chat/stream` だけがこの中継を通る。環境変数 `NEXT_PUBLIC_STREAM_BASE_URL`）。本番で確かめたのは、実質問の `POST /chat/stream` が 200 で回答と引用が出ること、CORS の `OPTIONS`、同時 2 件の疎通、切断の伝播 1 回（回答本文の最初のチャンク直後にブラウザーから切断し、21 ms 後に API が LLM 呼び出しの取り消しを記録。証拠等級 A、1 回の実測。生成の終盤だった可能性があり、モデル提供元で生成・課金が止まったことは未確認）。未確認: 中継自身の `: ping`、負荷の上限、本文サイズの上限、レート制限。詳細は [ops/sse-relay/README.md](../ops/sse-relay/README.md)）。****切断が伝わらない問題だけなら、先に⑥（h2c + hypercorn）を検討する。** Go の SSE 中継は、同時接続数や接続の分離など、別の理由が出たときに検討する。製品の必要から採用する条件は、[target-architecture.md](target-architecture.md) の「採用条件」にある。学習のために足すときは、利用者が決める。**100 同時の試験は 2026-10-25 に済み（本番には上限を入れない決定を含む。上の「Go の SSE 中継の 100 同時接続の試験」）。** |
| ⑤ | Django（SaaS 管理） | 条件付き。**学習用の試作を `ops/django-saas/` に作り、開発コンテナ内で測定した（2026-10-25、利用者の決定。結果は [target-architecture.md](target-architecture.md) の「Django の測定」。マイグレーションは `saas` スキーマだけで完結し `chatbot` は不変、JWT の検証は今の `app/auth.py` と同じ約 70 µs、手で書く量が減るのは管理画面とマイグレーションの自動生成。疑似データ・各 1 回で、実鍵・本番・課金は未測定。試作は本番には入れていない）。**2026-10-26 に、同じ試作を学習用の最小版まで広げた（`5d5c667`、コミット・プッシュ済み。本番の `docunavi-admin`・Supabase・Cloud Run には影響せず、実メール・実決済・実 PDF・実検索もしない）。`Plan`（人数・PDF 数・月間検索回数の上限。値は架空で、強制しない）、`Organization`、`Membership`（JWT の `sub`）、役割 4 段階（`viewer` / `member` / `admin` / `owner`）、`Subscription`（契約状態 `trialing` / `active` / `canceled`）、冪等な架空データ投入 `seed_demo`、Django Admin（組織・メンバー・プラン・契約）。テスト 35 件が通る。一方、Django 自体は 2026-10-26 に、ロール割り当ての運用者向け管理画面（⑧ のフェーズ 4a、利用者の決定）として `ops/access_admin/` に別に実装し、非公開の Cloud Run `docunavi-admin` に配備した（試作の `ops/django-saas/` は使っていない。運用者 1 人向けで、利用者向けの機能やマルチテナントではない）。**2026-11-01 に、利用者の指示で試作を「動く形」にした:** 画面の「メンバー管理」（追加・役割の変更・外す。以前は「準備中」の表示だけ）を実装した（`ops/django-saas/saas/member_admin.py`。規則は同ファイルの docstring: owner は全役割・admin は member / viewer だけ・プランの人数の上限・最後の owner を残す・同時操作の行ロック・別組織は 404・CSRF）。Django のテスト 88 件、`manage.py check`、`makemigrations --check`、一時 Postgres 17 上の `runserver` への HTTP（ログイン → CSRF なしの POST は 403 → 上限超えの追加は 409）で確認した。API の追加は owner だけのまま（画面は admin にも許す、試作の決定）。**本番には入れていない（`docunavi-admin` とは別物のまま）。メール招待・決済・実 PDF・実検索とのつなぎ、利用制限の強制、本番デプロイは未着手。**本番の SaaS 管理としての Django は、それらを決めるまで条件待ち。 利用者向け SaaS の具体的な要件、またはマルチテナント・課金・権限の本番要件が固まったときに、別途設計と承認を取ってから再開する。試作では、メール招待、決済・請求、実 PDF・実検索との接続、利用制限の強制、本番デプロイはしていない。マルチテナントの分け方（スキーマ・`tenant_id` の行・DB）と導入の時期は未決 |
| ⑥ | `--use-http2` + hypercorn（h2c） | **本番に適用済み（2026-10-25 に確認。②の追記）。** 以下は適用前の測定の記録。**2026-10-24 の実測（本番とは別の 0% のタグ付きリビジョン `docunavi-00015-zuw`、`--use-http2`、実 API・無料キー）:** `curl --http2` を殺して切る 2 回（最初の本文の前 1.5 秒、最初の `data:` の直後 8.2 秒）とも、約 20 ミリ秒後に「クライアントが切断したため LLM 呼び出しを取り消しました」の INFO ログが出た。リクエストログのプロトコルは `H2C`。切断後の 60 秒間に、再試行・ヘッジ・フォールバックのログは無い。**未確認:** 3 回目は切る前に最後まで流れた（17.5 秒。切断の測定は 2 回）。リクエストログの status は 3 件とも 200 で、499 は見えない（アプリのログが根拠）。ブラウザーの停止ボタンは、本番で確認した（2026-10-25。②の追記）。本番の切り替えは `docunavi-00016-ces` で済んでいる（いつ・誰の指示で付けたかは、この記録では確認していない）。**既知の問題:** h2c で切断した後、ローカルの hypercorn が SIGTERM に応じないことがある（Cloud Run は約 10 秒で SIGKILL。調べていない）。ビルドは hypercorn（`2976401`）。 |
| ⑦ | Claude Code 用の Vercel プラグイン | **導入済み（2026-10-25、利用者の決定。条件つき）。** `.devcontainer/install-vercel-plugin.sh` が起動のたびに project スコープで入れる（導入済みなら何もしない）。`vercel-plugin@vercel` 0.54.1、コミット `3c88cfc` を `claude plugin list` と `installed_plugins.json` で確認した（証拠等級 A）。詳細と更新の手順は [.devcontainer/README.md](../.devcontainer/README.md) の「開発環境の Vercel プラグイン」。**確認済み（コンテナの再ビルド後、2026-10-25、証拠等級 A）:** スキル `vercel:*` とエージェント 3 つが読み込まれ、SessionStart のフックが動いた。`~/.config/vercel-plugin/` は作られない（テレメトリ停止が効いている）。`/mcp` に、プラグイン由来のサーバーは出ない（既存の `vercel` だけ）ので、deny 規則は要らない。`CLAUDE_ENV_FILE` への追記は、このリポジトリでは起きない（プロファイラが「Vercel の目印が無い」で何もせずに終わる。一時ファイルを環境ファイルにして実行しても空。登録された他のフックは追記しない）。空の `~/.claude` とプロジェクトからの導入は、`CLAUDE_CONFIG_DIR` を分けた状態で `install-vercel-plugin.sh` を実行し、0.54.1・`3c88cfc` で入り、2 回目は「導入済み」になり、リポジトリは無変更だった。 条件: 知識の注入・スキルだけに使う（操作は既存の Vercel MCP）／`VERCEL_PLUGIN_TELEMETRY=off`／付属の MCP（`mcp.vercel.com`）は無効／`/vercel-plugin:deploy prod` は使わない（本番は `main` へのプッシュの Git 連携だけ）／設定ファイルの変更は承認を得てから。**調べたこと（2026-10-25、証拠等級 B。公式の [Vercel Plugin](https://vercel.com/docs/agent-resources/vercel-plugin) と、GitHub `vercel/vercel-plugin`〔v0.54.1〕の `hooks/*.mjs` を読んだ。実行はしていない。読んだのは、登録されたフックと、それが import する `compat`・`hook-env`・`logger`・`session-start-activation`・`skill-telemetry`・`telemetry`・`vercel-config`・`vercel-context`・`setup-telemetry`。`session-start-profiler.mjs`〔32 KB〕は、外部への実行・読み取り・ファイルの扱いに関わる箇所だけ。登録されない `pretooluse-skill-inject`・`user-prompt-submit-skill-inject`・`lexical-index` などは読んでいない）：** Claude Code 向けのフックは SessionStart ×3・PostToolUse（`Skill`）・SessionEnd。SessionStart は、空のディレクトリか Vercel / Next.js の目印があるプロジェクトでだけ働く。プロファイラは、`vercel --version` と `npm view vercel version`（npm のレジストリへの問い合わせ）を実行し、`package.json`・`vercel.json` を読み、`.env.local` などの**有無**だけを見る（中身は読まない）。`~/.config/vercel-plugin/`（インストール ID・スタンプ・セッション印）と一時ファイルに書く。環境ファイル（`CLAUDE_ENV_FILE`）に `export` 行を追記する経路がある（このリポジトリでは使われない。上の確認済みを参照）。テレメトリは `telemetry.vercel.com` へ、`dau:active_today`・`plugin:version`・`plugin:install_id`・`plugin:agent_harness`、スキルを読み込んだときの `skill:invoked` を送る。プロンプトとツールの中身は送らない。`VERCEL_PLUGIN_TELEMETRY=off` は、日次・スキルの両方の送信を止めることをコードで確認した。 **未確認:** Bun が要るとあるが、この環境に Bun は無い（Node 24 はある。フックは `node` で動く）／付属の MCP をどう無効にするか（プラグイン経由のツール名と、`.claude/settings.local.json` の規則が効くか）／インストール方法（`npx plugins add` が何を入れるか、Claude Code の `/plugin` 経由との違い）／インストール後の動作。 |
| ⑧ | ロール別 PDF グループ制御 | **フェーズ 1〜3 は完了（2026-10-26）。migration は本番に適用済みで、本番は `ACCESS_MODE=enforce`（Cloud Run `docunavi-00025-qey`）。フェーズ 4a（Django Admin。運用者向けのロール割り当て画面）は、R0〜R9 まで済み（2026-10-26）。`ops/access_admin` を非公開の Cloud Run `docunavi-admin` に配備済みで（`run.invoker` は利用者本人だけ。Django の表は `saas` スキーマ。`access_admin` ロール・`chatbot.admin_users` ビュー・監査は migration で適用済み）、パスワードと TOTP でのログインを、プロキシ経由で確かめた。限定ロール `cafe-staff` と、`cafe` への紐づけを作り、運用者本人に一時的に割り当てて `cafe` だけが見えることを確かめたあと、割り当てを解除して `default`（3 グループ）に戻ることを確認した（割り当てと解除は SQL で、監査の `actor` は `postgres`）。**未確認:** 検索が `cafe` 以外を返さないこと（実際の Gemini の埋め込みが要る）／Django 画面の「見えるグループ」の表示／`actor=access_admin` の監査記録。QR の白背景とダークモードの主ボタンの修正（`4ce3bc0`、`4b96444`）は 2026-10-26 に本番へ再配備し、`totp_reset` のあとの再登録で、白背景の QR を認証アプリで読み取れることまで確かめた（利用者の報告）。設計・手順・未確認は [access-admin.md](access-admin.md)。** 仕様と運用は [configuration.md](configuration.md) の「資料グループのアクセス制御」。**計画（2026-10-26、利用者の決定）:** **目的:** ユーザーロールごとに、表示・検索できる PDF グループ（`documents.collection`）を制御する。見えないグループは、一覧・検索・回答・出典・本文・PDF・関係図・履歴表示のすべてに出さない（全面不可）。**方針:** 権限の本体は Supabase Postgres と FastAPI。Django は運用者向け管理画面の候補の一つで、導入は必須ではない。**この項目で、上の 3 番の「資料は全員共通」（2026-10-20）を、グループ単位の絞り込みについて見直す**（利用者のアップロードは作らない点は変わらない）。**決定済みの設計（2026-10-26）:** ロールは「グループの束」だけで、コード上の `admin` などは持たない／ロールのない利用者は `default` ロール（現行の `zahard` `cafe` `guides` の 3 グループ。既存利用者の挙動を変えない）／ロールの持ち主は Supabase の `sub`（UUID）に紐づく自前の表。外部キーは張らない／権限の解決はキャッシュなしで毎リクエスト（必要になってから 30 秒程度の TTL を検討）／過去の会話は表示するが、現在見えないグループの出典・本文は表示しない／MCP（`docunavi-mcp`）は運用者専用・全グループのまま。利用者には公開しない（docs に明記する）／UI のモックは省略（`GET /collections` の結果に合わせて項目が減るだけで、構成は変えない）。**フェーズ 1（DB と migration）:** `chatbot.roles`・`chatbot.user_roles`・`chatbot.role_collection_access`・`chatbot.access_audit`、`default` ロールと現行 3 グループの初期データ、`chatbot_app` への SELECT、`anon` / `authenticated` の revoke。**RLS はこのフェーズでは入れない**（別フェーズで、性能の測定と、取り込み・MCP・起動時の検査・評価への影響を調べてから判断する。`documents` `chunks` の所有者は `chatbot_app` で、`FORCE ROW LEVEL SECURITY` が要る。2026-10-26 に本番の読み取りで、RLS が全表で無効であることと、`anon` / `authenticated` に `chatbot` スキーマの権限が無いことを確認した）。一時 Postgres での検証、migration ファイル（`supabase/migrations/`）の作成、コミット。push は別途承認。**フェーズ 2（FastAPI のアクセス制御）:** `app/access.py` の `visible_collections(user_id)`／検索・回答系に必須の `scope`（`Scope.ALL` を明示しない限り全件を検索しない）／次の 10 経路に制限: 検索の SQL（`search_vector._SEARCH_SQL`）・`GET /documents`・`/documents/{id}/chunks`・`/documents/{id}/file`・`/documents/{id}/graph`・`/documents/{id}/impact`・`GET /collections`・`/chat`・`/chat/stream`・会話履歴の表示／見えない資料は 404、見えないグループの指定は 403、見えるグループが 0 件なら 403「利用できる資料がありません」／エラー文は固定で、非表示グループ名を出さない（今の 409 の文は両方の範囲名を載せるので直す）／`ACCESS_MODE=off\|shadow\|enforce`（`shadow` は絞り込まず、減る件数だけをログに出す）／`cafe` だけのロールで `zahard` の内容が出ないことを確かめる、10 経路の漏れ総当たりテスト／docs の更新（アクセス制御の仕様、MCP が運用者専用であること、`ACCESS_MODE` の意味、運用者が SQL で `user_roles` を更新する手順）。コミット。push は別途承認。**フェーズ 3（本番適用と段階導入）:** 本番 DB への migration 適用は操作ごとに承認／本番はまず `ACCESS_MODE=shadow` で観察し、問題がなければ `enforce` に切り替える（切り替えも別途承認）／Vercel のデプロイ・Cloud Run の変更も別途承認。`supabase db reset`・`db pull`・`db dump` はしない。**フェーズ 4（管理画面。条件付き）:** フェーズ 3 の完了後に検討し、運用者が SQL で `user_roles` を更新する運用が続く場合にだけ着手する。候補は Django Admin・Next.js の管理画面・Supabase Studio での手動運用。Django を採るなら運用者専用の管理コンソールに限り（利用者向け機能は作らない）、試作用の `ops/django-saas` のロールは使い回さない。実装の前に、別途設計と承認を取る。**暫定の運用:** フェーズ 4 までは、運用者が SQL で `user_roles` を更新する。**未確認:** 公開環境のサインアップの方針（誰でも Google で入れるか）／運営者が UUID とメールをどう対応づけるか／`lib/collections.ts` の固定の表示名が、一覧の元になっていないか／Data API の公開スキーマに `chatbot` が入っていないこと（`authenticator` の設定を読み切れていない）。 |

- 検索の品質は、資料が少ないうちは、ハイブリッド検索より先にチャンクの分け方と `TOP_K` を見る。実際に、関係の薄い 2 件目のチャンクが出典に出た例がある（`TOP_K` は 2、入寮規程の PDF は 3 チャンク）。ハイブリッド検索（4c）を試すための資料の増やし方（2026-11-01）: `evals/fixtures/` の 5 冊（menu 2・gym 2・two_column 1）が 204 チャンクで、既存の 80 と合わせて 284 チャンク（11 冊）になり（`--apply` なし・DB なしで実測。実 API・本番 DB は使っていない）、最小構成の目安（300〜400）まで 16〜116 チャンク足りなかった。そこで、完全一致が効く型番・仕様書・リリースノートの資料（案 A）を足した（`20c6662`、2026-11-01）。`evals/fixtures/exact/`（もとは `evals/corpora/exact/`。下の ID の件で移した）に PDF 3 本（`ap_air_purifier_spec`・`rv_robot_vacuum_spec`・`notememo_release_notes`）と質問 e001–e050（もとは x001–x050）があり、新しいチャンクは 137 件で、合計は 421 チャンク（284 + 137）になった（コミットメッセージの記録。根拠の 50 文がすべて PDF の抽出結果にあることも同じ記録）。**案 A は実施済みで、資料を足す目的ではもう要らない。** 421 は目安（300〜400）の上端を 21 超えるので、量の不足は無い。別の条件（質問の型の偏りなど）で資料が要るかは、試験を設計するときに決める（今は決めていない）。**この PDF は `evals/corpora/` の評価用で、`storage/pdf/` にも本番 DB にも入れていない**（4c の試験そのものは未実施。条件は変わらず「取りこぼしが出てから」）。
- 段階 3（Cloudflare）は、自分のドメインを持ったときに、順序を問わず入れる（2026-10-22 時点で未実施）。
  形は、**Cloudflare を前段（DNS・独自ドメイン・レート制限）に置き、Cloud Run はアプリ本体（オリジン）のまま**にする。Cloud Run は置き換えない。
  根拠は、別の AI の回答（証拠等級 C。公式の文書は私が読んでいない）: Cloudflare はカスタムオリジンとして Cloud Run を使える。Cloud Run のカスタムドメインは、グローバル外部 Application Load Balancer が推奨で、ドメインマッピングは Preview（本番非推奨）という説明。
  **入れる前に確かめること:** 上の 2 点を公式の文書で（ドメインマッピングが `asia-northeast1` で使えるか、今も Preview か）。Cloudflare を前段にしたあと、`*.run.app` の直アクセスを塞ぐか（ingress）。クライアント IP がアプリにどう届くか（[configuration.md](configuration.md) の、前段にプロキシを置くときの注意）。
- **SSE と切断の測定（Vercel 分離の段階 2。2026-10-22〜24）。** LLM・DB・秘密を持たないプローブを Cloud Run に置き、Cloudflare の Worker（`workers.dev`）を通して測った。証拠等級 A。プローブと Worker は測定後に削除した。詳細と表は [target-architecture.md](target-architecture.md) の「測定済みのこと」。
  - Worker は SSE をバッファせずに通した。5 MB のバイナリも sha256 が一致した。最初の本文を 60 秒遅らせた SSE も、Cloud Run が 90 秒で切るまで届いた。
  - 切断は、Cloud Run の既定（コンテナまで HTTP/1.1。uvicorn）では伝わらない（4/4）。`--use-http2` と hypercorn（h2c）では、直接も Worker 経由も 6/6 で伝わった。
  - **実アプリでは未確認。** 本番の `docunavi` は、停止やタブを閉じたときに LLM 呼び出しを取り消していない可能性がある。確かめるなら、本番ログを使う別作業にする。
  - Go の SSE 中継は未測定で、候補のまま。
- **Go の SSE 中継の 100 同時接続の試験（2026-10-25、完了。証拠等級 A。Cloud Run、各 1 回、疑似の上流）。** 試験用に別名の中継と疑似上流を Cloud Run に置き（最大台数 1、同時実行数は上限より大きく設定）、負荷ツール `ops/sse-relay/bench/trial_load.py` で測った。試験用のサービス・イメージ・サービスアカウントは試験後に削除済み（本番の中継には触れていない）。
  - 1・10・25・50・75・100 本の同時接続はすべて成功した。上流の `active` は本数どおりで、エラーは 0。100 本の最初のバイトまでの時間は p50 約 100 ms、max 598 ms。
  - 100 本を保持したまま 101 本目を足すと、0.11 秒で 503（日本語の `detail`、`Retry-After: 5`、許可した Origin の CORS ヘッダー）になり、上流には届かなかった。中継のログに `stream_limit_rejected active_streams=100 limit=100` が出た。
  - 25 本をクライアントから切ると、25 本とも上流に伝わり（`client_disconnected` が 25 増加）、`active` は約 46 ms で 0 に戻った。
  - **本番の中継には上限を適用しない（利用者の決定。案 C）。** 本番の中継は Cloud Run の同時実行数 20 × 最大 2 台 = 40 本で、API 側も 2 台 × 同時 20 = 40 本。この 40 本に対して `MAX_CONCURRENT_STREAMS=100`（コードの既定）は届かず、効果がない。20 にすると中継自身が 503 を返すが、台数ごとの上限なので、別の台が空いていても 503 になりうる。本番の中継のイメージは `ac95150`（上限のコードを含まない）のまま。
  - **測っていないこと:** 中継のメモリ・CPU（Cloud Run）、100 本を切る試験、実 API・Gemini を通した試験（段階 1b）、冷えた起動、本番の中継、503 のあとの画面側の再試行。再検討の条件は [go-architecture-plan.md](go-architecture-plan.md) の §8。
- ホストでの `docker compose up` の確認は、順序表から外した（2026-10-20）。2026-10-21 に、`api` と `frontend`（Vite の開発サーバーを別コンテナに分けた）が healthy になることなどをホストで確かめた（範囲と未検証は [development.md](development.md) の「検証の記録」）。
- **`/chat/stream` の最初の本文までの無反応**（2026-10-21、Compose の確認で見えた課題。実キーの実 API で、質問してから最初のバイトまで 22〜31 秒。その後の本文は約 1.3 秒で流れる）。
  **2026-10-25 に、段階の表示を実装した（利用者が案 A を承認。証拠等級: 偽の LLM・偽の検索でのテストとブラウザー確認 = A、実 API・本番 = 未確認）。** レスポンスを最初の本文より前に始め、
  `data-status`（`analyzing` → `searching` → `generating`。transient）を流す。画面は、待機中の吹き出しの文言を「質問を読み取っています…」「資料を検索しています…」「回答を生成しています…」に変える（レイアウトは変えない）。
  **トレードオフとして、最初の本文の前の失敗も `error` チャンクで返すようになった**（HTTP ステータスは 200）。`/chat` が HTTP で返すステータスと `Retry-After` は、直前の `data-failure` で伝える（`error` チャンクは `errorText` しか運べない）。
  仕様は [api.md](api.md)。**実時間は変わらない（22〜31 秒は縮まっていない）。** 内訳（検索・埋め込み・最初の Gemini 呼び出しのどれが何秒か）は未計測で、実 API の呼び出しが要る（承認が要る）。
  **未確認:** 実 API・本番（Cloud Run の h2c → Vercel の画面）での段階の表示と、実際の 429・504 が `data-failure` で画面に届くこと、停止ボタンの切断が本番でこれまでどおり取り消されること（切断のログの文言が変わりうる。[configuration.md](configuration.md) の「クライアントの切断」）。
  **CLAUDE.md の「切断は失敗ではない」の節に、「レスポンスは最初の本文が届くまで始めない」とあるのは、この変更で古くなった（承認を得て直す）。**
- **開発コンテナまわりの完成形の構想**（2026-10-21、別の会話で整理して利用者が記録を頼んだもの。**未着手・順序表には入れない**。急がず、必要になってから段階的に進める）。
  着手前（2026-10-21）は、開発コンテナ（`docunavi-devcontainer`）と読み取り専用の Docker プロキシ（`docunavi-docker-proxy`）の 2 つがネットワーク `docunavi-dev` にあり、`api` と `frontend` はホストで `docker compose up` したときだけ動く、という形だった。
  構想は、これを 5 つのコンテナと 3 つのネットワークにする形。
  - コンテナ: `docunavi-devcontainer`、`docunavi-docker-proxy`、`docunavi-api`（:8000）、`docunavi-frontend`（:3000、ホストに公開。2026-10-24 までは Vite の :5173）、`docunavi-db`（Postgres。ローカルかステージングの DB を、アプリ用に置く）。
  - ネットワーク: `docunavi-dev`（開発コンテナ・プロキシ）、`docunavi-frontend-net`（frontend・api）、`docunavi-backend-net`（api・db。`internal: true`）。
  - ボリューム: 今の 7 つに、Postgres のデータ用 `pg-data` を足して 8 つ。
  - 進める順の案: ① `api`・`frontend` を Compose の管理に寄せ、ヘルスチェックを `db` にも付ける（`api` と `frontend` は、Compose 側にはすでにある）。② `db` をローカルかステージングにして、本番の DB と分ける。③ ネットワークを 3 つに分ける。④ プロファイルで dev・prod を分ける。
  - **得られること（別の会話のメリットの整理を、この構成の実際に合わせて直したもの）:** 本物の LLM を使う確認の DB を、`.env` の `DATABASE_URL` の先（本番と同じ DB なら、本番の履歴に確認用の会話が残る）から分けられる。`db` を `internal: true` のネットワークだけに置けば、ホストや他のコンテナから直接つながらない。
    **すでにできていること（効果として数えない）:** `docker compose up` 1 回での `api`・`frontend` の起動、ヘルスチェックと `depends_on: condition: service_healthy` による起動順。
    **増えるもの:** コンテナ（2→5）、ネットワーク（1→3）、ローカル DB のスキーマ・pgvector・マイグレーションの管理、空のローカル DB へ資料を入れ直す取り込みと埋め込み（`python -m app.ingestion` と `.embed`。実 API を呼ぶ）。
    チーム開発への対応や費用の削減は、このリポジトリでは根拠が無いので、メリットに数えない。
  - **決めたこと（2026-10-22、利用者の指示）:** `db` はローカルの Postgres（pgvector 入り）。`api` は `frontend` 経由だけにし、`localhost:8000` は公開しない（ビルド済みの画面や `curl` の確認は、`docker-compose.api-port.yml` を重ねて 127.0.0.1 に公開する）。
  **着手した範囲（2026-10-22。ホスト側でモックモードの起動まで確認済み。結果は `docs/development.md` の「ローカルの db」）:** `docker-compose.yml` に `db`・ネットワーク 2 つ（`frontend-net`、`internal` の `backend-net`）・`pg-data` を足し、3 サービスにヘルスチェックと `service_healthy` の起動順を付けた。
  `api` の `DATABASE_URL` は compose の `db` に固定（`.env` の値は使わない）。初期化は `db/local-init/01-bootstrap.sh`（入れ物だけ）。手順と注意は `docs/development.md` の「ローカルの db」。
  **資料の取り込み（2026-10-22 JST、ホスト側で実行済み。利用者の指示）:** テーブルの作成・マイグレーション 0001/0002/0003/0005・取り込み（3 資料群、80 チャンク）・埋め込み（実 API 3 回、約 $0.0023）が通り、`docker compose restart api` で `api` が healthy になった（実キーの起動の検査を通った）。結果は `docs/development.md` の「確認済み（資料の取り込み）」。
  **残り:** `api` が Gemini に届いて質問に答えること（画面から 1 問）/ ホストの `docker compose` との重なりの確認は、orphan の警告でプロジェクト名が同じことまで分かった / 構想の「3 つのネットワーク」のうち `docunavi-dev`（開発コンテナ側）は従来のまま /
  **開発コンテナを Compose ベースにした（2026-10-22、利用者の指示。再ビルド済みで、開発コンテナの中から確認できた範囲は設計どおり。ホスト側の確認と `stopCompose` は未実施）:** `devcontainer.json` が `dockerComposeFile` で `docker-compose.yml` と `.devcontainer/docker-compose.yml`（dev）を重ね、開くと `db` `api` `frontend` も起動する。
  確認の手順と未確認の前提は `.devcontainer/README.md` の「開発コンテナの構成（Compose）」。これで構想の 5 コンテナ・3 ネットワークが 1 つの設定にそろう（プロキシだけ別の Compose プロジェクト）。
  構想にあった「プロファイルで dev・prod を分ける」は、**2026-10-26 に形を変えて実装した（利用者の承認。ホストで、`config`・プロファイルの出し分け・ビルド・起動・healthy・`/health` まで確認済み）:** `dev` プロファイルは作らず（`db` `api` `frontend` は無所属のまま、常に起動対象）、`prod` プロファイルは `api-prod` だけ（本番と同じ `Dockerfile` の `CMD`、マウントなし・`--reload` なし・ポート非公開・`restart: "no"`。`frontend-prod` は作らない）。手順は `docs/development.md` の「本番相当の起動の確認」。**注意:** 資料を取り込む前の、実キーの `docker compose up` は `api` の起動検査で失敗し、`unhealthy` のまま止まる（取り込んだ後は `docker compose restart api`）。`docker compose down -v` は `pg-data`（埋め込み）も消す。
  **決まっていないこと:** 本物の LLM を使う確認の DB を、いつ・どの資料から入れるか（本番と同じ資料を埋め込み直すか、一部だけか）。

**budget-guard-disarm（実装・配備・動作確認済み、2026-11-01、`DRY_RUN=true`。順序表には入れない）:** `budget-guard` が課金を外した後に、人が承認して課金を戻す HTTP エンドポイント `POST /disarm`（`ops/budget_guard_disarm/`）。鍵は 2 つで、Cloud Run の `run.invoker` と、使い捨てのトークン（`X-Disarm-Token`。期限 15 分）。Secret Manager にはハッシュと期限だけを入れ、使うとそのバージョンを無効にする。構造化の監査ログを出す。`DRY_RUN` の既定は `true` で、この間は課金を変えず、トークンは消費する。**配備先は `imagen-app-2026`**（課金が `budget-guard` の停止の対象外。利用者の報告で、私は確認していない）。課金が外れると同じプロジェクトの Cloud Run と Secret も止まるため、`docunavi-app` には置けない。**動作確認（2026-11-01、利用者の報告）:** トークンなし 403／正しいトークン 200 `{"dry_run": true, "changed": false}`／同じトークンの再使用 403。**未確認:** `DRY_RUN=false` での実際の課金の再開、`latest` が無効になった後の挙動、etag 不一致のエラーの種類、必要な権限（`billing.user`・`projectBillingManager`）が足りるか、`Authorization` の受け渡し、構造化ログの中身。`DRY_RUN=false` への切り替えは、別の承認を得てから行う。**以前の案（Eventarc で再紐付けを検知して `DRY_RUN` を戻す）は採らなかった。** **`DRY_RUN` の対象は 2 つあり、現在値が違う（2026-11-01 に整理）:** ① `budget-guard-disarm` = `true`（上のとおり。動作確認は利用者の報告）。② `budget-guard`（本体。課金を外す側）= **`true`**。リビジョン `budget-guard-00004-b7r` の環境変数を `gcloud run services describe` の読み取りで確認した（2026-11-01、証拠等級 A。最新のリビジョンかつトラフィックの宛先）。誰がいつ `true` にしたかは、この記録では確認していない。下の受動確認の表の `DRY_RUN=false` は、2026-10-31 14:24 UTC の発動時のリビジョン `budget-guard-00003-dul` の値で、**その時点の記録であって現在値ではない**（矛盾ではなく、`false` → `true` と変わった）。**注意（`budget-guard` 本体）:** `DRY_RUN=true` の間は、上限に届いても課金を外さない（ログに「DRY_RUN: would disable billing」が出るだけ）。つまり、いまは予算による自動停止が働いていない。`false` に戻す手順と時期は決めていない（判断は利用者。原因の特定を先にする）。`docs/deploy.md` の G の手順 3（`--set-env-vars`）を再実行すると環境変数が上書きされる。**別案（未着手。原因の特定の後に、別コミットで提案する）:** 予算の通知の `costIntervalStart`（予算期間の開始。公式文書で確認）で、古い期間の通知を無視する判定を `budget-guard`（`ops/budget_guard/main.py`）に足す。コードの変更なので、原因が古い通知だと分かるまで提案しない。2026-11-01 00:31 UTC の発動が古い通知だったかは未確認。**`budget-guard-disarm` の `DRY_RUN=false` への切り替えは、課金の再開が実際に確認できるまで行わない**（確認の方法は、そのとき利用者と決める）。

**予算額の見直し（済み: ¥3,000、2026-11-01。利用者の決定）:** 月 ¥1,000 を ¥3,000 に上げた。変更後の `describe` の結果は、利用者の報告（金額 3000 円、しきい値 50/90/100%、クレジットは差引前〔`EXCLUDE_ALL_CREDITS`〕、通知先 `budget-alerts`、対象 `projects/114733761361`、`DRY_RUN=true` のまま〔これは `budget-guard` 本体の値。予算そのものに `DRY_RUN` は無い〕。予算の API は閲覧用の ID では読めず、私は確認していない。本体の `true` は 2026-11-01 に読み取りで確認した）。11 月の予想（節約前、推定）は約 ¥4,500 で、¥3,000 でも節約前の判定では届きうる。節約前の判定を残すかは未決（請求レポート〔10〜11 月、サービス別・SKU 別、クレジット別〕を見てから決める）。`DRY_RUN=true` の間は、金額を変えても停止しない。

**検索改善の実装（2026-11-01、利用者の指示。4c・GraphRAG・リランカー。プッシュ・デプロイはしていない）**

実装: `app/search/pipeline.py`（入口。`app/agents/tools.py` の `search_chunks_async` がここを呼ぶ）、`app/search/keyword.py`（BM25）、`app/search/rerank.py`、`app/search/graph.py`。ベクトル検索（`app/search/vector.py`）は変えていない（`Hit` に `origin` の列を足しただけ）。

| 環境変数 | 既定 | 中身 |
|---|---|---|
| `SEARCH_HYBRID` | on | アプリ内 BM25（日本語は 2-gram、型番は丸ごとと部品）の順位を、ベクトルの順位と RRF（k=60）で合わせる |
| `SEARCH_RERANK` | on | 候補を語の一致（IDF 重み付きの被覆率と、質問の型番が本文にそのまま出るか）で並べ直す。最終点 = RRF（最大を 1）と一致の点の平均。外部のモデルも API も使わない |
| `SEARCH_GRAPH` | **off** | 上位のチャンクが「第N条」と参照する先のチャンク（同じ資料の中）を、`top_k` の外に追加の文脈として加える。出典カードには出さない（`weak`）。2026-11-01 の実測で辺が 0 本だったため、既定は off（利用者の決定） |
| `SEARCH_CANDIDATES` / `SEARCH_GRAPH_MAX_EXTRA` / `SEARCH_CORPUS_TTL_SECONDS` | 20 / 1 / 60 | 1 次検索の候補数、加える参照先の上限、資料の写しを持つ秒数 |

- 3 つとも off なら `search_vector.search` をそのまま呼ぶ（これまでと同じ結果）。不正な値は 503 の固定文（黙って既定に戻さない）。**ハイブリッドとリランカーは既定が on なので、このまま配備すると本番の検索が変わる**（GraphRAG は既定 off）。ローカルの `docker compose` も同じ。配備は別に承認を得る。
- **ベクトルの下限（`MIN_SIMILARITY`）を崩さない:** キーワードだけで拾うのは、質問に型番・版番号・コード（数字を含む英数字の連なり）があり、本文にそのまま出るチャンクだけ。語が重なるだけのチャンクは、ベクトルの下限を越えていなければ返さない（「資料にない質問」を断る前提を守る）。
- **失敗は別経路に戻さない:** キーワード側の DB の読み取りが失敗したら、ベクトルだけにはせず 503。原因を絞るときは、フラグを 1 つずつ off にする。
- **ログ（INFO、1 回に 1 行、`app.search.pipeline`）:** 質問は本文を載せず 8 文字のハッシュと字数だけ。フラグ、候補の数（vector / keyword / keyword除外）、写しの件数、所要ミリ秒、返したチャンクごとに 資料・ページ・経路（`vector` / `keyword` / `vector+keyword` / `graph`）・`vec`（コサイン類似度）・`bm25`・`rrf`・`lex`・`score`。全部 off の経路にも、経路とコサイン類似度の行を出す。
- **GraphRAG の形（利用者が選んだ「既存の関係図で拡張」からの違い）:** `app/relations` は入寮規程の PDF の原本を毎回読み直して辺を作るので、検索のたびには呼べない。そこで同じ種類の規則（「第N条」の参照）を、DB のチャンクの本文に当てて辺を作る（`app/search/graph.py`）。入寮規程に限らず、「第N条」で始まる行を持つ資料なら効く。「前条」「前項」と、語の定義への辺（`app/relations/terms.py`）は張らない。汎用の実体グラフは未着手。
- MCP（`docunavi-mcp`）は `search_vector.search` を直接呼ぶので、ベクトル検索のまま。

確認したこと（証拠等級 A、偽の LLM・偽の検索・実 API なし）:
- 新しいテスト 38 件（`tests/test_search_pipeline.py`）と、本物の Postgres 17 での資料の読み取りの SQL 2 件（`tests/test_search_pipeline_postgres.py`。`ready` の資料・埋め込みのあるチャンクだけ・並び・範囲の絞り込み）。conftest が既存のテストでは 3 つのフラグを off にしている（ベクトル検索のテストを動かさないため）。
- **既存検索との比較テスト:** 全部 off が `search_vector.search` と同じ結果・同じ引数（候補を増やさない）になること、型番でベクトルが落とした答えのチャンクを拾えること、語だけの一致は拾わないこと、キーワードの順位でベクトルの並びが変わること、リランカーで型番を含むチャンクが先頭に来ること。
- **実際の資料での測定（`evals/fixtures/exact` の PDF 3 本・137 チャンク、完全一致の 40 問、キーワード側だけ）:** 根拠が上位 1 件に入るのは 30/40、上位 2 件は 37/40、上位 5 件も 37/40。ベクトル検索との比較は、次の「実 API での比較測定」。
- 全体: 一時 Postgres 17 を付けた pytest で 2583 件通過・4 件スキップ。失敗 2 件は今回の変更とは別（下）。Django は 88 件、`manage.py check`、`makemigrations --check` が通る。

実 API での比較測定（2026-11-01、利用者の承認。`scripts/eval_pipeline.py`、結果は `evals/results/pipeline_compare.json`）:
- 条件: 実際の埋め込み（`gemini-embedding-2`、768 次元）を `evals/cache/`（Git 管理外）に保存し、5 構成を同じ埋め込みで比べた。LLM は呼んでいない。埋め込みの呼び出しは累計 18 回（うち 429 が 2 回。再試行なし）、概算 約 $0.0109（429 の失敗分も数えると約 $0.0126）。重みは `evals/pipeline/plan.json` に、結果を見る前に固定した。
- 質問は、答えのある 64 問（exact 50 + guides 14）。表は、根拠が上位 k 件に入った数。

| 構成 | k=1 | k=3 | k=5 |
|---|---|---|---|
| ベクトル検索のみ | 51 (80%) | 61 (95%) | 62 (97%) |
| ハイブリッドのみ | 53 (83%) | 61 (95%) | 62 (97%) |
| ハイブリッド + リランカー | 52 (81%) | 63 (98%) | 64 (100%) |
| ハイブリッド + GraphRAG | 53 (83%) | 61 (95%) | 62 (97%) |
| 3 つとも on | 52 (81%) | 63 (98%) | 64 (100%) |

- 本番の `top_k=2` では、ベクトルのみ 57、ハイブリッド 61、ハイブリッド + リランカー 63（64 問中）。差が出たのは exact だけで、guides はどの構成でも 13/14（k=1）・14/14（k=3）。
- リランカーは k=1 で、良くなった 4 問（e034・e036・e049・t003）に対し、悪くなった 3 問（e016・e044・a004）。k=2 以上で悪くなった問はなかった。
- GraphRAG は辺が 0 本（資料全体で）で、一度も働かなかった。入寮規程が 3 チャンクで、各チャンクに多くの条が入り、参照が同じチャンクの中に収まるためと見ているが、原因は確かめていない。効果は「弱い」のではなく「測れていない」。このため `SEARCH_GRAPH` の既定は off。
- 限界: 検索のベクトル側を Python で再実装している（本番の SQL は通っていない）。検索クエリは質問文そのままで、本番の LLM による言い換えは使っていない。64 問で、差は 1〜2 問。exact は型番を問う質問で、ハイブリッドに有利な集まり。資料外の質問 8 問のうち、何かが返るのは 6 問で全構成同じ。ログに質問文は 1440 行中 0 行。

未確認・限界:
- RRF の重み（k=60）、リランカーの配合（0.5 : 0.5、識別子 0.5）、`SEARCH_CANDIDATES` は、結果を見る前に固定した初期値のまま。調整していない。調整するときは、測定に使った質問とは別の質問の集まりで行う。
- GraphRAG の効果は未測定（下の実測で辺が 0 本）。測るには、条ごとのチャンク分割か参照辺の拡張が要る（未着手。検討中）。
- リランカーは語彙の一致だけで、意味的な並べ替えではない。Gemini やクロスエンコーダーへの差し替えは、費用・依存・遅延が増えるので入れていない（`app/search/rerank.py` の `lexical_scores` と同じ形の関数にすれば差し替えられる）。
- 資料の写しは、範囲ごとにプロセス内へ持つ（既定 60 秒）。チャンクが数千を超えたら、読み取りと BM25 が重くなる。そのときは、DB の全文索引（pgroonga）への移行を測る。取り込み直した直後の最大 60 秒は、キーワード側が古い（ベクトル側は新しい）。
- 本物の LLM を通した `/chat/stream` と、本番の DB（pgvector 入り）での確認はしていない。

見つけた別の事実（今回は直していない）:
- `tests/test_eval_retrieval.py` の 2 件の失敗は直した（2026-11-01、別コミット）。原因は、`20c6662` で足した質問 x001–x050 のうち x001–x004 が `evals/fixtures/menu/` と ID が重なっていたことと、`--app` が `evals/corpora/*/questions.yaml` を全部読んで `storage/pdf` に無い PDF を探していたこと。`evals/fixtures/exact/` へ移し、ID を e001–e050 に改めた。
- `scripts/test-postgres-up.sh` が、このコンテナでは失敗する（`apt-get download` が「候補が無い」）。開発コンテナのイメージに apt のリストが残っていないため（2026-10-31 の Dockerfile への移設後と見られるが、原因の切り分けはしていない）。今回は `apt-get -o Dir::State::lists=… update` を使う回避で取得した（root 不要、`/tmp` の中だけ）。

**受動確認（起きたときに見る。能動的には確認しない）**

確かめるのに費用や意図的な障害が要るもの、または放っておけば確かめられるものは、タスクにしない。該当のできごとが起きたら、ログで見る。

| 項目 | 起きるとき | 見る場所 |
|---|---|---|
| 無料キーが 429 になったときの、有料キーへの切り替えと 60 秒の保持 | 無料枠が尽きたとき | Cloud Run のログの `app.agents.resilience` の WARNING |
| 月 3000 円の予算での課金の自動停止 | **課金が外れることは確認済み（2026-11-01、証拠等級 A。Cloud Run のログの読み取り）。** 2026-10-31 14:24 UTC（23:24 JST）に、`budget-guard`（その時点は `DRY_RUN=false`。リビジョン `budget-guard-00003-dul` の環境変数で確認。**現在は `true`**〔リビジョン `budget-guard-00004-b7r`、2026-11-01 に読み取りで確認〕）のログに「billing disabled for the project」が出た。14:56 UTC から同プロジェクトで「billing is disabled for this project」のエラーが続き、2026-11-01 00:06 UTC のログでも続いていた。**未確認:** ① 前払い ¥800 が費用の判定に数えられたか ② Gemini API の費用が無料クレジットの対象外だったか ③ 発動時の費用の額と内訳（どのサービスで 1000 円に届いたか。`costAmount` はログに出ない。請求レポートの権限が要る）。**再発（2026-11-01、証拠等級 A。監査ログ）:** 再紐付け（00:10:07 UTC、`AssignResourceToBillingAccount`）の 20 分 52 秒後（00:30:59 UTC）に、`budget-guard` が再び `DisableResourceBilling` を実行した。原因（古い通知か、今月の費用か）は未確認 | 請求レポート（Console）と `budget-guard` のログ。手動の publish はしない（本番の停止になる） |
| 主モデルが落ちたときの、別モデルへの実際の切り替え（回答の品質、取り消した試行の課金も） | 主モデルの試行が 3 回とも失敗したとき（設計上、それまでは切り替えない） | Cloud Run のログの `app.agents.resilience` の WARNING と、どのモデルが答えたか |
| 履歴の削除ジョブで、実際に会話が消えること | **確認済み（2026-10-25）。** 初回の定時（2026-10-25 03:00 JST。実行 `docunavi-history-cleanup-zdmdz`）が成功し（1/1 完了）、ログに「削除した会話: 10 件」が出た（証拠等級 A。`run jobs executions list` とログの読み取り）。どの会話が消えたか、残るべき会話が残ったかは見ていない | `run jobs executions list`（Job `docunavi-history-cleanup`）とそのログ。毎週日曜 03:00 JST |

- 上の順序表の 2 番にある「未確認」のうち、有料キーへの切り替えと予算での停止、およびフォールバックの実際の切り替えは、ここで見る（履歴の削除は 2026-10-25 に確認済み）。

## 方針

- **1 段ずつ足し、毎回「動く状態」を保つ。** 各段階の終わりに `pytest`・`npm test`・`docker compose up` が通ることを確かめてから次へ進む。
- **先回りしてリファクタしない。** 各段階の直前に要る最小の変更だけを、その段階で行う（下の「先に要る変更」）。
- **「設定が空なら従来どおり動く」を保つ。** モックモードと同じ流儀で、`DATABASE_URL` が空なら今の `MemorySaver` のまま動かす。
- **構成は変えない。** monorepo のまま `app/`・`frontend/`・`storage/` で進める。足すモジュールは必要になった時に作る（空のディレクトリは作らない）。
- **DB は「普通の Postgres + pgvector」として使う。** `DATABASE_URL` だけに依存し、Supabase の SDK や独自 API を使わない（後から Cloud SQL などへ移せるように）。例外はキーワード検索の索引（段階 4c）で、検索は 1 か所に閉じ込める。
- **学習用の構成であって、必須の構成ではない。** 各段階で「なぜ入れたか」を一言残し、後で取捨選択できるようにする。

## 段階

| 順 | 追加するもの | 学べること | 前提・注意 |
|---|---|---|---|
| 0 | Cloud Run に 1 コンテナでデプロイ（[deploy.md](deploy.md)） | コンテナの配備、シークレット、ログ、予算アラート、権限の分離（閲覧用・デプロイ用の SA） | **実施済み（2026-10-20）。** 最初は認証必須で配備し、`--max-instances` と予算アラートを先に設定する方針だった |
| 1 | Postgres（第一候補は Supabase。Cloud SQL でも可）と、履歴の保存先の差し替え | 接続プール、マイグレーション、状態の外出し | 現在の `MemorySaver` は、Cloud Run では再起動や複数インスタンスで履歴が失われる。**段階 0 を待たずに進められる**（Supabase の無料枠は GCP の課金が不要。履歴が残る効果は、デプロイしたときに出る）。**コードは実装済み、Supabase への実接続も確認済み（2026-10-14。下の「確認の状況」）。** |
| 2 | 認証 | JWT の検証、会話と利用者の紐づけ | **R2 のアップロードより先。** 無認証だと誰でも書き込める。**コードは実装済み（Supabase Auth の Google ログイン + バックエンドの JWT 検証。`AUTH_MODE=off|required`、既定は off）。実際の Supabase・Google との接続とログインは未確認**（[configuration.md](configuration.md) の「ログイン（認証）」） |
| 3 | Cloudflare（DNS・独自ドメイン・レート制限） | DNS、プロキシ、WAF | 自分のドメインが要る。ドメインが無ければ後回しでよい。順序はどこでもよい。**未実施**（2026-10-22）。Cloudflare が前段で、Cloud Run はオリジンのまま（上の「現状と優先順位」の段階 3 の項） |
| 4a | R2 に原本を置き、手動で取り込む（抽出 → チャンク化 → 埋め込み → pgvector へ upsert） | S3 互換 API、取り込みの冪等性、pgvector | **ローカルの PDF からの取り込み・埋め込みと DB 検索は実装済み。R2 に置く部分が残り。** 認証が前提。アップロード資料を利用者ごとに分けるか、全員共通かを先に決める。`page`・`source` などは取り込み時にしか取れないので、4a で持たせる |
| 4b | 自動化（R2 のイベント通知 → Queue → Cloud Run の取り込み） | イベント駆動、キュー、リトライ | 取り込みはチャット用サービスと分ける。push と pull は、遅延と認証の手間で選ぶ（下の「段階 4 の設計」） |
| 4c | ハイブリッド検索（日本語の全文索引）、引用、署名付き URL | 検索の品質、参照元の返し方 | **ハイブリッド検索とリランカーは、2026-11-01 に利用者の指示で実装した**（取りこぼしを待たない。アプリ内 BM25。DB の全文索引は使わない。下の「検索改善の実装」）。引用・署名付き URL は未着手 |
| 5 | Vercel でフロントを分離 | CORS、クロスオリジンの認証、環境ごとの API の向き先 | いちばん手間がかかる。**Next.js 化は決定済み**（2026-10。[target-architecture.md](target-architecture.md)）。着手の順序は上の「実装順序」の① |

Supabase と Cloud SQL の選び方の目安:

| | Supabase | Cloud SQL (Postgres) |
|---|---|---|
| 向くとき | 認証も欲しい、小さく始めたい | GCP 内で完結させたい、IAM で統一したい |
| 注意 | 無料枠はアクセスが無いと一時停止される。リージョンを Cloud Run に合わせる | 常時起動の課金が出る |

料金と無料枠は変わるので、決める前に公式で確認する。

第一候補は Supabase です（学習目的に合い、認証と Storage が同居し、小さく始められる）。無料枠は一時停止するので、デモ以外で使い続けるなら有料プランか Cloud SQL にします。アプリは `DATABASE_URL` だけに依存させるので、後から移せます。最終的な決定は、段階 1 に入るときに行います。

## すでにある継ぎ目

何もしなくてよいもの。

- API の向き先: `frontend/src/api/chat.ts` の `NEXT_PUBLIC_API_BASE_URL`（空なら同一オリジン）
- CORS: `app/main.py` の `CORS_ORIGINS`（コードの既定は `*`。本番は 2026-10-25 時点で Vercel の URL だけに絞り済み）
- API は画面を配信しない（Vite の画面の配信 `app/frontend.py` は 2026-10-24 に外した）。画面は Vercel の `frontend`
- Cloud Run 向けの作り: `$PORT`、非 root、SIGTERM を直接受ける起動、Vertex AI の ADC

## 先に要る変更

各段階の直前に行う。今はやらない。

| 段階 | 変更 | 根拠 |
|---|---|---|
| 1 | **済み。** `build_graph` に `checkpointer` 引数を足した（既定は `MemorySaver` のまま）。DB 版の保存先は、これに渡す | 引数の追加なので、既存の呼び出しとテストは変わらない。`tests/test_checkpointer_injection.py` |
| 1 | **済み。** DB 版の checkpointer は接続プールを起動時に開いて終了時に閉じる（`app/history.py`、`app/main.py` の lifespan）。`get_graph` は開いている保存先を渡してグラフを作り、キャッシュのキーに保存先を含める（閉じた後のグラフを使い回さない） | `tests/test_history_store.py`・`tests/test_history_postgres.py` |
| 1 | **コードは済み。** 接続は、Supabase のプーラー（Shared pooler）の URL を使う。開発コンテナからは届くことを確かめた（直接接続は IPv6 のみで届かなかった）。prepared statement は使わない設定にしたので、transaction mode（6543）でも動く設計で、そのポートで起動・往復・再起動をまたいだ続きを確かめた（2026-10-14）。**残り:** `--max-instances` × `DATABASE_POOL_MAX_SIZE` が接続数の上限に収まるか、Cloud Run から届くか | 直接接続は既定で IPv6（IPv4 は有料のアドオン） |
| 1・4b | 秘密情報の置き場所を決める。Supabase の `DATABASE_URL` と Cloudflare の API トークンは Secret Manager に置く。ローカルは `.env`（開発用には、権限を絞った DB ロールを使う） | ベンダーが増えると秘密も増える。開発コンテナには読み取り中心の権限だけを置く（[.devcontainer/README.md](../.devcontainer/README.md)） |
| 2 | **コードは済み。** `AUTH_MODE=required` では、履歴のキーが `<利用者 ID>:<thread_id>` になり、他の利用者の会話に届かない。`off`（既定）のあいだは、`thread_id` の形式の検証だけで、UUID の推測困難さが守り | 会話の一覧・本人による削除は、API を実装済み（[api.md](api.md)）。画面（左サイドバー）は実装済み。API と画面の本物のログインでの動作は確認済み（2026-10-19、利用者の報告）。実 DB での速度と返答生成中の削除は個別には未確認 |
| 4a | コーパスの読み込み（`app/agents/tools.py` の `CHUNK_DIR` と無期限の `lru_cache`）を差し替え可能にし、内容が変わったらキャッシュを捨てる。`search_chunks` の戻り値 `(ファイル名, 本文)` はそのまま使える | アップロードで資料が増減する |
| 3 つ目の外部サービスを入れる前 | 環境変数の読み取りが `graph.py`・`routes.py`・`main.py` に散らばっている。設定を 1 か所（settings）に集約する | 変数群（DB・R2・認証）が増える |
| 5 | Dockerfile の frontend ビルド段が不要になる（ビルド引数か、Dockerfile の分割）。CI の Docker ビルドの確認も合わせる | フロントを別の場所から配信する |

## 段階 4 の設計（2026-10-14）

取り込みの構成案（R2 → Queue → Cloud Run → pgvector の流れ、データモデル、日本語の全文検索、R2 から Cloud Run への受け渡し、段階 1・4 の確認の状況）は、
[ingestion-stage4-design.md](ingestion-stage4-design.md) に移した（2026-11-01。内容は変えていない）。4b の設計の叩き台は [r2-ingest-design.md](r2-ingest-design.md)。

## 公開前の守り（段階 0 から）

`/chat` は無認証で LLM を呼び、遅いときは 1 リクエストあたり最大 `LLM_MAX_ATTEMPTS` 本の試行が飛びます。公開すると、他人にトークンを使われる経路になります。
コードではなく運用設定で、最初のデプロイから行います。

- `--max-instances` で全体の上限を決める。
- `--concurrency` は、`同時実行数 × LLM_MAX_ATTEMPTS` が SDK の接続プール（既定 100）に収まるようにする（[configuration.md](configuration.md) の「容量の注意」）。
- 予算アラートを設定する（Console の「お支払い → 予算とアラート」）。
- `CORS_ORIGINS` を `*` から実ドメインに絞る。
- 最初は `--no-allow-unauthenticated` で配備し、`gcloud run services proxy` で確かめる。

## 今はやらないこと

- `app/agents/graph.py`（LLM 設定・モック・グラフが同居）の分割。テストが `build_graph`・`create_llm` などをモジュールから直接 import しているため、移動はテストの変更を伴う。分割する理由（複数エージェント化など）が出るまで保留する。
- `app/api/routes.py` の LLM エラー分類の分離。同じ理由で保留する。
- settings の集約やディレクトリの新設の先回り。上の表のとおり、必要になった段階で行う。
- 日本語の全文索引（PGroonga / pg_bigm）とハイブリッド検索を、4c より前に入れること。資料が増えて取りこぼしが出るまで、検索の品質は足さない。
- 取り込みの自動化（4b）を、手動の取り込み（4a）より先に作ること。冪等性とデータモデルを、手動で固めてから自動化する。
