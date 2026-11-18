---
paths:
  - "app/**"
---

# app/（バックエンド）の LLM・API・ガードを触るとき

領域の規約は [app/CLAUDE.md](../../app/CLAUDE.md)、検索は [search.md](search.md)。2026-11-18 に、ルートの CLAUDE.md の「守ること」と「構成」から、次の項目を移した（本文は変えていない。禁止の要点はルートの CLAUDE.md にも 1 行ずつ残してある）。

## LLM の呼び出し

- **LLM の呼び出しは不安定になりうる。** 遅い呼び出しは待たずに追加の試行を投げ（ヘッジ）、最初の成功を採る。追加の試行はトークンを増やし、
  モデルの変更も料金と品質に響くので、変える前に確認する。`/chat/stream` は「最初のチャンクが届いた試行」を採り、採った後は別の試行へ切り替えない
  （送った本文と食い違うため）。数値と環境変数は `docs/configuration.md` の「LLM の遅延・失敗への対処」。
- **別モデルへのフォールバックは任意で、既定は無効**（`GEMINI_FALLBACK_MODEL`。**本番では 2026-10-21 から `gemini-3.5-flash-lite` で有効**。コードの既定は無効のまま）。429 と 400/401/403/404 では試さない。主モデルを飛ばす記憶は、
  同じ質問の中（`AgentState.fallback_used`。`guard_input` が毎ターン戻す）と、プロセス内のブレーカー（**主モデルが実際にエラーを返したときだけ**開き、遅いだけでは開かない）。
  グラフは `lru_cache` で使い回されるので、リクエスト単位の状態は、グラフの状態か呼び出しごとのオブジェクトに置く。
  **未確認:** 実際に切り替わった回、取り消した試行の課金、本物の検索結果・長い履歴・HTTP 経由での引き継ぎ、切り替えた回の回答の品質
  （履歴の引き継ぎは偽のツール・短い履歴の実 API で確認）。詳細は `docs/configuration.md` の「主モデルが落ちたとき（フォールバック）」の「検証の状況」。
- **有料キーへの切り替えは任意で、既定は無効**（`GEMINI_API_KEY_PAID`）。無料キーが **429 で尽きた** ときだけ、同じモデルを有料キーで 1 回試す（LLM と質問の埋め込みの両方）。
  キーの値はログに出さない。本番には配備済みで、有料キーでの実 API の呼び出しも確認済み。**未確認:** 本物の 429 での切り替えと 60 秒の保持。詳細と配備の状態は `docs/configuration.md` の「無料キーが 429 のとき」。
- **`temperature` は指定しない。** Gemini 3 系は既定の 1.0 が公式の推奨で、`gemini-3.5-flash-lite` は指定を無視する。`tests/test_llm_config.py` が確かめる。

## 切断は失敗ではない

- `/chat` はグラフの実行と `request.receive()`（`http.disconnect` まで待つ監視）を `asyncio.wait` で競わせ、
  切断が先なら LLM 呼び出しを取り消して 499 を返す。この分岐は `except Exception` の外に置き、ログは INFO・固定文・`thread_id`・経過秒だけにする
  （例外にすると 502 と ERROR ログになる）。`request.is_disconnected()` のポーリングは使わない（外側の取り消しを飲み込み、遅れる）。
  本文の解析前には `receive()` を呼ばない。`/chat/stream` も同じ方針で、レスポンスは検証と認証の後すぐ始め
  （422・401 は HTTP のまま）、その後の失敗は `error` チャンクの固定メッセージで伝える（直前の transient な `data-failure` にステータスと
  Retry-After）。生成が始まる前に切断されても
  実行を取り残さないよう、後始末は `BackgroundTask` にも置く。

## ストリーミングでも出力ガードを外さない

- 漏洩の判定は「正規化後の連続一致 24 文字以上」で、`StreamingOutputGuard` は末尾の正規化後 31 文字を
  送らずに残して、閾値に届く前に止める。検知したら、そのターンのメッセージを定型文に置き換え、ツール呼び出しも捨てる。
  窓や保持の文字数を変えるときは、`tests/test_streaming_guard.py` の分割位置のテストで確かめる。

## コードの置き場所に付く不変条件（旧「構成」の節から。置き場所の表は docs/architecture.md）

- `app/api/routes.py`: LLM の失敗は固定メッセージの 429/504/500/502 に変換する。画面は SSE の `/chat/stream`（組み立ては `ui_stream.py`）を使う
- `app/agents/guardrails.py`: 完成した回答の検査と、ストリーミング中の `StreamingOutputGuard`。`resilience.py` はヘッジ付き再試行・フォールバック・有料キー切り替え
- `app/search/vector.py`: 本物の LLM では `DB_BACKEND=pg` + `DATABASE_URL` と `GEMINI_API_KEY` が必須（本番は pg）。起動時に DB の資料と埋め込みの状態を検査し、合わなければ起動を失敗させる。
  失敗しても別の経路には戻らず、API は 503 の固定文を返す。モックモード（キー未設定）は資料を検索しない固定の返答。設計は `docs/search-design.md`
- `app/history.py`: `DB_BACKEND=pg` なら Postgres、`d1` で `D1_API_URL` があれば D1（データ用 Worker 経由。本番の D1 は撤去済みで、テストの偽 Worker 用に残したコード）、空ならメモリ。開けなければ起動を失敗させ、メモリへは切り替えない。削除コマンドは `app/history_admin.py`（既定は件数の表示だけ、`--apply` で削除。アプリからは使わない）
- `app/auth.py`: `AUTH_MODE=off`（既定）は匿名、`required` は `/chat` と `/chat/stream` に Better Auth（Worker `docunavi-api`。Google ログイン）が発行した JWT（JWKS の ES256/RS256 だけ。`exp` `iss` `aud` `sub` 必須）を要求し、
  履歴のキーを `<sub>:<thread_id>` にする（`sub` 以外から利用者 ID を取らない）。公開鍵が無ければ共有秘密には切り替えず、起動を失敗させる。
  本番は 2026-11-08 から Better Auth。**未確認:** トークン期限切れ後の更新の記録に残る確認（`docs/configuration.md` の「ログイン（認証）」）
- `app/access.py` / `app/scope.py`: ロール別の資料グループ制御（`ACCESS_MODE=off`（既定）/`shadow`/`enforce`）。検索は `scope` が必須（`Scope.ALL` は明示。MCP は運用者専用で全グループ）。本番は 2026-10-26 から `enforce`（`docs/configuration.md` の「資料グループのアクセス制御」）
- `app/api/threads.py`（会話の一覧・発言・削除。`required` では本人の分だけ、`off` は一覧が 404）、`app/api/documents.py`（資料一覧。読み取り専用）、
  `app/api/graph.py`（入寮規程の関係図。`app/relations/` が規則ベースで PDF から都度計算する実験。DB に保存しない）。仕様は `docs/api.md`
- `app/agents/studio.py` と `langgraph.json`: LangSmith Studio（`langgraph dev`）用の入口で、ローカル開発専用。本番（Postgres の Cloud Run）は import せず、`LANGSMITH_*` も渡さない。手順は `docs/development.md`
- `storage/pdf/<名前>/`: PDF の資料（`cafe` `guides` `zahard`）。検索は DB を読むので、PDF を足しても `python -m app.ingestion` と
  `python -m app.ingestion.embed` をするまで検索には出ない。`cafe` `guides` は `evals/corpora/<同じ名前>/` の写し（作り直したら両方を更新）、`zahard` は .txt から作った PDF（`docs/configuration.md` の「評価用の PDF の写し」）。
  2 段組みの読み順の組み直し（`pdf_parser._split_columns`）は実物で確かめるまでアプリには置かない
- `storage/chunk/` は空でアプリは読まない。`scripts/bm25_baseline.py` は評価の基準線で、アプリは import しない。`frontend/` の `.next*/` と `node_modules/` は Git 管理外
