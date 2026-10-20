# MCP ツール化: 設計の叩き台

状態（2026-10-21）: **叩き台。コードも、Cloud Run・GCP 側の作成も、まだ無い。** 着手するかどうか自体は、まだ決めていない。
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

## ツールの形（案）

名前は `search_documents`。引数は `query`（文字列）と `top_k`（整数、1〜5 程度、既定は `TOP_K` に合わせる）。読み取り専用（`readOnlyHint`）であることを、ツールの注釈に付ける。

- 実体は `app.search_vector.search(query, top_k)`。しきい値（`MIN_SIMILARITY` 0.65）もそのまま使う。
- 返すもの: 各ヒットの、ファイル名・見出し付きの本文・ページ（`page`、`page_end`）。`similarity` を返すかは未定（アプリは LLM に渡していない）。
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
- 検索結果に `similarity` を含めるか。
