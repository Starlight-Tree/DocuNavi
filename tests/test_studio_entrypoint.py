"""LangSmith Studio の入口（app/agents/studio.py）。実際の API・DB は使わない（検索は記録つきの偽物、LLM は偽物）。

確かめるのは次の点。
- Studio は検索の範囲（ACCESS_SCOPE_KEY）を渡さないが、入口が整えたグラフは全グループで検索する（無いと、いつも空になる）
- Studio が差し込む checkpointer と衝突しないよう、グラフ自身の checkpointer は外してある
- 本番の経路（app/api/routes.py）は、この入口を import しない
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from app.agents import tools
from app.agents.graph import build_graph
from app.agents.studio import prepare_for_studio
from app.scope import Scope
from tests.fakes import FakeSearch
from tests.test_llm_direct_retrieval import RecordingLLM


@pytest.fixture
def scopes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []
    fake = FakeSearch()

    async def spy(query: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("scope"))
        return await fake(query, *args, **kwargs)

    monkeypatch.setattr(tools, "search_chunks_async", spy)
    return seen


def _ask(graph: Any) -> dict:
    # Studio と同じく、config に thread_id だけを渡す（ACCESS_SCOPE_KEY は渡さない）
    state = {"messages": [HumanMessage(content="ザーハードの大気について教えてください")], "context": ""}
    return asyncio.run(graph.ainvoke(state, {"configurable": {"thread_id": "t"}}))


def test_without_the_studio_entrypoint_a_config_without_scope_finds_nothing(scopes: list[Any]) -> None:
    graph = build_graph(RecordingLLM(calls=[]), retrieval="direct")

    _ask(graph)

    # 範囲が無いと、何も見えない範囲（Scope.NONE）で検索する（本物の検索は空を返す。偽の検索は範囲を見ないので、ここでは範囲だけ見る）
    assert len(scopes) == 1 and scopes[0].is_empty


def test_the_studio_entrypoint_searches_all_groups_without_a_scope_in_the_config(scopes: list[Any]) -> None:
    graph = prepare_for_studio(build_graph(RecordingLLM(calls=[]), retrieval="direct"))

    final = _ask(graph)

    assert scopes == [Scope.ALL]
    assert final["hit_count"] == 1 and final["sources"][0]["name"] == "ザーハード1.pdf"


def test_the_studio_entrypoint_drops_the_graphs_own_checkpointer() -> None:
    graph = prepare_for_studio(build_graph(RecordingLLM(calls=[]), retrieval="direct"))

    assert graph.checkpointer is None


def test_the_api_routes_never_import_the_studio_entrypoint() -> None:
    root = Path(__file__).resolve().parent.parent / "app"
    importers = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if p.name != "studio.py" and "agents.studio" in p.read_text(encoding="utf-8")
    ]

    assert importers == []
