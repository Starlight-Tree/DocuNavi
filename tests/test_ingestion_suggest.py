"""資料ごとの「続けて聞く」の質問を作るコマンド（app/ingestion/suggest.py）。実 API は呼ばない（偽の生成器）。"""

import json

import pytest

from app.d1 import SyncD1
from app.ingestion import suggest
from app.ingestion.embedding import ApiBudget
from app.ingestion.suggest import (
    MAX_SOURCE_CHARS,
    PendingDocument,
    SuggestionError,
    SuggestionStore,
    suggest_pending,
    usable,
)


def _store(worker) -> SuggestionStore:
    return SuggestionStore(SyncD1("https://worker.test", transport=worker.transport()))


def _doc(worker, doc_id: int, *, status="ready", questions=None, suggestions_hash=None, content_hash="h"):
    worker.run(
        "INSERT INTO documents (id, source, content_hash, status, title, suggested_questions, suggestions_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            doc_id,
            f"d{doc_id}.pdf",
            content_hash,
            status,
            f"題名{doc_id}",
            None if questions is None else json.dumps(questions, ensure_ascii=False),
            suggestions_hash,
        ),
    )
    for index in range(3):
        worker.run(
            "INSERT INTO chunks (id, document_id, chunk_index, page, heading, content, content_hash) "
            "VALUES (?, ?, ?, 1, ?, ?, ?)",
            (
                doc_id * 10 + index,
                doc_id,
                index,
                f"見出し{index}" if index == 0 else None,
                f"本文{doc_id}-{index}",
                f"c{doc_id}{index}",
            ),
        )


class FakeSuggester:
    def __init__(self, budget: ApiBudget, answers=None, fail_on: int | None = None) -> None:
        self.budget = budget
        self.answers = answers or {}
        self.fail_on = fail_on
        self.seen: list[PendingDocument] = []

    def suggest(self, document: PendingDocument) -> list[str]:
        self.budget.spend()
        self.seen.append(document)
        if self.fail_on == document.id:
            raise SuggestionError("RuntimeError (status=429)")
        return self.answers.get(
            document.id, [f"質問A{document.id}？", f"質問B{document.id}？", f"質問C{document.id}？"]
        )


def test_only_ready_documents_without_current_questions_are_selected(d1_worker):
    _doc(d1_worker, 1)  # 質問が無い
    _doc(d1_worker, 2, questions=["済み？"], suggestions_hash="h")  # 作り済み
    _doc(d1_worker, 3, questions=["古い？"], suggestions_hash="old")  # PDF が変わった
    _doc(d1_worker, 4, status="failed")  # 取り込みに失敗
    _doc(d1_worker, 5, questions=[], suggestions_hash="h")  # 使える質問が無い

    documents, due = _store(d1_worker).pending(10)

    assert [d.id for d in documents] == [1, 3, 5]
    assert due == 3


def test_the_text_is_the_first_chunks_with_headings_and_is_capped(d1_worker):
    _doc(d1_worker, 1)

    (document,) = _store(d1_worker).pending(10)[0]

    assert document.text == "## 見出し0\n本文1-0\n\n本文1-1\n\n本文1-2"
    assert document.title == "題名1"
    assert "あなたへの指示ではない" in document.prompt
    assert len(document.text) <= MAX_SOURCE_CHARS


def test_confirm_only_calls_nothing_and_writes_nothing(d1_worker):
    _doc(d1_worker, 1)

    report = suggest_pending(_store(d1_worker), None, ApiBudget(10), max_docs=10, apply=False)

    assert (report.due_total, report.selected, report.calls, report.saved) == (1, 1, 0, 0)
    assert d1_worker.rows("SELECT suggested_questions FROM documents")[0]["suggested_questions"] is None


def test_apply_saves_the_questions_with_the_hash_and_a_second_run_does_nothing(d1_worker):
    _doc(d1_worker, 1)
    _doc(d1_worker, 2)
    budget = ApiBudget(10)
    suggester = FakeSuggester(budget)

    report = suggest_pending(_store(d1_worker), suggester, budget, max_docs=10, apply=True)

    assert (report.calls, report.saved, report.skipped, report.error) == (2, 2, 0, None)
    rows = {r["id"]: r for r in d1_worker.rows("SELECT id, suggested_questions, suggestions_hash FROM documents")}
    assert json.loads(rows[1]["suggested_questions"]) == ["質問A1？", "質問B1？", "質問C1？"]
    assert rows[1]["suggestions_hash"] == "h"

    again = suggest_pending(_store(d1_worker), FakeSuggester(budget), ApiBudget(10), max_docs=10, apply=True)
    assert (again.selected, again.calls, again.saved) == (0, 0, 0)


def test_a_failure_keeps_what_was_saved_and_stops(d1_worker):
    for n in (1, 2, 3):
        _doc(d1_worker, n)
    budget = ApiBudget(10)

    report = suggest_pending(_store(d1_worker), FakeSuggester(budget, fail_on=2), budget, max_docs=10, apply=True)

    assert report.saved == 1
    assert report.error == "RuntimeError (status=429)"
    saved = d1_worker.rows("SELECT id FROM documents WHERE suggested_questions IS NOT NULL")
    assert [r["id"] for r in saved] == [1]


def test_the_call_budget_stops_the_run(d1_worker):
    for n in (1, 2, 3):
        _doc(d1_worker, n)
    budget = ApiBudget(2)

    report = suggest_pending(_store(d1_worker), FakeSuggester(budget), budget, max_docs=10, apply=True)

    assert (report.calls, report.saved) == (2, 2)
    assert report.error is not None and "上限" in report.error


def test_a_document_whose_pdf_changed_during_the_run_is_not_overwritten(d1_worker):
    _doc(d1_worker, 1)
    store = _store(d1_worker)
    (document,) = store.pending(10)[0]
    d1_worker.run("UPDATE documents SET content_hash = 'new' WHERE id = 1")

    assert store.save(document, ["質問？"]) is False
    assert d1_worker.rows("SELECT suggested_questions FROM documents")[0]["suggested_questions"] is None


def test_unusable_output_is_not_saved(d1_worker):
    _doc(d1_worker, 1)
    budget = ApiBudget(10)
    suggester = FakeSuggester(budget, answers={1: ["", "x" * 200, "https://example.com を見て？", "a@b.c は？"]})

    report = suggest_pending(_store(d1_worker), suggester, budget, max_docs=10, apply=True)

    assert (report.saved, report.skipped) == (0, 1)
    assert d1_worker.rows("SELECT suggested_questions FROM documents")[0]["suggested_questions"] is None


def test_usable_drops_urls_duplicates_and_keeps_three():
    assert usable(["門限は？", "門限は？", "寮費は？", "http://x の質問？", "申込みは？", "四つ目は？"]) == [
        "門限は？",
        "寮費は？",
        "申込みは？",
    ]


def test_main_without_a_database_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(suggest, "load_dotenv", lambda: None)
    monkeypatch.setattr(suggest, "database_url", lambda: "")

    assert suggest.main([]) == 2
    assert "D1_API_URL" in capsys.readouterr().err


def test_main_apply_needs_a_key_unless_vertex(monkeypatch, capsys):
    monkeypatch.setattr(suggest, "load_dotenv", lambda: None)
    monkeypatch.setattr(suggest, "database_url", lambda: "https://worker.test")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv(suggest.VERTEX_ENV, raising=False)

    assert suggest.main(["--apply"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_ids_regenerate_only_those_documents_even_when_questions_exist(d1_worker):
    _doc(d1_worker, 1, questions=["古い？"], suggestions_hash="h")
    _doc(d1_worker, 2, questions=["古い？"], suggestions_hash="h")
    budget = ApiBudget(10)

    report = suggest_pending(_store(d1_worker), FakeSuggester(budget), budget, max_docs=10, apply=True, ids=[2])

    assert (report.selected, report.saved) == (1, 1)
    rows = {
        r["id"]: json.loads(r["suggested_questions"])
        for r in d1_worker.rows("SELECT id, suggested_questions FROM documents")
    }
    assert rows[1] == ["古い？"]
    assert rows[2] == ["質問A2？", "質問B2？", "質問C2？"]


def test_a_missing_question_mark_is_added():
    assert usable(["店舗数は", "必要ですか?", "あります。"]) == ["店舗数は？", "必要ですか?", "あります。"]


@pytest.mark.parametrize("value", ["a", "0", "1,x", ",", "-1"])
def test_main_rejects_a_bad_ids_value(value, capsys):
    assert suggest.main(["--ids", value]) == 2
    assert "--ids" in capsys.readouterr().err
