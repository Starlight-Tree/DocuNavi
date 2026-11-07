"""テスト用の架空資料の生成（evals/synth/）: bible と計画。外部に接続しない。"""

from pathlib import Path

import pytest

from evals.synth.bible import BibleError, load_all, parse_bible
from evals.synth.plan import PlanError, build_plan, plan_target
from evals.synth.prompt import build_messages
from evals.synth.validate import validate_body

BIBLES = Path(__file__).resolve().parents[1] / "evals" / "synth" / "bibles"
TARGETS = {"zahard", "minato-coffee", "tsubasa-dorm", "aoba-aw820"}
DOC_TYPES = {"setting", "store-guide", "regulation", "manual"}  # workers/api/migrations の document_types の語彙


@pytest.fixture(scope="module")
def bibles():
    return load_all(BIBLES)


def test_four_targets_load(bibles):
    assert set(bibles) == TARGETS


def test_plan_reaches_twenty_per_target_including_existing(bibles):
    briefs = build_plan(bibles, per_target=20)
    by_target = {t: sum(1 for b in briefs if b.target == t) for t in TARGETS}
    assert by_target == {"zahard": 17, "minato-coffee": 19, "tsubasa-dorm": 19, "aoba-aw820": 19}
    existing = sum(b.existing_documents for b in bibles.values())
    assert len(briefs) + existing == 80


def test_plan_is_deterministic_and_ids_and_keys_are_unique(bibles):
    first = build_plan(bibles, per_target=20)
    second = build_plan(bibles, per_target=20)
    assert first == second
    assert len({b.id for b in first}) == len(first)
    assert len({b.key for b in first}) == len(first)


def test_plan_scales_to_the_bible_capacity_and_refuses_beyond(bibles):
    # 種類 × 変化の数まで。それを超える件数は、黙って重複を作らずに止まる
    capacity = {t: sum(len(k.variants) for k in b.kinds) + b.existing_documents for t, b in bibles.items()}
    assert all(c >= 20 for c in capacity.values())
    with pytest.raises(PlanError):
        plan_target(bibles["zahard"], per_target=capacity["zahard"] + 1)


def test_plan_rotates_kinds_so_a_small_sample_is_varied(bibles):
    first_two = [b.kind for b in plan_target(bibles["minato-coffee"], per_target=3)]
    assert len(set(first_two)) == 2  # 既存 1 件を引いた 2 件が、別の種類


def test_doc_types_use_only_the_approved_vocabulary(bibles):
    for bible in bibles.values():
        assert {k.doc_type for k in bible.kinds} <= DOC_TYPES


def test_every_kind_has_enough_facts_to_write_from(bibles):
    for bible in bibles.values():
        for kind in bible.kinds:
            assert len(bible.facts_for(kind)) >= 8, (bible.target, kind.id)


def test_facts_pass_their_own_validator_numbers_and_have_no_forbidden_terms(bibles):
    # 事実そのものが「数値の突合」「使えない語」で落ちない（bible の誤字や、実在の連絡先の混入を防ぐ）
    for bible in bibles.values():
        body = "# 題\n\n## 一\n\n" + "\n\n".join(f.text for f in bible.facts) + "\n\n## 二\n\n終わりです。"
        issues = [
            i
            for i in validate_body(body, bible, min_chars=1, max_chars=10**6)
            if i.code in {"unknown_number", "forbidden", "contact"}
        ]
        assert issues == [], (bible.target, issues)


def test_prompt_contains_only_the_selected_facts_and_the_format_lines(bibles):
    bible = bibles["aoba-aw820"]
    brief = next(b for b in build_plan(bibles, 20, ["aoba-aw820"]) if b.kind == "warranty-terms")
    system, user = build_messages(bible, brief)
    assert f"題名: {brief.title}" in user
    assert f"文字数: {brief.min_chars}〜{brief.max_chars}" in user
    assert "3,300円" in user  # 保証の事実は入る
    assert "E04は排水エラー" not in user  # 別の種類（エラー）の事実は入らない
    assert "架空" in system  # 断り書きは本文に書かせない旨が入っている


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(target="Bad Target"), "target"),
        (lambda d: d["facts"].append(dict(d["facts"][0])), "重複"),
        (lambda d: d["kinds"][0].update(tags=["no-such-tag"]), "タグ"),
        (lambda d: d["kinds"][0].update(chars=[500, 100]), "chars"),
        (lambda d: d["kinds"][0].update(doc_type="Manual"), "doc_type"),
    ],
)
def test_bible_format_errors_are_rejected(mutate, message):
    data = {
        "target": "demo",
        "label": "デモ",
        "premise": "概要",
        "facts": [{"id": "a", "text": "事実です。", "tags": ["core"]}],
        "kinds": [
            {
                "id": "k",
                "doc_type": "manual",
                "title": "{label}",
                "tags": ["core"],
                "variants": ["v"],
                "chars": [10, 20],
                "outline": ["x"],
            }
        ],
    }
    mutate(data)
    with pytest.raises(BibleError, match=message):
        parse_bible(data)


def test_bible_version_changes_when_a_fact_changes():
    base = {
        "target": "demo",
        "label": "デモ",
        "premise": "概要",
        "facts": [{"id": "a", "text": "容量は8kg。", "tags": ["core"]}],
        "kinds": [
            {
                "id": "k",
                "doc_type": "manual",
                "title": "{label}",
                "tags": ["core"],
                "variants": ["v"],
                "chars": [10, 20],
                "outline": ["x"],
            }
        ],
    }
    changed = {**base, "facts": [{"id": "a", "text": "容量は6kg。", "tags": ["core"]}]}
    assert parse_bible(base).version != parse_bible(changed).version
