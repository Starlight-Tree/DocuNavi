# アーキテクチャ

リクエストの流れと、各モジュールの役割です。API の仕様は [api.md](api.md)、環境変数は [configuration.md](configuration.md) にあります。

## グラフの流れ

`app/agents/graph.py` の LangGraph は、次の順に進みます。

```
START → guard_input → agent → (tools → agent)* → guard_output → END
```

- `guard_input` が入力を拒否したときは、LLM もツールも呼ばずに END へ進みます。
- `agent` は Gemini（キーが無ければモック）を呼びます。ツール呼び出しがあれば `tools`（設定資料の検索）へ進み、結果を持って `agent` に戻ります。
- `guard_output` が最終回答を検査して終了します。

## 構成

```
app/
  main.py            FastAPI アプリ（CORS、例外ハンドラ）
  api/routes.py      /health, /chat、入力検証、LLM エラーの分類、切断時の取り消し
  frontend.py        frontend/dist の配信と SPA フォールバック
  agents/graph.py    LangGraph のグラフ、SYSTEM_PROMPT、モック LLM
  agents/guardrails.py  入出力ガードレール
  agents/resilience.py  LLM 呼び出しのヘッジ付き再試行
  agents/tools.py    設定資料の検索（search_zahard_lore）
frontend/            チャット画面（Vite + React + TypeScript）。dist/ がビルド成果物
storage/chunk/       設定資料（RAG の検索対象）
tests/               pytest（モックモード固定）
```

会話履歴は `thread_id` ごとにプロセス内メモリ（`MemorySaver`）へ保持するため、再起動で消え、複数ワーカー間では共有されません。

## フロントエンドの配信

`app/frontend.py` が `frontend/dist` を配信します。

- 配信の仕組み: どのルートにも当たらなかった `GET`/`HEAD` だけが画面側に回り、拡張子のないパスは `index.html`（SPA のルーティング）、
  無いファイル（`/assets` 配下や拡張子付き）は 404 になります。`/chat`・`/health`・`/docs` などの API は影響を受けません。

## ガードレール

ルールベース（LLM を使わない）の入出力チェックです。`app/agents/guardrails.py` にあります。

- **入力**: 指示の上書き、システムプロンプトの開示要求、役割の乗っ取り、チャットテンプレートの偽装を検出すると、LLM とツールを呼ばずに定型文で拒否します。拒否したターンの発言は履歴から伏せ字に置き換えます（NFKC 正規化と不可視文字の除去、空白・記号の挿入に対応）。切断などで検査の前に取り消されたターンの発言が未回答のまま残っていても、次のターンの入力検査で拒否対象なら伏せ字に置き換えます（拒否した発言の原文が、後続の LLM 入力に出ない）。
- **出力**: 回答が `SYSTEM_PROMPT` をほぼそのまま含んでいる場合は定型文に置き換えます。

これは典型的な手口を安く弾くためのもので、プロンプトインジェクションを完全に防ぐものではありません。
