"""`chatbot` の表を読む・書く SQL（表名は `chatbot.` 付きで書く）。接続は Django の既定（DB ロールは access_admin）。

書き込みは user_roles の INSERT / DELETE だけ（DB ロールにはそれ以外の権限が無い）。
"""

from collections.abc import Iterable

from django.db import connection

MATRIX_SQL = "SELECT role, collection FROM chatbot.role_collection_access"


def access_matrix() -> dict[str, set[str]]:
    """ロール名 → そのロールが持つグループ。グループを持たないロールは入らない。"""
    matrix: dict[str, set[str]] = {}
    with connection.cursor() as cursor:
        cursor.execute(MATRIX_SQL)
        for role, collection in cursor.fetchall():
            matrix.setdefault(role, set()).add(collection)
    return matrix


def role_names() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM chatbot.roles ORDER BY name")
        return [row[0] for row in cursor.fetchall()]


def registered_collections() -> list[str]:
    """登録済みのグループ（document_collections）。資料にだけあるグループは含まない。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM chatbot.document_collections ORDER BY name")
        return [row[0] for row in cursor.fetchall()]


def role_user_counts() -> dict[str, int]:
    """ロール名 → 割り当てられている利用者の数。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT role, count(*) FROM chatbot.user_roles GROUP BY role")
        return {role: count for role, count in cursor.fetchall()}


def assigned_roles(user_id: str) -> frozenset[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT role FROM chatbot.user_roles WHERE user_id = %s::uuid", [user_id])
        return frozenset(row[0] for row in cursor.fetchall())


def assigned_roles_by_user(user_ids: Iterable[str]) -> dict[str, frozenset[str]]:
    """利用者 ID → 割り当てられたロール（割り当てが無い利用者は入らない）。"""
    ids = [str(i) for i in user_ids]
    result: dict[str, set[str]] = {}
    if not ids:
        return {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT user_id::text, role FROM chatbot.user_roles WHERE user_id = ANY(%s::uuid[])", [ids])
        for user_id, role in cursor.fetchall():
            result.setdefault(user_id, set()).add(role)
    return {user_id: frozenset(roles) for user_id, roles in result.items()}


def lock_user(user_id: str) -> None:
    """同じ利用者の割り当てを同時に変えないよう、トランザクションの間だけ排他にする（行ロックは UPDATE 権限が要るので使わない）。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"user_roles:{user_id}"])


def apply_role_change(user_id: str, add: Iterable[str], remove: Iterable[str]) -> None:
    """user_roles に、足すロールを INSERT し、外すロールを DELETE する（呼び出し側が transaction.atomic の中で行う）。

    変更の記録は DB のトリガー（access_audit）が残す。存在しないロールは外部キーで拒否される。
    """
    add_list, remove_list = sorted(add), sorted(remove)
    with connection.cursor() as cursor:
        if remove_list:
            cursor.execute(
                "DELETE FROM chatbot.user_roles WHERE user_id = %s::uuid AND role = ANY(%s::text[])",
                [user_id, remove_list],
            )
        if add_list:
            cursor.execute(
                "INSERT INTO chatbot.user_roles (user_id, role) SELECT %s::uuid, unnest(%s::text[])",
                [user_id, add_list],
            )
