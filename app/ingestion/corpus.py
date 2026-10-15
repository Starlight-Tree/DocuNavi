"""PDF を、検索（app/agents/tools.py の search_chunks）が採点する単位の列にする。

アプリの検索と評価（scripts/eval_retrieval.py）が同じ関数を通る。評価の baseline が、アプリの検索を測っていると言える根拠。
"""

from pathlib import Path
from typing import Any

from app.ingestion.chunker import chunk_pages
from app.ingestion.pdf_parser import IngestionError, extract_pages
from app.ingestion.service import find_pdfs, source_name

PASSAGES = ("parent", "sentence")


def load_pdf_corpus(
    directory: Path, passages: str = "parent"
) -> tuple[dict[str, list[Any]], dict[str, list[Any]], list[str]]:
    """配下の PDF を、取り込みと同じ経路（抽出・チャンク化）で読む。

    (名前 -> 検索の単位の列, 名前 -> ページ, 取り込めなかった PDF の「名前: 理由」)。取り込めない PDF を黙って落とさない。
    採点する単位は、段落のチャンクなら文（1 文ずつ）、表のチャンクなら行。passages="parent"（既定）では、表の行は
    表全体を 1 つのまとまりにする（行ごとに採点し、採用されたら表全体を返す）。"sentence" なら、表も 1 行ずつの文として返す。
    見出しは、変わったときに 1 度だけ単位として入れる。
    """
    if passages not in PASSAGES:
        raise ValueError(f"passages は {PASSAGES} のどれかにしてください")
    corpus: dict[str, list[Any]] = {}
    pages_by_doc: dict[str, list[Any]] = {}
    failures: list[str] = []
    for path in find_pdfs(directory):
        name = source_name(path, directory)
        try:
            pages = extract_pages(path)
        except (IngestionError, OSError) as exc:
            failures.append(f"{name}: {getattr(exc, 'reason', 'unreadable')}")
            continue
        pages_by_doc[name] = pages
        units: list[Any] = []
        last_heading: str | None = None
        for chunk in chunk_pages(pages):
            if chunk.heading and chunk.heading != last_heading:
                units.append(chunk.heading)
            last_heading = chunk.heading
            lines = chunk.text.split("\n")
            if chunk.kind == "table" and passages == "parent":
                units.append(tuple(lines))  # 表のまとまり
            else:
                units.extend(lines)
        corpus[name] = units
    return corpus, pages_by_doc, failures
