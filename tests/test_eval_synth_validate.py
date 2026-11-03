"""テスト用の架空資料の生成（evals/synth/）: 本文の検証規則。外部に接続しない。"""

import pytest

from evals.synth.bible import parse_bible
from evals.synth.validate import find_duplicate, quantity_tokens, shingles, validate_body

BIBLE = parse_bible(
    {
        "target": "demo",
        "label": "デモ",
        "premise": "概要",
        "revision": "2026年11月1日改訂",
        "contacts": ["050-0000-0000"],
        "forbidden": ["禁止語"],
        "facts": [
            {
                "id": "cap",
                "text": "洗濯できる衣類の重さは、原則として 8kg まで。消費電力は合計 1,200W 以内。",
                "tags": ["core"],
            },
            {
                "id": "hours",
                "text": "営業時間は 午前 9 時から午後 6 時 30 分まで。創業は2011年4月1日。保証金は 50,000 円。",
                "tags": ["core"],
            },
        ],
        "kinds": [
            {
                "id": "k",
                "doc_type": "manual",
                "title": "{label}",
                "tags": ["core"],
                "variants": ["v"],
                "chars": [10, 400],
                "outline": ["x"],
            }
        ],
    }
)


def body(*paragraphs: str, title: str = "# 題名") -> str:
    return "\n\n".join([title, "## 一", *paragraphs, "## 二", "以上です。"])


def codes(md: str, **kw) -> set[str]:
    limits = {"min_chars": 10, "max_chars": 2000} | kw
    return {i.code for i in validate_body(md, BIBLE, **limits)}


def test_a_clean_body_passes():
    assert codes(body("洗濯できる重さは 8kg までです。電気は1,200W以内です。")) == set()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("重さは 6kg までです。", "unknown_number"),  # 事実は 8kg
        ("重さは 18kg までです。", "unknown_number"),  # 8kg を含むが数字の途中
        ("営業は午後 7 時までです。", "unknown_number"),  # 事実は午後 6 時
        ("創業は2012年です。", "unknown_number"),
        ("保証金は 5,000 円です。", "unknown_number"),
    ],
)
def test_numbers_must_match_the_facts(text, expected):
    assert expected in codes(body(text))


def test_numbers_are_normalized_before_comparison():
    # 全角・桁区切り・数値と単位の間の空白が違っても、同じ数値として扱う
    assert "unknown_number" not in codes(body("重さは ８ kg、電力は 1200 W、金額は 50000円、時刻は午前９時です。"))
    assert quantity_tokens("毎月25日までに 3,300 円") == ["25日", "3300円"]


def test_dates_and_times_are_checked_by_whole_token():
    assert "unknown_number" not in codes(body("2011年4月に始まり、4月1日に開きます。午前9時から6時30分まで。"))
    assert "unknown_number" in codes(body("2011年5月に始まりました。"))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("```\ncode\n```", "format"),
        ("**太字**です。", "format"),
        ("<b>タグ</b>です。", "format"),
        ("![画像](a.png)です。", "format"),
        ("承知しました。以下が本文です。", "meta_text"),
        ("電話は 03-1234-5678 です。", "contact"),
        ("連絡は info@shop.co.jp です。", "contact"),
        ("詳細は https://www.shop.co.jp を見てください。", "contact"),
        ("禁止語を含みます。", "forbidden"),
        ("Google の製品です。", "forbidden"),
    ],
)
def test_rule_violations_are_reported(text, expected):
    assert expected in codes(body(text))


def test_allowed_contacts_and_safe_domains_pass():
    assert "contact" not in codes(body("電話は 050-0000-0000 です。連絡は help@example.com へ。"))


def test_structure_length_and_truncation():
    assert "no_title" in codes("## 見出し\n\n本文です。\n\n## 二\n\n終わりです。")
    assert "few_headings" in codes("# 題\n\n本文です。")
    assert "too_short" in codes(body("短い。"), min_chars=500)
    assert "too_long" in codes(body("長いです。" * 50), max_chars=100)
    assert "truncated" in codes("# 題\n\n## 一\n\n本文です。\n\n## 二\n\n途中で終わっ")


def test_degenerate_repetition_is_caught():
    assert "repetition" in codes(body(*["これは同じ文を繰り返しています。"] * 4))
    assert "repetition" in codes(body("あ" * 3000), max_chars=10_000)


def test_mostly_non_japanese_text_is_rejected():
    assert "language" in codes(body("This is an English paragraph with no Japanese text at all. " * 3))


def test_duplicate_detection_uses_shingle_jaccard():
    a = shingles(
        "# 題\n\n" + "洗濯機の運転が止まったときは、エラーコードを確かめてから、電源プラグを抜いてください。" * 2
    )
    same = shingles(
        "# 別の題\n\n" + "洗濯機の運転が止まったときは、エラーコードを確かめてから、電源プラグを抜いてください。" * 2
    )
    other = shingles("# 題\n\n保証期間はお買い上げの日から一年間で、出張修理の費用は期間内なら無料です。")
    assert find_duplicate(same, {"a": a}, 0.5) is not None
    assert find_duplicate(other, {"a": a}, 0.5) is None
