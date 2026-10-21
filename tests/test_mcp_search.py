"""資料検索の MCP サーバー（ops/mcp_search/main.py）。実 API・実 DB は使わない。

検索（`search_vector.search`）と DB のプールは偽物に差し替える。`mcp` パッケージが要るテスト（ツールの定義と、
クライアントからの呼び出し）は、入っていない環境ではスキップする（アプリの依存には足さない）。
"""

import asyncio
import logging
from typing import Any

import pytest

from app import history, search_vector
from app.search_vector import Hit
from ops.mcp_search import main as mcp_main

SECRET = "AIzaSECRETKEY postgresql://user:pw@db.example.com/x https://example.com/leak"


@pytest.fixture(autouse=True)
def no_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """プールは既定で『開いている』ことにして、実 DB へ接続しない。開く処理を確かめるテストは自分で外す。"""
    monkeypatch.setattr(history, "_pool", object())


def fake_search(monkeypatch: pytest.MonkeyPatch, hits: list[Hit] | Exception) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    async def search(query: str, top_k: int) -> list[Hit]:
        calls.append((query, top_k))
        if isinstance(hits, Exception):
            raise hits
        return hits

    monkeypatch.setattr(search_vector, "search", search)
    return calls


def test_format_shows_source_page_and_similarity() -> None:
    text = mcp_main.format_hits(
        [
            Hit("zahard.pdf", "見出し\n本文A", page=3, page_end=None, similarity=0.8149),
            Hit("zahard.pdf", "本文B", page=4, page_end=5, similarity=0.7),
            Hit("cafe.pdf", "本文C", page=None, page_end=None, similarity=None),
        ]
    )
    assert text == (
        "[zahard.pdf p.3 類似度 0.81]\n見出し\n本文A\n\n[zahard.pdf p.4-5 類似度 0.70]\n本文B\n\n[cafe.pdf]\n本文C"
    )


def test_format_ignores_an_invalid_page() -> None:
    text = mcp_main.format_hits([Hit("a.pdf", "x", page=0, page_end=2, similarity=0.9)])
    assert text == "[a.pdf 類似度 0.90]\nx"


def test_format_without_hits_is_a_fixed_sentence() -> None:
    assert mcp_main.format_hits([]) == mcp_main._NOT_FOUND_TEXT


def test_search_passes_query_and_top_k_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_search(monkeypatch, [Hit("a.pdf", "本文", page=1, similarity=0.9)])
    text = asyncio.run(mcp_main.search_documents("寮の門限", 3))
    assert calls == [("寮の門限", 3)]
    assert text == "[a.pdf p.1 類似度 0.90]\n本文"


def test_default_top_k_matches_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.tools import TOP_K

    assert mcp_main.DEFAULT_TOP_K == TOP_K
    calls = fake_search(monkeypatch, [])
    asyncio.run(mcp_main.search_documents("x"))
    assert calls == [("x", TOP_K)]


def test_search_failure_returns_a_fixed_message_and_logs_only_the_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    fake_search(monkeypatch, RuntimeError(SECRET))
    with caplog.at_level(logging.INFO), pytest.raises(ToolError) as info:
        asyncio.run(mcp_main.search_documents("x", 2))
    assert str(info.value) == mcp_main._SEARCH_FAILED_TEXT
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert "RuntimeError" in caplog.text
    assert "AIza" not in caplog.text and "postgresql" not in caplog.text and "example.com" not in caplog.text


def test_vector_search_error_is_also_a_fixed_message(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    fake_search(monkeypatch, search_vector.VectorSearchError("質問の埋め込みには GEMINI_API_KEY が要ります"))
    with pytest.raises(ToolError) as info:
        asyncio.run(mcp_main.search_documents("x", 2))
    assert str(info.value) == mcp_main._SEARCH_FAILED_TEXT


def test_the_query_is_not_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    fake_search(monkeypatch, [])
    with caplog.at_level(logging.DEBUG):
        asyncio.run(mcp_main.search_documents("私の秘密の質問", 2))
    assert "私の秘密の質問" not in caplog.text
    assert "hits=0" in caplog.text


def test_pool_is_opened_once_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "_pool", None)
    opened: list[int] = []

    async def open_pool() -> None:
        opened.append(1)
        await asyncio.sleep(0)
        monkeypatch.setattr(history, "_pool", object())

    monkeypatch.setattr(mcp_main, "_open_pool", open_pool)

    async def run() -> None:
        await asyncio.gather(*(mcp_main._ensure_pool() for _ in range(5)))
        await mcp_main._ensure_pool()

    asyncio.run(run())
    assert opened == [1]


def test_pool_without_database_url_is_a_search_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "_pool", None)
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(search_vector.VectorSearchError):
        asyncio.run(mcp_main._open_pool())
    assert history.get_pool() is None


def test_pool_failing_to_open_is_closed_and_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """接続できなかったとき、プールを残さない（次の呼び出しでやり直せる）。"""
    import psycopg_pool

    monkeypatch.setattr(history, "_pool", None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.invalid/x")
    events: list[str] = []

    class FakePool:
        def __init__(self, url: str, **kwargs: Any) -> None:
            events.append("init")
            assert kwargs["open"] is False and kwargs["max_size"] == history.pool_max_size()

        async def open(self, **kwargs: Any) -> None:
            raise OSError(SECRET)

        async def close(self) -> None:
            events.append("close")

        @staticmethod
        async def check_connection(conn: Any) -> None: ...

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", FakePool)
    with pytest.raises(OSError):
        asyncio.run(mcp_main._open_pool())
    assert events == ["init", "close"] and history.get_pool() is None


def test_tool_definition_is_read_only_and_bounded() -> None:
    pytest.importorskip("mcp")
    server = mcp_main.build_server()
    (tool,) = asyncio.run(server.list_tools())
    assert tool.name == "search_documents"
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.openWorldHint is False
    props = tool.inputSchema["properties"]
    assert tool.inputSchema["required"] == ["query"]
    assert props["query"]["minLength"] == 1 and props["query"]["maxLength"] == mcp_main.MAX_QUERY_CHARS
    assert props["top_k"]["minimum"] == 1 and props["top_k"]["maximum"] == mcp_main.MAX_TOP_K
    assert props["top_k"]["default"] == mcp_main.DEFAULT_TOP_K
    # 本文を命令として扱わないよう、説明に書いてある
    assert "信頼できないデータ" in (tool.description or "")


def test_call_from_a_client_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP のクライアントの接続（メモリ上）から、ツールを呼ぶ。入力の範囲外は、検索まで届かない。"""
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    calls = fake_search(monkeypatch, [Hit("a.pdf", "本文", page=2, similarity=0.88)])
    server = mcp_main.build_server()

    async def run() -> tuple[Any, Any, Any]:
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            ok = await client.call_tool("search_documents", {"query": "寮", "top_k": 1})
            too_many = await client.call_tool("search_documents", {"query": "寮", "top_k": 6})
            empty = await client.call_tool("search_documents", {"query": ""})
            return ok, too_many, empty

    ok, too_many, empty = asyncio.run(run())
    assert not ok.isError and ok.content[0].text == "[a.pdf p.2 類似度 0.88]\n本文"
    assert too_many.isError and empty.isError
    assert calls == [("寮", 1)]
