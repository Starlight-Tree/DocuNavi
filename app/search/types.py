"""検索の結果と失敗の型。ベクトル検索（`app/search/vector.py`）が使い、パイプライン・API・ツールが受け取る。"""

import json
from typing import Any, NamedTuple

MAX_SUGGESTIONS = 3
MAX_SUGGESTION_CHARS = 60

# 検索の SQL で `suggested_questions` を読む式。質問を作った時点の版（suggestions_hash）が今の版（content_hash）と違う、
# つまり PDF が入れ替わって再生成を待っている質問は、画面に出さない（古い PDF の内容の質問が残らないように）。
SUGGESTED_QUESTIONS_SQL = (
    "CASE WHEN d.suggestions_hash = d.content_hash THEN d.suggested_questions END AS suggested_questions"
)


def clean_questions(value: Any) -> tuple[str, ...] | None:
    """`documents.suggested_questions`（jsonb の配列、または JSON 文字列）を、画面に渡せる形にする。

    文字列だけを、前後の空白を除き、空・長すぎるもの・重複を落として、3 件まで。使えるものが無ければ None。
    DB の値は信用しない入力として扱う（形が壊れていても、検索を失敗させない）。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, list):
        return None
    questions: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if text and len(text) <= MAX_SUGGESTION_CHARS and text not in questions:
            questions.append(text)
    return tuple(questions[:MAX_SUGGESTIONS]) or None


class Hit(NamedTuple):
    """検索で拾った 1 チャンク。`page` は PDF の 1 始まりのページ、`page_end` はページをまたぐ表の最終ページ（またがなければ None）。

    `similarity` はコサイン類似度。画面の出典カードを絞る判断（`app/agents/tools.py` の `WEAK_GAP`）だけに使い、LLM には渡さない。
    `collection` は資料のグループ（`documents.collection`。未設定なら None）。画面の出典に添えるだけで、LLM には渡さない。
    `origin` は拾った経路（`app/search/pipeline.py` が付ける。`vector` / `keyword` / `vector+keyword` / `graph`。ベクトル検索だけのときは None）。
    `similarity` は、キーワードだけで拾った箇所と `graph` の箇所では None。
    `target` は資料の対象（`documents.target`。未分類なら None）。画面の出典に添える・対象で絞った検索の結果を確かめるためのもので、LLM には渡さない。
    `document_id` は資料の ID（`documents.id`）。画面が出典から資料を直接開くためのもので、LLM には渡さない。
    `page_label` / `page_end_label` は、PDF のページラベル（表紙・i・1・A-1 など）。画面の出典の表示用で、無ければ None（その場合は `page` を表示する）。LLM には渡さない。リンクと照合は `page`（通し番号）を使う。
    `title` は資料の題名（`documents.title`。無ければ None）。画面の出典の表示にだけ使い、LLM には渡さない。資料の識別（キー）は今までどおり `source`（ファイル名）。
    `suggested_questions` は、その資料に答えがある「続けて聞く」の質問（`documents.suggested_questions`。無ければ None）。画面の候補にだけ使い、LLM には渡さない。
    """

    source: str
    text: str
    page: int | None = None
    page_end: int | None = None
    similarity: float | None = None
    collection: str | None = None
    origin: str | None = None
    target: str | None = None
    document_id: int | None = None
    title: str | None = None
    page_label: str | None = None
    page_end_label: str | None = None
    suggested_questions: tuple[str, ...] | None = None


class VectorSearchError(RuntimeError):
    """資料の検索を使えない（設定・DB の状態・実行時の失敗）。メッセージは固定文（ファイル名まで）で、API はこの型を 503 の固定文にする。"""
