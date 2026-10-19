"""scripts/eval_topk.py の数え方。DB・API は使わない。"""

from scripts.eval_retrieval import Question
from scripts.eval_topk import chunk_sizes, miss_reasons, sweep_k, sweep_threshold
from scripts.eval_vector import Chunk


def _c(cid: int, source: str, content: str) -> Chunk:
    return Chunk(cid, source, cid, None, content, "text", f"h{cid}", f"i{cid}")


def _q(qid: str, evidence: str = "甲", docs: tuple[str, ...] = ("a.pdf",)) -> Question:
    return Question(
        id=qid, question="質問", category="factual", expected_doc_ids=docs, evidence=(evidence,), evidence_mode="any"
    )


OUT = Question(id="x1", question="資料外", category="out_of_scope", scope_kind="off_topic")
CHUNKS = [_c(1, "a.pdf", "甲の話"), _c(2, "a.pdf", "乙の話"), _c(3, "a.pdf", "丙の話"), _c(4, "b.pdf", "甲の話")]


def test_sweep_k_counts_gold_share_and_applies_the_threshold() -> None:
    rows = [
        (_q("q1"), [(CHUNKS[0], 0.8), (CHUNKS[1], 0.7), (CHUNKS[2], 0.6)]),  # 1 位が根拠
        (_q("q2"), [(CHUNKS[1], 0.8), (CHUNKS[0], 0.7), (CHUNKS[2], 0.6)]),  # 2 位が根拠
        (_q("q3"), [(CHUNKS[0], 0.64)]),  # しきい値 0.65 で切れる
        (OUT, [(CHUNKS[0], 0.9)]),  # 資料外は数えない
    ]
    k1, k2 = sweep_k(rows, CHUNKS, (1, 2), 0.65)
    assert (k1["answerable"], k1["satisfied"], k1["empty"], k1["returned_chunks"], k1["gold_chunks"]) == (3, 1, 1, 2, 1)
    assert (k2["satisfied"], k2["empty"], k2["returned_chunks"], k2["gold_chunks"]) == (2, 1, 4, 2)


def test_miss_reasons_separate_threshold_rank_and_absence() -> None:
    rows = [
        (_q("t"), [(CHUNKS[0], 0.64)]),  # 根拠は 1 位だが、しきい値で切れた
        (_q("r"), [(CHUNKS[1], 0.9), (CHUNKS[2], 0.8), (CHUNKS[0], 0.7)]),  # 根拠は 3 位
        (_q("n"), [(CHUNKS[1], 0.9), (CHUNKS[2], 0.8)]),  # 上位に根拠が無い
        (_q("ok"), [(CHUNKS[0], 0.9)]),
        (OUT, [(CHUNKS[0], 0.9)]),
    ]
    got = {m["id"]: m["reason"] for m in miss_reasons(rows, 2, 0.65)}
    assert got == {"t": "below_threshold", "r": "beyond_k", "n": "not_in_top"}


def test_miss_reasons_ignore_chunks_outside_the_expected_file() -> None:
    rows = [(_q("q", docs=("a.pdf",)), [(CHUNKS[3], 0.9)])]  # 別のファイルの同じ文は根拠にならない
    assert [m["reason"] for m in miss_reasons(rows, 2, 0.65)] == ["not_in_top"]


def test_sweep_threshold_counts_cut_answerable_and_passed_out_of_scope() -> None:
    rows = [(_q("q1"), [(CHUNKS[0], 0.7)]), (_q("q2"), [(CHUNKS[0], 0.6)]), (OUT, [(CHUNKS[1], 0.66)]), (OUT, [])]
    low, high = sweep_threshold(rows, (0.6, 0.65))
    assert (low["answerable_cut"], low["out_of_scope_passed"]) == (0, 1)
    assert (high["answerable_cut"], high["out_of_scope_passed"]) == (1, 1)
    assert (high["answerable"], high["out_of_scope"]) == (2, 2)


def test_chunk_sizes_by_source() -> None:
    sizes = chunk_sizes(CHUNKS)
    assert sizes["a.pdf"] == {"chunks": 3, "min": 3, "median": 3, "max": 3}
    assert sizes["b.pdf"]["chunks"] == 1
