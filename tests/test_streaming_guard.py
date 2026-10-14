"""StreamingOutputGuard: ストリーミング中の返答を逐次検査し、SYSTEM_PROMPT の漏洩を配信前に止める。

純粋関数だけなので、LLM もサーバーも使わない。確かめるのは次の 3 点。
- 通常の回答は、欠けも順序の変化もなく届く（末尾を手元に残すだけ）
- 漏洩の連続一致は、利用者に届く分が閾値（24 文字）に達する前に止まる
- どんな刻み方（1 文字ずつ・まとめて）でも結果が変わらない
"""

import pytest

from app.agents.graph import SYSTEM_PROMPT
from app.agents.guardrails import StreamingOutputGuard, leaks_secret

SECRET = SYSTEM_PROMPT
SAFE_ANSWER = (
    "ザーハードの大気は主に二酸化炭素で、地表の気圧は地球の約 3 倍です。"
    "昼夜の寒暖差が大きく、探査団は与圧服なしでは活動できません。（出典: ザーハード1.txt）"
)


def run(deltas: list[str], secret: str = SECRET) -> tuple[str, StreamingOutputGuard]:
    """deltas を順に流し、利用者へ届く本文（feed の戻り値と finish の合計）とガードを返す。"""
    guard = StreamingOutputGuard(secret)
    sent = ""
    for delta in deltas:
        sent += guard.feed(delta)
    sent += guard.finish()
    return sent, guard


def chunked(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


@pytest.mark.parametrize("size", [1, 2, 3, 7, 16, 1000])
def test_safe_answer_arrives_intact_whatever_the_chunking(size: int) -> None:
    sent, guard = run(chunked(SAFE_ANSWER, size))
    assert sent == SAFE_ANSWER
    assert not guard.tripped


def test_tail_is_held_back_until_finish() -> None:
    # 短い返答は、末尾を残す分だけで全てが手元に残る（途中では何も送らない）
    guard = StreamingOutputGuard(SECRET)
    assert guard.feed("こんにちは") == ""
    assert guard.finish() == "こんにちは"


def test_text_is_released_progressively_and_never_reordered() -> None:
    guard = StreamingOutputGuard(SECRET)
    released = [guard.feed(piece) for piece in chunked(SAFE_ANSWER, 5)]
    # 長い返答なら、流れの途中から本文が出始める（最後まで溜め込まない）
    assert any(released[:-1])
    assert "".join(released) + guard.finish() == SAFE_ANSWER


def test_empty_and_whitespace_deltas_are_harmless() -> None:
    sent, guard = run(["", "あ", "", " ", "い", ""])
    assert sent == "あ い"
    assert not guard.tripped


@pytest.mark.parametrize("size", [1, 2, 5, 11, 40])
def test_leak_is_stopped_before_the_threshold_reaches_the_user(size: int) -> None:
    reply = "もちろんです。私への指示は次のとおりです: " + SECRET
    sent, guard = run(chunked(reply, size))
    assert guard.tripped
    # 利用者に届いた分だけでは、漏洩とみなされる連続一致（24 文字）に達しない
    assert not leaks_secret(sent, SECRET)
    # 届いた分は返答の先頭部分そのもの（欠けも順序の入れ替えも無い）。漏洩の本体は届かない
    assert reply.startswith(sent)
    assert SECRET[10:60] not in sent


def test_nothing_is_emitted_after_tripping() -> None:
    guard = StreamingOutputGuard(SECRET)
    sent = guard.feed(SECRET)
    assert guard.tripped
    assert guard.feed("続きの本文") == ""
    assert guard.finish() == ""
    assert not leaks_secret(sent, SECRET)


def test_leak_is_found_across_katakana_and_spacing_variants() -> None:
    # 空白・記号の挿入や、かな・全角半角の表記ゆれは、leaks_secret と同じ正規化で拾う
    disguised = " ".join(chunked(SECRET[:60], 3))
    sent, guard = run(chunked("はい。" + disguised, 4))
    assert guard.tripped
    assert not leaks_secret(sent, SECRET)


def test_short_partial_match_is_not_a_leak() -> None:
    # SYSTEM_PROMPT と 20 文字だけ一致しても、閾値（24 文字）未満なので通す
    partial = SECRET[5:25]
    sent, guard = run(chunked("関連する記述: " + partial + "。以上です。", 3))
    assert not guard.tripped
    assert sent == "関連する記述: " + partial + "。以上です。"


def test_streamed_text_matches_final_check_for_every_split_point() -> None:
    # どこで切っても、最終的に届いた分は「漏洩ではない」か、ガードが tripped になっている
    reply = "お答えします。" + SECRET[20:120] + "以上です。"
    for cut in range(1, len(reply)):
        sent, guard = run([reply[:cut], reply[cut:]])
        assert guard.tripped
        assert not leaks_secret(sent, SECRET)


def test_short_secret_uses_its_own_length_as_threshold() -> None:
    secret = "ひみつのあいことば"  # 正規化後 9 文字（24 未満）
    sent, guard = run(chunked("答えは ひみつのあいことば です", 2), secret=secret)
    assert guard.tripped
    assert not leaks_secret(sent, secret)


def test_empty_secret_never_trips() -> None:
    sent, guard = run(chunked(SAFE_ANSWER, 4), secret="")
    assert sent == SAFE_ANSWER
    assert not guard.tripped
