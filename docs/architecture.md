# アーキテクチャ

リクエストの流れと、各モジュールの役割です。API の仕様は [api.md](api.md)、環境変数は [configuration.md](configuration.md) にあります。

## グラフの流れ

`app/agents/graph.py` の LangGraph は、次の順に進みます。

```
START → guard_input → agent → (tools → agent)* → guard_output → END
```

- `guard_input` が入力を拒否したときは、LLM もツールも呼ばずに END へ進みます。
- `agent` は Gemini（キーが無ければモック）を呼びます。ツール呼び出しがあれば `tools`（資料の検索）へ進み、結果を持って `agent` に戻ります。
- `guard_output` が最終回答を検査して終了します。

`POST /chat/stream` だけは、`agent` が LLM の応答をチャンクごとに受け（`stream_tokens` が真のとき）、逐次ガードを通した本文をカスタム
ストリームへ流します。`/chat`（`ainvoke`）の経路は変わりません。ヘッジは「最初のチャンクが届いた試行」を採ります（`hedged_stream`）。

## 構成

```
app/
  main.py            FastAPI アプリ（CORS、例外ハンドラ）
  api/routes.py      /health, /chat, /chat/stream、入力検証、LLM・履歴の保存先のエラーの分類、切断時の取り消し
  api/ui_stream.py   /chat/stream の応答（AI SDK の UI message stream）の組み立て
  history.py         会話履歴の保存先（DATABASE_URL があれば Postgres、無ければメモリ）。接続プールの開閉
  frontend.py        frontend/dist の配信と SPA フォールバック
  agents/graph.py    LangGraph のグラフ、SYSTEM_PROMPT、モック LLM
  agents/guardrails.py  入出力ガードレール（出力は、完成した回答の検査と、ストリーミング中の逐次検査）
  agents/resilience.py  LLM 呼び出しのヘッジ付き再試行（完了を待つ hedged_call と、最初のチャンクを待つ hedged_stream）
  agents/tools.py    資料の検索ツール（search_documents）。実体は search_vector.py（DB のベクトル検索。失敗しても別の経路に戻らない）
  search_vector.py   質問の埋め込みと pgvector のコサイン類似度検索、起動時の DB の検査（設計: docs/search-design.md）
  ingestion/         PDF の取り込み（python -m app.ingestion）と埋め込み（python -m app.ingestion.embed）。corpus.py は評価の BM25 基準線用
frontend/            チャット画面（Vite + React + TypeScript）。dist/ がビルド成果物
storage/pdf/<名前>/  PDF の資料（取り込みの元。検索は DB を読む。ディレクトリ名は整理用。いまは zahard・cafe・guides）
scripts/             評価の道具。bm25_baseline.py は、以前アプリが使った BM25（評価の基準線。アプリは使わない）
tests/               pytest（モックモード固定）
```

会話履歴は `thread_id` ごとに保持します。`DATABASE_URL` が空ならプロセス内メモリ（`MemorySaver`）で、再起動で消え、複数ワーカー間では
共有されません。設定すると Postgres に保存します（`app/history.py`。起動時にプールを開き、終了時に閉じる。
[configuration.md](configuration.md) の「会話履歴の保存先」）。

## フロントエンドの配信

`app/frontend.py` が `frontend/dist` を配信します。

- 配信の仕組み: どのルートにも当たらなかった `GET`/`HEAD` だけが画面側に回り、拡張子のないパスは `index.html`（SPA のルーティング）、
  無いファイル（`/assets` 配下や拡張子付き）は 404 になります。`/chat`・`/chat/stream`・`/health`・`/docs` などの API は影響を受けません。

## ガードレール

ルールベース（LLM を使わない）の入出力チェックです。`app/agents/guardrails.py` にあります。

- **入力**: 指示の上書き、システムプロンプトの開示要求、役割の乗っ取り、チャットテンプレートの偽装を検出すると、LLM とツールを呼ばずに定型文で拒否します。拒否したターンの発言は履歴から伏せ字に置き換えます（NFKC 正規化と不可視文字の除去、空白・記号の挿入に対応）。切断などで検査の前に取り消されたターンの発言が未回答のまま残っていても、次のターンの入力検査で拒否対象なら伏せ字に置き換えます（拒否した発言の原文が、後続の LLM 入力に出ない）。
- **出力**: 回答が `SYSTEM_PROMPT` をほぼそのまま含んでいる場合は定型文に置き換えます。
  ストリーミング（`/chat/stream`）では、完成した回答を待てないので、`StreamingOutputGuard` が本文を逐次検査します。漏洩の判定は
  「正規化後の連続一致が 24 文字以上」なので、末尾の正規化後 31 文字（24 + 余白）を送らずに残し、連続一致が閾値に達する前に止めます。
  止めたら LLM の受信をやめ、`guard_output` が全文を検査して定型文に置き換えます（履歴にも漏洩した本文は残りません）。
  直近の数十文字の窓で見るので、空白や記号を挟んで連続一致を引き延ばす回避は、ストリーミング中には拾えないことがあります
  （完成した回答は、`guard_output` が全文を検査します）。

これは典型的な手口を安く弾くためのもので、プロンプトインジェクションを完全に防ぐものではありません。
