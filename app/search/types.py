"""検索の結果と失敗の型。ベクトル検索（`app/search/vector.py`）が使い、パイプライン・API・ツールが受け取る。"""

from typing import NamedTuple


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


class VectorSearchError(RuntimeError):
    """資料の検索を使えない（設定・DB の状態・実行時の失敗）。メッセージは固定文（ファイル名まで）で、API はこの型を 503 の固定文にする。"""
