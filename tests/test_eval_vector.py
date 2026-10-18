"""scripts/eval_vector.py の純粋な部分（分割・融合・しきい値・チャンク単位の採点）。DB・API は使わない。"""

import math

from scripts.eval_retrieval import Question
from scripts.eval_vector import (
    HELD_OUT_RATIO,
    Chunk,
    choose_tau,
    first_gold_rank,
    rrf,
    satisfied,
    split_questions,
)


def _q(qid: str, doc: str | None = "a.pdf", evidence: tuple[str, ...] = ("甲",), mode: str = "any") -> Question:
    if doc is None:
        return Question(id=qid, question="資料外", category="out_of_scope", scope_kind="off_topic")
    return Question(
        id=qid, question="質問", category="factual", expected_doc_ids=(doc,), evidence=evidence, evidence_mode=mode
    )


def _c(cid: int, source: str, content: str, heading: str | None = None) -> Chunk:
    return Chunk(cid, source, cid, heading, content, "text", f"h{cid}", f"i{cid}")


def test_split_is_deterministic_and_stratified() -> None:
    questions = (
        [_q(f"a{i:02d}", "a.pdf") for i in range(7)]
        + [_q(f"b{i:02d}", "b.pdf") for i in range(5)]
        + [_q(f"x{i:02d}", None) for i in range(10)]
    )
    first = split_questions(questions)
    assert first == split_questions(list(reversed(questions)))
    for prefix, n in (("a", 7), ("b", 5), ("x", 10)):
        held = [qid for qid, s in first.items() if qid.startswith(prefix) and s == "held_out"]
        assert len(held) == round(n * HELD_OUT_RATIO)


def test_rrf_prefers_chunks_ranked_by_both_and_breaks_ties_by_id() -> None:
    assert rrf([[3, 1, 2], [2, 3]]) == [3, 2, 1]
    assert rrf([[5], [4]]) == [4, 5]  # 同点は id の小さい方
    assert rrf([[], []]) == []


def test_choose_tau_separates_when_possible_and_prefers_answering_on_ties() -> None:
    # 答えあり: 0.8, 0.7 / 資料外: 0.5, 0.4 -> 0.5 と 0.7 の中点で完全に分かれる
    tau, _ = choose_tau([(True, False, 0.8), (True, False, 0.7), (False, False, 0.5), (False, False, 0.4)])
    assert math.isclose(tau, 0.6)
    # 全部が答えあり: 何も答えなしにしない（-inf）
    tau, _ = choose_tau([(True, False, 0.8), (True, False, 0.7)])
    assert tau == -math.inf
    # BM25 が何か返した質問は、類似度が低くても答えなしにならない
    tau, curve = choose_tau([(True, True, 0.3), (False, False, 0.5)])
    assert tau > 0.5
    assert curve[-1]["answerable_kept"] == 1


def test_satisfied_any_needs_each_expected_file_and_all_needs_every_evidence() -> None:
    chunks = [_c(1, "a.pdf", "甲の話"), _c(2, "a.pdf", "乙の話"), _c(3, "b.pdf", "甲の話")]
    q_any = _q("q1", "a.pdf", ("甲", "丙"))
    assert satisfied(q_any, [chunks[0]])
    assert not satisfied(q_any, [chunks[2]])  # 期待ファイルの外のチャンクは数えない
    q_all = _q("q2", "a.pdf", ("甲", "乙"), mode="all")
    assert not satisfied(q_all, [chunks[0]])
    assert satisfied(q_all, chunks[:2])
    assert not satisfied(_q("q3", None), chunks)


def test_first_gold_rank_and_heading_counts_as_text() -> None:
    chunks = [_c(1, "a.pdf", "無関係"), _c(2, "a.pdf", "本文", heading="甲の節")]
    assert first_gold_rank(_q("q", "a.pdf", ("甲",)), chunks) == 2
    assert first_gold_rank(_q("q", "a.pdf", ("丁",)), chunks) is None
