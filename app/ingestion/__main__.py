"""PDF の取り込みコマンド: 解析し、チャンクに分け、保存先との差分を表示する。

アプリ本体（`app/main.py`）からは使わない。運用者が手元・ジョブから実行する。HTTP のアップロード口は作らない
（認証が入るまで、誰でも書き込める口を作らない。docs/roadmap.md の段階 2 が先）。

    python -m app.ingestion --input evals/corpora/cafe                # 確認だけ（何も書かない）
    python -m app.ingestion --input evals/corpora/cafe/x.pdf          # 1 ファイルだけ
    python -m app.ingestion --input evals/corpora/cafe --apply        # DATABASE_URL の Postgres に保存する
    python -m app.ingestion --input x.pdf --collection guides         # 資料群の名前を指定する
    python -m app.ingestion --input r2://cafe                         # R2 の cafe/ の PDF を一時ディレクトリに取り、同じ流れで取り込む
    python -m app.ingestion --input r2://cafe/menu.pdf --apply        # R2 の 1 ファイルだけ

`r2://` は、`R2_ACCOUNT_ID` `R2_BUCKET` `R2_CREDENTIALS_JSON`（`app/r2_store.py`）が要る。資料群の名前は、既定で `r2://` の次の名前になる。

資料群（documents.collection）は、既定で入力のディレクトリ名（ファイル 1 つなら置いてあるディレクトリの名前）。`--collection` で上書きできる。
名前は英数字・`_`・`-` の 40 文字まで。形が合わなければ保存せず（保存済みの値は残す）、確認のときも表示する。

`DATABASE_URL` があれば、確認のときも保存済みの内容と比べる（読むだけで、テーブルも作らない）。無ければ、保存済みは無いものとして数える。
`--apply` は `DATABASE_URL` が要る。表示するのは、ファイル名・題名（PDF の /Title）・ページ数・チャンク数と、新しい・変わらない・消えるチャンクの数だけ。

終了コード: 0 = 完了、1 = DB の操作に失敗、2 = 使い方の誤り・入力に PDF が無い・`--apply` なのに DATABASE_URL が未設定、
3 = 一部の PDF が取り込めなかった（他の PDF は処理した。理由は表示と、`--apply` なら documents.failure_reason）。
"""

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from app.d1 import D1Error, SyncD1
from app.history import database_url
from app.ingestion.pdf_parser import REASONS
from app.ingestion.repository import D1Repository, MemoryRepository
from app.ingestion.service import COLLECTION_PATTERN, DocumentReport, find_pdfs, ingest
from app.pdf_originals import MAX_ORIGINAL_BYTES
from app.pg import sync_client

R2_SCHEME = "r2://"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingestion",
        description="PDF を解析してチャンクに分ける（既定は確認だけ。--apply で保存する）",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="PDF のファイル、または PDF を含むディレクトリ。r2://<資料群>[/<ファイル名>] で R2 から取る",
    )
    parser.add_argument(
        "--collection",
        help="資料群の名前（英数字・_・- の 40 文字まで）。既定は入力のディレクトリ名",
    )
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
    print(f"  collection={report.collection or '(none)'}")
    print(f"  pages={report.pages}")
    print(f"  chunks={report.chunks}")
    print(f"  new_chunks={report.new_chunks}")
    print(f"  unchanged_chunks={report.unchanged_chunks}")
    print(f"  removed_chunks={report.removed_chunks}")
    print(f"  status={report.status}")


def _fetch_from_r2(spec: str, target: Path) -> Path | None:
    """`r2://<資料群>[/<ファイル名>]` の PDF を target に取り、取り込みの入力にするパスを返す。形が合わなければ None。"""
    collection, _, name = spec.removeprefix(R2_SCHEME).partition("/")
    if not COLLECTION_PATTERN.fullmatch(collection):
        return None
    from app import r2_store

    store = r2_store.default_store()
    files = r2_store.download_collection(store, collection, target, MAX_ORIGINAL_BYTES, only=name or None)
    if name:
        return (
            files[0] if files else target / collection / name
        )  # 無ければ、存在しないパスのまま渡す（「PDF がありません」になる）
    (target / collection).mkdir(parents=True, exist_ok=True)
    return target / collection


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.collection is not None and not COLLECTION_PATTERN.fullmatch(args.collection):
        print("--collection は、英数字・_・- の 40 文字までで指定してください。", file=sys.stderr)
        return 2
    if not args.input.startswith(R2_SCHEME):
        return _run(args, Path(args.input))
    from app import r2_store

    # R2 の PDF は一時ディレクトリに取る（終わったら消える）。取り込みの流れは、ローカルの PDF と同じ
    with tempfile.TemporaryDirectory(prefix="docunavi-r2-") as tmp:
        try:
            root = _fetch_from_r2(args.input, Path(tmp))
        except r2_store.R2ConfigError as exc:
            print(f"R2 の設定が足りません（{exc}）。", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - 例外の文字列に接続先・鍵の ID が入りうる。クラス名だけを出す
            print(f"R2 から取得できませんでした（{type(exc).__name__}）。", file=sys.stderr)
            return 1
        if root is None:
            print("r2:// の後は、資料群の名前（英数字・_・- の 40 文字まで）を指定してください。", file=sys.stderr)
            return 2
        return _run(args, root)


def _run(args: argparse.Namespace, input_path: Path) -> int:
    paths = find_pdfs(input_path)
    if not paths:
        print("入力に PDF がありません。", file=sys.stderr)
        return 2

    load_dotenv()
    url = database_url()
    if args.apply and not url:
        print("--apply には D1_API_URL と D1_API_TOKEN が要ります（未設定です）。", file=sys.stderr)
        return 2

    if not url:
        reports = ingest(paths, input_path, MemoryRepository(), apply=False, collection=args.collection)
    else:
        try:
            with sync_client(SyncD1, url) as client:
                repo = D1Repository(client)
                reports = ingest(paths, input_path, repo, apply=args.apply, collection=args.collection)
        except D1Error as exc:
            # 例外の文字列に URL・トークンは入れていない（型と HTTP ステータスだけ）
            print(f"DB の操作に失敗しました（{exc}）。", file=sys.stderr)
            return 1

    for report in reports:
        _print_report(report)
    failed = sum(1 for r in reports if r.status == "failed")
    suffix = "" if args.apply else "（確認だけ。何も保存していません。--apply で保存します）"
    print(f"文書 {len(reports)} 件、取り込めなかった文書 {failed} 件{suffix}")
    return 3 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
