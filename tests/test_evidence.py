"""追質問の根拠セット（app/agents/evidence.py）。直前の質問の検索結果を、続きの番号で足す部品の単体テスト。"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents import evidence


def _tool(content: str, artifact: list[dict]) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="t1", artifact=artifact)


def _item(name: str, **extra) -> dict:
    return {"name": name, "page": 1, "weak": False, **extra}


def test_split_numbered_returns_bodies_without_headers() -> None:
    content = "[1] a.pdf\n本文A\n二行目\n\n[2] b.pdf\n本文B"
    assert evidence.split_numbered(content, 2) == ["本文A\n二行目", "本文B"]


def test_split_numbered_ignores_bracket_text_inside_a_body() -> None:
    content = "[1] a.pdf\n手順 [2] を参照\n\n[2] b.pdf\n本文B"
    assert evidence.split_numbered(content, 2) == ["手順 [2] を参照", "本文B"]


def test_split_numbered_follows_the_numbering_start() -> None:
    assert evidence.split_numbered("[3] a.pdf\nA\n\n[4] b.pdf\nB", 2) == ["A", "B"]


def test_split_numbered_returns_none_when_the_count_does_not_match() -> None:
    assert evidence.split_numbered("[1] a.pdf\nA", 2) is None
    assert evidence.split_numbered("本文だけ", 1) is None


def test_previous_evidence_takes_the_last_search_of_the_previous_question() -> None:
    history = [
        HumanMessage(content="大気は？"),
        _tool("該当する設定資料は見つかりませんでした。", []),
        _tool("[1] a.pdf\n気温の本文\n\n[2] b.pdf\nBの本文", [_item("a.pdf"), _item("b.pdf")]),
        AIMessage(content="答え"),
        HumanMessage(content="夕方はどう？"),
    ]
    pairs = evidence.previous_evidence(history)
    assert [(item["name"], text) for item, text in pairs] == [("a.pdf", "気温の本文"), ("b.pdf", "Bの本文")]


def test_previous_evidence_skips_weak_items_and_limits_the_count() -> None:
    items = [_item(f"{n}.pdf") for n in range(5)]
    items[1]["weak"] = True
    content = "\n\n".join(f"[{n + 1}] {n}.pdf\n本文{n}" for n in range(5))
    history = [HumanMessage(content="q1"), _tool(content, items), HumanMessage(content="q2")]
    names = [item["name"] for item, _ in evidence.previous_evidence(history)]
    assert names == ["0.pdf", "2.pdf", "3.pdf"]


def test_previous_evidence_is_empty_without_an_earlier_question() -> None:
    assert evidence.previous_evidence([HumanMessage(content="最初の質問")]) == []


def test_previous_evidence_ignores_masked_tool_messages() -> None:
    history = [HumanMessage(content="q1"), _tool("（伏せた結果）", []), HumanMessage(content="q2")]
    assert evidence.previous_evidence(history) == []


def test_previous_evidence_gives_up_when_the_content_shape_is_unexpected() -> None:
    history = [
        HumanMessage(content="q1"),
        _tool("[1] a.pdf\nA", [_item("a.pdf"), _item("b.pdf")]),
        HumanMessage(content="q2"),
    ]
    assert evidence.previous_evidence(history) == []


def test_merge_carried_numbers_after_the_current_results_and_skips_duplicates() -> None:
    current = [{"name": "x.pdf", "_text": "今回の本文"}]
    carried = [(_item("x.pdf"), "今回の本文"), (_item("a.pdf"), "前の本文")]
    content, added = evidence.merge_carried("[1] x.pdf\n今回の本文", current, carried)
    assert content == "[1] x.pdf\n今回の本文\n\n[2] a.pdf\n前の本文"
    assert [item["name"] for item in added] == ["a.pdf"]


def test_merge_carried_numbers_from_one_when_the_current_search_found_nothing() -> None:
    content, added = evidence.merge_carried(
        "該当する設定資料は見つかりませんでした。", [], [(_item("a.pdf"), "前の本文")]
    )
    assert content.endswith("\n\n[1] a.pdf\n前の本文")
    assert len(added) == 1


def test_merge_carried_without_new_items_keeps_the_content() -> None:
    assert evidence.merge_carried("[1] a.pdf\nA", [{"name": "a.pdf", "_text": "A"}], [(_item("a.pdf"), "A")]) == (
        "[1] a.pdf\nA",
        [],
    )
