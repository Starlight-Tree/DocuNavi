"""資料の分類（対象 `target`・資料種別 `doc_type`）を、本物の Postgres で検証する（TEST_DATABASE_URL があるときだけ動く）。

準備と実行例は tests/test_access_postgres.py と同じ（テストごとに専用スキーマ）。pgvector は、一時 Postgres に入っていれば本物の SQL を
動かす（入っていなければ検索のテストだけスキップ。手順は docs/development.md の「テスト」節）。

確かめること:
- M1（supabase/migrations/20261103150000）: 再実行しても安全、資料の行と updated_at を変えない、名前の変更と未登録の名前を DB が弾く
- バックフィル（supabase/backfills/20261103150000）: 6 件に入る、再実行は何もしない、前提が合わない・手動の分類がある・部分的な分類は例外で止まり、何も変えない
- 検索（本物の SQL）: 対象を指定すると別の対象が混ざらない、権限の範囲との積集合、ベクトルと BM25 の両経路、キャッシュ経由でも混ざらない
  （SQL で分類を変えたら次の検索から効く）、対象を指定しない検索は従来どおり
- API（enforce・実際のロール）: `/targets` `/doc-types` が見える資料のある分類だけを見える件数で返す、チャットの対象の固定と 409・422・403
"""

import asyncio
import os
import re
import uuid
from datetime import UTC
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import history
from app.ingestion import embedding
from app.main import app
from app.scope import Scope
from app.search import pipeline as search_pipeline
from app.search import vector as search_vector
from tests.test_access_postgres import _bearer, required, signer  # noqa: F401 - フィクスチャ
from tests.test_history_postgres import db  # noqa: F401 - フィクスチャ

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").strip(), reason="TEST_DATABASE_URL が未設定"
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"
BACKFILL = ROOT / "supabase" / "backfills" / "20261103150000_classify_initial_documents.sql"
COLLECTIONS_MIGRATION = "20261025205258_document_collections.sql"
ROLE_MIGRATION = "20261026045921_role_collection_access.sql"
IMAGES_MIGRATION = "20261101150000_document_images.sql"
CLASSIFICATION_MIGRATION = "20261103150000_document_classification.sql"

# 本番の 6 件と同じ id・source・collection（バックフィルの前提）
PROD_DOCS = (
    (1, "cafe_store_guide.pdf", "cafe"),
    (2, "aoba_aw820_manual.pdf", "guides"),
    (3, "tsubasa_dorm_rules.pdf", "guides"),
    (4, "ザーハード1.pdf", "zahard"),
    (5, "ザーハード2.pdf", "zahard"),
    (6, "ザーハード3.pdf", "zahard"),
)
EXPECTED = {
    1: ("minato-coffee", "store-guide"),
    2: ("aoba-aw820", "manual"),
    3: ("tsubasa-dorm", "regulation"),
    4: ("zahard", "setting"),
    5: ("zahard", "setting"),
    6: ("zahard", "setting"),
}
FIXED_TIME = "2026-01-02 03:04:05+00"


def _substituted(sql: str, schema: str) -> str:
    return re.sub(r"\bchatbot\b", schema, sql)


def _apply_migration(admin: psycopg.Connection, name: str, schema: str) -> None:
    sql = (MIGRATIONS / name).read_text(encoding="utf-8")
    assert sql.startswith("set role postgres;")
    admin.execute(_substituted(sql.split("\n", 1)[1], schema))


def _run_backfill(admin: psycopg.Connection, schema: str) -> None:
    admin.execute(_substituted(BACKFILL.read_text(encoding="utf-8"), schema))


def _admin(schema: str) -> psycopg.Connection:
    conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    conn.execute(f'SET search_path = "{schema}", extensions')
    return conn


def _create_documents_table(admin: psycopg.Connection) -> None:
    admin.execute(
        "CREATE TABLE documents (id bigserial PRIMARY KEY, source text NOT NULL, title text, collection text, "
        "status text NOT NULL DEFAULT 'ready', failure_reason text, pages int, updated_at timestamptz, "
        "content_hash text)"
    )


def _seed_prod_documents(admin: psycopg.Connection) -> None:
    for doc_id, source, collection in PROD_DOCS:
        admin.execute(
            "INSERT INTO documents (id, source, collection, updated_at) VALUES (%s, %s, %s, %s)",
            (doc_id, source, collection, FIXED_TIME),
        )


@pytest.fixture
def bare(db):  # noqa: F811
    """documents の表だけがあり、M1 はまだ適用していない専用スキーマ。(スキーマ名) を返す。"""
    _, schema = db
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'SET search_path = "{schema}"')
        _create_documents_table(admin)
    return schema


@pytest.fixture
def classified_schema(bare):
    """M1 を適用し、本番と同じ 6 件の資料（未分類）を入れた専用スキーマ。"""
    with _admin(bare) as admin:
        _apply_migration(admin, CLASSIFICATION_MIGRATION, bare)
        _seed_prod_documents(admin)
    return bare


def _rows(schema: str) -> dict[int, tuple]:
    with _admin(schema) as admin:
        found = admin.execute("SELECT id, source, collection, target, doc_type, updated_at FROM documents").fetchall()
    return {row[0]: row[1:] for row in found}


# ---------------------------------------------------------------- M1


def test_m1_adds_columns_and_vocabulary_without_touching_documents_and_is_rerunnable(bare):
    with _admin(bare) as admin:
        _seed_prod_documents(admin)
        before = admin.execute("SELECT id, source, collection, updated_at FROM documents ORDER BY id").fetchall()
        _apply_migration(admin, CLASSIFICATION_MIGRATION, bare)
        _apply_migration(admin, CLASSIFICATION_MIGRATION, bare)  # 再実行しても安全
        after = admin.execute("SELECT id, source, collection, updated_at FROM documents ORDER BY id").fetchall()
        assert after == before  # 資料の行も、updated_at も変わらない
        assert admin.execute(
            "SELECT count(*) FROM documents WHERE target IS NOT NULL OR doc_type IS NOT NULL"
        ).fetchone() == (0,)
        assert admin.execute("SELECT name FROM document_targets ORDER BY sort_order").fetchall() == [
            ("zahard",),
            ("minato-coffee",),
            ("tsubasa-dorm",),
            ("aoba-aw820",),
        ]
        assert admin.execute("SELECT name FROM document_types ORDER BY sort_order").fetchall() == [
            ("setting",),
            ("store-guide",),
            ("regulation",),
            ("manual",),
        ]


def test_the_database_rejects_renames_unknown_names_and_deleting_a_used_target(classified_schema):
    with _admin(classified_schema) as admin:
        with pytest.raises(psycopg.errors.RaiseException):
            admin.execute("UPDATE document_targets SET name = 'renamed' WHERE name = 'zahard'")
        with pytest.raises(psycopg.errors.RaiseException):
            admin.execute("UPDATE document_types SET name = 'renamed' WHERE name = 'manual'")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            admin.execute("UPDATE documents SET target = 'no-such-target' WHERE id = 1")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            admin.execute("UPDATE documents SET doc_type = 'no-such-type' WHERE id = 1")
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute("INSERT INTO document_targets (name, label) VALUES ('Bad Name', 'x')")
        admin.execute("UPDATE documents SET target = 'zahard' WHERE id = 4")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            admin.execute("DELETE FROM document_targets WHERE name = 'zahard'")  # 使用中は消せない
        admin.execute("UPDATE document_targets SET label = '別の表示名' WHERE name = 'zahard'")  # 表示名は変えてよい
        assert admin.execute("SELECT label FROM document_targets WHERE name = 'zahard'").fetchone() == ("別の表示名",)


# ---------------------------------------------------------------- バックフィル


def test_backfill_classifies_the_six_documents_keeps_updated_at_and_is_idempotent(classified_schema):
    with _admin(classified_schema) as admin:
        admin.execute(
            "INSERT INTO documents (id, source, collection, updated_at) VALUES (7, 'later.pdf', 'zahard', %s)",
            (FIXED_TIME,),
        )
        _run_backfill(admin, classified_schema)
        _run_backfill(admin, classified_schema)  # 再実行は何もしない（エラーにもならない）
    rows = _rows(classified_schema)
    for doc_id, (target, doc_type) in EXPECTED.items():
        assert rows[doc_id][2:4] == (target, doc_type)
    assert rows[7][2:4] == (None, None)  # 7 件目以降には触れない
    assert {r[4].astimezone(UTC).isoformat() for r in rows.values()} == {
        "2026-01-02T03:04:05+00:00"
    }  # updated_at の意味を変えない
    assert {r[1] for r in rows.values()} == {"cafe", "guides", "zahard"}  # collection も変えない


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("DELETE FROM documents WHERE id = 5", "id=5 の資料がありません"),
        ("UPDATE documents SET source = 'other.pdf' WHERE id = 2", "id=2 が期待と違います"),
        ("UPDATE documents SET collection = 'cafe' WHERE id = 3", "id=3 が期待と違います"),
        ("UPDATE documents SET target = 'minato-coffee', doc_type = 'store-guide' WHERE id = 4", "id=4"),  # 別の分類
        ("UPDATE documents SET target = 'minato-coffee' WHERE id = 1", "id=1"),  # 期待と同じ target だけ（部分的）
        ("UPDATE documents SET doc_type = 'store-guide' WHERE id = 1", "id=1"),  # 期待と同じ doc_type だけ（部分的）
        ("UPDATE documents SET target = 'zahard', doc_type = 'manual' WHERE id = 6", "id=6"),  # 片方だけ違う
    ],
)
def test_backfill_stops_on_a_wrong_premise_or_an_existing_classification_and_changes_nothing(
    classified_schema, setup, message
):
    with _admin(classified_schema) as admin:
        admin.execute(setup)
        before = _rows(classified_schema)
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            _run_backfill(admin, classified_schema)
        assert message in str(excinfo.value)
    assert _rows(classified_schema) == before  # 全部入るか、何も入らないか（途中までは入らない）


def test_backfill_accepts_an_already_classified_state_as_a_noop(classified_schema):
    with _admin(classified_schema) as admin:
        for doc_id, (target, doc_type) in EXPECTED.items():
            admin.execute("UPDATE documents SET target = %s, doc_type = %s WHERE id = %s", (target, doc_type, doc_id))
        before = _rows(classified_schema)
        _run_backfill(admin, classified_schema)
    assert _rows(classified_schema) == before


# ---------------------------------------------------------------- 検索（本物の SQL）


def _unit(axis: int) -> list[float]:
    values = [0.0] * embedding.DIMENSIONS
    values[axis] = 1.0
    return values


COMMON, ORTHOGONAL = _unit(0), _unit(1)


@pytest.fixture
def world(db):  # noqa: F811
    """pgvector 込みの専用スキーマ。本番と同じ 6 件の資料（バックフィル済み）に、チャンクを入れる。

    どの資料も「共通語」の本文と COMMON のベクトル（質問の COMMON と類似度 1.0）を 1 つ持つ。ザーハード3（id 6）と
    店舗案内（id 1）は、型番 ZX-900 の本文を ORTHOGONAL のベクトルで持つ（ベクトルでは拾えず、BM25 の型番の経路だけで拾える）。
    id 7 は、対象のない（未分類）資料。
    """
    _, schema = db
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        try:
            admin.execute("CREATE SCHEMA IF NOT EXISTS extensions")
            admin.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA extensions")
        except psycopg.Error:
            pytest.skip("pgvector が使えない（一時 Postgres に postgresql-17-pgvector を入れる。docs/development.md）")
        admin.execute(f'SET search_path = "{schema}", extensions')
        _create_documents_table(admin)
        admin.execute(
            "CREATE TABLE chunks (id bigserial PRIMARY KEY, document_id bigint NOT NULL, chunk_index int NOT NULL DEFAULT 0, "
            "page int, page_end int, kind text, heading text, content text NOT NULL, "
            "embedding extensions.vector(768), embedding_model text, embedding_dimensions int, embedding_input_hash text)"
        )
        for name in (COLLECTIONS_MIGRATION, ROLE_MIGRATION, IMAGES_MIGRATION, CLASSIFICATION_MIGRATION):
            _apply_migration(admin, name, schema)
        _seed_prod_documents(admin)
        admin.execute(
            "INSERT INTO documents (id, source, collection, updated_at) VALUES (7, 'unclassified.pdf', 'zahard', %s)",
            (FIXED_TIME,),
        )
        _run_backfill(admin, schema)

        def add_chunk(doc_id: int, index: int, text: str, vector: list[float]) -> None:
            admin.execute(
                "INSERT INTO chunks (document_id, chunk_index, page, content, embedding, embedding_model, embedding_dimensions) "
                "VALUES (%s, %s, 1, %s, %s::extensions.vector(768), %s, %s)",
                (doc_id, index, text, embedding.vector_literal(vector), embedding.MODEL, embedding.DIMENSIONS),
            )

        for doc_id, source, _ in (*PROD_DOCS, (7, "unclassified.pdf", "zahard")):
            add_chunk(doc_id, 0, f"共通語 {source}", COMMON)
        add_chunk(6, 1, "型番 ZX-900 は、ザーハード3の装置です", ORTHOGONAL)
        add_chunk(1, 1, "型番 ZX-900 は、店舗のレジです", ORTHOGONAL)
    return schema


@pytest.fixture
def search_env(monkeypatch):
    """質問の埋め込みを、決めたベクトルにする（外部 API を呼ばない）。ハイブリッド・リランク・GraphRAG は on。"""
    for name in ("SEARCH_HYBRID", "SEARCH_RERANK", "SEARCH_GRAPH"):
        monkeypatch.setenv(name, "on")
    monkeypatch.delenv("SEARCH_CORPUS_TTL_SECONDS", raising=False)
    search_pipeline.clear_cache()

    async def fake_embed(query: str) -> list[float]:
        return ORTHOGONAL if "ZX-900" in query else COMMON

    monkeypatch.setattr(search_vector, "embed_query", fake_embed)
    fetches: list[tuple] = []
    real_fetch = search_pipeline._fetch_chunks

    async def counting_fetch(scope, collection, *rest):
        fetches.append((scope.sql_names(), collection, *rest))
        return await real_fetch(scope, collection, *rest)

    monkeypatch.setattr(search_pipeline, "_fetch_chunks", counting_fetch)
    yield fetches
    search_pipeline.clear_cache()


def _search(
    query: str,
    *,
    scope: Scope = Scope.ALL,
    target: str | None = None,
    collection: str | None = None,
    document_ids: list[int] | None = None,
    top_k: int = 20,
):
    async def scenario():
        async with history.history_store():
            kwargs: dict = {} if target is None else {"target": target}
            if document_ids is not None:
                kwargs["document_ids"] = document_ids
            return await search_pipeline.search(query, top_k, collection, scope=scope, **kwargs)

    return asyncio.run(scenario())


def _sources(hits) -> set[str]:
    return {hit.source for hit in hits}


ZAHARD = {"ザーハード1.pdf", "ザーハード2.pdf", "ザーハード3.pdf"}


@pytest.mark.parametrize("flags", ["on", "off"])
def test_a_target_never_lets_another_targets_documents_in(world, search_env, monkeypatch, flags):
    for name in ("SEARCH_HYBRID", "SEARCH_RERANK", "SEARCH_GRAPH"):
        monkeypatch.setenv(name, flags)  # off = ベクトル検索だけの経路（pipeline が vector.search をそのまま呼ぶ）
    everything = ZAHARD | {
        "cafe_store_guide.pdf",
        "aoba_aw820_manual.pdf",
        "tsubasa_dorm_rules.pdf",
        "unclassified.pdf",
    }
    assert _sources(_search("共通語")) == everything  # 対象を指定しない検索は従来どおり（未分類の資料も入る）
    assert _sources(_search("共通語", target="aoba-aw820")) == {"aoba_aw820_manual.pdf"}
    assert _sources(_search("共通語", target="minato-coffee")) == {"cafe_store_guide.pdf"}
    assert _sources(_search("共通語", target="zahard")) == ZAHARD  # 未分類（id 7）は、どの対象を選んでも入らない
    assert _search("共通語", target="no-such-target") == []
    hits = _search("共通語", target="tsubasa-dorm")
    assert [h.target for h in hits] == ["tsubasa-dorm"]  # 返った行の対象も合っている


def test_the_target_is_intersected_with_the_permission_scope(world, search_env):
    guides = Scope.only(["guides"])
    assert _sources(_search("共通語", scope=guides)) == {"aoba_aw820_manual.pdf", "tsubasa_dorm_rules.pdf"}
    assert _sources(_search("共通語", scope=guides, target="tsubasa-dorm")) == {"tsubasa_dorm_rules.pdf"}
    # 見えないグループの資料は、対象で指定しても出ない（対象を選んでも権限は広がらない）
    assert _search("共通語", scope=guides, target="zahard") == []
    assert _search("共通語", scope=guides, target="minato-coffee") == []
    assert _search("共通語", scope=Scope.only(["cafe"]), target="aoba-aw820") == []
    assert _search("共通語", scope=Scope.NONE, target="zahard") == []
    # 旧グループと対象を同時に使うと、積集合（片方を捨てて広げない）
    assert _sources(_search("共通語", collection="guides", target="aoba-aw820")) == {"aoba_aw820_manual.pdf"}
    assert _search("共通語", collection="cafe", target="aoba-aw820") == []


def test_the_keyword_route_respects_the_target_and_the_scope(world, search_env):
    # ZX-900 は、ベクトルでは拾えない（直交）。BM25 の型番の経路だけで拾う
    both = {"ザーハード3.pdf", "cafe_store_guide.pdf"}
    assert _sources(_search("ZX-900")) == both
    assert _sources(_search("ZX-900", target="zahard")) == {"ザーハード3.pdf"}
    assert _sources(_search("ZX-900", target="minato-coffee")) == {"cafe_store_guide.pdf"}
    assert _search("ZX-900", target="aoba-aw820") == []
    assert _sources(_search("ZX-900", scope=Scope.only(["cafe"]))) == {"cafe_store_guide.pdf"}
    assert _search("ZX-900", scope=Scope.only(["cafe"]), target="zahard") == []  # 権限との積集合


def test_the_cache_follows_a_classification_change_made_by_sql(world, search_env):
    fetches = search_env
    assert _sources(_search("ZX-900", target="zahard")) == {"ザーハード3.pdf"}
    assert _sources(_search("ZX-900", target="zahard")) == {"ザーハード3.pdf"}
    assert len(fetches) == 1  # 分類が変わっていなければ、写しを使い回す（毎回は作り直さない）

    with _admin(world) as admin:  # 運用者が SQL で、ザーハード3 を別の対象へ移す
        admin.execute("UPDATE documents SET target = 'minato-coffee' WHERE id = 6")

    assert _search("ZX-900", target="zahard") == []  # TTL（60 秒）を待たずに、次の検索から効く
    assert len(fetches) == 2
    assert _sources(_search("ZX-900", target="minato-coffee")) == {"ザーハード3.pdf", "cafe_store_guide.pdf"}
    assert _sources(_search("共通語", target="zahard")) == {"ザーハード1.pdf", "ザーハード2.pdf"}  # ベクトル側も同じ
    assert "ザーハード3.pdf" in _sources(_search("共通語", target="minato-coffee"))


def test_a_target_that_became_empty_finds_nothing_without_an_error(world, search_env):
    assert _sources(_search("共通語", target="aoba-aw820")) == {"aoba_aw820_manual.pdf"}
    with _admin(world) as admin:
        admin.execute("UPDATE documents SET target = NULL, doc_type = NULL WHERE id = 2")
    assert _search("共通語", target="aoba-aw820") == []
    assert _search("ZX-900", target="aoba-aw820") == []


def test_without_a_target_the_cache_stays_ttl_only_as_before(world, search_env):
    fetches = search_env
    assert "ザーハード3.pdf" in _sources(_search("ZX-900"))
    with _admin(world) as admin:
        admin.execute("UPDATE documents SET target = 'minato-coffee' WHERE id = 6")
    _search("ZX-900")
    assert len(fetches) == 1  # 対象を指定しない検索は、従来どおり TTL だけで更新する（毎回の確認はしない）


@pytest.mark.parametrize("flags", ["on", "off"])
def test_selected_documents_are_the_only_ones_searched_and_the_permission_scope_still_applies(
    world, search_env, monkeypatch, flags
):
    # 資料単位の選択（画面のチェック）。実際の SQL（d.id = ANY(bigint[])）で、選んだ資料だけが入ることを確かめる
    for name in ("SEARCH_HYBRID", "SEARCH_RERANK", "SEARCH_GRAPH"):
        monkeypatch.setenv(name, flags)
    assert _sources(_search("共通語", document_ids=[1, 6])) == {"cafe_store_guide.pdf", "ザーハード3.pdf"}
    assert _sources(_search("共通語", document_ids=[6, 1, 6])) == {
        "cafe_store_guide.pdf",
        "ザーハード3.pdf",
    }  # 並び・重複は無関係
    assert _sources(_search("共通語", document_ids=[7])) == {"unclassified.pdf"}  # 未分類でも、選べば入る
    assert _search("共通語", document_ids=[999]) == []  # 存在しない資料は、エラーではなく 0 件
    assert _search("共通語", document_ids=[]) == []  # 空の選択は何も探さない（全部と読み替えない）
    # 権限の範囲との積集合（選んでも、見えない資料は出ない）
    guides = Scope.only(["guides"])
    assert _sources(_search("共通語", scope=guides, document_ids=[1, 2, 6])) == {"aoba_aw820_manual.pdf"}
    assert _search("共通語", scope=Scope.NONE, document_ids=[1, 2]) == []
    assert _search("共通語", scope=Scope.only(["cafe"]), document_ids=[2, 3]) == []


def test_the_keyword_route_and_the_cache_follow_the_selected_documents(world, search_env):
    fetches = search_env
    assert _sources(_search("ZX-900", document_ids=[6])) == {"ザーハード3.pdf"}
    assert _sources(_search("ZX-900", document_ids=[1])) == {"cafe_store_guide.pdf"}
    assert _search("ZX-900", document_ids=[2]) == []  # 型番の本文を持たない資料だけを選ぶと、キーワード側からも出ない
    assert _sources(_search("ZX-900", document_ids=[1, 6])) == {"ザーハード3.pdf", "cafe_store_guide.pdf"}
    n = len(fetches)
    _search("ZX-900", document_ids=[6, 1])  # 同じ選び方（並びだけ違う）は、写しを使い回す
    assert len(fetches) == n


def test_the_conversation_scope_by_collection_is_unchanged(world, search_env):
    # 旧 collection だけの検索（target なし）は、分類の列があっても従来と同じ結果
    assert _sources(_search("共通語", collection="guides")) == {"aoba_aw820_manual.pdf", "tsubasa_dorm_rules.pdf"}
    assert _sources(_search("共通語", collection="zahard")) == ZAHARD | {"unclassified.pdf"}


# ---------------------------------------------------------------- API（enforce・実際のロール）

CAFE_USER = str(uuid.uuid4())
PLAIN_USER = str(uuid.uuid4())
EMPTY_USER = str(uuid.uuid4())


@pytest.fixture
def api_world(world, monkeypatch):
    monkeypatch.setenv("ACCESS_MODE", "enforce")
    with _admin(world) as admin:
        admin.execute("INSERT INTO roles (name, label) VALUES ('cafeonly', '店舗だけ'), ('nothing', '無し')")
        admin.execute("INSERT INTO role_collection_access (role, collection) VALUES ('cafeonly', 'cafe')")
        admin.execute(
            "INSERT INTO user_roles (user_id, role) VALUES (%s, 'cafeonly'), (%s, 'nothing')", (CAFE_USER, EMPTY_USER)
        )
        admin.execute("UPDATE documents SET status = 'processing' WHERE id = 5")
    return world


def _names(res, key):
    assert res.status_code == 200, res.text
    return {item["name"]: item for item in res.json()[key]}


def test_targets_and_doc_types_list_only_classifications_with_visible_documents(api_world, required, signer):  # noqa: F811
    plain, cafe, empty = (_bearer(signer, u) for u in (PLAIN_USER, CAFE_USER, EMPTY_USER))
    with TestClient(app) as client:
        targets = _names(client.get("/targets", headers=plain), "targets")
        assert set(targets) == {"zahard", "minato-coffee", "tsubasa-dorm", "aoba-aw820"}
        assert (targets["zahard"]["document_count"], targets["zahard"]["ready_count"]) == (
            3,
            2,
        )  # id 7（未分類）は数えない。id 5 は取り込み中なので、準備ができている数には入らない
        assert targets["zahard"]["label"] == "ザーハード"
        types = _names(client.get("/doc-types", headers=plain), "doc_types")
        assert set(types) == {"setting", "store-guide", "regulation", "manual"}

        # cafe だけのロール: 見えない資料の対象・種別・件数は、一覧から漏れない
        assert set(_names(client.get("/targets", headers=cafe), "targets")) == {"minato-coffee"}
        assert set(_names(client.get("/doc-types", headers=cafe), "doc_types")) == {"store-guide"}
        assert client.get("/targets", headers=empty).status_code == 403  # 見えるグループが 0 件

        # 見える資料が 1 件だけ入っている対象は、その 1 件だけを数える（同じ対象の見えない資料は数えない）
        with _admin(api_world) as admin:
            admin.execute("UPDATE documents SET collection = 'cafe' WHERE id = 6")
        cafe_targets = _names(client.get("/targets", headers=cafe), "targets")
        assert set(cafe_targets) == {"minato-coffee", "zahard"}
        assert cafe_targets["zahard"]["document_count"] == 1


def test_documents_carry_target_and_doc_type_and_hide_invisible_ones(api_world, required, signer):  # noqa: F811
    plain, cafe = (_bearer(signer, u) for u in (PLAIN_USER, CAFE_USER))
    with TestClient(app) as client:
        items = {d["id"]: d for d in client.get("/documents", headers=plain).json()["documents"]}
        assert (items[2]["target"], items[2]["doc_type"]) == ("aoba-aw820", "manual")
        assert (items[7]["target"], items[7]["doc_type"]) == (None, None)  # 未分類
        seen = client.get("/documents", headers=cafe).json()["documents"]
        assert [(d["id"], d["target"]) for d in seen] == [(1, "minato-coffee")]


def _chat(client, headers, body):
    return client.post("/chat", json=body, headers=headers)


@pytest.mark.real_search
def test_the_chat_target_is_fixed_per_thread_and_never_widens_the_permission(
    api_world,
    required,
    signer,
    search_env,
    monkeypatch,  # noqa: F811
):
    from app.agents import graph as graph_module
    from app.api import routes
    from tests.fakes import SearchingMockLLM

    monkeypatch.setattr(graph_module, "create_llm", lambda: SearchingMockLLM())
    routes._graph_for.cache_clear()
    plain, cafe = (_bearer(signer, u) for u in (PLAIN_USER, CAFE_USER))
    try:
        with TestClient(app) as client:
            # 両方の指定は 422（新しい会話でも）
            both = _chat(client, plain, {"message": "共通語", "target": "zahard", "collection": "zahard"})
            assert both.status_code == 422

            # 見えない・存在しない対象は、新しい会話では 403（同じ固定文。名前は載せない）
            hidden = _chat(client, cafe, {"message": "共通語", "target": "aoba-aw820"})
            missing = _chat(client, cafe, {"message": "共通語", "target": "no-such-target"})
            assert hidden.status_code == missing.status_code == 403
            assert hidden.json() == missing.json() and "aoba" not in hidden.text

            # 対象を指定した新しい会話: 検索は対象の資料だけ
            first = _chat(client, plain, {"message": "共通語", "target": "aoba-aw820", "thread_id": "t-aoba"})
            assert first.status_code == 200, first.text
            assert first.json()["target"] == "aoba-aw820"
            assert "aoba_aw820_manual.pdf" in first.json()["reply"]
            assert "cafe_store_guide.pdf" not in first.json()["reply"]

            # 会話の対象は、チェックポイントに inline で残る。ロールバックの前に「対象つきの会話が無いか」を数える SQL
            # （docs/configuration.md の「対象と資料種別」）が、この形に依存している
            with _admin(api_world) as admin:
                counted = admin.execute(
                    "SELECT count(DISTINCT thread_id) FROM checkpoints "
                    "WHERE checkpoint->'channel_values'->>'target' IS NOT NULL"
                ).fetchone()
            assert counted == (1,)

            # 続き: 省略は保存された対象のまま（広がらない）。同じ対象は通る。違う指定は 409
            again = _chat(client, plain, {"message": "共通語", "thread_id": "t-aoba"})
            assert again.status_code == 200 and again.json()["target"] == "aoba-aw820"
            assert "cafe_store_guide.pdf" not in again.json()["reply"]
            assert (
                _chat(client, plain, {"message": "共通語", "thread_id": "t-aoba", "target": "aoba-aw820"}).status_code
                == 200
            )
            assert (
                _chat(client, plain, {"message": "共通語", "thread_id": "t-aoba", "target": "zahard"}).status_code
                == 409
            )
            assert (
                _chat(client, plain, {"message": "共通語", "thread_id": "t-aoba", "collection": "guides"}).status_code
                == 409
            )

            # 保存の画面向けの情報
            thread = client.get("/threads/t-aoba/messages", headers=plain).json()
            assert (thread["target"], thread["collection"], thread["scope_unavailable"]) == ("aoba-aw820", None, False)

            # 旧グループの会話: 保存された範囲のまま。対象を足すと 409
            legacy = _chat(client, plain, {"message": "共通語", "collection": "cafe", "thread_id": "t-legacy"})
            assert legacy.status_code == 200 and legacy.json()["collection"] == "cafe"
            keep = _chat(client, plain, {"message": "共通語", "thread_id": "t-legacy"})
            assert keep.status_code == 200 and keep.json()["collection"] == "cafe" and keep.json()["target"] is None
            assert "aoba_aw820_manual.pdf" not in keep.json()["reply"]
            assert (
                _chat(
                    client, plain, {"message": "共通語", "thread_id": "t-legacy", "target": "minato-coffee"}
                ).status_code
                == 409
            )
            legacy_thread = client.get("/threads/t-legacy/messages", headers=plain).json()
            assert (legacy_thread["collection"], legacy_thread["target"]) == ("cafe", None)

            # 対象が空になっても、続きの会話は 200（検索は 0 件）。画面向けには「利用できない」と伝える
            with _admin(api_world) as admin:
                admin.execute("UPDATE documents SET target = NULL, doc_type = NULL WHERE id = 2")
            empty = _chat(client, plain, {"message": "共通語", "thread_id": "t-aoba"})
            assert empty.status_code == 200 and "aoba_aw820_manual.pdf" not in empty.json()["reply"]
            moved = client.get("/threads/t-aoba/messages", headers=plain).json()
            assert (moved["target"], moved["scope_unavailable"]) == (None, True)
            # 空の対象を新しい会話で指定すると 403（存在しない対象と同じ）
            assert _chat(client, plain, {"message": "共通語", "target": "aoba-aw820"}).status_code == 403
    finally:
        routes._graph_for.cache_clear()
