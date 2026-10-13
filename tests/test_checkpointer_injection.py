"""会話履歴の保存先（checkpointer）を build_graph に差し込めることの検証。

既定はプロセス内メモリ（MemorySaver）のままで、渡した保存先には履歴が書かれ、同じ保存先を共有する別の
グラフ（再起動後のプロセスや別インスタンスに相当）から続きを読める。DB 版の保存先を入れる前提の継ぎ目。
"""

import asyncio
import uuid

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agents.graph import MockChatModel, build_graph


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _turn(graph, message: str, thread_id: str) -> dict:
    return asyncio.run(
        graph.ainvoke({"messages": [HumanMessage(content=message)], "context": ""}, _config(thread_id))
    )


def _history(graph, thread_id: str) -> list:
    return list(asyncio.run(graph.aget_state(_config(thread_id))).values.get("messages", []))


def test_default_checkpointer_is_in_memory():
    graph = build_graph(MockChatModel())

    assert isinstance(graph.checkpointer, MemorySaver)


def test_injected_checkpointer_is_used_and_receives_history():
    saver = MemorySaver()
    graph = build_graph(MockChatModel(), checkpointer=saver)
    thread_id = str(uuid.uuid4())

    _turn(graph, "ザーハードの大気について教えて", thread_id)

    assert graph.checkpointer is saver
    stored = saver.get_tuple(_config(thread_id))
    assert stored is not None
    assert len(stored.checkpoint["channel_values"]["messages"]) >= 2


def test_history_is_shared_between_graphs_with_the_same_checkpointer():
    saver = MemorySaver()
    thread_id = str(uuid.uuid4())
    _turn(build_graph(MockChatModel(), checkpointer=saver), "ザーハードの大気について教えて", thread_id)

    # 別のグラフ（再起動後や別インスタンスに相当）が、同じ保存先から続きを読む
    other = build_graph(MockChatModel(), checkpointer=saver)

    assert len(_history(other, thread_id)) >= 2


def test_history_is_not_shared_between_default_checkpointers():
    thread_id = str(uuid.uuid4())
    _turn(build_graph(MockChatModel()), "ザーハードの大気について教えて", thread_id)

    other = build_graph(MockChatModel())

    assert _history(other, thread_id) == []
