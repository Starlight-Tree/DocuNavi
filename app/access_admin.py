"""利用者へのロールの割り当て（`user_roles`）の保守コマンド（ロードマップ ⑧）。

アプリ本体（`app/main.py`）からは使わない。運用者が、アプリと同じ `DATABASE_URL`（`DB_BACKEND=pg`。`.env` も読む）で手元から実行する。
既定は確認だけで、何も書かない。`--apply` を付けたときだけ書く。ロールの作成と、ロールが持つグループの編集は、このコマンドの範囲外（SQL。`docs/configuration.md`）。

    python -m app.access_admin show   --email <メール>                    # 利用者のロールと、見えるグループ
    python -m app.access_admin assign --email <メール> --role <ロール>     # 割り当てた後の、見えるグループを表示する
    python -m app.access_admin assign --email <メール> --role <ロール> --apply
    python -m app.access_admin revoke --user-id <ID> --role <ロール> --apply

利用者は `--email`（`"user".email`。大文字小文字は区別しない）か `--user-id`（`"user".id` = JWT の `sub`）で選ぶ。
表示するのはロール名とグループ名だけで、メールと ID は出さない。変更は `access_audit` に 1 行ずつ残す（`actor` は `access_admin_cli`。ID だけでメールは入れない）。

規則は `app/access.py` と同じ: ロールが 1 つも無い利用者は `default` ロール。最初のロールを割り当てると `default` は使われなくなる（見えるグループが減りうる）。
最後のロールを外すと `default` に戻る。見えるグループが増える解除は、`--allow-widen` が無ければ書かない。
**運用の規則（2026-11-18 に決定）:** `user_roles` の変更は、このコマンドだけで行う。SQL で直接 `DELETE` しない（最後の 1 行を消すと、見える範囲が `default` ロールまで広がる。DB 側に防ぐ制約は無い）。

アプリの利用資格（`app_members`。`MEMBERSHIP_MODE`。資料のロールとは独立）の保守は `member` コマンド。同じく既定は確認だけで、何度流しても結果は同じ（冪等）:

    python -m app.access_admin member show   --email <メール>
    python -m app.access_admin member add    --email <メール> [--note <メモ>] --apply
    python -m app.access_admin member remove --user-id <ID> --apply
    python -m app.access_admin member list                                  # 資格のある利用者の人数だけ（メール・ID は出さない）

外した資格は、新しいリクエストには、各インスタンスのキャッシュ TTL（`ACCESS_CACHE_TTL_SECONDS`。既定 60 秒）以内に効く。進行中の応答は止まらない。
開発用の資格付与はマイグレーションに入れない。ローカルの DB には、このコマンドで付ける（利用者の行が無ければ付けない）。

終了コード: 0 = 完了（変更なしも含む）、1 = 利用者・ロールが無い、DB の失敗、広がる解除を拒否した、2 = 使い方の誤り・DATABASE_URL 未設定・`DB_BACKEND` が pg ではない。
"""

import argparse
import json
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from app.access import _VISIBLE_SQL, DEFAULT_ROLE
from app.d1 import D1Error, SyncD1
from app.pg import backend, database_url, sync_client

ACTOR = "access_admin_cli"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.access_admin",
        description="利用者へのロールの割り当て（既定は確認だけ。--apply で書く）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("show", "利用者のロールと見えるグループを表示する"),
        ("assign", "ロールを割り当てる"),
        ("revoke", "ロールを外す"),
    ):
        p = sub.add_parser(name, help=help_text)
        who = p.add_mutually_exclusive_group(required=True)
        who.add_argument("--email", metavar="MAIL", help='利用者のメール（"user".email）')
        who.add_argument("--user-id", metavar="ID", help='利用者の ID（"user".id = JWT の sub）')
        if name != "show":
            p.add_argument("--role", required=True, metavar="ROLE", help="ロール名")
            p.add_argument("--apply", action="store_true", help="実際に書く（無ければ確認だけ）")
        if name == "revoke":
            p.add_argument("--allow-widen", action="store_true", help="見えるグループが増える解除も許す")
    member = sub.add_parser("member", help="アプリの利用資格（app_members）。show / add / remove / list")
    member.add_argument("action", choices=("show", "add", "remove", "list"))
    who = member.add_mutually_exclusive_group()
    who.add_argument(
        "--email", metavar="MAIL", help='利用者のメール（"user".email）。list 以外で、これか --user-id が要る'
    )
    who.add_argument("--user-id", metavar="ID", help='利用者の ID（"user".id = JWT の sub）')
    member.add_argument("--note", default="", metavar="TEXT", help="add のとき残すメモ（任意。個人情報は書かない）")
    member.add_argument("--apply", action="store_true", help="実際に書く（無ければ確認だけ）")
    return parser


def _user_id(db, args: argparse.Namespace) -> str | None:
    if args.user_id is not None:
        rows = db.execute('SELECT id FROM "user" WHERE id = %(v)s', {"v": args.user_id})
    else:
        rows = db.execute('SELECT id FROM "user" WHERE lower(email) = lower(%(v)s)', {"v": args.email})
    return str(rows[0]["id"]) if rows else None


def _roles(db, uid: str) -> list[str]:
    rows = db.execute("SELECT role FROM user_roles WHERE user_id = %(uid)s ORDER BY role", {"uid": uid})
    return [str(r["role"]) for r in rows]


def _visible(db, uid: str) -> list[str]:
    rows = db.execute(_VISIBLE_SQL, {"uid": uid, "default": DEFAULT_ROLE})
    return sorted(str(r["collection"]) for r in rows)


def _after_visible(db, roles: list[str]) -> list[str]:
    """割り当てが `roles` になったときに見えるグループ（空なら default ロール）。"""
    names = roles or [DEFAULT_ROLE]
    found: set[str] = set()
    for name in names:
        rows = db.execute("SELECT collection FROM role_collection_access WHERE role = %(r)s", {"r": name})
        found.update(str(r["collection"]) for r in rows)
    return sorted(found)


def _fmt(items: Sequence[str]) -> str:
    return "、".join(items) if items else "（なし）"


def _run_member(db, args: argparse.Namespace) -> int:
    """`member` コマンド。利用資格（`app_members`）の確認・付与・取り消し。付与・取り消しは冪等（すでに同じ状態なら変更なし）。"""
    if args.action == "list":
        rows = db.execute("SELECT count(*) AS n FROM app_members")
        print(f"利用資格のある利用者: {int(rows[0]['n'])} 人")
        return 0
    uid = _user_id(db, args)
    if uid is None:
        print("その利用者が見つかりません（資格は、ログイン済みの利用者にだけ付けられます）。", file=sys.stderr)
        return 1
    is_member = bool(db.execute("SELECT 1 AS ok FROM app_members WHERE user_id = %(uid)s", {"uid": uid}))
    if args.action == "show":
        print(f"利用資格: {'あり' if is_member else 'なし'}")
        return 0
    adding = args.action == "add"
    if adding == is_member:
        print(f"変更なし（すでに利用資格が{'ある' if adding else 'ない'}）。")
        return 0
    print(f"利用資格: {'なし → あり' if adding else 'あり → なし'}")
    if not args.apply:
        print("書き込みはしていません（--apply で書きます）。")
        return 0
    if adding:
        write = (
            "INSERT INTO app_members (user_id, note) VALUES (%(uid)s, %(note)s) ON CONFLICT (user_id) DO NOTHING",
            {"uid": uid, "note": args.note},
        )
    else:
        write = ("DELETE FROM app_members WHERE user_id = %(uid)s", {"uid": uid})
    audit = (
        "INSERT INTO access_audit (actor, table_name, op, row_data) "
        "VALUES (%(a)s, 'app_members', %(op)s, %(d)s::jsonb)",
        {"a": ACTOR, "op": "INSERT" if adding else "DELETE", "d": json.dumps({"user_id": uid})},
    )
    db.batch([write, audit])
    print("書きました（access_audit に記録）。")
    if not adding:
        print(
            "新しいリクエストには、各インスタンスのキャッシュ TTL（既定 60 秒）以内に効きます。進行中の応答は止まりません。"
        )
    return 0


def _run(db, args: argparse.Namespace) -> int:
    if args.command == "member":
        return _run_member(db, args)
    uid = _user_id(db, args)
    if uid is None:
        print("その利用者が見つかりません。", file=sys.stderr)
        return 1
    current = _roles(db, uid)
    visible = _visible(db, uid)

    if args.command == "show":
        print(f"ロール: {_fmt(current) if current else f'（割り当てなし = {DEFAULT_ROLE}）'}")
        print(f"見えるグループ: {_fmt(visible)}")
        return 0

    if not db.execute("SELECT 1 AS ok FROM roles WHERE name = %(r)s", {"r": args.role}):
        print("そのロールがありません（ロールの作成は SQL で行います）。", file=sys.stderr)
        return 1

    assigning = args.command == "assign"
    if assigning == (args.role in current):
        state = "割り当て済み" if assigning else "割り当てられていない"
        print(f"変更なし（このロールは、すでに{state}）。")
        return 0

    new_roles = sorted([*current, args.role]) if assigning else [r for r in current if r != args.role]
    after = _after_visible(db, new_roles)
    print(
        f"ロール: {_fmt(current) if current else f'（割り当てなし = {DEFAULT_ROLE}）'} → {_fmt(new_roles) if new_roles else f'（割り当てなし = {DEFAULT_ROLE}）'}"
    )
    print(f"見えるグループ: {_fmt(visible)} → {_fmt(after)}")
    if assigning and not current:
        print(
            f"注意: 最初のロールなので、{DEFAULT_ROLE} は使われなくなります（足したいときは {DEFAULT_ROLE} も割り当てる）。"
        )
    widens = bool(set(after) - set(visible))
    if not assigning and widens and not args.allow_widen:
        print("見えるグループが増える解除です。書きません（意図どおりなら --allow-widen）。", file=sys.stderr)
        return 1
    if not args.apply:
        print("書き込みはしていません（--apply で書きます）。")
        return 0

    row = json.dumps({"user_id": uid, "role": args.role})
    if assigning:
        write = ("INSERT INTO user_roles (user_id, role) VALUES (%(uid)s, %(r)s)", {"uid": uid, "r": args.role})
        op = "INSERT"
    else:
        write = ("DELETE FROM user_roles WHERE user_id = %(uid)s AND role = %(r)s", {"uid": uid, "r": args.role})
        op = "DELETE"
    audit = (
        "INSERT INTO access_audit (actor, table_name, op, row_data) VALUES (%(a)s, 'user_roles', %(op)s, %(d)s::jsonb)",
        {"a": ACTOR, "op": op, "d": row},
    )
    db.batch([write, audit])
    print("書きました（access_audit に記録）。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "member" and args.action != "list" and args.email is None and args.user_id is None:
        parser.error("member show / add / remove には --email か --user-id が要ります")
    load_dotenv()
    try:
        if backend() != "pg":
            print("DB_BACKEND=pg にしてください（Postgres の user_roles だけが対象です）。", file=sys.stderr)
            return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    url = database_url()
    if not url:
        print("DATABASE_URL が未設定です。", file=sys.stderr)
        return 2
    try:
        with sync_client(SyncD1, url) as db:
            return _run(db, args)
    except D1Error as exc:
        print(f"DB の操作に失敗しました（{exc}）。", file=sys.stderr)  # URL・パスワードは含まない
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
