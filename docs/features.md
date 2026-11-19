# 機能一覧

利用者の操作ごとに、できること・条件・制限を並べた一覧です（リファレンス）。仕組みは [architecture.md](architecture.md)、API は [api.md](api.md)、設定は [configuration.md](configuration.md)。

- **確認の範囲（2026-11-15。2026-11-20 に、コードと、本番の Cloud Run・Job の設定の読み取りで見直した）:** コードと既存の文書から書いた。画面は実機では見ていない。本番での確認状況は各文書の「未確認」と [roadmap.md](roadmap.md) が正本。
- **印:** 条件の頭に **設定**（コードの既定と本番の設定が違うことがある）か **実験**（範囲や精度に限りがある）が付く行以外は、`main` のコードでは使える。本番に出ているかは、この印では決まらない（画面は、下の「画面の配備」にある変更が本番にまだ出ていないとみられる。API は、配信中の Cloud Run のリビジョンが `main` のどのコミットかを、この文書では確かめていない）。
- **画面の配備（2026-11-20）:** 最後の `deploy.yml` の実行は 2026-11-18（対象 `a3474e8`。`gh run list`、A）。それより後の `frontend/` の変更（PDF.js のビューア、パネルのビュー切り替えボタン、版の表示、列見出しの並べ替えなど）は、本番にはまだ出ていないとみられる（別の経路での配備は未確認）。画面の記述は、`main` のコードに合わせてある。

## チャット

<table width="100%">
<tr>
<th width="15%">機能</th>
<th width="33%">できること</th>
<th width="37%">条件・制限</th>
<th width="15%">詳細</th>
</tr>
<tr><td>質問と回答</td><td>資料の内容を質問し、回答を逐次表示で読む。会話を続けられる</td><td>1〜2000 文字。資料に無いことは LLM が「無い」と答える</td><td><a href="search-design.md">検索設計</a></td></tr>
<tr><td>出典</td><td>回答の下の「N 件のソース」から、出典の資料とページを右パネルで開く（本文中の <code>[n]</code> は表示しない。2026-11-16 から）</td><td>出典は回答全体の根拠の一覧で、文ごとの対応は示さない。<b>文を出典が支持するかは確かめない</b></td><td><a href="api.md">API</a></td></tr>
<tr><td>回答対象</td><td>入力欄のチップで、質問の範囲（対象）を選ぶ</td><td>会話ごとに固定。権限の範囲は超えられない</td><td><a href="configuration.md">設定</a></td></tr>
<tr><td>資料の選択</td><td>資料一覧でチェックした資料だけで答えさせる</td><td>ターンごとの指定。0 件では送信できない</td><td><a href="api.md">API</a></td></tr>
<tr><td>追質問</td><td>「それ」「もっと」など短い質問を、前の話題で補って検索する</td><td>補完は LLM の短い 1 回。精度は実 API で未確認</td><td><a href="configuration.md">設定</a></td></tr>
<tr><td>質問の例</td><td>空の会話に例を、回答のあとに続きの質問を出す</td><td>資料ごとに、取り込み後に LLM で作って保存した質問（`documents.suggested_questions`、`python -m app.ingestion.suggest`）。作っていない資料・保存前の古い会話には出ない。PDF が入れ替わって再生成を待つ資料の質問も出ない（2026-11-17 に、ファイル名の固定表から変更。取り込みの後の自動生成は、本番の取り込み Job 2 本で有効〔<code>SUGGEST_ENABLED=true</code>。2026-11-20 に gcloud の読み取りで確認、A〕。Job 経由の生成の実行は未確認）</td><td><a href="../frontend/src/lib/followUps.ts">コード</a></td></tr>
<tr><td>停止・再試行</td><td>生成の停止、回答のコピー、失敗後の再試行</td><td>送り直しても通らない失敗（422・409・403）に再試行は出ない</td><td><a href="api.md">API</a></td></tr>
<tr><td>会話の一覧</td><td>過去の会話を選んで続きから読む・削除する</td><td>ログイン時のみ。最新 50 件、本人の分だけ</td><td><a href="api.md">API</a></td></tr>
<tr><td>再開の出典</td><td>再開した回答にも出典が付く</td><td>検索しなかった回答と、いま見えない資料に基づく回答には付かない</td><td><a href="api.md">API</a></td></tr>
</table>

## 資料

<table width="100%">
<tr>
<th width="15%">機能</th>
<th width="33%">できること</th>
<th width="37%">条件・制限</th>
<th width="15%">詳細</th>
</tr>
<tr><td>資料一覧</td><td>検索・グループ・対象・種別で絞って見る。状態を確認する</td><td>読み取り専用。分類の設定は運用者の SQL</td><td><a href="api.md">API</a></td></tr>
<tr><td>閲覧</td><td>既定は抽出テキスト（取り込んだ内容。取り込みの結果を確かめる）で、PDF・関係図は、ヘッダー右上のアイコンのボタン（「PDFを開く」「関係図を見る」。タブは無い）で移る。チャットの出典から開くと、PDF で開く（原本で根拠を確かめる）。パネルの最上段は、題名（左。2 行まで。その上に、あれば版を小さく）とアイコン（右）だけ。「資料」の見出し・資料グループ名・取り込みの日付・ページ数は出さない（日付は取り込みの記録で利用者の判断に使えない。ページ数は PDF のビューアが示す）。版（「初版」「改訂 2」など）は、取り込みの版の一覧 `revisions.csv` の値で、無い資料には何も出さない。PDF の保存は、PDF ビューアのツールバーの保存（ダウンロードのアイコン）で行う（ヘッダーの独自のダウンロードアイコンは 2026-11-18 に外した。ビューアは 2026-11-19 から PDF.js〔ADR 0014〕。2026-11-20 から、全ページを縦に並べて連続でスクロールし（ツールバーは下部に浮かぶ）、ページの移動（番号の入力）・拡大縮小・保存だけを持ち、検索・印刷は無い。出典から開くと、そのページへ移り、根拠のチャンクを PDF の文字の上で黄色に強調する〔1 か所に決まるときだけ〕）。ファイルパスは出さない（題名に重ねると、題名の全文とファイルパスを表示）。ページ数は、PDF があれば出さない（ビューアが示す）</td><td>PDF は 20 MiB まで。保存名は資料名（題名）になる（同じオリジンの中継 `/api/documents/{id}/file` が `Content-Disposition` の `filename*` を付けるため。ADR 0013。認証なしの構成は API を直接指す。ローカルでは PDF の表示を確認済み〔PDF.js への置き換えは 2026-11-19 にローカルの Chromium で確認〕。本番での Cookie 経由の表示・保存名・`/pdfjs/` の配信は未確認）。保存できるのは、閲覧できる資料だけ。表示の確認は PC のブラウザーだけ。保存名の中継（ADR 0013）は 2026-11-18 の <code>deploy.yml</code> の実行で本番へ出た。PDF.js のビューア・ヘッダーのボタン・版の表示は、それより後の変更で、本番にはまだ出ていないとみられる（未確認。上の「画面の配備」）</td><td><a href="api.md">API</a></td></tr>
<tr><td>関係図</td><td>入寮規程の条文の参照関係と、変更の影響範囲を見る</td><td><b>実験</b>。<b>入寮規程 1 資料だけ</b>。定義語は辞書の 4 語だけ</td><td><a href="api.md">API</a></td></tr>
</table>

## 検索

<table width="100%">
<tr>
<th width="15%">機能</th>
<th width="33%">できること</th>
<th width="37%">条件・制限</th>
<th width="15%">詳細</th>
</tr>
<tr><td>ベクトル検索</td><td>質問の埋め込みと pgvector で近いチャンクを探す</td><td><code>ready</code> の PDF が対象。LLM に渡すのは上位 2 件が基本</td><td><a href="search-design.md">検索設計</a></td></tr>
<tr><td>検索の重ね</td><td>BM25 との統合、語の一致での並べ直し、「第N条」の参照先の追加</td><td><b>設定</b>。コードの既定は 3 つとも on。本番の環境変数に <code>SEARCH_HYBRID</code> などは無く、コードの既定が効くとみられる（2026-11-20、gcloud の読み取り、A）</td><td><a href="decisions/README.md">決定記録</a></td></tr>
</table>

## 安全対策・アクセス制御

<table width="100%">
<tr>
<th width="15%">機能</th>
<th width="33%">できること</th>
<th width="37%">条件・制限</th>
<th width="15%">詳細</th>
</tr>
<tr><td>ガードレール</td><td>指示の上書きなどを拒否し、回答の漏洩を逐次検査する</td><td>ルールベース。インジェクションを完全には防がない</td><td><a href="guardrails.md">ガード</a></td></tr>
<tr><td>ロール制御</td><td>ロールごとに、見える資料グループを制限する</td><td>本番は <code>enforce</code>。限定ロールでの動作は未確認。変更の反映に最大 60 秒</td><td><a href="configuration.md">設定</a></td></tr>
<tr><td>ログイン</td><td>Google ログイン。サーバーが JWT を検証する</td><td>本番は必須。期限切れ後の更新は未確認</td><td><a href="configuration.md">設定</a></td></tr>
<tr><td>利用資格</td><td>ログインした利用者のうち、許可された人（<code>app_members</code>）だけに使わせる</td><td>本番は <code>enforce</code>（2026-11-20 に環境変数で確認、A）。許可外は 403 で、画面は「利用許可がありません」を出す。申請・承認の仕組みは無く、運用者が追加する（反映に最大 60 秒）</td><td><a href="configuration.md">設定</a></td></tr>
</table>

## LLM の信頼性

<table width="100%">
<tr>
<th width="15%">機能</th>
<th width="33%">できること</th>
<th width="37%">条件・制限</th>
<th width="15%">詳細</th>
</tr>
<tr><td>ヘッジ</td><td>遅い呼び出しに追加の試行を投げ、最初の成功を採る</td><td>トークンが増える</td><td><a href="configuration.md">設定</a></td></tr>
<tr><td>予備モデル</td><td>主モデルの一時的な失敗で、別モデルを 1 回試す</td><td><b>設定</b>。本番は有効。実際に切り替わった回は未確認</td><td><a href="configuration.md">設定</a></td></tr>
<tr><td>有料キー</td><td>無料キーが 429 のとき、有料キーで試す</td><td><b>設定</b>。本物の 429 では未確認</td><td><a href="configuration.md">設定</a></td></tr>
<tr><td>エラー文</td><td>失敗を、例外の内容を含まない固定文で返す</td><td>切断は失敗にせず、呼び出しを取り消す</td><td><a href="api.md">API</a></td></tr>
</table>

## 画面

<table width="100%">
<tr>
<th width="15%">機能</th>
<th width="33%">できること</th>
<th width="37%">条件・制限</th>
<th width="15%">詳細</th>
</tr>
<tr><td>サイドバー</td><td>開閉と縮小表示。チャット・新しい会話・ナレッジへ移動</td><td>基準は PC（幅 1280px 前後）</td><td>—</td></tr>
<tr><td>テーマ</td><td>ライト・ダークの切り替え</td><td>—</td><td>—</td></tr>
<tr><td>モード表示</td><td><code>mock</code>（固定の返答）のときだけ、サイドバーにバッジを出す（<code>gemini</code> のときは何も出さない）</td><td>モックは検索しない。<code>gemini</code> で出さなくなったのは 2026-11-19 の変更（<code>f2e3c25</code>）で、最後の配備の対象 <code>a3474e8</code> には含まれず、本番ではまだ出るとみられる（上の「画面の配備」。未確認）</td><td>—</td></tr>
</table>

## 運用（利用者の画面には出ない）

<table width="100%">
<tr>
<th width="15%">機能</th>
<th width="33%">できること</th>
<th width="37%">条件・制限</th>
<th width="15%">詳細</th>
</tr>
<tr><td>取り込み</td><td>R2 の PDF をチャンク化・埋め込みして DB へ</td><td>同期は週 1 回（2026-11-10〜11 に毎時から変更、<a href="decisions/0012-accept-prisma-free-limit-and-cut-operations.md">ADR 0012</a>。Job の最終実行が 2026-11-14 のままで、毎時ではないとみられる）。R2 通知の Job を起こす Scheduler は一時停止の記録（現状は未確認）</td><td><a href="roadmap.md">計画</a></td></tr>
<tr><td>履歴の掃除</td><td>古い会話履歴の削除 Job</td><td>2026-11-15 03:00 JST の定期実行は、対象 0 件で終了（<a href="deploy.md">deploy.md</a> の D、2026-11-20、gcloud の読み取り、A）。削除が起きうる最初は 2026-11-22。対象があったときの結果は未確認</td><td><a href="roadmap.md">計画</a></td></tr>
<tr><td>バックアップ</td><td><code>pg_dump</code> を R2 へ週 1 回</td><td>実データでの復元は未確認</td><td><a href="pg-backup.md">バックアップ</a></td></tr>
<tr><td>MCP 検索</td><td>Claude Code から資料を検索</td><td>運用者専用（全グループ）</td><td><a href="mcp-tool-design.md">MCP 設計</a></td></tr>
</table>

## 提供していないこと

- PDF 以外の取り込み、画面からの資料の追加・編集・削除
- ロールの管理画面（運用者の SQL）
- 外部の道具で作った 2 段組み PDF での読み順の確認（組み直しは組み込み済み。確かめたのは reportlab 製の 1 冊と、`storage/pdf/` の 433 ページ〔2 段と判定したページは 0 件〕だけ）

機能を足す・変えるときは、対応する行も更新する。
