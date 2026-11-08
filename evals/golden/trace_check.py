"""結果の JSON と、LangSmith のトレースを突き合わせる（検索語・出典・件数）。API（LLM・埋め込み）は呼ばず、LangSmith を読むだけ。

    python -m evals.golden.trace_check evals/golden/results/baseline.json

`--trace` で流した結果が前提。トレースのルート実行（タグ `golden` とラベル）ごとに、ツール（search_documents）の入力（検索語）と
出力（出典のファイル名）を、結果の JSON の同じターンの記録と比べる。キーは環境変数（`LANGSMITH_*`）から。値は出さない。
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any


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


def fetch_traces(label: str, since: dt.datetime) -> dict[tuple[str, int], dict[str, Any]]:
    from langsmith import Client

    client = Client()
    traces: dict[tuple[str, int], dict[str, Any]] = {}
    for project in client.list_projects(limit=50):
        for root in client.list_runs(
            project_id=project.id, is_root=True, start_time=since, filter='has(tags, "golden")'
        ):
            meta = (root.extra or {}).get("metadata", {})
            if meta.get("label") != label:
                continue
            tools = [
                r
                for r in client.list_runs(
                    project_id=project.id, trace_id=root.trace_id, run_type="tool", start_time=since
                )
                if r.name == "search_documents"
            ]
            tools.sort(key=lambda r: r.start_time)
            traces[(meta["golden_id"], int(meta["rep"]))] = {
                "queries": [str((r.inputs or {}).get("query")) for r in tools],
                "sources": sorted({s for r in tools for s in tool_sources(r.outputs or {})}),
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
