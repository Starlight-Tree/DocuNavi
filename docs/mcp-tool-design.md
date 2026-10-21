# MCP ツール化: 設計の叩き台

状態（2026-10-21）: **`search_documents`（`ops/mcp_search/`）を `docunavi-mcp` として Cloud Run に配備し、本物の検索が通った。日常の接続は、Claude Code の `headersHelper`（`scripts/mcp-auth-header.sh`）でプロキシなしの直接接続にした（下の「headersHelper での直接接続」）。確認したのは接続と 1 回の検索まで。期限切れ後の自動復旧と、再ビルド後の自動接続は未検証。**
ロードマップの表の 8（将来の拡張）。目的は、資料の検索を、エージェント（まず Claude Code）から使えるツールにすること。
実用上の必要は小さい（資料は 6 件）。主な価値は、リモート MCP サーバーと、Cloud Run の IAM 認証を学ぶこと。

```text
Claude Code ──(Streamable HTTP + IAM 認証)──▶ Cloud Run の MCP サービス（新規）──▶ search_vector.search ──▶ Postgres（pgvector）
                                                                                    └─▶ 質問の埋め込み（Gemini API、無料キー）
```

## 決めたこと（2026-10-21、仮承認。後で変えてよい）

| 項目 | 決定 | 理由 |
|---|---|---|
| サービス構成 | **別サービス**（既存の `docunavi` には足さない） | `docunavi` は公開で Supabase の JWT 認証、MCP は IAM 認証で、方式が合わない。接続数・最大インスタンス・秘密情報も別に絞れる |
| 最初のツール | **資料の検索**（`search_documents` 相当、読み取り専用）。「資料への質問」は作らない | 「質問」は中で Gemini が回答を作り、呼び出し側の LLM と二重になる。ガードレールと出典の扱いも重くなる。検索なら質問の埋め込み 1 回で済む |
| 利用者 | **Claude Code だけ** | まず自分で使う。ほかのエージェントは後 |
| 依存の置き場 | **専用のイメージ**（`ops/mcp_search/`）。アプリの `requirements*.in` に `mcp` は足さない | 本番のアプリの依存を動かさず、いつでも消せる。アプリのコード（`app/`）は同じものをイメージに入れ、`app.search_vector.search` をそのまま呼ぶ |
| 検索結果の `similarity` | **返す**（見出しに `類似度 0.82`） | 呼び出し側の LLM が関連の強さを判断できる。『資料にない』の判断には使えない粗い目安と、ツールの説明に書く。要らなければ消すだけ |

## 公式で確かめたこと（Google Cloud、2026-10-21）

出典: [Host MCP servers on Cloud Run](https://docs.cloud.google.com/run/docs/host-mcp-servers)、[Build and deploy a remote MCP server on Cloud Run](https://docs.cloud.google.com/run/docs/tutorials/deploy-remote-mcp-server)。

| 項目 | 内容 |
|---|---|
| トランスポート | Streamable HTTP と SSE。stdio は非対応 |
| 載せられる先 | Cloud Run の**サービス**のみ（Job は不可） |
| デプロイ | コンテナイメージ（`gcloud run deploy --image … --port …`）かソース（`--source .`）。基本の手順に `--functional-type` は無い |
| `--functional-type` | チュートリアルは `--functional-type=mcp-server`、案内ページ（IAP の手順）は `--functional-type=MCP_SERVER`。表記が違う。付けるとサービスが Agent Registry にツールとして自動登録される（チュートリアル）。ほかの値と効果は書かれていない。GA の `gcloud run deploy` にあるかは未確認（`beta` の一覧にはある）。**必須ではなさそう** |
| 認証 | `--no-allow-unauthenticated` で非公開にし、呼び出し側に `roles/run.invoker` を与える |
| 接続の方法 | ① `gcloud run services proxy`（手元に認証付きの中継を立てる） ② OIDC の ID トークンを `Authorization: Bearer` で送る（audience は `*.run.app` の URL） ③ IAP |

## 確認できていないこと（着手の前に、小さく試す）

- **Claude Code から非公開サービスへつなぐ方法。** 公式には、Claude Code への言及がない。①〜③のどれが通るかは、実際に試すまで分からない。
  - ① プロキシ: コンテナーの gcloud は閲覧専用の ID（`docunavi-viewer`）に固定されていて、`run.invoker` は持たない。`gcloud run services proxy` がガードのフックに止められる可能性もある。ホスト側で立てた中継に、コンテナーから届くかも未確認。
  - ② ID トークン: 有効期限は約 1 時間（公式のこの 2 ページには書かれていない。一般的な挙動）。Claude Code の MCP 設定にヘッダーを入れる方法で渡すなら、更新の仕組みが要る。トークンの値をファイルや設定に保存するのは、CLAUDE.md の方針（トークンの中身は表示・保存しない）と衝突しうる。
  - ③ IAP: 設定が重い。
- **MCP サーバーの実装方法。** Python の公式 SDK（`mcp` パッケージ）を使う想定だが、バージョン・依存・既存の `requirements*.in` との関係は未調査。
- **`--functional-type` を付けるべきか。** 付けたときの違い（Agent Registry の費用・権限を含む）は未確認。

## 検証の結果（2026-10-21、`ops/mcp_probe` を `docunavi-mcp-probe` として配備）

- **① プロキシ（`gcloud run services proxy … --port 3000`）で、Claude Code から `ping` が通った。** 登録は local スコープの `claude mcp add --transport http mcp-probe http://127.0.0.1:3000/mcp`（ヘッダーなし、秘密情報なし）。`/mcp` が Connected、`ping` が `pong`。
  - 呼び出し側の権限は、ユーザー自身の `roles/run.invoker`（プロキシがユーザーの ID で認証する）。コンテナーの日常の ID（`docunavi-viewer`）には要らない。
  - プロキシは、gcloud の部品が入っていないと動かない。この gcloud は部品の管理が無効（apt 版）なので `sudo apt-get install google-cloud-cli-cloud-run-proxy` が要る。再ビルドで消える（`devcontainer.json` には足していない）。
  - 起動は `scripts/mcp-proxy.sh`（ユーザー自身の権限。定義の 3 行は要らない。`docunavi-mcp` 用に 3001 番）。再ビルド後は `gcloud auth login` → 上の apt → このスクリプト → `/mcp` で Reconnect。
- **制約: プロキシは起動時の ID トークンを固定で使う。期限が切れると `401` になる。** 起動から約 45 分で `401` になったことを確認した（期限の長さの確定ではない。公式の 2 ページに期限の記述は無い）。プロキシを起動し直せば直る。
- **Claude Code は、`401` を受けると OAuth の探索（`/.well-known/…`、`POST /register`）に進み、`/mcp` の画面に「SDK auth failed: Dynamic Client Registration rejected」と出る。** サーバーに OAuth が無いこと自体は原因ではない。画面のエラーが、本当の原因（トークン切れ）を隠す。切れたら Cloud Run のログ（`gcloud logging read`）で `401` を確かめる。
- **② ID トークンを直接送る方法は、`headersHelper` で通った**（下の「headersHelper での直接接続」。値を設定に保存せず、接続のたびにヘルパーが取る）。**未確認:** ③ IAP。固定のトークンを `--header` で渡す方法は、プロキシの Google ID トークンと衝突するので試していない。
- プロキシ（`scripts/mcp-proxy.sh`）は、今は予備。再ビルドで消える apt の部品と、約 45 分ごとの再起動が要る。
- **未確認:** 日常で使えるかの判断。1 時間ごとの再起動が許容できるか。

## ツールの形（案）

名前は `search_documents`。引数は `query`（文字列、1〜500 文字）と `top_k`（整数、1〜5、既定は 2 = アプリの `TOP_K`）。範囲外は検索まで届かず、入力の検証エラーになる。読み取り専用（`readOnlyHint`）であることを、ツールの注釈に付ける。

- 実体は `app.search_vector.search(query, top_k)`。しきい値（`MIN_SIMILARITY` 0.65）もそのまま使う。
- 返すもの: 各ヒットの、ファイル名・ページ・類似度を見出しにして、見出し付きの本文（`[a.pdf p.3-4 類似度 0.82]` の次の行から本文）。アプリが LLM に渡す文面とは違い、出典を示せるようにしている。
- **出力ガードは通らない。** アプリのガードレール（`StreamingOutputGuard` など）は LLM の回答を検査するもので、生のチャンクを返す MCP には掛からない。返すのは運営者が置いた共通の資料だけなので、漏れて困る内容は含まれない（システムプロンプトの漏洩は別の話）。
- **返した本文は、呼び出し側から見て「信頼できないデータ」。** 資料に書かれた指示にエージェントが従わないよう、ツールの説明に書く。
- 「全文を返す」ツール（`fetch` 相当）と資料一覧は、最初は作らない。必要になってから足す。

## 費用と上限

- 質問の埋め込みは 1 回あたり Gemini の API を 1 回呼ぶ（無料キー。尽きたときの有料キー切り替えは、アプリと同じ仕組みを使うなら、MCP 側にも有料キーを渡すかの判断が要る。**未決**）。
- 認証を IAM にしても、呼べる人が増えれば呼び出しが増える。`--max-instances` を小さく置き、予算ガード（月 1000 円）の対象に入るかを確かめる（対象は `docunavi-app` のプロジェクト全体の見込み。未確認）。
- DB の接続数: 新しいサービスの `--max-instances` × `DATABASE_POOL_MAX_SIZE` を、既存の `docunavi`（2 × 5）と取り込みの Job に足して、Supabase のプーラーの上限に収める（[deploy.md](deploy.md) の接続数の節）。MCP 側は検索だけなので、`DATABASE_POOL_MAX_SIZE` は 1〜2 で足りる見込み（未測定）。

## 作るもの（着手するとき）

1. `app/` とは別の入口（例: `mcp_server/` または `ops/mcp_search/`）。`app.search_vector` を import するか、薄く複製するかを決める。履歴・認証・LLM は読み込まない。
2. 専用のコンテナーイメージ（`docunavi-ingest` と同様に別イメージ）と、Cloud Run サービス `docunavi-mcp`（`--no-allow-unauthenticated`、`--max-instances 1〜2`）。専用のサービスアカウントには、`DATABASE_URL` と `GEMINI_API_KEY` の Secret の `secretAccessor` だけ。
3. 呼び出し側の ID への `roles/run.invoker`。誰に与えるか（利用者自身か、専用のサービスアカウントか）は未決。
4. Claude Code への登録（`claude mcp add`、スコープは local）。登録と権限ルールの変更は、ユーザーの承認が要る。

## 先にやる小さな検証（コードを書く前）

1. 空の最小 MCP サーバー（ツール 1 つ、固定の返答）を Cloud Run に置き、①〜③のうち、Claude Code から実際に呼べる方法を 1 つ見つける。**GCP への書き込みなので、コマンドを提示してユーザーが実行する**（課金は最小）。
2. 通ったら、`search_documents` を本物の検索につなぐ。
3. 通らなければ、Cloud Run をやめて、手元（開発コンテナー内）の stdio の MCP サーバーにする案を検討する（学習の目的の半分は外れる）。

## 決めていないこと

- 着手するか。着手の時期。
- 有料キーを MCP 側にも渡すか。
- 呼び出し側の ID（利用者本人か、専用の SA か）と、ID トークンの渡し方。

## 実装（2026-10-21）

- `ops/mcp_search/main.py`: FastMCP（`mcp==1.30.0`、Streamable HTTP、ステートレス）。ツールは `search_documents` の 1 つで、`readOnlyHint` 付き。
  - DB の接続プールは最初の呼び出しで開いて使い回す（FastMCP の `lifespan` はステートレスだと呼び出しごとに走るため）。プールは `app.history` の置き場所に入れる（`search_vector.search` がそこから借りる）。履歴のテーブルは作らない。
  - 失敗は固定文（`資料の検索に失敗しました。`）だけを返す。例外の文字列と質問の本文は、応答にもログにも載せない（型の名前と件数だけ）。
- `ops/mcp_search/Dockerfile`: ビルドの文脈はリポジトリの根。アプリの `requirements.txt` と `ops/mcp_search/requirements.txt`（`mcp` だけ）を 1 回の `pip install` に渡す。`ops/mcp_search/cloudbuild.yaml` が `--file` でこれを指す。
- テスト: `tests/test_mcp_search.py`（検索と DB は偽物。`mcp` が無い環境では、ツール定義とクライアント呼び出しの 4 件をスキップする。`mcp` はアプリの開発依存に足していないため、CI ではこの 4 件が走らない）。
- **確かめたこと:** `mcp` を入れた環境で全体のテストが通る（13 件を含む）。ローカルで HTTP サーバーとして起動し、`initialize`・`tools/list`・`tools/call` が通ること、DB とキーが無いときに固定文のエラーになることを確かめた。
- **確かめていないこと:** 本物の DB・Gemini での検索、Docker のビルド（開発コンテナーに Docker が無い。Cloud Build で初めて走る）、Cloud Run での動作、Claude Code からのツール呼び出し。
- **配備の前に決めること:** ① `DATABASE_URL` に、アプリと同じ書き込み権限の Secret（`database-url`）を渡すか、読み取り専用ロール（`readonly`）の URL を別の Secret にするか（このサービスは読むだけなので、後者が最小権限）。② 有料キーを渡すか。③ `run.invoker` を誰に与えるか。④ `DATABASE_POOL_MAX_SIZE` は 2 程度に絞る（接続数の節）。

## 配備と確認（2026-10-21）

- **配備:** サービス `docunavi-mcp`（`asia-northeast1`、非公開、最大 1 インスタンス、並行 10、`DATABASE_POOL_MAX_SIZE=2`）、専用のサービスアカウント `docunavi-mcp`。Secret は `database-url-readonly`（読み取り専用ロールの URL。新規）と `gemini-api-key`（無料キー。アプリと共用）の `secretAccessor` だけ。有料キー（`gemini-api-key-paid`）は渡していない。`run.invoker` は利用者本人だけ。
- **Docker のビルド:** Cloud Build で `SUCCESS`（1 分 16 秒）。アプリの固定済みの依存と `mcp==1.30.0` は、競合なく入った。
- **実際の検索（1 回、無料キー）:** プロキシ（`gcloud run services proxy docunavi-mcp --port 3001`）経由で `search_documents` を呼び、質問「糸くずフィルターの掃除」に対して、洗濯機の説明書の該当箇所が 1 位（類似度 0.80）で返った。Cloud Run のログでは、Gemini の埋め込みの呼び出しは 1 回（200）、質問の本文はログに出ていない。
- **読み取り専用ロールでの検索:** 同じ検索の SQL が `readonly` ロールで動くこと、書き込みは `read-only transaction` で拒否されること、会話履歴の表は読めないことを、配備の前に確かめた。
- **確認済み（2026-10-21、Claude Code の会話の中から）:** `search_documents` を 1 回呼び、期待どおりの結果が返った。質問「糸くずフィルターの掃除」、1 位は `aoba_aw820_manual.pdf` p.2（類似度 0.80）、2 位は同 p.2（0.68、乾燥フィルターの記述で関連は薄い）。無料キーで Gemini の埋め込み 1 回、書き込みなし。
- **未確認:** 本物の 429 での有料キーへの切り替え（有料キーを渡していないので起きない）、予算ガードの対象に入るか、`headersHelper` での期限切れ後の復旧、再ビルド後に `docunavi-search` が手作業なしでつながるか（次の節）。
- **保留:** 有料キーを渡すか、apt の部品を再ビルド時に自動で入れるか、`pgpass` の 2 行目（ID もパスワードも空）の扱い。

## headersHelper での直接接続（2026-10-21）

登録は local スコープの `{"type":"http","url":"<Cloud Run の URL>/mcp","headersHelper":"/workspaces/docunavi/scripts/mcp-auth-header.sh"}`（`claude mcp add-json`。URL は秘密ではないが、文書には書かない）。
ヘルパーは `gcloud auth print-identity-token` の結果を `{"Authorization":"Bearer …"}` で標準出力に返すだけ。公式の文書（code.claude.com/docs/en/mcp）では、接続・再接続のたびに実行され（10 秒で打ち切り）、キャッシュされない。401/403 では再実行して 1 回やり直す。

### 確認済み

- 直接接続で `/mcp` が Connected になり、`search_documents` を 1 回（無料キー、書き込みなし）呼んだ。結果はプロキシ経由と同じ（`aoba_aw820_manual.pdf` p.2 が 0.80、同 p.2 が 0.68）。ヘルパーは 10 秒以内に終わった。
- ヘルパーが使う ID は、ログイン済みのユーザー本人（`gcloud auth list` で 1 アカウントだけ）。日常の閲覧専用のなりすましは、gcloud の設定ファイルには無く、環境変数 `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` だけ（`gcloud config configurations describe default` で確認）。ヘルパーは `env -u` でこの変数を外す。`run.invoker` を持つのは本人だけ。
- Cloud Run のリクエストログ（`--billing-project=docunavi-app` が要る）で、ヘッダー付きの 200/202 と、ヘッダー無しの 403 を見分けられる。

### つながらなかったときの経緯と見方

1. **信頼（trust）が保存されていないと、ヘルパーは実行されない。** プロジェクト／ローカルのヘルパーは、`~/.claude/.claude.json` の `projects["<パス>"].hasTrustDialogAccepted` が `true` のときだけ動く。拡張機能のセッションでは信頼のダイアログが出ず、`false` のままだった。ヘッダーが付かず Cloud Run の IAM が 403 を返し、Claude Code は OAuth の探索に進んで「SDK auth failed: Dynamic Client Registration rejected (HTTP 403)」と出す（本当の原因を隠す）。
2. **信頼を直しても、「認証が必要」の記録が残ると接続を試さない。** `~/.claude/mcp-needs-auth-cache.json` にそのサーバー名の行があると、ログに `Skipping connection (cached needs-auth)` と出て接続しない。その行を消し、ウィンドウを再読み込みする（拡張機能のセッションでは `/mcp reconnect` が使えない）。
3. **画面の Authenticate は押さない。** ヘッダー無しの OAuth の探索を始め、403 になる。
4. 見る場所: 拡張機能のログの `headersHelper` の行、上の 2 つの設定ファイル、Cloud Run のリクエストログ。

### 安全上の注意

- ヘルパーは Claude Code が実行するので、ガードのフック（`guard-cloud-cli.py`）も、確認の表示も通らず、ユーザー自身の権限で動く。スクリプトが書き換えられると、その権限で何でも動く。そのため、CLAUDE.md で「エージェントは実行しない・変更は事前に承認」とし、スクリプトは ID トークンを返す以外をしない。
- トークンはファイルにもログにも書かない。端末で手で実行すると画面に出るので実行しない。

### 未検証（恒久化が済んだとは言えない）

- **期限切れ後の自動復旧。** ID トークンは約 1 時間（プロキシでは約 45 分で 401 を観測）。公式の文書どおり 401/403 でヘルパーが再実行されるかは、待って確かめていない。
- **再ビルド後の自動接続。** 信頼のフラグと「認証が必要」の記録は `~/.claude` のボリュームにあるので残る見込みだが、再ビルドして確かめていない。gcloud のログインは、今回の再ビルドでは残っていた（理由は不明）。`devcontainer.json` は「gcloud のログインは残さない」方針で、残らない場合は `gcloud auth login` が要り、ヘルパーは失敗する（標準エラーに理由を出す）。
- 登録名 `docunavi-search` への置き換えは確認済み（ウィンドウの再読み込みのあと、同じ質問で `search_documents` を 1 回呼び、結果が同じだった）。試験用の `docunavi-search2` は、その後に削除した。
