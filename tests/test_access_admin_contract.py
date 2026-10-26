"""管理画面（ops/access_admin）の「見えるグループ」の計算が、アプリ本体（app/access.py）と食い違わないことを確かめる契約テスト。

本物の Postgres（TEST_DATABASE_URL）に、アプリと同じ表を作り、ロールの割り当てのあらゆる組み合わせについて、
アプリの `visible_collections`（SQL）と、管理画面の `services.visible_groups`（Python）の結果を突き合わせる。
どちらかの規則を変えたら、このテストが落ちる。サービス側は `app/` を import しない（その検査もここに置く）。
"""

import ast
import asyncio
import itertools
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from app import access
from ops.access_admin.accessadmin import services
from tests.test_access_postgres import _admin, seeded  # noqa: F401 - フィクスチャ
from tests.test_history_postgres import db  # noqa: F401 - フィクスチャ

SERVICES_FILE = Path(services.__file__)
needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL", "").strip(), reason="TEST_DATABASE_URL が未設定")


def test_services_use_only_the_standard_library():
    """services.py は Django にも app/ にも依存しない（管理画面の外からも、アプリ側のテストからも読み込める）。"""
    tree = ast.parse(SERVICES_FILE.read_text(encoding="utf-8"))
    imported = {
        node.module.split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
        if isinstance(node, ast.Import) or node.module
    }
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


async def _app_visible(user_id: str | None) -> frozenset[str]:
    from app import history

    async with history.history_store():
        return await access.visible_collections(user_id)


def _matrix(conn: psycopg.Connection) -> dict[str, set[str]]:
    matrix: dict[str, set[str]] = {}
    for role, collection in conn.execute("SELECT role, collection FROM role_collection_access").fetchall():
        matrix.setdefault(role, set()).add(collection)
    return matrix


@needs_db
def test_every_combination_of_roles_gives_the_same_groups_as_the_app(seeded):  # noqa: F811
    with _admin(seeded) as conn:
        roles = [r[0] for r in conn.execute("SELECT name FROM roles ORDER BY name").fetchall()]
        assert len(roles) >= 4  # default / cafeonly / guidesonly / nothing
        matrix = _matrix(conn)
        # グループを持つロールの方が多い組み合わせも見る
        conn.execute("INSERT INTO role_collection_access (role, collection) VALUES ('cafeonly', 'zahard')")
        matrix = _matrix(conn)
        checked = 0
        for size in range(len(roles) + 1):
            for combo in itertools.combinations(roles, size):
                user = str(uuid.uuid4())
                for role in combo:
                    conn.execute("INSERT INTO user_roles (user_id, role) VALUES (%s, %s)", (user, role))
                expected = asyncio.run(_app_visible(user))
                assert services.visible_groups(combo, matrix) == expected, combo
                checked += 1
        assert checked == 2 ** len(roles)
    # 割り当てが 1 件も無い利用者と、匿名（None）は default
    assert asyncio.run(_app_visible(str(uuid.uuid4()))) == services.visible_groups([], matrix)
    assert asyncio.run(_app_visible(None)) == services.visible_groups([], matrix)


@needs_db
def test_a_role_missing_from_the_matrix_adds_nothing_like_the_app(seeded):  # noqa: F811
    """グループを 1 つも持たないロール（`nothing`）だけの利用者は、default にならず、何も見えない（アプリも同じ）。"""
    with _admin(seeded) as conn:
        matrix = _matrix(conn)
        user = str(uuid.uuid4())
        conn.execute("INSERT INTO user_roles (user_id, role) VALUES (%s, 'nothing')", (user,))
    assert "nothing" not in matrix
    assert asyncio.run(_app_visible(user)) == frozenset() == services.visible_groups(["nothing"], matrix)
