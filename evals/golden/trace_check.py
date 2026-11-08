"""結果の JSON と、LangSmith のトレースを突き合わせる（検索語・出典・件数）。API（LLM・埋め込み）は呼ばず、LangSmith を読むだけ。

    python -m evals.golden.trace_check evals/golden/results/baseline.json

`--trace` で流した結果が前提。トレースのルート実行（タグ `golden` とラベル）ごとに、ツール（search_documents）の入力（検索語）と
出力（出典のファイル名）を、結果の JSON の同じターンの記録と比べる。キーは環境変数（`LANGSMITH_*`）から。値は出さない。
取得は `POST /api/v2/runs/query`（runs.query）で一括（cursor で全件。429 は Retry-After と指数バックオフ + ジッターで再試行）。
"""

import argparse
import datetime as dt
import json
import os
import random
import sys
import time
from collections.abc import Callable, Iterator
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx


def tool_sources(output: Any) -> list[str]:
    """ツール（search_documents）の出力から、出典のファイル名を取り出す。"""
    artifact = output.get("artifact") if isinstance(output, dict) else None
    if artifact is None and isinstance(output, dict):
        artifact = (output.get("output") or {}).get("artifact") if isinstance(output.get("output"), dict) else None
    return sorted({str(s.get("name")) for s in artifact or [] if isinstance(s, dict)})


def check(report: dict[str, Any], traces: dict[tuple[str, int], dict[str, Any]]) -> list[str]:
    """traces: (golden_id, rep) → {"queries": [...], "sources": [...]}。食い違いの一覧（空なら一致）。"""
    problems: list[str] = []
    for turn in report["turns"]:
        key = (turn["id"], turn["rep"])
        trace = traces.get(key)
        if trace is None:
            problems.append(f"{key[0]}#{key[1]}: トレースが見つからない")
            continue
        if trace["queries"] != turn["queries"]:
            problems.append(f"{key[0]}#{key[1]}: 検索語が違う（結果 {turn['queries']} / トレース {trace['queries']}）")
        if trace["sources"] != sorted(turn["sources"]):
            problems.append(f"{key[0]}#{key[1]}: 出典が違う（結果 {turn['sources']} / トレース {trace['sources']}）")
    return problems


SELECTS = [
    "ID", "NAME", "RUN_TYPE", "START_TIME", "END_TIME", "TRACE_ID", "TOTAL_TOKENS", "TOTAL_COST", "FIRST_TOKEN_TIME",
    "TAGS", "METADATA", "INPUTS", "OUTPUTS",
]  # fmt: skip
PAGE_SIZE = 100
MAX_RETRIES = 6
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0


def retry_delay(attempt: int, retry_after: str | None, rand: Callable[[], float] = random.random) -> float:
    """429 のあとの待ち時間（秒）。Retry-After（秒、または HTTP の日付）があれば、それより短くしない。
    無ければ、指数バックオフ（1, 2, 4, … 秒。上限 60 秒）に、半分〜全部のジッターを掛ける。"""
    exponential = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt)) * (0.5 + 0.5 * rand())
    if not retry_after:
        return exponential
    try:
        asked = float(retry_after)
    except ValueError:
        try:
            asked = (parsedate_to_datetime(retry_after) - dt.datetime.now(dt.UTC)).total_seconds()
        except (TypeError, ValueError):
            return exponential
    return min(BACKOFF_CAP_S, max(asked, 0.0)) + 0.1 * rand()  # 指定の時刻ちょうどに全員が戻らないよう、少しずらす


class RunsQuery:
    """`POST /api/v2/runs/query`（runs.query）の薄いクライアント。cursor で全件を順に取り、429 は再試行する。

    トレースごとの `GET /runs/:id` はしない。取る項目は `selects` で必要なものだけに絞る。キーは環境変数（`LANGSMITH_API_KEY`）から読み、
    ログにも例外にも出さない。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        tenant_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        headers = {"x-api-key": api_key}
        if tenant_id:
            headers["x-tenant-id"] = tenant_id
        self._http = httpx.Client(
            base_url=base_url.rstrip("/").removesuffix("/api/v1"), headers=headers, transport=transport, timeout=60
        )
        self._sleep = sleep
        self._rand = rand
        self._max_retries = max_retries
        self.requests = 0
        self.retries = 0

    @classmethod
    def from_env(cls) -> "RunsQuery":
        key = os.environ.get("LANGSMITH_API_KEY", "").strip()
        if not key:
            sys.exit("LANGSMITH_API_KEY が未設定です（値は表示しません）。")
        endpoint = os.environ.get("LANGSMITH_ENDPOINT", "").strip() or "https://api.smith.langchain.com"
        return cls(endpoint, key, tenant_id=os.environ.get("LANGSMITH_WORKSPACE_ID", "").strip() or None)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            self.requests += 1
            response = self._http.post("/api/v2/runs/query", json=body)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            if attempt == self._max_retries:
                break
            self.retries += 1
            self._sleep(retry_delay(attempt, response.headers.get("retry-after"), self._rand))
        raise RuntimeError(f"LangSmith が 429（レート制限）を返し続けました（再試行 {self._max_retries} 回）")

    def query(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """body の条件に合う run を、cursor で最後まで順に返す。"""
        body = {"page_size": PAGE_SIZE, "selects": SELECTS, **body}
        while True:
            page = self._post(body)
            yield from page.get("items") or []
            cursor = page.get("next_cursor")
            if not cursor:
                return
            body = {**body, "cursor": cursor}


def _metadata(run: dict[str, Any]) -> dict[str, Any]:
    meta = run.get("metadata") or {}
    return meta if isinstance(meta, dict) else {}


def fetch_traces(
    label: str, since: dt.datetime, client: RunsQuery | None = None, project_ids: list[str] | None = None
) -> dict[tuple[str, int], dict[str, Any]]:
    """ラベルの付いた golden のトレースから、(golden_id, rep) → 検索語・出典 を取る。
    問い合わせは、ルート実行の一覧と、ツール実行の一覧の 2 系列だけ（件数が多ければ cursor で続ける）。"""
    client = client or RunsQuery.from_env()
    if project_ids is None:
        from langsmith import Client

        project_ids = [str(p.id) for p in Client().list_projects(limit=50)]
    common = {"project_ids": project_ids, "min_start_time": since.astimezone(dt.UTC).isoformat()}
    roots: dict[str, tuple[str, int]] = {}
    for run in client.query({**common, "is_root": True, "filter": 'has(tags, "golden")'}):
        meta = _metadata(run)
        if meta.get("label") == label and "golden_id" in meta:
            roots[str(run["trace_id"])] = (str(meta["golden_id"]), int(meta["rep"]))
    by_trace: dict[str, list[dict[str, Any]]] = {}
    if roots:
        for run in client.query({**common, "run_type": "TOOL", "filter": 'eq(name, "search_documents")'}):
            if str(run.get("trace_id")) in roots:
                by_trace.setdefault(str(run["trace_id"]), []).append(run)
    traces: dict[tuple[str, int], dict[str, Any]] = {}
    for trace_id, key in roots.items():
        tools = sorted(by_trace.get(trace_id, []), key=lambda r: str(r.get("start_time")))
        traces[key] = {
            "queries": [str((r.get("inputs") or {}).get("query")) for r in tools],
            "sources": sorted({s for r in tools for s in tool_sources(r.get("outputs") or {})}),
        }
    return traces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("result", type=Path)
    args = parser.parse_args(argv)
    report = json.loads(args.result.read_text("utf-8"))
    created = dt.datetime.fromisoformat(report["created"])
    traces = fetch_traces(report["label"], created - dt.timedelta(hours=2))
    problems = check(report, traces)
    print(f"結果 {len(report['turns'])} ターン / トレース {len(traces)} 件")
    print("\n".join(problems) if problems else "検索語と出典: すべて一致")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
