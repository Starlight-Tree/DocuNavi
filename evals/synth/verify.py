"""出力の点検（読み取りだけ）。台帳・本文・PDF・マニフェストが食い違っていないか、全ページに「テスト用の架空資料」が入っているかを確かめる。"""

from __future__ import annotations

import json
from pathlib import Path

from evals.synth.ledger import Ledger
from evals.synth.pipeline import build_manifest, sha256_bytes
from evals.synth.render import MARKER, NOTICE


def verify_output(out_dir: Path) -> list[str]:
    from pypdf import PdfReader  # アプリの依存（requirements.in）

    problems: list[str] = []
    ledger = Ledger(out_dir / "ledger.jsonl")
    known_pdfs: set[Path] = set()
    for doc_id, s in sorted(ledger.state.items()):
        if s.get("status") != "accepted":
            continue
        md = out_dir / s["md"]
        if not md.exists():
            problems.append(f"{doc_id}: 本文がありません: {s['md']}")
        elif sha256_bytes(md.read_bytes()) != s.get("md_sha256"):
            problems.append(f"{doc_id}: 本文のハッシュが台帳と違います")
        if not s.get("pdf"):
            continue
        pdf = out_dir / s["pdf"]
        known_pdfs.add(pdf.resolve())
        if not pdf.exists():
            problems.append(f"{doc_id}: PDF がありません: {s['pdf']}")
            continue
        if sha256_bytes(pdf.read_bytes()) != s.get("pdf_sha256"):
            problems.append(f"{doc_id}: PDF のハッシュが台帳と違います")
        try:
            reader = PdfReader(str(pdf))
            subject = (reader.metadata.subject if reader.metadata else None) or ""
            if MARKER not in subject:
                problems.append(f"{doc_id}: メタデータ（Subject）に {MARKER} がありません")
            for number, page in enumerate(reader.pages, start=1):
                if NOTICE not in (page.extract_text() or ""):
                    problems.append(f"{doc_id}: {number} ページ目に「{NOTICE}」がありません")
        except Exception as exc:
            problems.append(f"{doc_id}: PDF を読めません（{type(exc).__name__}）")
    for pdf in sorted((out_dir / "pdf").glob("*/*.pdf")) if (out_dir / "pdf").exists() else []:
        if pdf.resolve() not in known_pdfs:
            problems.append(f"台帳にない PDF があります: {pdf.relative_to(out_dir)}")
    manifest = out_dir / "manifest.jsonl"
    if manifest.exists():
        on_disk = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
        if on_disk != build_manifest(out_dir):
            problems.append("manifest.jsonl が台帳と食い違っています（manifest を作り直す）")
    return problems
