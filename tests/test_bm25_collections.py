"""BM25 の基準線（scripts/bm25_baseline.py）の、資料群（storage/pdf/ の PDF のディレクトリ。ザーハードも PDF）をまたぐ検索のテスト。

アプリの検索ではない（アプリは DB のベクトル検索）。評価の BM25 の経路が使う実装なので、挙動を固定しておく。
検索そのものの精度は tests/test_bm25_baseline.py と評価（scripts/eval_retrieval.py）が見る。ここは、資料群を足しても
他の資料群の結果が動かないこと、範囲の指定、PDF を取り込めないときの扱いを確かめる。
"""

import hashlib
import logging
from pathlib import Path

import pytest

from scripts import bm25_baseline as tools
from scripts import eval_retrieval as ev
from scripts.bm25_baseline import search_chunks, search_collections
from tests.bm25_fixtures import txt_dir  # noqa: F401  (フィクスチャ)
from tests.pdf_fixtures import build_pdf, paragraphs
from tests.zahard_corpus import ZAHARD_FILES, ZAHARD_PDF_DIR

ROOT = Path(__file__).resolve().parents[1]
EVAL_CAFE = ROOT / "evals" / "corpora" / "cafe"
APP_CAFE = ROOT / "storage" / "pdf" / "cafe"
EVAL_GUIDES = ROOT / "evals" / "corpora" / "guides"
APP_GUIDES = ROOT / "storage" / "pdf" / "guides"
GUIDES_PDFS = ("aoba_aw820_manual.pdf", "tsubasa_dorm_rules.pdf")


@pytest.fixture
def pdf_dir(tmp_path, monkeypatch):
    """storage/pdf/ を空の一時ディレクトリに差し替える（読み込みのキャッシュも、前後で捨てる）。"""
    root = tmp_path / "pdf"
    root.mkdir()
    monkeypatch.setattr(tools, "PDF_DIR", root)
    tools._load_pdf_collections.cache_clear()
    yield root
    tools._load_pdf_collections.cache_clear()


# ---- アプリの資料と、評価の資料 ----


def test_app_pdf_is_the_one_the_cafe_baseline_measured():
    # storage/pdf/ の PDF は評価用の PDF の写し。食い違うと、評価の baseline が、アプリの検索を測っていないことになる
    app_pdf = APP_CAFE / "cafe_store_guide.pdf"
    eval_pdf = EVAL_CAFE / "cafe_store_guide.pdf"
    assert hashlib.sha256(app_pdf.read_bytes()).hexdigest() == hashlib.sha256(eval_pdf.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", GUIDES_PDFS)
def test_app_guides_pdfs_are_the_ones_the_guides_baseline_measured(name):
    # cafe と同じ。storage/pdf/guides/ は evals/corpora/guides/ の PDF の写し（2 段組みの 1 冊は、アプリに置かない）
    assert (
        hashlib.sha256((APP_GUIDES / name).read_bytes()).hexdigest()
        == hashlib.sha256((EVAL_GUIDES / name).read_bytes()).hexdigest()
    )


def test_app_guides_collection_holds_only_the_evaluated_pdfs():
    assert sorted(p.name for p in APP_GUIDES.glob("*.pdf")) == sorted(GUIDES_PDFS)


def test_app_searches_the_pdf_collections():
    # ザーハードも PDF の資料群（storage/pdf/zahard/）。資料群は名前順で、結果の交互採用もこの順
    collections = tools.load_collections()
    assert list(collections) == ["cafe", "guides", "zahard"]
    assert set(collections["zahard"]) == set(ZAHARD_FILES)
    assert set(collections["cafe"]) == {"cafe_store_guide.pdf"}
    assert set(collections["guides"]) == set(GUIDES_PDFS)


# 他の資料群の文が、2 語（MIN_SHARED_TERMS）の偶然の一致で、質問の資料群の結果に足されることがある（実測、2026-10-15）。
# 資料群ごとに採点しても、各資料群が「自分の中の 1 位」を返すので、関係のない資料群が混ざる余地は残る。
# 下の 2 件は、guides を足したときに見つかった全部（68 問）。増えたら、このテストが落ちる（理由を調べてから足す）。
KNOWN_SPILL = {
    # 「日曜日が定休日の店舗」に、guides の取扱説明書（「土曜日、日曜日、祝日はお休み」）が足される
    "c007": ["aoba_aw820_manual.pdf"],
    # 「寮でペットを飼えるか」（資料外）に、cafe の PDF が返る（guides の中だけなら空）
    "t010": ["cafe_store_guide.pdf"],
}


@pytest.mark.parametrize("k", [2, 5])
def test_a_collection_is_not_disturbed_by_the_others(k):
    # 資料群を足しても、それぞれの質問に対する結果が変わらない（評価セットの全質問で、単独で検索したときと同じ）。
    # 1 つに混ぜると崩れる: 「ザーハード」の出現率が下がって主題語として除外されず、返るファイルが増える。
    # 例外は KNOWN_SPILL の 2 件だけ。そこでは、単独の結果が順序もそのままで残り、足されたのは他の資料群のファイルだけ
    own_corpora = {
        ROOT / "evals" / "questions.yaml": ev.load_pdf_corpus(ZAHARD_PDF_DIR)[0],
        EVAL_CAFE / "questions.yaml": ev.load_pdf_corpus(EVAL_CAFE)[0],
        EVAL_GUIDES / "questions.yaml": ev.load_pdf_corpus(EVAL_GUIDES)[0],
    }
    checked, spilled = 0, set()
    for path, own in own_corpora.items():
        for q in ev.load_questions(path):
            app_hits = search_chunks(q.question, k)
            own_hits = search_chunks(q.question, k, own)
            checked += 1
            if app_hits == own_hits:
                continue
            spilled.add(q.id)
            own_names = [name for name, _ in own_hits]
            assert [hit for hit in app_hits if hit[0] in own_names] == own_hits, q.id
            assert [name for name, _ in app_hits if name not in own_names] == KNOWN_SPILL.get(q.id), q.id
    assert spilled == set(KNOWN_SPILL)
    assert checked >= 68


# ---- 資料群ごとの採点と、結果の合わせ方 ----

# 「ザーハード」は 1 つの資料群の中では全ての文に出る主題語（除外される）。別の資料群の文が 20 文増えると、
# 混ぜた全体では出現率が 25% を下回り、除外されなくなる
THEME = {
    f"t{i}.txt": f"ザーハードの{topic}は{state}。"
    for i, (topic, state) in enumerate([("酸素", "薄い"), ("重力", "強い"), ("大気", "重い"), ("水", "少ない")])
}
OTHER = {f"o{i}.txt": f"珈琲の{i}番の豆は、香りが深い。" for i in range(20)}


def test_search_scores_each_collection_with_its_own_statistics():
    query = "ザーハードの酸素"
    alone = search_chunks(query, 4, THEME)
    assert [name for name, _ in alone] == ["t0.txt"]

    mixed = search_chunks(query, 4, {**THEME, **OTHER})  # 1 つに混ぜた場合: 主題語が除外されず、無関係な文まで返る
    assert len([name for name, _ in mixed]) > 1

    assert search_collections(query, 4, {"theme": THEME, "other": OTHER}) == alone


def test_collections_are_taken_in_turn_by_rank():
    a = {"a1.txt": "赤い林檎は甘い。", "a2.txt": "林檎の木は高い。\\林檎の花は白い。\\林檎は秋に実る。"}
    b = {"b1.txt": "林檎のパイを焼く。\\林檎を煮る。", "b2.txt": "林檎の香りの茶もある。"}
    # 1 つの資料群の中の順位（a1 が a2 より上、b1 が b2 より上）は、単独の検索で決まる
    alone_a = [name for name, _ in search_chunks("林檎", 5, a)]
    alone_b = [name for name, _ in search_chunks("林檎", 5, b)]
    assert len(alone_a) == 2 and len(alone_b) == 2

    def names(top_k, collections):
        return [name for name, _ in search_collections("林檎", top_k, collections)]

    assert names(1, {"a": a, "b": b}) == [alone_a[0]]
    assert names(2, {"a": a, "b": b}) == [alone_a[0], alone_b[0]]
    assert names(3, {"a": a, "b": b}) == [alone_a[0], alone_b[0], alone_a[1]]
    assert names(4, {"a": a, "b": b}) == [alone_a[0], alone_b[0], alone_a[1], alone_b[1]]
    assert names(9, {"b": b, "a": a}) == [alone_b[0], alone_a[0], alone_b[1], alone_a[1]]  # 資料群の並び順に従う
    assert names(0, {"a": a, "b": b}) == []


def test_a_collection_without_hits_does_not_take_a_slot():
    a = {"a1.txt": "赤い林檎は甘い。", "a2.txt": "林檎の木は高い。\\林檎の花は白い。\\林檎は秋に実る。"}
    b = {"b1.txt": "桜の花が咲く。"}
    hits = search_collections("林檎", 2, {"a": a, "b": b})
    assert [name for name, _ in hits] == [name for name, _ in search_chunks("林檎", 2, a)]
    assert len(hits) == 2


# ---- 範囲（SEARCH_SCOPE） ----

ZAHARD_QUERY = "ザーハードの大気の酸素濃度はどれくらい？"
CAFE_QUERY = "オアシス珈琲の定休日"


def _files(query):
    return {name for name, _ in search_chunks(query)}


def test_scope_defaults_to_every_collection(monkeypatch):
    monkeypatch.delenv(tools.SEARCH_SCOPE_ENV, raising=False)
    assert tools.search_scope() == ["cafe", "guides", "zahard"]
    assert _files(ZAHARD_QUERY) == {"ザーハード1.pdf"}
    assert _files(CAFE_QUERY) == {"cafe_store_guide.pdf"}


@pytest.mark.parametrize("value", ["", "  ", "all", "ALL"])
def test_scope_all_and_blank_mean_every_collection(monkeypatch, value):
    monkeypatch.setenv(tools.SEARCH_SCOPE_ENV, value)
    assert tools.search_scope() == ["cafe", "guides", "zahard"]


def test_scope_can_narrow_to_one_collection(monkeypatch):
    monkeypatch.setenv(tools.SEARCH_SCOPE_ENV, "zahard")
    assert _files(ZAHARD_QUERY) == {"ザーハード1.pdf"}
    assert _files(CAFE_QUERY) == set()

    monkeypatch.setenv(tools.SEARCH_SCOPE_ENV, " cafe ")
    assert _files(ZAHARD_QUERY) == set()
    assert _files(CAFE_QUERY) == {"cafe_store_guide.pdf"}

    monkeypatch.setenv(tools.SEARCH_SCOPE_ENV, "cafe, zahard")
    assert tools.search_scope() == ["cafe", "zahard"]


@pytest.mark.parametrize("value", ["zahrd", "zahard,cafee", ","])
def test_scope_with_an_unknown_name_is_an_error(monkeypatch, value):
    monkeypatch.setenv(tools.SEARCH_SCOPE_ENV, value)
    with pytest.raises(ValueError) as excinfo:
        tools.search_scope()
    assert "SEARCH_SCOPE" in str(excinfo.value)
    assert "zahard" in str(excinfo.value)  # 選べる名前を示す


# ---- PDF の読み込み ----


def test_a_pdf_that_cannot_be_read_is_left_out_and_logged(pdf_dir, caplog):
    folder = pdf_dir / "faq"
    folder.mkdir()
    (folder / "good.pdf").write_bytes(build_pdf([paragraphs("返品は購入から七日以内に受け付けます。")]))
    (folder / "broken.pdf").write_bytes(b"not a pdf")

    with caplog.at_level(logging.WARNING, logger="scripts.bm25_baseline"):
        collections = tools._load_pdf_collections()

    assert list(collections) == ["faq"]
    assert list(collections["faq"]) == ["good.pdf"]
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("faq/broken.pdf" in m and "unreadable" in m for m in messages)
    assert not any(str(pdf_dir) in m for m in messages)  # サーバーのパスは出さない


def test_a_directory_without_readable_pdfs_is_not_a_collection(pdf_dir):
    (pdf_dir / "empty").mkdir()
    (pdf_dir / "bad").mkdir()
    (pdf_dir / "bad" / "x.pdf").write_bytes(b"%PDF-1.4 broken")
    assert tools._load_pdf_collections() == {}


def test_pdfs_in_a_directory_named_like_the_text_collection_join_it(pdf_dir, monkeypatch):
    monkeypatch.setattr(tools, "load_chunks", lambda: {"a.txt": "赤い林檎は甘い。"})
    folder = pdf_dir / tools.TEXT_COLLECTION
    folder.mkdir()
    (folder / "b.pdf").write_bytes(build_pdf([paragraphs("青い海原は広い。")]))

    collections = tools.load_collections()
    assert list(collections) == [tools.TEXT_COLLECTION]
    assert list(collections[tools.TEXT_COLLECTION]) == ["a.txt", "b.pdf"]


def test_without_a_pdf_directory_only_the_text_collection_is_searched(txt_dir, pdf_dir, monkeypatch):
    # .txt の資料群の経路（ザーハードは PDF に移ったが、コードには残っている）。元の .txt（txt_dir）だけを検索する
    monkeypatch.setattr(tools, "PDF_DIR", pdf_dir / "none")
    tools._load_pdf_collections.cache_clear()
    assert list(tools.load_collections()) == [tools.TEXT_COLLECTION]
    assert _files(ZAHARD_QUERY) == {"ザーハード1.txt"}
