"""データ用 Worker（workers/api/）の偽物。本物の SQLite（D1 と同じ方言）に `tests/fixtures/d1_schema.sql`（D1 の撤去前のスキーマの写し）を適用して使う。

`/health` `/v1/sql`（複数の文は 1 トランザクション）`/v1/vectors/{upsert,delete,query}`（総当たりのコサイン類似度）を話す。
`httpx.MockTransport` として `D1Pool` / `SyncD1` の `transport=` に渡す。Cloudflare には何も送らない。
"""

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import httpx

MIGRATION = Path(__file__).resolve().parent / "fixtures" / "d1_schema.sql"
TOKEN = "test-token"


class FakeWorker:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(MIGRATION.read_text(encoding="utf-8"))
        self.vectors: dict[str, dict[str, Any]] = {}
        self.sql_log: list[str] = []
        self.fail_next: str | None = None

    # --- テストから使う口 ---
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def run(self, sql: str, params: tuple = ()) -> None:
        self.db.execute(sql, params)

    # --- Worker の挙動 ---
    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        body = json.loads(request.content or b"{}")
        if self.fail_next:
            message, self.fail_next = self.fail_next, None
            return httpx.Response(400, json={"error": message})
        if path == "/v1/sql":
            return self._sql(body["statements"])
        if path == "/v1/vectors/upsert":
            for v in body["vectors"]:
                self.vectors[v["id"]] = {"values": v["values"], "metadata": v.get("metadata", {})}
            return httpx.Response(200, json={"count": len(body["vectors"])})
        if path == "/v1/vectors/delete":
            for i in body["ids"]:
                self.vectors.pop(i, None)
            return httpx.Response(200, json={"count": len(body["ids"])})
        if path == "/v1/vectors/query":
            return self._query(body)
        return httpx.Response(404, json={"error": "not found"})

    def _sql(self, statements: list[dict[str, Any]]) -> httpx.Response:
        results = []
        try:
            self.db.execute("BEGIN")
            for st in statements:
                self.sql_log.append(st["sql"])
                cur = self.db.execute(st["sql"], st.get("params") or [])
                rows = [dict(r) for r in cur.fetchall()] if cur.description else []
                results.append({"rows": rows, "meta": {"changes": cur.rowcount}})
            self.db.execute("COMMIT")
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            return httpx.Response(400, json={"error": str(exc)[:300]})
        return httpx.Response(200, json={"results": results})

    def _query(self, body: dict[str, Any]) -> httpx.Response:
        query, top_k, flt = body["vector"], body["topK"], body.get("filter") or {}

        def keep(meta: dict[str, Any]) -> bool:
            for key, cond in flt.items():
                value = meta.get(key)
                if isinstance(cond, dict):
                    if "$in" in cond and value not in cond["$in"]:
                        return False
                elif value != cond:
                    return False
            return True

        def cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)

        scored = [(i, cosine(query, v["values"])) for i, v in self.vectors.items() if keep(v["metadata"])]
        scored.sort(key=lambda t: -t[1])
        return httpx.Response(200, json={"matches": [{"id": i, "score": s} for i, s in scored[:top_k]]})
