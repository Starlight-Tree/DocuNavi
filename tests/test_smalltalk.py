"""挨拶・雑談の判定（app/agents/smalltalk.py）。実際の API・DB は使わない。

この判定は、まだグラフにつないでいない。つなぐ前に、**誤って資料の質問を「雑談」と判定しない**ことを確かめる
（誤判定は、資料の質問が検索されない検索漏れになる）。
"""

import pytest

from app.agents.smalltalk import is_smalltalk


@pytest.mark.parametrize(
    "text",
    [
        "こんにちは",
        "こんにちは！",
        "  こんにちは。 ",
        "こんにちは〜",
        "おはようございます",
        "こんばんは",
        "ありがとう",
        "ありがとうございました！",
        "よろしくお願いします",
        "お疲れ様です",
        "さようなら",
        "ＨＥＬＬＯ",
        "Hello!",
        "thank you",
        "ばいばい",
    ],
)
def test_pure_greetings_are_smalltalk(text: str) -> None:
    assert is_smalltalk(text)


@pytest.mark.parametrize(
    "text",
    [
        # 挨拶に質問が付いたものは、必ず検索する
        "こんにちは、寮の門限は何時ですか？",
        "こんにちは 門限を教えて",
        "ありがとう。ところで門限は？",
        "おはようございます。ザーハードについて教えてください",
        "hello, what is the curfew?",
        # 疑問符が付いたものは質問の可能性がある
        "こんにちは？",
        "ありがとう?",
        # 資料の話題になりうる短い語は、短くても検索する
        "門限",
        "寮",
        "カフェ",
        "ザーハード",
        "営業時間",
        "料金は",
        "教えて",
        "はい",
        "いいえ",
        "それで？",
        "もっと詳しく",
        # 一覧に無い言葉は偽（迷うものは検索する）
        "元気ですか",
        "こんにちはこんにちは",
        "こんにちは。ありがとう",
        "",
        "   ",
        "！！！",
    ],
)
def test_anything_else_is_searched(text: str) -> None:
    assert not is_smalltalk(text)


def test_the_judgement_is_a_whole_match_never_a_prefix_or_a_substring() -> None:
    assert not is_smalltalk("こんにちは門限")
    assert not is_smalltalk("門限こんにちは")
    assert not is_smalltalk("ありがとうの意味")
