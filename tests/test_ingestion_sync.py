"""R2 の突き合わせ（app/ingestion/sync.py）。取り込みと埋め込みは偽物で、R2・DB・API は使わない。"""

import pytest

from app import r2_store
from app.ingestion import sync


class Calls:
    """取り込み・埋め込みの偽物。呼ばれた順と引数を記録し、資料群ごとの終了コードを返す。"""

    def __init__(self, ingest_codes: dict[str, int] | None = None, embed_code: int = 0) -> None:
        self.ingest_codes = ingest_codes or {}
        self.embed_code = embed_code
        self.log: list[tuple[str, list[str]]] = []

    def ingest(self, argv):
        self.log.append(("ingest", list(argv)))
        name = argv[1].removeprefix("r2://")
        return self.ingest_codes.get(name, 0)

    def embed(self, argv):
        self.log.append(("embed", list(argv)))
        return self.embed_code

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.log]


def _run(names, calls, **kwargs):
    kwargs.setdefault("apply", True)
    return sync.run_sync(names, calls.ingest, calls.embed, **kwargs)


def test_ingests_every_collection_then_embeds_once():
    calls = Calls()
    assert _run(["cafe", "guides", "zahard"], calls) == 0
    assert calls.kinds() == ["ingest", "ingest", "ingest", "embed"]  # 埋め込みは、全部の取り込みのあとに 1 回
    assert calls.log[0][1] == ["--input", "r2://cafe", "--apply"]
    assert calls.log[-1][1][0] == "--apply"


def test_check_only_passes_no_apply_to_either_command():
    calls = Calls()
    assert _run(["cafe"], calls, apply=False) == 0
    assert all("--apply" not in argv for _, argv in calls.log)
    assert calls.kinds() == ["ingest", "embed"]  # 確認だけでも、埋め込みの見積りは出す


def test_embed_limits_are_passed_through():
    calls = Calls()
    _run(["cafe"], calls, max_chunks=50, max_calls=2)
    assert calls.log[-1][1] == ["--apply", "--max-chunks", "50", "--max-calls", "2"]


def test_broken_pdf_still_embeds_the_others_and_exits_zero(capsys):
    # 壊れた PDF は取り込むたびに終了コード 3 になる。ここで止めると、他の資料が永久に検索に出ない
    calls = Calls({"guides": 3})
    assert _run(["cafe", "guides"], calls) == 0
    assert calls.kinds()[-1] == "embed"
    err = capsys.readouterr().err
    assert "guides" in err and "failure_reason" in err


@pytest.mark.parametrize("code", [1, 2, 7])
def test_failed_ingest_skips_embedding(code, capsys):
    calls = Calls({"guides": code})
    result = _run(["cafe", "guides"], calls)
    assert result == (2 if code == 2 else 1)  # 想定外のコードは、一時的な失敗として 1
    assert "embed" not in calls.kinds()
    assert "guides" in capsys.readouterr().err


def test_mixed_failures_report_one():
    calls = Calls({"cafe": 2, "guides": 1})
    assert _run(["cafe", "guides"], calls) == 1
    assert "embed" not in calls.kinds()


def test_other_collections_are_still_ingested_after_a_failure():
    calls = Calls({"cafe": 1})
    _run(["cafe", "guides"], calls)
    assert calls.kinds() == ["ingest", "ingest"]


def test_embed_failure_is_reported():
    assert _run(["cafe"], Calls(embed_code=1)) == 1
    assert _run(["cafe"], Calls(embed_code=2)) == 2
    assert _run(["cafe"], Calls(embed_code=9)) == 1


def test_oddly_named_prefix_is_skipped_and_does_not_block(capsys):
    calls = Calls()
    assert _run(["cafe", "bad name", "日本語"], calls) == 0
    assert [argv[1] for kind, argv in calls.log if kind == "ingest"] == ["r2://cafe"]
    assert calls.kinds()[-1] == "embed"
    assert "bad name" not in capsys.readouterr().err  # 名前そのものはログに出さない


def test_no_collections_does_nothing(capsys):
    calls = Calls()
    assert _run([], calls) == 0
    assert calls.log == []
    assert "R2 にありません" in capsys.readouterr().err


class FakeStore:
    def __init__(self, names=None, error: Exception | None = None) -> None:
        self._names = names or []
        self._error = error

    def collections(self):
        if self._error:
            raise self._error
        return self._names


def _wire(monkeypatch, store_or_error, calls):
    def default_store():
        if isinstance(store_or_error, Exception):
            raise store_or_error
        return store_or_error

    monkeypatch.setattr(r2_store, "default_store", default_store)
    monkeypatch.setattr(sync, "_call_ingest", calls.ingest)
    monkeypatch.setattr(sync, "_call_embed", calls.embed)


def test_main_lists_r2_and_runs(monkeypatch):
    calls = Calls()
    _wire(monkeypatch, FakeStore(["cafe", "guides"]), calls)
    assert sync.main(["--apply", "--max-chunks", "10"]) == 0
    assert calls.kinds() == ["ingest", "ingest", "embed"]
    assert calls.log[-1][1] == ["--apply", "--max-chunks", "10", "--max-calls", str(sync.DEFAULT_MAX_CALLS)]


def test_main_without_apply_is_check_only(monkeypatch):
    calls = Calls()
    _wire(monkeypatch, FakeStore(["cafe"]), calls)
    assert sync.main([]) == 0
    assert all("--apply" not in argv for _, argv in calls.log)


def test_main_reports_missing_r2_config_without_values(monkeypatch, capsys):
    calls = Calls()
    _wire(monkeypatch, r2_store.R2ConfigError("missing: R2_BUCKET"), calls)
    assert sync.main(["--apply"]) == 2
    assert calls.log == []
    assert "R2_BUCKET" in capsys.readouterr().err


def test_main_hides_r2_error_details(monkeypatch, capsys):
    calls = Calls()
    _wire(monkeypatch, RuntimeError("https://secret-account.r2.cloudflarestorage.com refused"), calls)
    assert sync.main(["--apply"]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret-account" not in err
    assert calls.log == []


@pytest.mark.parametrize("flag", ["--max-chunks", "--max-calls"])
def test_main_rejects_non_positive_limits(flag, monkeypatch):
    calls = Calls()
    _wire(monkeypatch, FakeStore(["cafe"]), calls)
    assert sync.main(["--apply", flag, "0"]) == 2
    assert calls.log == []
