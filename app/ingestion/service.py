"""PDF を解析・チャンク化し、保存先との差分を数え、必要なら保存する。"""

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.ingestion.chunker import chunk_pages
from app.ingestion.pdf_parser import IngestionError, extract_pages, extract_title
from app.ingestion.repository import KEEP_REVISION, DocumentState, Repository, Revision, _KeepRevision, diff_counts
from app.ingestion.revisions import match_key


@dataclass(frozen=True)
class DocumentReport:
    source: str
    status: str  # 確認のみ: would_insert / would_update / unchanged / failed。保存後: inserted / updated / unchanged / failed
    pages: int = 0
    chunks: int = 0
    new_chunks: int = 0
    unchanged_chunks: int = 0
    removed_chunks: int = 0
    reason: str | None = None  # failed のときの理由コード（pdf_parser.REASONS）
    title: str | None = None  # PDF のメタデータ /Title（無い・使えないときは None）
    collection: str | None = (
        None  # 保存する資料群の名前（入力のディレクトリ名、または --collection。決まらなければ None）
    )
    saved: bool = False  # 保存の SQL を流したか（変更のない文書は、読むだけで流さない）
    revision_kept: bool = (
        False  # True = 一覧が無いので、保存済みの版に触れない（revision=None の「版なしにする」と区別する）
    )
    revision: str | None = (
        None  # 保存する版（revisions.csv の値。None = 版なしにする、または一覧が無くて触れない（revision_kept））
    )


# 資料群の名前の形。DB の制約（workers/api/migrations/0001_schema.sql）と同じ
COLLECTION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,40}")


def collection_name(root: Path) -> str | None:
    """入力から資料群の名前を決める。ディレクトリならその名前、ファイル 1 つなら置いてあるディレクトリの名前。形が合わなければ None。"""
    name = (root if root.is_dir() else root.parent).resolve().name
    return name if COLLECTION_PATTERN.fullmatch(name) else None


def find_pdfs(target: Path) -> list[Path]:
    """ファイルならそれ 1 つ、ディレクトリなら配下の *.pdf（拡張子の大小は問わない）を名前順に。"""
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    return []


def source_name(path: Path, root: Path) -> str:
    """保存先での文書の識別子。入力のディレクトリからの相対パス（ファイル 1 つなら、ファイル名）。"""
    base = root if root.is_dir() else root.parent
    return path.relative_to(base).as_posix()


def _is_current(
    state: DocumentState | None,
    file_hash: str,
    pages: int,
    title: str | None,
    collection: str | None,
    revision: Revision = KEEP_REVISION,
) -> bool:
    """保存済みの行が、今回保存する値と同じか（同じなら保存の SQL は要らない）。"""
    return (
        state is not None
        and state.status == "ready"
        and state.content_hash == file_hash
        and state.pages == pages
        and state.title == title
        and (collection is None or state.collection == collection)
        and (isinstance(revision, _KeepRevision) or state.revision == revision)
    )


def ingest_file(
    path: Path,
    source: str,
    repo: Repository,
    *,
    apply: bool,
    collection: str | None = None,
    revision: Revision = KEEP_REVISION,
) -> DocumentReport:
    try:
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        pages = extract_pages(path)
    except OSError:
        return DocumentReport(source=source, status="failed", reason="unreadable")
    except IngestionError as exc:
        if apply:
            repo.mark_failed(source, file_hash, exc.reason)
        return DocumentReport(source=source, status="failed", reason=exc.reason)

    title = extract_title(path)
    chunks = chunk_pages(pages)
    new_hashes = [c.content_hash for c in chunks]
    stored = repo.stored_hashes(source)
    new, unchanged, removed = diff_counts(stored, new_hashes)

    if stored is None:
        status = "inserted" if apply else "would_insert"
    elif stored == new_hashes:
        status = "unchanged"
    else:
        status = "updated" if apply else "would_update"

    saved = False
    state = repo.document_state(source) if apply and stored == new_hashes else None
    content_current = stored == new_hashes and _is_current(state, file_hash, len(pages), title, collection)
    if apply and not content_current:
        # 変更があるとき（チャンク・ファイル・ページ数・題名・資料群のどれか）と、直前に failed と記録されていたとき（ready に戻す）だけ保存する。
        # 変更のない文書を毎回書き直すと、毎時の突き合わせが全チャンクの UPDATE を流す（2026-11-10 のログから推計で約 2,700 本 / 回。実測は --watch）
        # 題名は PDF の /Title をそのまま渡す。PDF に題名が無ければ None で、保存済みの題名も消える（PDF が正）
        # 資料群は、決まっているときだけ上書きする（None なら、保存済みの値を残す）
        # 版は、一覧（revisions.csv）があるときだけ渡す。渡さなければ、保存済みの版に触れない
        repo.save(source, file_hash, len(pages), chunks, title=title, collection=collection, revision=revision)
        saved = True
    elif apply and not _is_current(state, file_hash, len(pages), title, collection, revision):
        # 版だけが変わったとき: チャンクの行は書き直さず、版の UPDATE を 1 本だけ流す
        repo.set_revision(source, None if isinstance(revision, _KeepRevision) else revision)
        saved = True

    return DocumentReport(
        source=source,
        status=status,
        pages=len(pages),
        chunks=len(chunks),
        new_chunks=new,
        unchanged_chunks=unchanged,
        removed_chunks=removed,
        title=title,
        collection=collection,
        saved=saved,
        revision=None if isinstance(revision, _KeepRevision) else revision,
        revision_kept=isinstance(revision, _KeepRevision),
    )


def ingest(
    paths: Iterable[Path],
    root: Path,
    repo: Repository,
    *,
    apply: bool,
    collection: str | None = None,
    revisions: Mapping[str, str] | None = None,
) -> list[DocumentReport]:
    """collection を渡さなければ、入力のディレクトリ名（`collection_name`）を資料群の名前にする。

    revisions を渡したとき（版の一覧がある）は、一覧にある資料にその版を、無い資料には版なしを保存する（一覧が正）。
    渡さない（None。一覧が無い）ときは、保存済みの版に触れない。"""
    name = collection if collection is not None else collection_name(root)
    files = [(p, source_name(p, root)) for p in paths]
    repo.prefetch([source for _, source in files])  # 文書ごとに読まず、まとめて 2 本で読む
    return [
        ingest_file(
            p,
            source,
            repo,
            apply=apply,
            collection=name,
            revision=KEEP_REVISION if revisions is None else revisions.get(match_key(source)),
        )
        for p, source in files
    ]
