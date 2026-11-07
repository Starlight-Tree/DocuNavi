"""Supabase（Postgres + pgvector）の資料データを、Cloudflare（D1 + Vectorize + R2）へ移す、一度きりの移行コマンド。

読み取り専用で Supabase を読み（`DATABASE_URL`、`.env` も読む）、データ用 Worker（`D1_API_URL` / `D1_API_TOKEN`）へ書く。
既定は確認だけ（件数を数えるだけで、何も書かない）。`--apply` を付けたときだけ書く。値（接続文字列・トークン・本文）は表示しない。

    python scripts/migrate_supabase_to_d1.py            # 件数の確認だけ
    python scripts/migrate_supabase_to_d1.py --apply    # 書き込む（同じ ID の行は置き換える。やり直せる）

移すもの: 資料グループ・対象・種別の語彙、roles / role_collection_access、documents、chunks（ID をそのまま保つ。Vectorize のベクトル ID が chunks.id）、
ベクトル（Vectorize。資料の collection / target / document_id をメタデータに付ける）、document_images（画像のバイト列は R2 の `hero/<資料>/<ID>.<拡張子>`、
行には R2 のキーだけ）。移さないもの: 会話履歴（捨てる方針）、access_audit、user_roles（0 件）、saas.*（Django）、認証のユーザー（Better Auth で作り直す）。

終了コード: 0 = 完了（--apply では件数の照合まで一致）、1 = 失敗・不一致、2 = 設定の不足。
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from app.d1 import MAX_BATCH, D1Error, SyncD1

SCHEMA = "chatbot"
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
VECTOR_BATCH = 200


def _ms(value: Any) -> int | None:
    return int(value.timestamp() * 1000) if isinstance(value, datetime) else None


def _fetch(conn: Any, sql: str) -> list[dict[str, Any]]:
    return conn.execute(sql).fetchall()


def _insert(table: str, columns: Sequence[str], row: dict[str, Any]) -> tuple[str, list[Any]]:
    marks = ", ".join("?" for _ in columns)
    return f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({marks})", [row[c] for c in columns]


def _flush(db: SyncD1, statements: list[tuple[str, list[Any]]]) -> None:
    for i in range(0, len(statements), MAX_BATCH):
        db.batch(statements[i : i + MAX_BATCH])


def _vocab(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "created_at": _ms(r["created_at"]), "updated_at": _ms(r["updated_at"])} for r in rows]


def _migrate_images(db: SyncD1, images: list[dict[str, Any]]) -> None:
    from app.r2_store import default_store

    store = default_store()
    image_cols = [
        "id", "document_id", "kind", "status", "mime_type", "r2_key", "byte_size", "width", "height",
        "document_hash", "prompt_sha256", "model", "created_at", "approved_at",
    ]  # fmt: skip
    rows = []
    for img in images:
        key = f"hero/{img['document_id']}/{img['id']}.{EXTENSIONS[img['mime_type']]}"
        data = bytes(img["data"])
        store.put(key, data, content_type=img["mime_type"])
        rows.append({**img, "r2_key": key, "byte_size": len(data)})
    _flush(db, [_insert("document_images", image_cols, r) for r in rows])
    print(f"画像を R2 と D1 へ書きました: {len(rows)} 件")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supabase → Cloudflare（D1 + Vectorize + R2）の一度きりの移行")
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（無ければ件数を数えるだけ）")
    parser.add_argument("--skip-vectors", action="store_true", help="ベクトルを Vectorize へ送らない（SQL の検証用）")
    parser.add_argument(
        "--images-only",
        action="store_true",
        help="画像だけを R2 と D1 へ書く（documents の INSERT OR REPLACE が chunks を連鎖削除するので、稼働中の D1 にはこちらを使う）",
    )
    parser.add_argument("--skip-images", action="store_true", help="画像を R2 へ送らない")
    args = parser.parse_args(argv)

    load_dotenv()
    source_url = os.environ.get("DATABASE_URL", "").strip()
    if not source_url:
        print("DATABASE_URL（移行元の Supabase）が未設定です。", file=sys.stderr)
        return 2
    if args.apply and not (os.environ.get("D1_API_URL", "").strip() and os.environ.get("D1_API_TOKEN", "").strip()):
        print("--apply には D1_API_URL と D1_API_TOKEN が要ります（未設定です）。", file=sys.stderr)
        return 2

    import psycopg
    from psycopg.rows import dict_row

    try:
        with psycopg.connect(source_url, row_factory=dict_row, connect_timeout=10, prepare_threshold=None) as src:
            src.read_only = True  # この接続では書き込めない
            collections = _vocab(_fetch(src, f"SELECT * FROM {SCHEMA}.document_collections ORDER BY name"))
            targets = _vocab(_fetch(src, f"SELECT * FROM {SCHEMA}.document_targets ORDER BY name"))
            types = _vocab(_fetch(src, f"SELECT * FROM {SCHEMA}.document_types ORDER BY name"))
            roles = _fetch(src, f"SELECT name, label FROM {SCHEMA}.roles ORDER BY name")
            access = _fetch(
                src, f"SELECT role, collection FROM {SCHEMA}.role_collection_access ORDER BY role, collection"
            )
            documents = [
                {**r, "created_at": _ms(r["created_at"]), "updated_at": _ms(r["updated_at"])}
                for r in _fetch(src, f"SELECT * FROM {SCHEMA}.documents ORDER BY id")
            ]
            chunks = [
                {**r, "created_at": _ms(r["created_at"])}
                for r in _fetch(
                    src,
                    "SELECT id, document_id, chunk_index, page, page_end, kind, heading, content, content_hash, created_at, "
                    "embedding_model, embedding_dimensions, embedding_input_hash, embedding::text AS embedding "
                    f"FROM {SCHEMA}.chunks ORDER BY id",
                )
            ]
            images = [
                {**r, "created_at": _ms(r["created_at"]), "approved_at": _ms(r["approved_at"])}
                for r in _fetch(src, f"SELECT * FROM {SCHEMA}.document_images ORDER BY id")
            ]
    except psycopg.Error as exc:
        print(f"移行元の読み取りに失敗しました（{type(exc).__name__}）。", file=sys.stderr)
        return 1

    with_vector = [c for c in chunks if c["embedding"] is not None]
    print(
        f"移行元: 語彙 {len(collections)}/{len(targets)}/{len(types)}、roles {len(roles)}、"
        f"資料 {len(documents)}、チャンク {len(chunks)}（ベクトルあり {len(with_vector)}）、画像 {len(images)}"
    )
    if not args.apply:
        print("（確認だけ。何も書いていません。--apply で書き込みます）")
        return 0

    doc_meta = {d["id"]: d for d in documents}
    try:
        with SyncD1() as db:
            if args.images_only:
                _migrate_images(db, images)
                return 0
            statements: list[tuple[str, list[Any]]] = []
            vocab_cols = ["name", "label", "description", "sort_order", "created_at", "updated_at"]
            for table, rows in (
                ("document_collections", collections),
                ("document_targets", targets),
                ("document_types", types),
            ):
                statements += [_insert(table, vocab_cols, r) for r in rows]
            statements += [_insert("roles", ["name", "label"], r) for r in roles]
            statements += [_insert("role_collection_access", ["role", "collection"], r) for r in access]
            doc_cols = [
                "id", "source", "content_hash", "status", "failure_reason", "pages", "title",
                "collection", "target", "doc_type", "created_at", "updated_at",
            ]  # fmt: skip
            statements += [_insert("documents", doc_cols, r) for r in documents]
            _flush(db, statements)
            print("語彙・roles・資料を書きました")

            chunk_cols = [
                "id", "document_id", "chunk_index", "page", "page_end", "kind", "heading", "content", "content_hash",
                "embedding_model", "embedding_dimensions", "embedding_input_hash", "created_at",
            ]  # fmt: skip
            _flush(db, [_insert("chunks", chunk_cols, r) for r in chunks])
            print("チャンクを書きました")

            if not args.skip_vectors:
                sent = 0
                for i in range(0, len(with_vector), VECTOR_BATCH):
                    batch = []
                    for c in with_vector[i : i + VECTOR_BATCH]:
                        meta: dict[str, Any] = {"document_id": c["document_id"]}
                        doc = doc_meta[c["document_id"]]
                        if doc["collection"] is not None:
                            meta["collection"] = doc["collection"]
                        if doc["target"] is not None:
                            meta["target"] = doc["target"]
                        batch.append({"id": str(c["id"]), "values": json.loads(c["embedding"]), "metadata": meta})
                    sent += db.vectors_upsert(batch)
                print(f"ベクトルを Vectorize へ送りました: {sent} 件")

            if images and not args.skip_images:
                _migrate_images(db, images)

            counts = {
                t: db.execute(f"SELECT count(*) AS n FROM {t}")[0]["n"]
                for t in (
                    "document_collections",
                    "document_targets",
                    "document_types",
                    "roles",
                    "role_collection_access",
                    "documents",
                    "chunks",
                )
            }
            hashes = db.execute("SELECT content_hash FROM chunks ORDER BY id")
    except (D1Error, KeyError) as exc:
        print(
            f"書き込みに失敗しました（{type(exc).__name__}: {exc if isinstance(exc, D1Error) else ''}）",
            file=sys.stderr,
        )
        return 1

    expected = {
        "document_collections": len(collections), "document_targets": len(targets), "document_types": len(types),
        "roles": len(roles), "role_collection_access": len(access), "documents": len(documents), "chunks": len(chunks),
    }  # fmt: skip
    mismatched = {t: (counts[t], expected[t]) for t in expected if counts[t] != expected[t]}
    same_hashes = [h["content_hash"] for h in hashes] == [c["content_hash"] for c in chunks]
    if mismatched or not same_hashes:
        print(f"件数またはハッシュが一致しません: {mismatched or 'content_hash'}", file=sys.stderr)
        return 1
    print("照合: 件数と chunks.content_hash が一致しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
