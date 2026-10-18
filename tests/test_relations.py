"""app/relations（規程の条・項の分割と、関係の抽出）のテスト。実文書の測定は scripts/eval_relations.py。"""

from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.ingestion.pdf_parser import Page, extract_pages
from app.relations.articles import parse_regulation
from app.relations.extract import extract_edges
from scripts.eval_relations import GOLD, PDF, TERMS, evaluate

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _reg(text: str):
    return parse_regulation([Page(number=1, text=text)])


def _edges(text: str):
    return {(e.type, e.src, e.dst): e for e in extract_edges(_reg(text), now=NOW)}


SAMPLE = """題名
第1章 総則
第1条 この規程は、運営を定める。
第2条 費用は、次のとおりとする。月額 100 円とする。
2 割引は 10 円とする。
3 保証金は 50 円とする。
第3条 前条の費用は、納める。
2 前項の定めにかかわらず、第2条第2項の割引を受ける者は、納めない。
第2章 雑則
第4条 第1条の目的に従う。
2 第9条の定めにかかわらず、第3条の規定は適用する。
"""


def test_splits_chapters_articles_paragraphs():
    reg = _reg(SAMPLE)
    assert reg.title == "題名"
    assert [(c.number, c.title) for c in reg.chapters] == [(1, "総則"), (2, "雑則")]
    assert [(a.number, a.chapter, len(a.paragraphs)) for a in reg.articles] == [
        (1, 1, 1),
        (2, 1, 3),
        (3, 1, 2),
        (4, 2, 2),
    ]


def test_exception_and_references_with_targets():
    edges = _edges(SAMPLE)
    assert ("references", "a2p1", "a2p2") in edges  # 次のとおり → 後ろの項
    assert ("references", "a2p1", "a2p3") in edges
    assert ("references", "a3p1", "a2p1") in edges  # 前条 → 前の条の第 1 項
    assert ("exception_of", "a3p2", "a3p1") in edges  # 前項の定めにかかわらず
    assert ("references", "a3p2", "a2p2") in edges  # 第N条第M項
    assert ("references", "a4p1", "a1p1") in edges  # 第N条
    assert ("same_chapter", "a4", "ch2") in edges


def test_exception_span_is_not_also_a_reference():
    edges = _edges(SAMPLE)
    # 「前項の定めにかかわらず」は exception_of だけ（references の前項としては数えない）
    assert ("references", "a3p2", "a3p1") not in edges


def test_unresolvable_references_make_no_edge():
    edges = _edges(SAMPLE)
    # 第9条は無い。第 1 項の「前項」も指す先が無い
    assert not [k for k in edges if k[2].startswith("a9")]
    assert not [
        k
        for k in _edges("題名\n第1章 総則\n第1条 前項の定めにかかわらず、行う。\n")
        if k[0] in {"references", "exception_of"}
    ]


def test_article_number_must_be_sequential():
    # 行頭が「第5条」でも、直前の条が 2 なら新しい条ではなく、前の項の続き（折り返した行の中の参照）
    reg = _reg("題名\n第1章 総則\n第1条 あ。\n第2条 い。\n第5条の定めによる。\n第3条 う。\n")
    assert [a.number for a in reg.articles] == [1, 2, 3]
    assert "第5条の定めによる" in reg.articles[1].paragraphs[0].text


def test_wrapped_lines_join_without_separator_and_keep_leading_space():
    reg = _reg("題名\n第1章 総則\n第1条 退寮予定日の\n 7 日前までに提出する。\n")
    assert reg.articles[0].paragraphs[0].text == "退寮予定日の 7 日前までに提出する。"


def test_paragraph_number_must_follow_the_previous_one():
    reg = _reg("題名\n第1章 総則\n第1条 あ。\n3 いきなり 3 の行。\n2 い。\n")
    assert [p.number for p in reg.articles[0].paragraphs] == [1, 2]
    assert "3 いきなり 3 の行" in reg.articles[0].paragraphs[0].text


def test_footer_and_page_numbers_are_dropped_and_page_is_recorded():
    pages = [
        Page(1, "題名\n第1章 総則\n第1条 あ。\n脚の行\n1 / 2"),
        Page(2, "2 い。\n脚の行\n2 / 2"),
    ]
    reg = parse_regulation(pages)
    assert [p.text for p in reg.articles[0].paragraphs] == ["あ。", "い。"]
    assert [p.page for p in reg.articles[0].paragraphs] == [1, 2]


def test_footnote_marker_becomes_a_reference_edge():
    text = "題名\n第1章 総則\n第1条 あ。\n2 い。1\n脚注\n1 注の本文。\n"
    reg = _reg(text)
    assert reg.articles[0].paragraphs[1].text == "い。"
    assert reg.articles[0].paragraphs[1].footnote == 1
    edge = {(e.type, e.src, e.dst): e for e in extract_edges(reg, now=NOW)}[("references", "a1p2", "fn1")]
    assert edge.evidence in reg.articles[0].paragraphs[1].raw


def test_edge_metadata_and_article_hash():
    reg = _reg(SAMPLE)
    for e in extract_edges(reg, now=NOW):
        assert e.extracted_by == "rule"
        assert e.updated_at == "2026-10-18T12:00:00+00:00"
        assert e.page == 1
    changed = _reg(SAMPLE.replace("月額 100 円", "月額 200 円"))
    by_number = lambda r: {a.number: a.text_hash for a in r.articles}  # noqa: E731
    before, after = by_number(reg), by_number(changed)
    assert before[2] != after[2]
    assert {n for n in before if before[n] == after[n]} == {1, 3, 4}  # 変えていない条のハッシュは同じ


def test_dorm_rules_meet_the_pass_line():
    gold = yaml.safe_load(Path(GOLD).read_text(encoding="utf-8"))
    reg = parse_regulation(extract_pages(PDF))
    terms = yaml.safe_load(Path(TERMS).read_text(encoding="utf-8"))
    result = evaluate(reg, extract_edges(reg, terms=terms, now=NOW), gold)
    assert result["verdict"] == {k: True for k in result["verdict"]}, result


TERM_SAMPLE = """題名
第1章 総則
第1条 寮費は、月額 100 円とする。
第2条 寮費は、毎月納める。
2 共益費は、寮費に含まれない。
第3条 共益費は、月額 10 円とする。1
脚注
1 寮費は変わらない。
"""
SAMPLE_TERMS = {"寮費": "a1p1", "共益費": "a3p1"}


def test_uses_term_edges_point_to_the_defining_paragraph():
    edges = {(e.type, e.src, e.dst): e for e in extract_edges(_reg(TERM_SAMPLE), terms=SAMPLE_TERMS, now=NOW)}
    assert ("uses_term", "a2p1", "a1p1") in edges
    assert ("uses_term", "a2p2", "a1p1") in edges  # 1 つの項に 2 語: それぞれの定義へ
    assert ("uses_term", "a2p2", "a3p1") in edges
    assert ("uses_term", "fn1", "a1p1") in edges  # 脚注からも張る
    edge = edges[("uses_term", "a2p1", "a1p1")]
    assert (edge.evidence, edge.extracted_by, edge.page, edge.updated_at) == (
        "寮費",
        "rule",
        1,
        "2026-10-18T12:00:00+00:00",
    )


def test_uses_term_skips_the_definition_itself_and_needs_a_dictionary():
    edges = extract_edges(_reg(TERM_SAMPLE), terms=SAMPLE_TERMS, now=NOW)
    assert not [e for e in edges if e.type == "uses_term" and e.src == e.dst]
    assert not [e for e in edges if e.type == "uses_term" and e.src in {"a1p1", "a3p1"}]  # 自分が定義する語だけの項
    assert not [e for e in extract_edges(_reg(TERM_SAMPLE), now=NOW) if e.type == "uses_term"]  # 辞書なし


def test_a_dictionary_pointing_at_a_missing_paragraph_is_an_error():
    import pytest

    with pytest.raises(ValueError):
        extract_edges(_reg(TERM_SAMPLE), terms={"寮費": "a9p1"}, now=NOW)


def test_changing_article_13_reaches_articles_15_17_18_through_terms():
    reg = parse_regulation(extract_pages(PDF))
    terms = yaml.safe_load(Path(TERMS).read_text(encoding="utf-8"))
    edges = extract_edges(reg, terms=terms, now=NOW)
    users = {e.src for e in edges if e.type == "uses_term" and e.dst == "a13p1"}
    assert users == {"a15p1", "a17p1", "a18p1", "fn1"}
