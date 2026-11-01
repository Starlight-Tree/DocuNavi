"""ヒーロー画像の取り込み・承認コマンド（運用者用）。書き込める DATABASE_URL が要る。

    python -m app.hero_images list                                   # 画像の一覧（id・資料・状態・バイト数）
    python -m app.hero_images import --plan plan.json --dir images/  # ジョブの出力（<資料ID>/<ハッシュ先頭16文字>.png）を draft で入れる
    python -m app.hero_images approve 12                             # 画像を見てから承認する（同じ資料の古い approved は rejected になる）
    python -m app.hero_images reject 12

終了コード: 0 = 完了、1 = DB の操作に失敗、2 = 使い方の誤り・DATABASE_URL 未設定・入力の問題。
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from app.hero_images import HeroImageError, format_rows, import_images, list_images, load_plan, set_status
from app.history import database_url


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.hero_images", description="ヒーロー画像の取り込みと承認")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="画像の一覧")
    imp = commands.add_parser("import", help="ジョブの出力を draft で入れる")
    imp.add_argument("--plan", required=True, type=Path, help="生成に使った plan.json")
    imp.add_argument("--dir", required=True, type=Path, dest="directory", help="ジョブの出力を置いたフォルダー")
    for name in ("approve", "reject"):
        command = commands.add_parser(name, help=f"画像を {name} にする")
        command.add_argument("image_id", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv()
    url = database_url()
    if not url:
        print("DATABASE_URL が未設定です。", file=sys.stderr)
        return 2
    import psycopg

    try:
        plan = load_plan(args.plan) if args.command == "import" else None
        with psycopg.connect(url, prepare_threshold=None, connect_timeout=10) as conn:
            if args.command == "list":
                print(format_rows(list_images(conn)))
            elif args.command == "import":
                assert plan is not None
                for item in import_images(conn, plan, args.directory):
                    print(
                        f"画像 {item.image_id}（資料 {item.document_id}、{item.bytes} バイト）を draft で入れました。"
                    )
                print("画像を目で確かめてから、approve で承認してください（承認するまで画面には出ません）。")
            else:
                print(set_status(conn, args.image_id, "approved" if args.command == "approve" else "rejected"))
    except HeroImageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"DB の操作に失敗しました（{type(exc).__name__}）。", file=sys.stderr)  # 接続先・SQL は出さない
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
