"""ロール別の資料グループ制御を、本物の Postgres で検証する（TEST_DATABASE_URL があるときだけ動く）。

準備と実行例は tests/test_history_postgres.py と同じ（テストごとに専用スキーマを作って消す）。マイグレーション
（supabase/migrations の 20261025205258 と 20261026045921）の SQL を、専用スキーマに向けて適用してから確かめる。
確かめるのは、見えるグループを引く SQL（ロールなし = default、複数ロール = 和集合、割り当て変更の即時反映）と、資料・グループの
一覧の SQL の絞り込み（`allowed` の配列の渡し方を含む）、API の端から端まで。
検索の SQL（pgvector が要る）は動かせない。絞り込みの条件（`ALLOWED_FILTER_SQL`）は一覧の SQL と共通なので、ここで確かめる。
"""

import asyncio
import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import access, auth
from app.api import collections, documents
from app.main import app
from tests.test_auth import SUPABASE_URL, Clock, JwksServer, Signer
from tests.test_history_postgres import db  # noqa: F401 - フィクスチャ

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").strip(), reason="TEST_DATABASE_URL が未設定"
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"
ROLE_MIGRATION = "20261026045921_role_collection_access.sql"
COLLECTIONS_MIGRATION = "20261025205258_document_collections.sql"

CAFE_USER = str(uuid.uuid4())  # cafe だけのロール
MULTI_USER = str(uuid.uuid4())  # cafe と guides のロール
EMPTY_USER = str(uuid.uuid4())  # 資料グループの無いロール
PLAIN_USER = str(uuid.uuid4())  # ロールなし = default


def _apply_migration(admin: psycopg.Connection, name: str, schema: str) -> None:
    """マイグレーションの SQL を専用スキーマへ適用する（先頭の set role と、スキーマ名 chatbot を差し替える）。"""
    sql = (MIGRATIONS / name).read_text(encoding="utf-8")
    assert sql.startswith("set role postgres;")
    sql = sql.split("\n", 1)[1]
    admin.execute(re.sub(r"\bchatbot\b", schema, sql))


@pytest.fixture
def seeded(db):  # noqa: F811
    """専用スキーマに、ロールの表・資料の表・初期データを作る。(スキーマ名) を返す。"""
    _, schema = db
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'SET search_path = "{schema}"')
        admin.execute(
            "CREATE TABLE documents (id bigserial PRIMARY KEY, source text NOT NULL, title text, collection text, "
            "status text NOT NULL DEFAULT 'ready', failure_reason text, pages int, updated_at timestamptz, "
            "content_hash text)"
        )
        admin.execute(
            "CREATE TABLE chunks (id bigserial PRIMARY KEY, document_id bigint NOT NULL, chunk_index int NOT NULL DEFAULT 0, "
            "page int, page_end int, kind text, heading text, content text NOT NULL DEFAULT '本文', "
            "embedding text, embedding_model text)"
        )
        _apply_migration(admin, COLLECTIONS_MIGRATION, schema)
        _apply_migration(admin, ROLE_MIGRATION, schema)
        for source, collection in (
            ("zahard_world.pdf", "zahard"),
            ("cafe_hours.pdf", "cafe"),
            ("tsubasa_dorm_rules.pdf", "guides"),
            ("orphan.pdf", None),
        ):
            admin.execute("INSERT INTO documents (source, collection) VALUES (%s, %s)", (source, collection))
        admin.execute("INSERT INTO chunks (document_id, embedding) SELECT id, 'v' FROM documents")
        admin.execute(
            "INSERT INTO roles (name, label) VALUES ('cafeonly', '店舗だけ'), ('guidesonly', '規程だけ'), ('nothing', '無し')"
        )
        admin.execute(
            "INSERT INTO role_collection_access (role, collection) VALUES ('cafeonly', 'cafe'), ('guidesonly', 'guides')"
        )
        admin.execute(
            "INSERT INTO user_roles (user_id, role) VALUES (%s, 'cafeonly'), (%s, 'cafeonly'), (%s, 'guidesonly'), (%s, 'nothing')",
            (CAFE_USER, MULTI_USER, MULTI_USER, EMPTY_USER),
        )
    return schema


def _admin(schema: str) -> psycopg.Connection:
    conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    conn.execute(f'SET search_path = "{schema}"')
    return conn


async def _visible(user_id: str | None) -> frozenset[str]:
    from app import history

    async with history.history_store():
        return await access.visible_collections(user_id)


def test_visible_collections_follow_the_roles_and_the_default_role(seeded):
    assert asyncio.run(_visible(PLAIN_USER)) == {"zahard", "cafe", "guides"}  # ロールなし = default
    assert asyncio.run(_visible(None)) == {"zahard", "cafe", "guides"}  # 匿名も default
    assert asyncio.run(_visible(CAFE_USER)) == {"cafe"}
    assert asyncio.run(_visible(MULTI_USER)) == {"cafe", "guides"}  # 複数のロールは和集合（default は足さない）
    assert asyncio.run(_visible(EMPTY_USER)) == frozenset()  # ロールはあるが資料グループが無い


def test_a_change_of_roles_applies_to_the_next_lookup_with_no_cache(seeded):
    assert asyncio.run(_visible(PLAIN_USER)) == {"zahard", "cafe", "guides"}
    with _admin(seeded) as admin:
        admin.execute("INSERT INTO user_roles (user_id, role) VALUES (%s, 'guidesonly')", (PLAIN_USER,))
    assert asyncio.run(_visible(PLAIN_USER)) == {"guides"}  # default ではなくなる
    with _admin(seeded) as admin:
        admin.execute("DELETE FROM user_roles WHERE user_id = %s", (PLAIN_USER,))
        admin.execute("DELETE FROM role_collection_access WHERE role = 'default' AND collection = 'zahard'")
    assert asyncio.run(_visible(PLAIN_USER)) == {"cafe", "guides"}
    with _admin(seeded) as admin:  # 変更は監査表に残る
        rows = admin.execute("SELECT table_name, op FROM access_audit ORDER BY id DESC LIMIT 3").fetchall()
    assert rows == [("role_collection_access", "DELETE"), ("user_roles", "DELETE"), ("user_roles", "INSERT")]


def test_the_list_sqls_filter_by_the_allowed_array(seeded):
    with _admin(seeded) as admin:

        def listed(allowed):
            rows = admin.execute(documents._LIST_SQL.replace("%(limit)s", "100"), {"allowed": allowed}).fetchall()
            return sorted(r[1] for r in rows)  # source

        assert listed(None) == ["cafe_hours.pdf", "orphan.pdf", "tsubasa_dorm_rules.pdf", "zahard_world.pdf"]
        assert listed(["cafe"]) == ["cafe_hours.pdf"]  # グループの無い資料は、範囲を絞ると含まれない
        assert listed(["cafe", "guides"]) == ["cafe_hours.pdf", "tsubasa_dorm_rules.pdf"]
        assert listed([]) == []

        def groups(allowed):
            rows = admin.execute(collections._LIST_SQL.replace("%(limit)s", "100"), {"allowed": allowed}).fetchall()
            return sorted(r[0] for r in rows)  # name

        assert groups(None) == ["cafe", "guides", "zahard"]
        assert groups(["cafe"]) == ["cafe"]
        assert groups([]) == []


@pytest.fixture
def signer() -> Signer:
    return Signer()


@pytest.fixture
def required(monkeypatch, signer) -> None:
    monkeypatch.setattr(auth, "fetch_jwks_document", JwksServer(signer))
    monkeypatch.setattr(auth, "_cache", None)
    monkeypatch.setattr(auth, "_now", Clock())
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)


def _bearer(signer: Signer, sub: str) -> dict:
    return {"Authorization": f"Bearer {signer.token(sub=sub)}"}


def test_enforce_end_to_end_with_real_roles(seeded, required, signer, monkeypatch):
    monkeypatch.setenv("ACCESS_MODE", "enforce")
    plain, cafe, multi, empty = (_bearer(signer, u) for u in (PLAIN_USER, CAFE_USER, MULTI_USER, EMPTY_USER))
    with _admin(seeded) as admin:
        ids = {s: i for i, s in admin.execute("SELECT id, source FROM documents").fetchall()}

    with TestClient(app) as client:  # 起動時の検査（ロールの表と default ロール）も通る

        def names(path, key, headers):
            res = client.get(path, headers=headers)
            assert res.status_code == 200, (path, res.text)
            return sorted(item["source" if key == "documents" else "name"] for item in res.json()[key])

        # ロールなしは default（3 グループ。グループの無い資料は含まれない）
        assert names("/documents", "documents", plain) == [
            "cafe_hours.pdf",
            "tsubasa_dorm_rules.pdf",
            "zahard_world.pdf",
        ]
        assert names("/collections", "collections", plain) == ["cafe", "guides", "zahard"]
        assert names("/documents", "documents", cafe) == ["cafe_hours.pdf"]
        assert names("/collections", "collections", cafe) == ["cafe"]
        assert names("/documents", "documents", multi) == ["cafe_hours.pdf", "tsubasa_dorm_rules.pdf"]

        # 見えない資料は存在しないのと同じ 404
        zahard = ids["zahard_world.pdf"]
        assert client.get(f"/documents/{zahard}/chunks", headers=plain).status_code == 200
        hidden = client.get(f"/documents/{zahard}/chunks", headers=cafe)
        missing = client.get("/documents/99999/chunks", headers=cafe)
        assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json()) == (404, hidden.json())
        assert client.get(f"/documents/{ids['orphan.pdf']}/chunks", headers=cafe).status_code == 404

        # 資料グループの無いロールは 403
        for path in ("/documents", "/collections", f"/documents/{zahard}/chunks"):
            res = client.get(path, headers=empty)
            assert (res.status_code, res.json()) == (403, {"detail": access.DETAIL_NO_COLLECTIONS}), path
        res = client.post("/chat", json={"message": "こんにちは", "collection": "zahard"}, headers=cafe)
        assert (res.status_code, res.json()) == (403, {"detail": access.DETAIL_FORBIDDEN_COLLECTION})
        assert client.post("/chat", json={"message": "こんにちは"}, headers=empty).status_code == 403

        # 割り当てを変えると、次のリクエストから変わる（再起動もキャッシュの失効も要らない）
        with _admin(seeded) as admin:
            admin.execute("INSERT INTO role_collection_access (role, collection) VALUES ('cafeonly', 'zahard')")
        assert names("/collections", "collections", cafe) == ["cafe", "zahard"]


def test_startup_fails_when_the_role_tables_are_missing(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ACCESS_MODE", "enforce")
    with pytest.raises(access.AccessConfigError, match="20261026045921"), TestClient(app):
        pass
