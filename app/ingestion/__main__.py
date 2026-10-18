"""PDF の取り込みコマンド: 解析し、チャンクに分け、保存先との差分を表示する。

アプリ本体（`app/main.py`）からは使わない。運用者が手元・ジョブから実行する。HTTP のアップロード口は作らない
（認証が入るまで、誰でも書き込める口を作らない。docs/roadmap.md の段階 2 が先）。

    python -m app.ingestion --input evals/corpora/cafe                # 確認だけ（何も書かない）
    python -m app.ingestion --input evals/corpora/cafe/x.pdf          # 1 ファイルだけ
    python -m app.ingestion --input evals/corpora/cafe --apply        # DATABASE_URL の Postgres に保存する

`DATABASE_URL` があれば、確認のときも保存済みの内容と比べる（読むだけで、テーブルも作らない）。無ければ、保存済みは無いものとして数える。
`--apply` は `DATABASE_URL` が要る。表示するのは、ファイル名・題名（PDF の /Title）・ページ数・チャンク数と、新しい・変わらない・消えるチャンクの数だけ。

終了コード: 0 = 完了、1 = DB の操作に失敗、2 = 使い方の誤り・入力に PDF が無い・`--apply` なのに DATABASE_URL が未設定、
3 = 一部の PDF が取り込めなかった（他の PDF は処理した。理由は表示と、`--apply` なら documents.failure_reason）。
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from app.history import database_url
from app.ingestion.pdf_parser import REASONS
from app.ingestion.repository import MemoryRepository, PostgresRepository
from app.ingestion.service import DocumentReport, find_pdfs, ingest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingestion",
        description="PDF を解析してチャンクに分ける（既定は確認だけ。--apply で保存する）",
    )
    parser.add_argument("--input", required=True, type=Path, help="PDF のファイル、または PDF を含むディレクトリ")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="何も書かない（既定）")
    mode.add_argument("--apply", action="store_true", help="DATABASE_URL の Postgres に保存する")
    return parser


def _print_report(report: DocumentReport) -> None:
    print(report.source)
    if report.status == "failed":
        print(f"  status=failed reason={report.reason}")
        print(f"  {REASONS.get(report.reason or '', '')}")
        return
    print(f"  title={report.title or '(none)'}")
    print(f"  pages={report.pages}")
    print(f"  chunks={report.chunks}")
    print(f"  new_chunks={report.new_chunks}")
    print(f"  unchanged_chunks={report.unchanged_chunks}")
    print(f"  removed_chunks={report.removed_chunks}")
    print(f"  status={report.status}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = find_pdfs(args.input)
    if not paths:
        print("入力に PDF がありません。", file=sys.stderr)
        return 2

    load_dotenv()
    url = database_url()
    if args.apply and not url:
        print("--apply には DATABASE_URL が要ります（未設定です）。", file=sys.stderr)
        return 2

    if not url:
        reports = ingest(paths, args.input, MemoryRepository(), apply=False)
    else:
        # アプリと同じ理由で、psycopg は DATABASE_URL があるときだけ import する
        import psycopg

        try:
            # prepare_threshold=None: transaction mode のプーラーでも動かす（app/history.py と同じ）
            with psycopg.connect(url, prepare_threshold=None, connect_timeout=10) as conn:
                repo = PostgresRepository(conn, create_tables=args.apply)
                reports = ingest(paths, args.input, repo, apply=args.apply)
        except psycopg.Error as exc:
            # 例外の文字列には接続先が入りうる。クラス名だけを出す
            print(f"DB の操作に失敗しました（{type(exc).__name__}）。", file=sys.stderr)
            return 1

    for report in reports:
        _print_report(report)
    failed = sum(1 for r in reports if r.status == "failed")
    suffix = "" if args.apply else "（確認だけ。何も保存していません。--apply で保存します）"
    print(f"文書 {len(reports)} 件、取り込めなかった文書 {failed} 件{suffix}")
    return 3 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
