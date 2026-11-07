"""Cloudflare D1 / Vectorize への HTTP クライアント（データ用 Worker `workers/api/` の `/v1/*` を呼ぶ）。

環境変数: `D1_API_URL`（Worker の URL）と `D1_API_TOKEN`（サービス用のトークン。値はログに出さない）。`D1_API_URL` が空なら DB を使わない
（会話履歴はメモリ、資料の検索はしない。以前の `DATABASE_URL` が空のときと同じ）。

- SQL は SQLite の書き方。`%(name)s` の名前付き引数を `?` に直して送る（同じ名前を何度使ってもよい）。list / set は JSON 文字列で渡す
  （SQL 側は `json_each(%(name)s)` で展開する。None は NULL）。
- 1 つの `batch` は D1 の 1 トランザクション。対話的なトランザクション（読んでから書く）は HTTP 越しにはできない。
- 失敗は `D1Error`。例外の文字列に URL やトークンを入れない（型の名前と HTTP ステータスだけ）。
"""

import json
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_NAMED = re.compile(r"%\((\w+)\)s")
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
MAX_BATCH = 100


class D1Error(RuntimeError):
    """D1 / Vectorize の呼び出しに失敗した（メッセージに URL・トークン・SQL の値は含めない）。"""


def api_url() -> str:
    return os.environ.get("D1_API_URL", "").strip().rstrip("/")


def api_token() -> str:
    return os.environ.get("D1_API_TOKEN", "").strip()


def configured() -> bool:
    return bool(api_url())


def _encode(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return json.dumps(sorted(value))
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    if isinstance(value, bool):
        return int(value)
    return value


def convert(sql: str, params: Mapping[str, Any] | Sequence[Any] | None) -> tuple[str, list[Any]]:
    """`%(name)s` を `?` に直し、出現順の値の並びを返す。位置引数（`?` のまま）の並びはそのまま通す。"""
    if params is None:
        return sql, []
    if not isinstance(params, Mapping):
        return sql, [_encode(v) for v in params]
    values: list[Any] = []

    def sub(match: re.Match[str]) -> str:
        values.append(_encode(params[match.group(1)]))
        return "?"

    return _NAMED.sub(sub, sql), values


def _headers() -> dict[str, str]:
    token = api_token()
    if not token:
        raise D1Error("D1_API_TOKEN が設定されていません")
    return {"authorization": f"Bearer {token}", "content-type": "application/json"}


def _raise(response: httpx.Response) -> None:
    if response.status_code >= 400:
        # Worker のエラー本文は固定の短い文（SQL の値は含まない）だが、念のため先頭だけ
        detail = ""
        try:
            detail = str(response.json().get("error", ""))[:120]
        except Exception:  # noqa: BLE001
            pass
        raise D1Error(f"D1 API が {response.status_code} を返しました: {detail}")


class Cursor:
    """psycopg のカーソルに似た読み取り口（`fetchall` / `fetchone`）。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    async def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, pool: "D1Pool") -> None:
        self._pool = pool

    async def execute(self, sql: str, params: Mapping[str, Any] | Sequence[Any] | None = None) -> Cursor:
        return Cursor((await self._pool.batch([(sql, params)]))[0])

    async def batch(
        self, statements: Sequence[tuple[str, Mapping[str, Any] | Sequence[Any] | None]]
    ) -> list[list[dict[str, Any]]]:
        return await self._pool.batch(statements)


class D1Pool:
    """Worker への HTTP 接続（`httpx.AsyncClient`）。`psycopg_pool` の代わりに `get_pool()` が返す。"""

    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None) -> None:
        # transport はテスト用（偽の Worker を差し込む）
        self._client = httpx.AsyncClient(
            base_url=url, timeout=_TIMEOUT, limits=httpx.Limits(max_connections=20), transport=transport
        )

    async def open(self) -> None:
        try:
            response = await self._client.get("/health")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise D1Error(f"データ用 Worker に接続できません ({type(exc).__name__})") from None

    async def close(self) -> None:
        await self._client.aclose()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        yield Connection(self)

    async def batch(
        self, statements: Sequence[tuple[str, Mapping[str, Any] | Sequence[Any] | None]]
    ) -> list[list[dict[str, Any]]]:
        payload = []
        for sql, params in statements:
            text, values = convert(sql, params)
            payload.append({"sql": text, "params": values})
        try:
            response = await self._client.post("/v1/sql", headers=_headers(), json={"statements": payload})
        except httpx.HTTPError as exc:
            raise D1Error(f"D1 API の呼び出しに失敗しました ({type(exc).__name__})") from None
        _raise(response)
        return [r["rows"] for r in response.json()["results"]]

    async def vectors_query(
        self, vector: Sequence[float], top_k: int, filter: Mapping[str, Any] | None = None
    ) -> list[tuple[str, float]]:
        body: dict[str, Any] = {"vector": list(vector), "topK": top_k}
        if filter:
            body["filter"] = filter
        try:
            response = await self._client.post("/v1/vectors/query", headers=_headers(), json=body)
        except httpx.HTTPError as exc:
            raise D1Error(f"Vectorize の呼び出しに失敗しました ({type(exc).__name__})") from None
        _raise(response)
        return [(m["id"], float(m["score"])) for m in response.json()["matches"]]


class SyncD1:
    """同期版（取り込みの CLI・キュー処理用）。対話的なトランザクションは無く、`batch` が 1 トランザクション。"""

    def __init__(self, url: str | None = None, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(base_url=url or api_url(), timeout=_TIMEOUT, transport=transport)

    def __enter__(self) -> "SyncD1":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client.close()

    def batch(
        self, statements: Sequence[tuple[str, Mapping[str, Any] | Sequence[Any] | None]]
    ) -> list[list[dict[str, Any]]]:
        payload = []
        for sql, params in statements:
            text, values = convert(sql, params)
            payload.append({"sql": text, "params": values})
        try:
            response = self._client.post("/v1/sql", headers=_headers(), json={"statements": payload})
        except httpx.HTTPError as exc:
            raise D1Error(f"D1 API の呼び出しに失敗しました ({type(exc).__name__})") from None
        _raise(response)
        return [r["rows"] for r in response.json()["results"]]

    def execute(self, sql: str, params: Mapping[str, Any] | Sequence[Any] | None = None) -> list[dict[str, Any]]:
        return self.batch([(sql, params)])[0]

    def vectors_upsert(self, vectors: Sequence[Mapping[str, Any]]) -> int:
        """vectors: {"id": str, "values": list[float], "metadata": {...}}。500 件までずつ送る。"""
        sent = 0
        for i in range(0, len(vectors), 500):
            part = list(vectors[i : i + 500])
            try:
                response = self._client.post("/v1/vectors/upsert", headers=_headers(), json={"vectors": part})
            except httpx.HTTPError as exc:
                raise D1Error(f"Vectorize の呼び出しに失敗しました ({type(exc).__name__})") from None
            _raise(response)
            sent += len(part)
        return sent

    def vectors_delete(self, ids: Sequence[str]) -> int:
        done = 0
        for i in range(0, len(ids), 1000):
            part = list(ids[i : i + 1000])
            try:
                response = self._client.post("/v1/vectors/delete", headers=_headers(), json={"ids": part})
            except httpx.HTTPError as exc:
                raise D1Error(f"Vectorize の呼び出しに失敗しました ({type(exc).__name__})") from None
            _raise(response)
            done += len(part)
        return done
