"""資料の版の一覧（`revisions.csv`）を読む。版は「初版」「改訂 2」「2026年4月1日時点」のような、資料に付ける短い文字列。

一覧は、資料群のディレクトリ（R2 なら `<資料群>/revisions.csv`）に PDF と並べて置く。1 行目は `source,revision`、以降は 1 資料 1 行:

    source,revision
    aoba-aw820/guide-01.pdf,初版
    aoba-aw820/guide-02.pdf,改訂 2

`source` は取り込みの識別子で、資料群のディレクトリからの相対パス。**資料群の名前は含めない**
（例: 資料群 `synthetic-test` の中の `aoba-aw820/guide-01.pdf` は、`aoba-aw820/guide-01.pdf` と書く）。
照合は Unicode の NFC にそろえて行う（Excel や macOS のファイル名で、濁点の表し方が違っても同じ資料として扱う）。
版は人が明示した値だけを使う
（PDF の本文・メタデータや LLM からは推測しない。誤ると、誤った安心感を与えるため）。

形が悪い一覧は、読み込みの時点で止める（一部だけ反映して、誤った版を保存しない）。
"""

import csv
import io
import unicodedata
from collections.abc import Collection, Mapping
from pathlib import Path

REVISIONS_FILE = "revisions.csv"
MAX_REVISION_CHARS = 40  # DB の制約（db/migrations/0006_document_revision.sql）と同じ
MAX_FILE_BYTES = 256 * 1024
_HEADER = ["source", "revision"]


class RevisionsError(ValueError):
    """一覧の形の誤り。メッセージは固定の文（行の番号だけを含み、値は含めない）。"""


def match_key(source: str) -> str:
    """照合用の source。Unicode を NFC にそろえる（濁点が結合文字に分かれた名前と、1 文字の名前を同じ資料にする）。保存する source は変えない。"""
    return unicodedata.normalize("NFC", source)


def clean_revision(value: str) -> str | None:
    """前後の空白を除く。空・長すぎる・制御文字や改行を含む値は None（使えない）。"""
    text = value.strip()
    if not text or len(text) > MAX_REVISION_CHARS:
        return None
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        return None
    return text


def parse_revisions(data: bytes) -> dict[str, str]:
    """CSV の中身から {source: 版} を作る。形が悪ければ RevisionsError。"""
    if len(data) > MAX_FILE_BYTES:
        raise RevisionsError("版の一覧が大きすぎます。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RevisionsError("版の一覧は UTF-8 で保存してください。") from None
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:  # 引用符の閉じ忘れなど。固定の文にする（値は含めない）
        raise RevisionsError("版の一覧を CSV として読めません（引用符の閉じ忘れなど）。") from None
    if not rows or [cell.strip() for cell in rows[0]] != _HEADER:
        raise RevisionsError("版の一覧の 1 行目は「source,revision」にしてください。")
    revisions: dict[str, str] = {}
    for number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):  # 空行
            continue
        if len(row) != 2:
            raise RevisionsError(f"版の一覧の {number} 行目は、2 列（source,revision）にしてください。")
        source = match_key(row[0].strip())
        revision = clean_revision(row[1])
        if not source:
            raise RevisionsError(f"版の一覧の {number} 行目の source が空です。")
        if revision is None:
            raise RevisionsError(
                f"版の一覧の {number} 行目の版は、1〜{MAX_REVISION_CHARS} 文字で、改行・制御文字を含めないでください。"
            )
        if source in revisions:
            raise RevisionsError(f"版の一覧の {number} 行目の source が、前の行と重なっています。")
        revisions[source] = revision
    return revisions


def load_revisions(directory: Path) -> dict[str, str] | None:
    """`directory/revisions.csv` を読む。ファイルが無ければ None（版には触れない）。形が悪ければ RevisionsError。"""
    path = directory / REVISIONS_FILE
    if not path.is_file():
        return None
    return parse_revisions(path.read_bytes())


def unmatched(revisions: Mapping[str, str], sources: Collection[str]) -> list[str]:
    """一覧にあって、取り込んだ資料に無い source（綴りの違い・消した資料）。名前順。照合は NFC にそろえて行う。"""
    present = {match_key(s) for s in sources}
    return sorted(set(revisions) - present)
