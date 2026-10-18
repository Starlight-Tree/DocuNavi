"""会話履歴（Postgres）の保守コマンド: 保存期間を過ぎた会話の削除と、1 件の会話の削除。

アプリ本体（`app/main.py`）からは使わない。運用者が、アプリと同じ `DATABASE_URL`（`.env` も読む）で手元・ジョブから実行する。
既定は確認だけで、何も消さない。`--apply` を付けたときだけ削除する。

    python -m app.history_admin --older-than-days 30             # 30 日以上動きのない会話の数を表示する
    python -m app.history_admin --older-than-days 30 --apply     # それらを削除する
    python -m app.history_admin --thread-id <thread_id> --apply  # 1 件の会話を削除する

「動きのない」は、会話の最新のチェックポイントの時刻（`checkpoint->>'ts'`）で決める。時刻の無い会話は消さない。
表示するのは件数だけで、`thread_id` は出さない（知っていれば会話を続けられる値のため）。
一覧を取ってから消すまでの間に新しい発言が入った会話も消えうる。保存期間を日単位で決める用途なので許容している。

終了コード: 0 = 完了、1 = DB に接続できない・テーブルが無いなどの失敗、2 = 使い方の誤り・DATABASE_URL 未設定。
"""

import argparse
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from app.history import database_url

_STALE_THREADS_SQL = """
SELECT thread_id
FROM checkpoints
GROUP BY thread_id
HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s)
ORDER BY thread_id
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.history_admin",
        description="会話履歴の削除（既定は確認だけ。--apply で削除する）",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--older-than-days", type=int, metavar="N", help="N 日以上動きのない会話を対象にする（N は 1 以上）"
    )
    target.add_argument("--thread-id", metavar="ID", help="この 1 件の会話を対象にする")
    parser.add_argument("--apply", action="store_true", help="実際に削除する（無ければ件数を表示するだけ）")
    return parser


def _find_threads(conn, args: argparse.Namespace) -> list[str]:
    if args.thread_id is not None:
        row = conn.execute("SELECT 1 FROM checkpoints WHERE thread_id = %s LIMIT 1", (args.thread_id,)).fetchone()
        return [args.thread_id] if row else []
    rows = conn.execute(_STALE_THREADS_SQL, (args.older_than_days,)).fetchall()
    return [row["thread_id"] for row in rows]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.older_than_days is not None and args.older_than_days < 1:
        print("--older-than-days は 1 以上にしてください。", file=sys.stderr)
        return 2
    if args.thread_id is not None and not args.thread_id.strip():
        print("--thread-id が空です。", file=sys.stderr)
        return 2

    load_dotenv()
    url = database_url()
    if not url:
        print("DATABASE_URL が未設定です（履歴がメモリのときは、消す対象がありません）。", file=sys.stderr)
        return 2

    # アプリと同じ理由で、psycopg は DATABASE_URL があるときだけ import する
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    try:
        # prepare_threshold=None: transaction mode のプーラーでも動かす（app/history.py と同じ）
        with psycopg.connect(
            url, autocommit=True, row_factory=dict_row, prepare_threshold=None, connect_timeout=10
        ) as conn:
            threads = _find_threads(conn, args)
            if not args.apply:
                print(f"対象の会話: {len(threads)} 件（削除はしていません。--apply で削除します）")
                return 0
            saver = PostgresSaver(conn)
            for thread_id in threads:
                saver.delete_thread(thread_id)
            print(f"削除した会話: {len(threads)} 件")
            return 0
    except psycopg.errors.UndefinedTable:
        print("履歴のテーブルがありません（接続先と search_path を確かめてください）。", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        # 例外の文字列には接続先が入りうる。クラス名だけを出す
        print(f"DB の操作に失敗しました（{type(exc).__name__}）。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
