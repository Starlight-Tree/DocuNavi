"""PostgreSQL（Prisma Postgres）への接続。`app/d1.py`（D1 = SQLite を HTTP 越しに呼ぶ）と同じ形の口を、psycopg で実装する。

`DB_BACKEND=pg` のときだけ使う（既定は `d1`。`app/history.py` の `new_pool()` と、ここの `sync_client()` が切り替える）。
環境変数: `DATABASE_URL`（Postgres の接続文字列。アプリはプール側 `pooled.db.prisma.io`、取り込みの CLI は直接 `db.prisma.io`）。値はログに出さない。
`DATABASE_POOL_MAX_SIZE`（1 プロセスの接続プールの上限。既定 3。1 以上の整数。取り込みなどの CLI は `SyncPg` で 1 接続）。

アプリの SQL は SQLite の書き方のまま渡される。`translate()` が、使っている構文だけを Postgres に直す（D1 を外すときに、SQL 自体を直してここを消す）:
`?` → `%s`、`x IN (SELECT value FROM json_each(p))` → `x::text IN (SELECT jsonb_array_elements_text(p::jsonb))`、
`unixepoch() * 1000` → `now()`、`INSERT OR REPLACE / IGNORE` → `ON CONFLICT`、`json_extract(col, '$.k')` → `col ->> 'k'`。
返す行は D1 と同じ形にする（`timestamptz` は unix ミリ秒の整数、`numeric` は int / float）。失敗は `D1Error`（呼び出し側がそのまま捕まえる）。
"""

import json
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.d1 import D1Error

# INSERT OR REPLACE の衝突の対象（主キー）。ここに無い表の INSERT OR REPLACE は未対応（ValueError）
_PRIMARY_KEYS = {
    "checkpoints": ("thread_id", "checkpoint_ns", "checkpoint_id"),
    "checkpoint_writes": ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
}

_JSON_EACH_IN = re.compile(
    r"(\bcoalesce\([^()]*\)|[A-Za-z_][\w.]*)\s+IN\s*\(\s*SELECT\s+value\s+FROM\s+json_each\(\s*(\?|%\(\w+\)s)\s*\)\s*\)",
    re.IGNORECASE,
)
_UNIXEPOCH_AGE = re.compile(r"\(\s*unixepoch\(\)\s*-\s*(\?|%\(\w+\)s)\s*\*\s*86400\s*\)\s*\*\s*1000", re.IGNORECASE)
_UNIXEPOCH_MS = re.compile(r"unixepoch\(\)\s*\*\s*1000", re.IGNORECASE)
_INSERT_OR = re.compile(
    r"\bINSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+(\w+)\s*\(([^)]*)\)(.*)$", re.IGNORECASE | re.DOTALL
)
_JSON_EXTRACT = re.compile(r"json_extract\(\s*(\w+)\s*,\s*'\$\.(\w+)'\s*\)\s*(IS|=)\s*(\?|%\(\w+\)s)", re.IGNORECASE)


# `%(x)s IS NULL` だけの出現は、Postgres が型を決められない（"could not determine data type of parameter"）。text に固定する
_PARAM_IS_NULL = re.compile(r"(%\(\w+\)s|\?)\s+IS\s+(NOT\s+)?NULL", re.IGNORECASE)


def backend() -> str:
    """`DB_BACKEND`（d1 = 既定 / pg）。それ以外の値は、黙って d1 にせず ValueError。"""
    value = os.environ.get("DB_BACKEND", "d1").strip().lower() or "d1"
    if value not in ("d1", "pg"):
        raise ValueError("DB_BACKEND は d1 か pg です")
    return value


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


DEFAULT_POOL_MAX_SIZE = 3


def pool_max_size() -> int:
    """`DATABASE_POOL_MAX_SIZE`（1 プロセスの接続プールの上限。既定 3）。1 以上の整数だけ。それ以外は、黙って既定にせず ValueError。"""
    raw = os.environ.get("DATABASE_POOL_MAX_SIZE", "").strip()
    if not raw:
        return DEFAULT_POOL_MAX_SIZE
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("DATABASE_POOL_MAX_SIZE は 1 以上の整数です") from None
    if value < 1:
        raise ValueError("DATABASE_POOL_MAX_SIZE は 1 以上の整数です")
    return value


def _replace_outside_quotes(sql: str, old: str, new: str) -> str:
    parts = re.split(r"('(?:[^']|'')*')", sql)
    return "".join(p if i % 2 else p.replace(old, new) for i, p in enumerate(parts))


def translate(sql: str, with_params: bool = True) -> str:
    """SQLite の書き方の SQL を、Postgres の書き方に直す（上の docstring の構文だけ）。"""
    out = _JSON_EACH_IN.sub(
        lambda m: f"{m.group(1)}::text IN (SELECT jsonb_array_elements_text({m.group(2)}::jsonb))", sql
    )
    out = _PARAM_IS_NULL.sub(lambda m: f"{m.group(1)}::text IS {m.group(2) or ''}NULL", out)
    out = _UNIXEPOCH_AGE.sub(lambda m: f"(now() - make_interval(secs => {m.group(1)} * 86400))", out)
    out = _UNIXEPOCH_MS.sub("now()", out)
    out = _JSON_EXTRACT.sub(
        lambda m: (
            f"({m.group(1)} ->> '{m.group(2)}') {'IS NOT DISTINCT FROM' if m.group(3).upper() == 'IS' else '='} {m.group(4)}::text"
        ),
        out,
    )
    if (m := _INSERT_OR.search(out)) is not None:
        verb, table, cols_text, rest = m.groups()
        cols = [c.strip() for c in cols_text.split(",")]
        head = f"INSERT INTO {table} ({cols_text}){rest}"
        if verb.upper() == "IGNORE":
            out = out[: m.start()] + head + " ON CONFLICT DO NOTHING"
        else:
            pk = _PRIMARY_KEYS.get(table)
            if pk is None:
                raise ValueError(f"INSERT OR REPLACE は {table} に未対応です")
            sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in pk)
            out = out[: m.start()] + head + f" ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {sets}"
    if with_params:
        out = re.sub(r"%(?!\(|s|%)", "%%", out)  # 引数を渡すとき、リテラルの % は %% にする
    return _replace_outside_quotes(out, "?", "%s")


def _encode(value: Any) -> Any:
    """list / set は JSON の文字列（`json_each` の置き換えが ::jsonb で読む）。bool・None・数値・文字列・bytes はそのまま。"""
    if isinstance(value, (set, frozenset)):
        return json.dumps(sorted(value))
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return value


def _encode_params(params: Mapping[str, Any] | Sequence[Any] | None) -> Mapping[str, Any] | list[Any] | None:
    if params is None:
        return None
    if isinstance(params, Mapping):
        return {k: _encode(v) for k, v in params.items()}
    return [_encode(v) for v in params]


def _plain(value: Any) -> Any:
    """D1 が返す形に揃える（timestamptz は unix ミリ秒、numeric は int / float、memoryview は bytes）。"""
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def _shape(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _plain(v) for k, v in row.items()} for row in rows]


def _fail(exc: BaseException) -> D1Error:
    # 例外の文字列（接続文字列・SQL の値を含みうる）は入れず、型の名前だけ
    return D1Error(f"DB の呼び出しに失敗しました ({type(exc).__name__})")


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _filter_sql(flt: Mapping[str, Any] | None) -> tuple[str, list[Any]]:
    """Vectorize のメタデータフィルター（collection / target / document_id。値は文字列か {"$in": [...]}）を SQL の条件に直す。"""
    clauses, params = [], []
    for key, column, cast in (
        ("collection", "d.collection", "text[]"),
        ("target", "d.target", "text[]"),
        ("document_id", "d.id", "bigint[]"),
    ):
        if not flt or key not in flt:
            continue
        value = flt[key]
        if isinstance(value, Mapping):
            if set(value) != {"$in"}:
                raise ValueError(f"未対応のフィルター: {key}")
            clauses.append(f"{column} = ANY(%s::{cast})")
            params.append(list(value["$in"]))
        else:
            clauses.append(f"{column} = %s")
            params.append(value)
    if flt and set(flt) - {"collection", "target", "document_id"}:
        raise ValueError("未対応のフィルターのキー")
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


_KNN_SQL = (
    "SELECT c.id::text AS id, 1 - (c.embedding <=> %s::vector) AS score FROM chunks c "
    "JOIN documents d ON d.id = c.document_id WHERE c.embedding IS NOT NULL{where} "
    "ORDER BY c.embedding <=> %s::vector LIMIT %s"
)


class Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    async def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, pool: "PgPool") -> None:
        self._pool = pool

    async def execute(self, sql: str, params: Mapping[str, Any] | Sequence[Any] | None = None) -> Cursor:
        return Cursor((await self._pool.batch([(sql, params)]))[0])

    async def batch(
        self, statements: Sequence[tuple[str, Mapping[str, Any] | Sequence[Any] | None]]
    ) -> list[list[dict[str, Any]]]:
        return await self._pool.batch(statements)


class PgPool:
    """`D1Pool` と同じ口（open / close / connection / batch / vectors_query）。`batch` は 1 トランザクション。"""

    def __init__(self, url: str, *, max_size: int = 3) -> None:
        # PgBouncer（transaction モード）越しなので、サーバー側のプリペアドステートメントは使わない（prepare_threshold=None）
        self._pool = AsyncConnectionPool(
            url,
            min_size=1,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 15},
        )

    async def open(self) -> None:
        try:
            await self._pool.open(wait=True, timeout=20)
            async with self._pool.connection() as conn:
                await conn.execute("select 1")
        except Exception as exc:  # noqa: BLE001
            raise D1Error(f"DB に接続できません ({type(exc).__name__})") from None

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        yield Connection(self)

    async def batch(
        self, statements: Sequence[tuple[str, Mapping[str, Any] | Sequence[Any] | None]]
    ) -> list[list[dict[str, Any]]]:
        results: list[list[dict[str, Any]]] = []
        try:
            async with self._pool.connection() as conn, conn.transaction():
                for sql, params in statements:
                    bound = _encode_params(params)
                    cur = await conn.execute(translate(sql, bound is not None), bound)
                    results.append(_shape(await cur.fetchall()) if cur.description is not None else [])
        except psycopg.Error as exc:
            raise _fail(exc) from None
        return results

    async def vectors_query(
        self, vector: Sequence[float], top_k: int, filter: Mapping[str, Any] | None = None
    ) -> list[tuple[str, float]]:
        where, fparams = _filter_sql(filter)
        lit = _vector_literal(vector)
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute(_KNN_SQL.format(where=where), [lit, *fparams, lit, int(top_k)])
                return [(r["id"], float(r["score"])) for r in await cur.fetchall()]
        except psycopg.Error as exc:
            raise _fail(exc) from None


class SyncPg:
    """`SyncD1` と同じ口（batch / execute / vectors_upsert / vectors_delete）。取り込みの CLI・キュー処理用。"""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or database_url()
        if not self._url:
            raise D1Error("DATABASE_URL が設定されていません")
        self._conn: psycopg.Connection | None = None
        self.statements = 0  # 流した SQL の本数（Prisma Postgres の operations の見積り。取り込みの CLI が最後に出す）

    def __enter__(self) -> "SyncPg":
        try:
            self._conn = psycopg.connect(self._url, row_factory=dict_row, prepare_threshold=None, connect_timeout=15)
        except psycopg.Error as exc:
            raise _fail(exc) from None
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _db(self) -> psycopg.Connection:
        if self._conn is None:
            raise D1Error("接続が開いていません")
        return self._conn

    def batch(
        self, statements: Sequence[tuple[str, Mapping[str, Any] | Sequence[Any] | None]]
    ) -> list[list[dict[str, Any]]]:
        results: list[list[dict[str, Any]]] = []
        try:
            with self._db().transaction():
                for sql, params in statements:
                    bound = _encode_params(params)
                    self.statements += 1
                    cur = self._db().execute(translate(sql, bound is not None), bound)
                    results.append(_shape(cur.fetchall()) if cur.description is not None else [])
        except psycopg.Error as exc:
            raise _fail(exc) from None
        return results

    def execute(self, sql: str, params: Mapping[str, Any] | Sequence[Any] | None = None) -> list[dict[str, Any]]:
        return self.batch([(sql, params)])[0]

    def vectors_upsert(self, vectors: Sequence[Mapping[str, Any]]) -> int:
        """`chunks.embedding` に書く（id = チャンク ID）。Vectorize の代わり。"""
        try:
            with self._db().transaction():
                for v in vectors:
                    self.statements += 1
                    self._db().execute(
                        "UPDATE chunks SET embedding = %s::vector WHERE id = %s",
                        [_vector_literal(v["values"]), int(v["id"])],
                    )
        except psycopg.Error as exc:
            raise _fail(exc) from None
        return len(vectors)

    def vectors_delete(self, ids: Sequence[str]) -> int:
        """消えるチャンクの行は別の文が消すので、ここは何もしない（行の削除で埋め込みも消える）。"""
        return len(ids)


def new_pool(url: str) -> PgPool:
    return PgPool(url, max_size=pool_max_size())


def sync_client(d1_class: Any, url: str | None = None) -> Any:
    """`DB_BACKEND` に合わせた同期クライアント（d1 なら渡された `SyncD1`、pg なら `SyncPg`）。`with` で使う。
    D1 のクラスは呼び出し側から受け取る（テストが各 CLI の `SyncD1` を差し替えるため）。"""
    return SyncPg(url) if backend() == "pg" else d1_class(url)
