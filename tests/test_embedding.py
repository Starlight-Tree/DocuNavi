"""チャンクの埋め込み（app/ingestion/embedding.py、embed.py）の、実 API と DB を使わない部分。

実 API は呼ばない。SDK は偽物に差し替える。pgvector への UPDATE は、素の Postgres（CI）に pgvector が無いので、ここでは確かめない
（Supabase で、実行の承認を得てから確かめる）。
"""

import math
from types import SimpleNamespace

import pytest

from app.ingestion import embed as cli
from app.ingestion import embedding
from app.ingestion.embedding import (
    BATCH_SIZE,
    DIMENSIONS,
    ApiBudget,
    BudgetExceeded,
    EmbeddingError,
    GeminiEmbedder,
    PendingChunk,
    PostgresEmbeddingStore,
    check_vectors,
    document_text,
    embed_pending,
    input_hash,
    query_text,
    vector_literal,
)


def _vec(seed: float = 1.0) -> list[float]:
    return [seed] + [0.0] * (DIMENSIONS - 1)


class FakeStore:
    """EmbeddingStore の偽物。保存した行は、次の pending から消える（本物と同じ）。"""

    def __init__(self, n: int, *, stale_hash_ids: frozenset[int] = frozenset()) -> None:
        self.chunks = {
            i: PendingChunk(i, f"h{i}", None if i % 2 else f"見出し{i}", f"本文{i}") for i in range(1, n + 1)
        }
        self.done: dict[int, list[float]] = {}
        self.done_input: dict[int, str] = {}
        self.stale = stale_hash_ids
        self.saves = 0

    def _pending(self):
        return [c for i, c in sorted(self.chunks.items()) if self.done_input.get(i) != c.input_hash]

    def pending_embeddings(self, limit):
        return self._pending()[:limit]

    def count_pending(self):
        return len(self._pending())

    def save_embeddings(self, rows):
        self.saves += 1
        saved = 0
        for chunk, vector in rows:
            if chunk.id in self.stale:
                continue  # 本文のハッシュが変わっていた行は飛ばす
            self.done[chunk.id] = vector
            self.done_input[chunk.id] = chunk.input_hash
            saved += 1
        return saved


class FakeEmbedder:
    def __init__(self, budget: ApiBudget, *, fail_on_call: int | None = None) -> None:
        self.budget = budget
        self.fail_on_call = fail_on_call
        self.sent: list[list[str]] = []

    def embed(self, texts):
        self.budget.spend()
        if self.fail_on_call == self.budget.calls:
            raise EmbeddingError("ServerError (status=503)")
        self.sent.append(list(texts))
        return [_vec(len(self.sent)) for _ in texts]


# ---- 本文の形 ----


def test_document_and_query_text_follow_the_documented_prefixes():
    assert document_text("会社の概要", "本文です。") == "title: 会社の概要 | text: 本文です。"
    assert document_text(None, "本文です。") == "title: none | text: 本文です。"
    assert query_text("営業時間は？") == "task: search result | query: 営業時間は？"


def test_vector_literal_is_pgvector_text_format():
    assert vector_literal([0.5, 1, -2.25]) == "[0.5,1.0,-2.25]"


def test_input_hash_covers_the_prefix_the_heading_and_the_content():
    base = input_hash("見出し", "本文")
    assert input_hash("見出し", "本文") == base
    assert input_hash("別の見出し", "本文") != base  # 本文が同じでも、見出しが変われば別の入力
    assert input_hash(None, "本文") != base
    assert input_hash("見出し", "別の本文") != base
    assert PendingChunk(1, "h", "見出し", "本文").input_hash == base


# ---- 返りの検査 ----


def test_check_vectors_rejects_an_aggregated_single_vector():
    # 文字列のリストを渡すと、1 本にまとめられて返る。送った件数と合わなければ止める
    with pytest.raises(EmbeddingError, match="数が合いません"):
        check_vectors([_vec()], 3)


@pytest.mark.parametrize(
    "bad, message",
    [
        ([1.0] * (DIMENSIONS - 1), "次元が合いません"),
        ([0.0] * DIMENSIONS, "不正"),
        ([math.nan] + [0.0] * (DIMENSIONS - 1), "不正"),
        ([math.inf] + [0.0] * (DIMENSIONS - 1), "不正"),
    ],
)
def test_check_vectors_rejects_bad_vectors(bad, message):
    with pytest.raises(EmbeddingError, match=message):
        check_vectors([bad], 1)


def test_check_vectors_accepts_good_vectors():
    assert check_vectors([_vec(1), _vec(2)], 2) == [_vec(1), _vec(2)]


# ---- GeminiEmbedder（SDK は偽物） ----


class _FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def embed_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _embedder(response, budget=None):
    embedder = GeminiEmbedder("test-key", budget or ApiBudget(10))
    models = _FakeModels(response)
    embedder._client = SimpleNamespace(models=models)
    return embedder, models


def test_each_text_is_wrapped_in_its_own_content_and_dimensions_are_requested():
    response = SimpleNamespace(embeddings=[SimpleNamespace(values=_vec(1)), SimpleNamespace(values=_vec(2))])
    embedder, models = _embedder(response)
    assert embedder.embed(["一つ目", "二つ目"]) == [_vec(1), _vec(2)]
    (call,) = models.calls
    assert call["model"] == "gemini-embedding-2"
    assert call["config"].output_dimensionality == 768
    # 文字列のリストではなく、1 本ずつの Content（リストだと 1 本にまとめられる）
    assert [c.parts[0].text for c in call["contents"]] == ["一つ目", "二つ目"]
    assert all(not isinstance(c, str) for c in call["contents"])


def test_an_aggregated_response_is_an_error():
    embedder, _ = _embedder(SimpleNamespace(embeddings=[SimpleNamespace(values=_vec())]))
    with pytest.raises(EmbeddingError, match="数が合いません"):
        embedder.embed(["一つ目", "二つ目"])


def test_api_errors_keep_only_the_type_and_status():
    class ServerError(Exception):
        code = 503

    embedder, _ = _embedder(ServerError("https://example.test/secret-url?key=abc 本文"))
    with pytest.raises(EmbeddingError) as exc:
        embedder.embed(["本文"])
    assert str(exc.value) == "ServerError (status=503)"


def test_the_sdk_is_told_not_to_retry():
    embedder = GeminiEmbedder("test-key", ApiBudget(1))
    options = embedder._client._api_client._http_options
    assert options.retry_options.attempts == 1


def test_the_budget_stops_the_call_before_it_is_sent():
    embedder, models = _embedder(SimpleNamespace(embeddings=[SimpleNamespace(values=_vec())]), ApiBudget(1))
    embedder.embed(["a"])
    with pytest.raises(BudgetExceeded):
        embedder.embed(["b"])
    assert len(models.calls) == 1


# ---- Vertex AI の経路（SDK は偽物。EMBEDDING_USE_VERTEXAI） ----


def _record_clients(monkeypatch):
    from google import genai

    created: list[dict] = []

    class RecordingClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(genai, "Client", RecordingClient)
    return created


def test_the_api_key_path_is_the_default_and_ignores_the_llm_vertex_variable(monkeypatch):
    created = _record_clients(monkeypatch)
    monkeypatch.delenv("EMBEDDING_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    embedding.make_client("test-key", 1000)
    (kwargs,) = created
    assert kwargs["vertexai"] is False and kwargs["api_key"] == "test-key"
    assert "project" not in kwargs and "location" not in kwargs


def test_vertex_ai_uses_adc_with_the_global_location_and_no_api_key(monkeypatch):
    created = _record_clients(monkeypatch)
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", "true")
    monkeypatch.delenv("EMBEDDING_VERTEX_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    embedding.make_client("must-not-be-used", 1000)
    (kwargs,) = created
    assert kwargs["vertexai"] is True
    assert "api_key" not in kwargs
    assert kwargs["project"] is None and kwargs["location"] == "global"
    assert kwargs["http_options"].retry_options.attempts == 1


def test_vertex_ai_takes_the_project_and_location_from_the_environment(monkeypatch):
    created = _record_clients(monkeypatch)
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", "1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-project")
    monkeypatch.setenv("EMBEDDING_VERTEX_LOCATION", "us")
    embedding.make_client(None, 1000)
    (kwargs,) = created
    assert (kwargs["project"], kwargs["location"]) == ("some-project", "us")


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_vertex_ai_is_off_unless_the_variable_is_truthy(monkeypatch, value):
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", value)
    assert embedding.use_vertex() is False


def test_the_embedder_is_built_without_a_key_on_vertex_ai(monkeypatch):
    created = _record_clients(monkeypatch)
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", "true")
    GeminiEmbedder(None, ApiBudget(1))
    assert created[0]["vertexai"] is True


# ---- embed_pending ----


def test_a_dry_run_makes_no_calls_and_saves_nothing():
    store, budget = FakeStore(70), ApiBudget(10)
    embedder = FakeEmbedder(budget)
    report = embed_pending(store, embedder, budget, max_chunks=300, apply=False)
    assert (report.pending_total, report.selected, report.calls_needed) == (70, 70, 3)
    assert report.characters > 0 and report.estimated_cost_usd > 0
    assert embedder.sent == [] and budget.calls == 0 and store.done == {} and not report.applied


def test_apply_batches_the_chunks_and_saves_them_all():
    store, budget = FakeStore(70), ApiBudget(10)
    embedder = FakeEmbedder(budget)
    report = embed_pending(store, embedder, budget, max_chunks=300, apply=True)
    assert [len(batch) for batch in embedder.sent] == [BATCH_SIZE, BATCH_SIZE, 70 - 2 * BATCH_SIZE]
    assert (report.saved, report.calls, report.error) == (70, 3, None)
    assert embedder.sent[0][:2] == ["title: none | text: 本文1", "title: 見出し2 | text: 本文2"]


def test_vertex_ai_sends_one_text_per_call(monkeypatch):
    # SDK は Vertex の gemini-embedding-2 に 1 回 1 本しか渡させない（複数だと ValueError）。呼び出しの回数も本数に合わせて数える
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", "true")
    assert embedding.batch_size() == 1
    store, budget = FakeStore(5), ApiBudget(10)
    embedder = FakeEmbedder(budget)
    dry = embed_pending(store, embedder, budget, max_chunks=300, apply=False)
    assert dry.calls_needed == 5
    report = embed_pending(store, embedder, budget, max_chunks=300, apply=True)
    assert [len(batch) for batch in embedder.sent] == [1] * 5
    assert (report.saved, report.calls, report.error) == (5, 5, None)


def test_a_second_run_skips_what_is_already_embedded():
    store = FakeStore(40)
    first_budget = ApiBudget(10)
    embed_pending(store, FakeEmbedder(first_budget), first_budget, max_chunks=300, apply=True)
    second_budget = ApiBudget(10)
    second = FakeEmbedder(second_budget)
    report = embed_pending(store, second, second_budget, max_chunks=300, apply=True)
    assert second.sent == [] and (report.selected, report.saved, report.calls) == (0, 0, 0)


def test_max_chunks_caps_the_selection():
    store, budget = FakeStore(100), ApiBudget(10)
    report = embed_pending(store, FakeEmbedder(budget), budget, max_chunks=40, apply=True)
    assert (report.pending_total, report.selected, report.saved) == (100, 40, 40)
    assert store.count_pending() == 60


def test_a_plan_that_exceeds_the_call_cap_is_refused_before_any_call():
    store, budget = FakeStore(100), ApiBudget(2)  # 100 件は 4 回要る
    embedder = FakeEmbedder(budget)
    report = embed_pending(store, embedder, budget, max_chunks=300, apply=True)
    assert embedder.sent == [] and budget.calls == 0 and store.done == {}
    assert report.error and "上限" in report.error


def test_a_failure_keeps_earlier_batches_and_the_next_run_continues():
    store, budget = FakeStore(70), ApiBudget(10)
    report = embed_pending(store, FakeEmbedder(budget, fail_on_call=2), budget, max_chunks=300, apply=True)
    assert report.error == "ServerError (status=503)"
    assert (report.saved, report.calls, store.count_pending()) == (BATCH_SIZE, 2, 70 - BATCH_SIZE)
    budget2 = ApiBudget(10)
    resumed = FakeEmbedder(budget2)
    report2 = embed_pending(store, resumed, budget2, max_chunks=300, apply=True)
    assert (report2.saved, report2.error, store.count_pending()) == (70 - BATCH_SIZE, None, 0)
    assert sum(len(b) for b in resumed.sent) == 70 - BATCH_SIZE


def test_a_changed_heading_makes_an_embedded_chunk_pending_again():
    store, budget = FakeStore(3), ApiBudget(10)
    embed_pending(store, FakeEmbedder(budget), budget, max_chunks=300, apply=True)
    assert store.count_pending() == 0
    old = store.chunks[2]
    store.chunks[2] = PendingChunk(old.id, old.content_hash, "書き換えた見出し", old.content)  # 本文のハッシュは同じ
    budget2 = ApiBudget(10)
    embedder = FakeEmbedder(budget2)
    report = embed_pending(store, embedder, budget2, max_chunks=300, apply=True)
    assert (report.selected, report.saved) == (1, 1)
    assert embedder.sent == [["title: 書き換えた見出し | text: 本文2"]]


def test_a_too_long_input_stops_the_run_before_any_call():
    store, budget = FakeStore(3), ApiBudget(10)
    store.chunks[2] = PendingChunk(2, "h2", None, "あ" * (embedding.MAX_INPUT_CHARS + 1))
    embedder = FakeEmbedder(budget)
    report = embed_pending(store, embedder, budget, max_chunks=300, apply=True)
    assert embedder.sent == [] and budget.calls == 0 and store.done == {}
    assert report.error and "id: [2]" in report.error


def test_rows_whose_text_changed_meanwhile_are_not_counted_as_saved():
    store, budget = FakeStore(3, stale_hash_ids=frozenset({2})), ApiBudget(10)
    report = embed_pending(store, FakeEmbedder(budget), budget, max_chunks=300, apply=True)
    assert report.saved == 2 and 2 not in store.done


# ---- コマンド ----


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")


def test_the_command_needs_a_database_url(_env, capsys):
    assert cli.main([]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_apply_needs_a_real_api_key(_env, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused.invalid/db")
    monkeypatch.setenv("GEMINI_API_KEY", "your_api_key_here")
    assert cli.main(["--apply"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_apply_on_vertex_ai_does_not_need_an_api_key(_env, monkeypatch, capsys):
    # キーの検査を通り、そのあとの DB 接続で失敗する（1）。キーが無いための終了（2）にはならない
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused.invalid/db")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", "true")
    assert cli.main(["--apply"]) == 1
    assert "GEMINI_API_KEY" not in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--max-chunks", "--max-calls"])
def test_caps_must_be_positive(_env, flag, capsys):
    assert cli.main([flag, "0"]) == 2


def test_default_caps_match_the_agreed_limits():
    assert (embedding.DEFAULT_MAX_CHUNKS, embedding.DEFAULT_MAX_CALLS) == (300, 10)
    assert embedding.MODEL == "gemini-embedding-2" and embedding.DIMENSIONS == 768


# ---- PostgresEmbeddingStore（接続は偽物。SQL そのものは Supabase で確かめる） ----


class _FakeCursor:
    def __init__(self, rows=None, rowcount=1):
        self._rows, self.rowcount = rows or [], rowcount

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows=None):
        self.rows, self.executed = rows or [], []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return _FakeCursor(self.rows)

    def transaction(self):
        from contextlib import nullcontext

        return nullcontext()


def _row(chunk: PendingChunk, *, has_vector=True, model="gemini-embedding-2", dims=768, stored=None):
    return (
        chunk.id,
        chunk.content_hash,
        chunk.heading,
        chunk.content,
        has_vector,
        model,
        dims,
        stored or chunk.input_hash,
    )


def test_the_store_selects_rows_that_are_missing_or_made_from_another_input():
    ok = PendingChunk(1, "h1", "見出し", "本文1")
    no_vector = PendingChunk(2, "h2", None, "本文2")
    other_model = PendingChunk(3, "h3", None, "本文3")
    other_dims = PendingChunk(4, "h4", None, "本文4")
    heading_changed = PendingChunk(5, "h5", "新しい見出し", "本文5")
    rows = [
        _row(ok),
        _row(no_vector, has_vector=False, model=None, dims=None, stored=""),
        _row(other_model, model="gemini-embedding-001"),
        _row(other_dims, dims=3072),
        _row(heading_changed, stored=input_hash("古い見出し", "本文5")),
    ]
    store = PostgresEmbeddingStore(_FakeConn(rows))
    assert [c.id for c in store.pending_embeddings(100)] == [2, 3, 4, 5]
    assert store.count_pending() == 4
    assert [c.id for c in store.pending_embeddings(2)] == [2, 3]


def test_the_store_updates_only_rows_whose_input_is_unchanged():
    conn = _FakeConn()
    chunk = PendingChunk(7, "h7", "見出し", "本文7")
    assert PostgresEmbeddingStore(conn).save_embeddings([(chunk, _vec())]) == 1
    sql, params = conn.executed[0]
    assert "content_hash = %s" in sql and "heading IS NOT DISTINCT FROM %s" in sql and "embedding_input_hash" in sql
    assert params[-3:] == (7, "h7", "見出し")
    assert chunk.input_hash in params
