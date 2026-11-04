"""追質問の検索語の補完（app/agents/followup.py）。純粋な関数だけで、LLM もサーバーも使わない。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents import followup


@pytest.mark.parametrize(
    "question",
    [
        "それはいつ？",
        "営業時間は？",
        "もっと詳しく教えてください、お願いします",
        "その場所の名前は何ですか。教えてください",
        "さらに別の例は？",
    ],
)
def test_dependent_or_short_questions_need_completion(question: str) -> None:
    assert followup.needs_completion(question)


@pytest.mark.parametrize(
    "question",
    [
        "ザーハードの大気の主成分は何ですか？",
        "入寮の申し込み期限はいつまでですか。",
        "みなとコーヒーでは何時から営業していますか",  # 「では」は単独の質問にも出る
        "なぜザーハリアン文明は姿を消したのですか",
    ],
)
def test_self_contained_questions_do_not(question: str) -> None:
    assert not followup.needs_completion(question)


def test_prior_context_collects_earlier_questions_and_the_last_answer() -> None:
    messages = [
        HumanMessage(content="大気について"),
        AIMessage(content="", tool_calls=[{"name": "search_documents", "args": {"query": "q"}, "id": "1"}]),
        ToolMessage(content="結果", tool_call_id="1"),
        AIMessage(content="薄い大気です。"),
        HumanMessage(content="それは何でできている？"),
    ]
    questions, answer = followup.prior_context(messages)
    assert questions == ["大気について"]
    assert answer == "薄い大気です。"  # ツール呼び出しだけの発話・ツールの結果は拾わない


def test_prior_context_skips_redacted_texts() -> None:
    messages = [
        HumanMessage(content="伏せた発言"),
        AIMessage(content="伏せた回答"),
        HumanMessage(content="それは？"),
    ]
    assert followup.prior_context(messages, frozenset({"伏せた発言", "伏せた回答"})) == ([], "")


def test_first_turn_has_no_prior_context() -> None:
    assert followup.prior_context([HumanMessage(content="大気について")]) == ([], "")


def test_rule_query_prepends_the_previous_question() -> None:
    assert followup.rule_query("それはいつ？", ["大気について", "文明について"]) == "文明について それはいつ？"
    assert followup.rule_query("それはいつ？", []) == "それはいつ？"


def test_rewrite_messages_carry_recent_questions_the_answer_head_and_the_question() -> None:
    system, human = followup.rewrite_messages("それは？", ["q1", "q2", "q3", "q4"], "a" * 1000)
    assert system.content == followup.REWRITE_INSTRUCTION
    text = human.content
    assert "q1" not in text and all(q in text for q in ("q2", "q3", "q4"))  # 直近の 3 件だけ
    assert "a" * followup.MAX_ANSWER_CHARS in text and "a" * (followup.MAX_ANSWER_CHARS + 1) not in text
    assert text.rstrip().endswith("最後の質問: それは？")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ザーハードの大気の成分", "ザーハードの大気の成分"),
        ("検索文: ザーハードの大気の成分\n補足です", "ザーハードの大気の成分"),
        ("「ザーハードの大気の成分」", "ザーハードの大気の成分"),
        ("\n\n  ザーハードの大気の成分  \n", "ザーハードの大気の成分"),
        ("", None),
        ("   \n  ", None),
        ("あ" * (followup.MAX_QUERY_CHARS + 1), None),
    ],
)
def test_parse_rewrite(raw: str, expected: str | None) -> None:
    assert followup.parse_rewrite(raw) == expected
