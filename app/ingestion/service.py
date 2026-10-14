"""PDF を解析・チャンク化し、保存先との差分を数え、必要なら保存する。"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.ingestion.chunker import chunk_pages
from app.ingestion.pdf_parser import IngestionError, extract_pages
from app.ingestion.repository import Repository, diff_counts


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


def ingest_file(path: Path, source: str, repo: Repository, *, apply: bool) -> DocumentReport:
    try:
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        pages = extract_pages(path)
    except OSError:
        return DocumentReport(source=source, status="failed", reason="unreadable")
    except IngestionError as exc:
        if apply:
            repo.mark_failed(source, file_hash, exc.reason)
        return DocumentReport(source=source, status="failed", reason=exc.reason)

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

    if apply:
        # 内容が同じでも保存する。直前に failed と記録されていれば ready に戻る（チャンクの行は、ハッシュが同じなら触らない）
        repo.save(source, file_hash, len(pages), chunks)

    return DocumentReport(
        source=source,
        status=status,
        pages=len(pages),
        chunks=len(chunks),
        new_chunks=new,
        unchanged_chunks=unchanged,
        removed_chunks=removed,
    )


def ingest(paths: Iterable[Path], root: Path, repo: Repository, *, apply: bool) -> list[DocumentReport]:
    return [ingest_file(p, source_name(p, root), repo, apply=apply) for p in paths]
