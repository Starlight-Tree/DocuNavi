"""scripts/eval_search_compare.py の純粋な部分（pgroonga の問い合わせ・融合・採点・集計）。DB・API は使わない。"""

from scripts.eval_retrieval import Question
from scripts.eval_search_compare import (
    _PGROONGA_SQL,
    ARMS,
    by_category,
    final_groups,
    fuse,
    load_selection_questions,
    pgroonga_query,
    score_question,
    summarize,
)
from scripts.eval_vector import Chunk


def _c(cid: int, source: str, content: str) -> Chunk:
    return Chunk(cid, source, cid, None, content, "text", f"h{cid}", f"i{cid}")


def _q(qid: str, doc: str | None, evidence: tuple[str, ...] = ("甲",), category: str = "factual") -> Question:
    if doc is None:
        return Question(id=qid, question="資料外", category="out_of_scope", scope_kind="off_topic")
    return Question(id=qid, question="質問", category=category, expected_doc_ids=(doc,), evidence=evidence)


def test_pgroonga_query_takes_word_runs_of_two_or_more_characters_joined_by_or() -> None:
    # ひらがなは区切り。1 文字の連なり（「は」「の」は区切りなので残らない）や、重複は落とす
    assert pgroonga_query("ザーハードの大気の酸素濃度はどれくらい？") == "ザーハード OR 大気 OR 酸素濃度"
    assert pgroonga_query("店の営業時間は 営業時間") == "営業時間"
    assert pgroonga_query("Wi-Fi の パスワード") == "wi OR fi OR パスワード"


def test_pgroonga_query_is_none_without_usable_words() -> None:
    assert pgroonga_query("これは？") is None
    assert pgroonga_query("") is None
    assert pgroonga_query("a と b") is None  # 1 文字の連なりだけ


def test_pgroonga_query_never_contains_query_syntax() -> None:
    query = pgroonga_query('「(営業) OR -休み」"time:*" AND (x)') or ""
    # 構文の文字（括弧・引用符・コロン・*・先頭の -）は、語の核には入らない。OR は結合のためのものだけ（小文字化で、質問の中の OR は語になる）
    for ch in '()"\':*\\':
        assert ch not in query
    assert all(not part.startswith("-") for part in query.split(" OR "))
    assert "AND" not in query  # 大文字の AND は構文になりうるので、小文字にしてある


def test_pgroonga_sql_qualifies_the_extension_objects_because_search_path_is_chatbot_only() -> None:
    assert "OPERATOR(extensions.&@~)" in _PGROONGA_SQL
    assert "extensions.pgroonga_score(c.tableoid, c.ctid)" in _PGROONGA_SQL
    assert _PGROONGA_SQL.count("%s") == 2  # 問い合わせと LIMIT。値は連結せず、パラメーターで渡す


def test_fuse_builds_the_five_arms_with_rrf_over_the_pairs() -> None:
    ranked = fuse({"bm25": [1, 2], "vector": [2, 3], "pgroonga": [3, 1]})
    assert tuple(ranked) == ARMS
    assert ranked["bm25"] == [1, 2] and ranked["vector"] == [2, 3] and ranked["pgroonga"] == [3, 1]
    assert ranked["bm25_vector"] == [2, 1, 3]  # 2 は両方にある
    assert ranked["pgroonga_vector"] == [3, 2, 1]  # 3 は両方にある。残りは、順位の高い方（vector の 1 位の 2）が先
    assert fuse({"bm25": [], "vector": [], "pgroonga": []})["pgroonga_vector"] == []


def test_score_question_reports_hit_rank_and_empty_per_arm() -> None:
    chunks = {1: _c(1, "a.pdf", "乙の話"), 2: _c(2, "a.pdf", "甲の話"), 3: _c(3, "b.pdf", "甲の話")}
    q = _q("q1", "a.pdf")
    ranked = {"bm25": [], "vector": [1, 2], "pgroonga": [3, 2], "bm25_vector": [1, 2], "pgroonga_vector": [2]}
    entry = score_question(q, ranked, chunks)
    assert entry["arms"]["bm25"]["empty"] is True
    assert entry["arms"]["bm25"]["first_gold_rank"] is None
    assert entry["arms"]["vector"]["first_gold_rank"] == 2
    assert entry["arms"]["vector"]["hit"] == {"1": False, "3": True, "5": True}
    assert entry["arms"]["pgroonga"]["first_gold_rank"] == 2  # b.pdf のチャンクは期待ファイルの外
    assert entry["arms"]["pgroonga_vector"]["hit"]["1"] is True


def test_out_of_scope_question_has_no_hit_fields_and_counts_empty_in_the_summary() -> None:
    chunks = {1: _c(1, "a.pdf", "甲の話")}
    ranked_empty = {arm: [] for arm in ARMS}
    ranked_some = {arm: [1] for arm in ARMS}
    absent_empty = score_question(_q("x1", None), ranked_empty, chunks)
    absent_some = score_question(_q("x2", None), ranked_some, chunks)
    assert "hit" not in absent_empty["arms"]["bm25"]
    answerable = score_question(_q("q1", "a.pdf"), ranked_some, chunks)
    summary = summarize([answerable, absent_empty, absent_some])
    assert summary["n_answerable"] == 1 and summary["n_out_of_scope"] == 2
    for arm in ARMS:
        assert summary[arm]["hit_at"] == {"1": 1, "3": 1, "5": 1}
        assert summary[arm]["mrr"] == 1.0
        assert summary[arm]["out_of_scope_empty"] == 1
        assert summary[arm]["answerable_empty"] == 0


def test_by_category_counts_hit_at_3_per_arm_and_skips_out_of_scope() -> None:
    chunks = {1: _c(1, "a.pdf", "甲の話"), 2: _c(2, "a.pdf", "乙の話")}
    hit = score_question(_q("q1", "a.pdf", category="factual"), {arm: [1] for arm in ARMS}, chunks)
    miss = score_question(_q("q2", "a.pdf", category="factual"), {arm: [2] for arm in ARMS}, chunks)
    other = score_question(_q("q3", "a.pdf", category="numeric"), {arm: [1] for arm in ARMS}, chunks)
    absent = score_question(_q("x1", None), {arm: [1] for arm in ARMS}, chunks)
    result = by_category([hit, miss, other, absent])
    assert set(result) == {"factual", "numeric"}
    assert result["factual"]["n"] == 2 and result["factual"]["bm25"] == 1
    assert result["numeric"]["pgroonga_vector"] == 1


def test_selection_questions_load_without_duplicate_ids() -> None:
    questions = load_selection_questions()
    assert len(questions) >= 36
    assert len({q.id for q in questions}) == len(questions)


def test_final_groups_split_by_file_and_by_whether_the_gold_chunk_overlaps_the_selection() -> None:
    chunks = {1: _c(1, "a.pdf", "甲の話"), 2: _c(2, "a.pdf", "乙の話")}
    hit = {arm: [1] for arm in ARMS}
    miss = {arm: [2] for arm in ARMS}
    a = {**score_question(_q("g1", "a.pdf"), hit, chunks), "file": "independent", "new_chunk": False}
    b = {**score_question(_q("f1", "a.pdf"), miss, chunks), "file": "claude", "new_chunk": True}
    absent = {**score_question(_q("g2", None), {arm: [] for arm in ARMS}, chunks), "file": "independent"}
    groups = final_groups([a, b, absent])
    assert set(groups) == {"overall", "file:claude", "file:independent", "new_chunk", "overlap_chunk"}
    assert groups["overall"]["n_answerable"] == 2 and groups["overall"]["n_out_of_scope"] == 1
    assert groups["file:independent"]["n_answerable"] == 1 and groups["file:independent"]["n_out_of_scope"] == 1
    assert groups["new_chunk"]["n_answerable"] == 1 and groups["new_chunk"]["bm25"]["hit_at"]["1"] == 0  # 資料外は入らない
    assert groups["overlap_chunk"]["n_answerable"] == 1 and groups["overlap_chunk"]["bm25"]["hit_at"]["1"] == 1
    assert groups["new_chunk"]["n_out_of_scope"] == 0
