"""検索の重ね（app/search/pipeline.py）の資料の読み取りの SQL を、本物の Postgres で検証する（TEST_DATABASE_URL があるときだけ動く）。

準備と実行例は tests/test_access_postgres.py と同じ（テストごとに専用スキーマ）。pgvector が無いので、`embedding` は text の代用の列にする。
確かめるのは、`ready` の資料だけ・埋め込みのあるチャンクだけ・資料名とチャンクの順の並び・範囲（`allowed`）と `collection` の絞り込み。
ベクトルの距離を引く検索の SQL（pgvector が要る）は、ここでは動かせない。
"""

import asyncio
import os

import psycopg
import pytest

from app import history
from app.scope import Scope
from app.search import pipeline as search_pipeline
from tests.test_history_postgres import db  # noqa: F401 - フィクスチャ

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").strip(), reason="TEST_DATABASE_URL が未設定"
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


@pytest.fixture
def corpus_db(db):  # noqa: F811
    _, schema = db
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'SET search_path = "{schema}"')
        admin.execute(
            "CREATE TABLE documents (id bigserial PRIMARY KEY, source text NOT NULL, collection text, "
            "status text NOT NULL DEFAULT 'ready')"
        )
        admin.execute(
            "CREATE TABLE chunks (id bigserial PRIMARY KEY, document_id bigint NOT NULL, chunk_index int NOT NULL, "
            "page int, page_end int, heading text, content text NOT NULL, embedding text)"
        )
        for source, collection, status in (
            ("b_cafe.pdf", "cafe", "ready"),
            ("a_world.pdf", "zahard", "ready"),
            ("c_pending.pdf", "zahard", "failed"),
            ("d_orphan.pdf", None, "ready"),
        ):
            admin.execute(
                "INSERT INTO documents (source, collection, status) VALUES (%s, %s, %s)", (source, collection, status)
            )
        rows = (
            ("a_world.pdf", 1, 2, "見出し", "世界の本文 2", "v"),
            ("a_world.pdf", 0, 1, None, "世界の本文 1", "v"),
            ("a_world.pdf", 2, 3, None, "埋め込みが無い", None),
            ("b_cafe.pdf", 0, 1, None, "店の本文", "v"),
            ("c_pending.pdf", 0, 1, None, "取り込み中", "v"),
            ("d_orphan.pdf", 0, 1, None, "グループの無い資料", "v"),
        )
        for source, index, page, heading, content, embedding in rows:
            admin.execute(
                "INSERT INTO chunks (document_id, chunk_index, page, heading, content, embedding) "
                "SELECT id, %s, %s, %s, %s, %s FROM documents WHERE source = %s",
                (index, page, heading, content, embedding, source),
            )
    return schema


async def _fetch(scope: Scope, collection: str | None) -> list[search_pipeline.Chunk]:
    async with history.history_store():
        return await search_pipeline._fetch_chunks(scope, collection)


def test_only_ready_documents_and_embedded_chunks_in_document_then_chunk_order(corpus_db):
    chunks = asyncio.run(_fetch(Scope.ALL, None))
    assert [(c.source, c.text) for c in chunks] == [
        ("a_world.pdf", "世界の本文 1"),
        ("a_world.pdf", "見出し\n世界の本文 2"),
        ("b_cafe.pdf", "店の本文"),
        ("d_orphan.pdf", "グループの無い資料"),
    ]
    assert [(c.page, c.collection) for c in chunks][:2] == [(1, "zahard"), (2, "zahard")]


def test_the_scope_and_the_collection_narrow_the_rows(corpus_db):
    only_cafe = asyncio.run(_fetch(Scope.only(["cafe"]), None))
    assert [c.source for c in only_cafe] == ["b_cafe.pdf"]  # グループの無い資料も、範囲を絞ると出ない
    assert asyncio.run(_fetch(Scope.ALL, "zahard")) and {
        c.source for c in asyncio.run(_fetch(Scope.ALL, "zahard"))
    } == {"a_world.pdf"}
    assert asyncio.run(_fetch(Scope.only(["cafe"]), "zahard")) == []  # 範囲の外のグループを指定しても出ない
    assert asyncio.run(_fetch(Scope.NONE, None)) == []
