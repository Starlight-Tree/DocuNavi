"""回答の評価（scripts/eval_answers.py）の、API を呼ばない部分: 質問の読み込み、文面の差し替え、経過の記録。"""

from pathlib import Path

import pytest
import yaml

from app.agents import graph, tools
from scripts import eval_answers
from scripts.eval_answers import WORDINGS, load_plan, use_wording
from scripts.eval_retrieval import OUT_OF_SCOPE

RESEARCH = Path(eval_answers.ANSWERS_DIR / "questions_research.yaml")


def _write(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "q.yaml"
    path.write_text(yaml.safe_dump(items, allow_unicode=True), encoding="utf-8")
    return path


def test_the_fixed_research_questions_load_with_their_roles_and_answers():
    plan = load_plan(RESEARCH)
    roles = [role for _, role in plan]
    assert len(plan) == 14
    assert roles.count("answerable") == 10 and roles.count("on_topic_absent") == 3 and roles.count("off_topic") == 1
    assert all(q.answer for q, _ in plan)
    assert [q.category == OUT_OF_SCOPE for q, _ in plan] == [role != "answerable" for role in roles]


def test_inline_questions_do_not_reuse_an_approved_id(tmp_path):
    path = _write(tmp_path, [{"id": "q001", "role": "answerable", "question": "なにか", "answer": "なにか"}])
    with pytest.raises(ValueError, match="重なっています"):
        load_plan(path)


def test_an_id_only_item_still_comes_from_the_approved_sets_and_duplicates_are_rejected(tmp_path):
    ((question, role),) = load_plan(_write(tmp_path, [{"id": "q008", "role": "answerable"}]))
    assert question.id == "q008" and role == "answerable"
    with pytest.raises(ValueError, match="重複"):
        load_plan(_write(tmp_path, [{"id": "q008", "role": "answerable"}, {"id": "q008", "role": "answerable"}]))


def test_an_inline_question_needs_an_answer_and_a_known_role(tmp_path):
    with pytest.raises(ValueError):
        load_plan(_write(tmp_path, [{"id": "n900", "role": "answerable", "question": "なにか"}]))
    with pytest.raises(ValueError, match="role"):
        load_plan(_write(tmp_path, [{"id": "n900", "role": "nothing", "question": "なにか", "answer": "x"}]))


@pytest.fixture
def restore_wording(monkeypatch):
    monkeypatch.setattr(graph, "SYSTEM_PROMPT", graph.SYSTEM_PROMPT)
    monkeypatch.setattr(tools, "_NOT_FOUND_TEXT", tools._NOT_FOUND_TEXT)


def test_the_before_wording_restores_the_old_text_and_current_changes_nothing(restore_wording):
    current = (graph.SYSTEM_PROMPT, tools._NOT_FOUND_TEXT)
    use_wording("current")
    assert (graph.SYSTEM_PROMPT, tools._NOT_FOUND_TEXT) == current

    use_wording("before")
    assert tools._NOT_FOUND_TEXT == "該当する設定資料は見つかりませんでした。別のキーワードで再検索してください。"
    assert "検索し直し" not in graph.SYSTEM_PROMPT and "資料には記載がありません" in graph.SYSTEM_PROMPT
    assert tools._NOT_FOUND_TEXT.startswith(eval_answers._NOT_FOUND_MARK)
    with pytest.raises(ValueError):
        use_wording("other")
    assert set(WORDINGS) == {"current", "before"}


def test_the_current_not_found_text_starts_with_the_mark_the_eval_looks_for():
    assert tools._NOT_FOUND_TEXT.startswith(eval_answers._NOT_FOUND_MARK)


# ---- レート制限で止める・質問の間に待つ ----


class _FakeRateLimit(Exception):
    """GoogleRateLimitError の代わり（型の名前に RateLimit を含む）。"""

    def __init__(self) -> None:
        super().__init__("quota")
        self.code = 429
        self.details = {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "31s"},
                ]
            }
        }


class GoogleRateLimitError(_FakeRateLimit):
    pass


def test_a_rate_limit_is_recognised_by_its_type_and_its_quota_and_delay_are_recorded():
    exc = GoogleRateLimitError()
    assert eval_answers.is_rate_limited(exc)
    assert not eval_answers.is_rate_limited(ValueError("x"))
    assert eval_answers.rate_limit_info(exc) == {
        "retry_after_seconds": 31,
        "quota_ids": ["GenerateRequestsPerMinutePerProjectPerModel"],
    }
    assert eval_answers.rate_limit_info(ValueError("x")) == {"retry_after_seconds": None, "quota_ids": []}


def _fake_run(monkeypatch, limited_at: str | None):
    import asyncio as asyncio_module

    from app.agents import graph as graph_module

    monkeypatch.setattr(graph_module, "create_llm", lambda: object())
    monkeypatch.setattr(graph_module, "build_graph", lambda *a, **k: object())
    sleeps: list[float] = []
    ran: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def fake_run_one(graph, question, budget):
        ran.append(question.id)
        limited = question.id == limited_at
        return {
            "calls": 1,
            "elapsed_seconds": 0.0,
            "error": "GoogleRateLimitError" if limited else None,
            "rate_limited": limited,
            "rate_limit": {"retry_after_seconds": None, "quota_ids": []} if limited else None,
            "says_no_info": None,
            "searches": [],
            "answer": None,
        }

    monkeypatch.setattr(eval_answers.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(eval_answers, "run_one", fake_run_one)
    assert asyncio_module is eval_answers.asyncio
    return sleeps, ran


def test_the_run_waits_between_questions_but_not_before_the_first(monkeypatch):
    import asyncio

    sleeps, ran = _fake_run(monkeypatch, limited_at=None)
    plan = load_plan(RESEARCH)[:3]
    records, _ = asyncio.run(eval_answers.run_all(plan, eval_answers.CallBudget(10, 5), pause_seconds=30))
    assert ran == ["n001", "n002", "n003"] and len(records) == 3
    assert sleeps == [30, 30]


def test_the_run_stops_at_the_first_rate_limit_without_retrying_or_going_on(monkeypatch):
    import asyncio

    sleeps, ran = _fake_run(monkeypatch, limited_at="n002")
    plan = load_plan(RESEARCH)[:4]
    records, _ = asyncio.run(eval_answers.run_all(plan, eval_answers.CallBudget(10, 5), pause_seconds=5))
    assert ran == ["n001", "n002"]  # n002 は 1 回だけ。n003 以降は測らない
    assert [r["rate_limited"] for r in records] == [False, True]
    assert sleeps == [5]
