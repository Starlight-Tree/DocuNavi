"""アプリの利用資格（`MEMBERSHIP_MODE`。`app/access.py` の `enforce_membership` / `current_member`、表 `app_members`）のテスト。実 DB・実 API は使わない
（本物の Postgres を使う部分は `PG_TEST_URL` があるときだけ、使い捨ての DB で確かめる）。

確かめるのは、次の 6 つ。
1. 資料・チャット・会話を扱う全経路（`/health` 以外。ルートの一覧から自動で数える）が、資格のない利用者を、LLM を呼ぶ前に 403 で断ること。
   ストリーミングも、応答を始める前に断る。新しい経路が関門を通らなければ、このテストが落ちる。
2. 資格の 403 が、資料の権限の 403 と機械的に区別できること（識別コード `APP_MEMBERSHIP_REQUIRED`）。DB が引けないときは 403 ではなく 503。
3. `MEMBERSHIP_MODE` と `ACCESS_MODE` が独立していること（片方が off でも、もう片方は効く）。
4. `shadow` は拒否しないこと（DB 障害でも続ける）。`off` は DB を読まないこと。
5. 判定のキャッシュ（許可・不許可の両方を控える。期限が切れると引き直す。引けなかった結果は控えない）と、取り消しが
   有効な JWT を持つ利用者にも効くこと（資格は JWT の寿命ではなく、リクエストごとに確かめる）。
6. 起動時の検査（知らない値・匿名との組み合わせ・表が無いとき）と、運用コマンド `member`、表のマイグレーション。
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import access, access_admin, auth, startup_checks
from app.agents import graph as graph_module
from app.api import routes
from app.main import app
from app.pg import PgPool, SyncPg
from tests.test_access import (  # noqa: F401  (fixture)
    DEFAULT_ROLE_SET,
    docs_pool,
    grouped_search,
    groups_pool,
    set_access,
)
from tests.test_history_pg_backend import MIGRATIONS, needs_pg, pg_url  # noqa: F401  (fixture)
from tests.test_threads import _bearer, required, signer, user  # noqa: F401  (fixture)

CODE = access.MEMBERSHIP_REQUIRED_CODE
REFUSED = {"detail": access.DETAIL_MEMBERSHIP_REQUIRED, "code": CODE}
UNAVAILABLE = {"detail": access.DETAIL_MEMBERSHIP_UNAVAILABLE}
PUBLIC_PATHS = {"/health"}  # 認証も資格も要らない（死活確認）。ここに足すのは、意図して公開する経路だけ


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("MEMBERSHIP_MODE", raising=False)
    monkeypatch.delenv("ACCESS_MODE", raising=False)
    access.clear_cache()
    yield
    access.clear_cache()


@pytest.fixture
def set_membership(monkeypatch: pytest.MonkeyPatch):
    """MEMBERSHIP_MODE と、利用資格の判定（DB の代わり）を決める。引いた利用者 ID の一覧を返す。"""

    def install(mode: str, member: bool = True, error: Exception | None = None) -> list[Any]:
        calls: list[Any] = []

        async def fake_is_member(user_id: str | None) -> bool:
            calls.append(user_id)
            if error is not None:
                raise error
            return member

        monkeypatch.setenv("MEMBERSHIP_MODE", mode)
        monkeypatch.setattr(access, "is_member", fake_is_member)
        return calls

    return install


@pytest.fixture
def llm_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """グラフ（LLM）を作った回数。断られた要求は、LLM に届かない（グラフを作る前に断る）。"""
    built: list[int] = []
    real = graph_module.create_llm

    def spy():
        built.append(1)
        return real()

    monkeypatch.setattr(graph_module, "create_llm", spy)
    routes._graph_for.cache_clear()
    yield built
    routes._graph_for.cache_clear()


def _fill(path: str) -> str:
    """ルートの `{パラメーター}` に、形だけ合う値を入れる。"""
    out = path
    while "{" in out:
        start, end = out.index("{"), out.index("}")
        name = out[start + 1 : end]
        out = out[:start] + ("1" if name.endswith("id") and "thread" not in name else "abc") + out[end + 1 :]
    return out


def _api_routes() -> list[APIRoute]:
    """アプリの全 `APIRoute`。FastAPI は `include_router` を遅延させるので、`_IncludedRouter` の中（`original_router`）まで辿る。"""
    found: list[APIRoute] = []

    def walk(routes_: Any, prefix: str = "") -> None:
        for route in routes_:
            if isinstance(route, APIRoute):
                found.append(route)
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes, prefix + (getattr(route.include_context, "prefix", "") or ""))

    walk(app.routes)
    assert not any(r.path.startswith("//") for r in found)
    return found


def _guarded_routes() -> list[tuple[str, str]]:
    found = []
    for route in _api_routes():
        if route.path not in PUBLIC_PATHS:
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                found.append((method.lower(), _fill(route.path)))
    return found


def _calls_in(dependant: Any) -> set[Any]:
    """依存の木にある関数すべて。"""
    out: set[Any] = set()
    for sub in dependant.dependencies:
        out.add(sub.call)
        out |= _calls_in(sub)
    return out


def _call(client: TestClient, method: str, url: str, **kwargs: Any):
    if method == "post":
        kwargs.setdefault("json", {"message": "大気", "thread_id": str(uuid.uuid4())})
    return client.request(method.upper(), url, **kwargs)


# ---- 1. 全経路が関門を通る ------------------------------------------------------------------------------


def test_the_route_enumeration_sees_the_data_paths():
    """この一覧が空・極端に短いと、下の総当たりが何も確かめていないことになる。"""
    found = set(_guarded_routes())
    assert {("post", "/chat"), ("post", "/chat/stream"), ("get", "/documents"), ("get", "/threads")} <= found
    assert ("delete", "/threads/abc") in found and ("get", "/collections") in found
    assert len(found) >= 12


def test_every_route_but_health_has_the_membership_gate_in_its_dependencies():
    """振る舞いの総当たり（下）に加えて、構造でも確かめる。関門を通らない経路を足すと、ここで名前が出る。"""
    gates = {access.request_access, access.current_member}
    ungated = [r.path for r in _api_routes() if r.path not in PUBLIC_PATHS and not (_calls_in(r.dependant) & gates)]
    assert ungated == []


def test_every_route_but_health_refuses_a_non_member_before_doing_anything(client, set_membership, llm_calls):
    calls = set_membership("enforce", member=False)
    for method, url in _guarded_routes():
        res = _call(client, method, url)
        assert (res.status_code, res.json()) == (403, REFUSED), (method, url)
    assert calls  # 判定は実際に引かれた
    assert llm_calls == []  # 断られた要求は、LLM（グラフ）に届かない


def test_health_stays_public(client, set_membership):
    calls = set_membership("enforce", member=False)
    assert client.get("/health").status_code == 200
    assert calls == []


def test_streaming_is_refused_with_a_plain_403_before_any_stream_starts(client, set_membership, llm_calls):
    set_membership("enforce", member=False)
    res = client.post("/chat/stream", json={"message": "大気", "thread_id": str(uuid.uuid4())})
    assert res.status_code == 403 and res.json() == REFUSED
    assert "text/event-stream" not in res.headers["content-type"]
    assert llm_calls == []


def test_a_member_is_not_refused_on_the_data_paths(
    client, set_membership, set_access, grouped_search, docs_pool, groups_pool
):
    calls = set_membership("enforce", member=True)
    set_access("enforce", DEFAULT_ROLE_SET)
    thread = str(uuid.uuid4())
    for method, url in (
        ("get", "/documents"),
        ("get", "/collections"),
        ("get", "/documents/2/chunks"),
        ("get", "/documents/3/graph"),
        ("get", "/documents/3/impact?node=a13"),
        ("post", "/chat"),
        ("post", "/chat/stream"),
    ):
        res = _call(
            client, method, url, **({"json": {"message": "営業時間", "thread_id": thread}} if method == "post" else {})
        )
        assert res.status_code == 200, (method, url, res.status_code)
    assert len(calls) == 7


# ---- 2. 資格の 403 は、機械的に区別できる ----------------------------------------------------------------


def test_the_membership_403_has_a_code_and_the_resource_403_does_not(
    client, set_membership, set_access, grouped_search, docs_pool, groups_pool
):
    set_membership("enforce", member=True)
    set_access("enforce", frozenset())  # 資格はあるが、見える資料が 0 件
    res = client.get("/documents")
    assert res.status_code == 403 and res.json() == {"detail": access.DETAIL_NO_COLLECTIONS}
    assert "code" not in res.json()

    set_membership("enforce", member=False)
    res = client.get("/documents")
    assert res.status_code == 403 and res.json()["code"] == CODE
    assert res.json()["detail"] == access.DETAIL_MEMBERSHIP_REQUIRED  # 固定の日本語（画面がそのまま見せられる）


def test_when_membership_cannot_be_read_enforce_answers_503_not_403_and_never_allows(
    client, set_membership, llm_calls, caplog
):
    set_membership("enforce", error=RuntimeError("postgresql://u:pw@h/x AIzaSECRET"))
    for method, url in _guarded_routes():
        res = _call(client, method, url)
        assert (res.status_code, res.json()) == (503, UNAVAILABLE), (method, url)
    assert "postgresql://" not in caplog.text and "AIzaSECRET" not in caplog.text
    assert llm_calls == []


# ---- 3. MEMBERSHIP_MODE と ACCESS_MODE は独立 -----------------------------------------------------------


def test_membership_enforce_works_even_when_access_is_off(client, set_membership, set_access):
    visible_calls = set_access("off")
    set_membership("enforce", member=False)
    res = client.get("/collections")
    assert (res.status_code, res.json()) == (403, REFUSED)
    assert visible_calls == []  # 資料の権限は読んでいない（off）


def test_membership_off_leaves_access_enforce_working_and_reads_no_membership(
    client, set_membership, set_access, groups_pool
):
    calls = set_membership("off", member=False)  # 資格を見ない。member=False でも断られない
    set_access("enforce", frozenset())
    res = client.get("/collections")
    assert (res.status_code, res.json()) == (403, {"detail": access.DETAIL_NO_COLLECTIONS})  # 資料の権限だけが効く
    set_access("enforce", DEFAULT_ROLE_SET)
    assert client.get("/collections").status_code == 200
    assert calls == []


def test_membership_enforce_503_does_not_depend_on_the_access_mode(client, set_membership, set_access):
    set_access("off")
    set_membership("enforce", error=ConnectionError("x"))
    assert client.get("/collections").json() == UNAVAILABLE


def test_the_default_role_resources_do_not_decide_membership(client, set_membership, set_access):
    """`default` ロールに資料があっても（全員が見える資料）、資格のない利用者は使えない。資料の割り当てで資格は変わらない。"""
    set_membership("enforce", member=False)
    set_access("enforce", DEFAULT_ROLE_SET)
    assert client.get("/collections").json() == REFUSED


# ---- 4. shadow と off -----------------------------------------------------------------------------------


def test_shadow_does_not_refuse_but_logs_what_enforce_would_refuse(
    client, set_membership, set_access, groups_pool, caplog
):
    calls = set_membership("shadow", member=False)
    set_access("enforce", DEFAULT_ROLE_SET)
    with caplog.at_level(logging.INFO, logger="app.access"):
        res = client.get("/collections")
    assert res.status_code == 200
    assert calls and "利用資格(shadow): /collections は enforce では拒否される" in caplog.text


def test_shadow_continues_when_membership_cannot_be_read(client, set_membership, set_access, groups_pool, caplog):
    set_membership("shadow", error=RuntimeError("x"))
    set_access("enforce", DEFAULT_ROLE_SET)
    assert client.get("/collections").status_code == 200
    assert "利用資格を引けませんでした (mode=shadow, RuntimeError)" in caplog.text


def test_off_reads_nothing(client, set_membership, set_access, groups_pool):
    calls = set_membership("off", member=False)
    set_access("enforce", DEFAULT_ROLE_SET)
    assert client.get("/collections").status_code == 200
    assert calls == []


def test_an_unknown_mode_is_an_error_not_off(monkeypatch):
    monkeypatch.setenv("MEMBERSHIP_MODE", "strict")
    with pytest.raises(access.AccessConfigError, match="MEMBERSHIP_MODE"):
        access.membership_mode()


# ---- 5. キャッシュと、取り消し ---------------------------------------------------------------------------


class MemberPool:
    """`app_members` の偽物。SQL の中身は見ず、`uid` が members にあれば 1 行を返す。引いた回数を数える。"""

    def __init__(self, members: set[str] | None = None, error: Exception | None = None) -> None:
        self.members = members if members is not None else set()
        self.error = error
        self.queries: list[dict[str, Any]] = []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Cursor:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self.rows = rows

            async def fetchall(self) -> list[dict[str, Any]]:
                return self.rows

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any]) -> Cursor:
                assert sql == access._MEMBER_SQL, sql
                pool.queries.append(params)
                if pool.error is not None:
                    raise pool.error
                return Cursor([{"ok": 1}] if params["uid"] in pool.members else [])

        yield Conn()


def _is_member(user_id: str | None) -> bool:
    return asyncio.run(access.is_member(user_id))


def _expire(user_id: str) -> None:
    """控えの期限を過去にする（時計は触らない）。"""
    expiry, member, pool = access._member_cache[user_id]
    access._member_cache[user_id] = (time.monotonic() - 1, member, pool)


def test_is_member_caches_both_answers_within_the_ttl(monkeypatch):
    pool = MemberPool({"in"})
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    assert [_is_member("in"), _is_member("in"), _is_member("out"), _is_member("out")] == [True, True, False, False]
    assert [q["uid"] for q in pool.queries] == ["in", "out"]  # 許可も不許可も 1 回ずつだけ引く


def test_is_member_asks_again_after_the_ttl_for_both_answers(monkeypatch):
    pool = MemberPool({"in"})
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    assert (_is_member("in"), _is_member("out")) == (True, False)
    pool.members = {"out"}  # 資格が入れ替わった
    assert (_is_member("in"), _is_member("out")) == (True, False)  # TTL 内は古い答え
    _expire("in")
    _expire("out")
    assert (_is_member("in"), _is_member("out")) == (False, True)  # 期限切れ後は、DB の答え
    assert len(pool.queries) == 4


def test_a_restart_starts_with_an_empty_cache(monkeypatch):
    pool = MemberPool({"u"})
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    assert _is_member("u")
    pool.members = set()
    access.clear_cache()  # 再起動（プロセス内の控えが消える）と同じ
    assert not _is_member("u")


def test_ttl_zero_disables_the_cache(monkeypatch):
    monkeypatch.setenv("ACCESS_CACHE_TTL_SECONDS", "0")
    pool = MemberPool({"u"})
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    assert (_is_member("u"), _is_member("u")) == (True, True)
    assert len(pool.queries) == 2


def test_a_failed_lookup_is_not_cached(monkeypatch):
    pool = MemberPool({"u"}, error=ConnectionError("down"))
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    with pytest.raises(ConnectionError):
        _is_member("u")
    pool.error = None
    assert _is_member("u")  # 失敗を「資格なし」として控えていない


def test_a_different_pool_invalidates_the_cache(monkeypatch):
    first, second = MemberPool({"u"}), MemberPool(set())
    monkeypatch.setattr(access, "get_pool", lambda: first)
    assert _is_member("u")
    monkeypatch.setattr(access, "get_pool", lambda: second)
    assert not _is_member("u")


def test_anonymous_and_no_pool(monkeypatch):
    monkeypatch.setattr(access, "get_pool", lambda: None)
    assert _is_member(None) is False  # 匿名は資格なし（DB も読まない）
    with pytest.raises(access.AccessUnavailable):
        _is_member("u")


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(access, "_CACHE_MAX_ENTRIES", 3)
    pool = MemberPool({"a", "b", "c", "d"})
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    for name in "abcd":
        _is_member(name)
    assert len(access._member_cache) == 3 and "a" not in access._member_cache


def test_revoking_membership_stops_a_user_who_still_holds_a_valid_jwt(client, required, signer, user, monkeypatch):
    """資格は、JWT の寿命（15 分）ではなく、リクエストごとに（控えの TTL 以内で）確かめる。"""
    pool = MemberPool({user})
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    monkeypatch.setenv("MEMBERSHIP_MODE", "enforce")
    headers = _bearer(signer, user)

    assert client.get("/threads", headers=headers).status_code == 200
    pool.members.clear()  # 取り消した（トークンは有効なまま）
    assert (
        client.get("/threads", headers=headers).status_code == 200
    )  # 控えの TTL 内（通常は TTL 以内に効く、という限界）
    _expire(user)  # TTL が過ぎた
    res = client.get("/threads", headers=headers)
    assert (res.status_code, res.json()) == (403, REFUSED)
    assert client.post("/chat", json={"message": "大気"}, headers=headers).json() == REFUSED
    # 同じトークンの検証は通っている（401 ではない）。断っているのは資格
    pool.members.add(user)
    _expire(user)
    assert client.get("/threads", headers=headers).status_code == 200  # 付与も、期限切れ後に効く


def test_a_user_without_membership_cannot_use_a_valid_jwt_but_is_not_deleted(
    client, required, signer, user, monkeypatch
):
    other = str(uuid.uuid4())
    pool = MemberPool({other})
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    monkeypatch.setenv("MEMBERSHIP_MODE", "enforce")
    assert client.get("/threads", headers=_bearer(signer, user)).json() == REFUSED
    assert client.get("/threads", headers=_bearer(signer, other)).status_code == 200
    assert client.get("/threads").status_code == 401  # 資格より前に、認証（トークンなしは 401）


# ---- 6. 起動時の検査 -------------------------------------------------------------------------------------


def test_startup_rejects_membership_without_required_auth(monkeypatch):
    monkeypatch.setenv("MEMBERSHIP_MODE", "enforce")
    monkeypatch.setenv("AUTH_MODE", "off")
    with pytest.raises(access.AccessConfigError, match="AUTH_MODE=required"):
        access.check_membership_config()


def test_startup_warns_when_membership_is_unset_in_production_but_not_when_explicit(monkeypatch, caplog):
    monkeypatch.setenv("AUTH_MODE", "required")
    with caplog.at_level(logging.INFO, logger="app.access"):
        assert access.check_membership_config() == "off"
    assert "MEMBERSHIP_MODE が未設定です" in caplog.text

    caplog.clear()
    monkeypatch.setenv("MEMBERSHIP_MODE", "off")
    with caplog.at_level(logging.INFO, logger="app.access"):
        assert access.check_membership_config() == "off"
    assert "未設定" not in caplog.text and "MEMBERSHIP_MODE=off" in caplog.text


def test_startup_does_not_warn_in_local_development(monkeypatch, caplog):
    monkeypatch.setenv("AUTH_MODE", "off")
    assert access.check_membership_config() == "off"
    assert "MEMBERSHIP_MODE" not in caplog.text


def test_verify_membership_off_reads_nothing(monkeypatch):
    monkeypatch.setattr(access, "get_pool", lambda: pytest.fail("off では DB を読まない"))
    asyncio.run(access.verify_membership_config("off"))


def test_verify_membership_needs_a_database_and_the_table(monkeypatch):
    monkeypatch.setattr(access, "get_pool", lambda: None)
    with pytest.raises(access.AccessConfigError, match="DB"):
        asyncio.run(access.verify_membership_config("enforce"))

    class NoTable(MemberPool):
        @asynccontextmanager
        async def connection(self):
            raise RuntimeError('relation "app_members" does not exist postgresql://u:pw@h/x')
            yield

    monkeypatch.setattr(access, "get_pool", lambda: NoTable())
    with pytest.raises(access.AccessConfigError, match="マイグレーション 0003") as exc:
        asyncio.run(access.verify_membership_config("shadow"))
    assert "postgresql://" not in str(exc.value)  # 例外の文字列を載せない（型の名前だけ）


def test_verify_membership_warns_when_enforce_has_no_members(monkeypatch, caplog):
    class Counting:
        def __init__(self, n: int) -> None:
            self.n = n

        @asynccontextmanager
        async def connection(self):
            n = self.n

            class Conn:
                async def execute(self, sql: str):
                    assert "app_members" in sql

                    class Cursor:
                        async def fetchall(self_inner):
                            return [{"members": n}]

                    return Cursor()

            yield Conn()

    with caplog.at_level(logging.INFO, logger="app.access"):
        monkeypatch.setattr(access, "get_pool", lambda: Counting(0))
        asyncio.run(access.verify_membership_config("enforce"))
        assert "全員が拒否されます" in caplog.text
        caplog.clear()
        monkeypatch.setattr(access, "get_pool", lambda: Counting(2))
        asyncio.run(access.verify_membership_config("enforce"))
    assert "全員が拒否されます" not in caplog.text and "app_members 2 件" in caplog.text


def test_run_startup_checks_verifies_membership_independently_of_access_mode(monkeypatch):
    seen: list[str] = []

    async def fake_data_checks() -> None:
        seen.append("data")

    async def fake_verify(mode: str) -> None:
        seen.append(f"membership:{mode}")

    monkeypatch.setattr(startup_checks, "_run_data_checks", fake_data_checks)
    monkeypatch.setattr(access, "verify_membership_config", fake_verify)
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("ACCESS_MODE", "off")
    monkeypatch.setenv("MEMBERSHIP_MODE", "enforce")
    asyncio.run(startup_checks.run_startup_checks())
    assert seen == ["data", "membership:enforce"]


def test_the_app_refuses_to_start_when_membership_is_on_but_unusable(required, monkeypatch):
    monkeypatch.setenv("MEMBERSHIP_MODE", "enforce")  # DATABASE_URL が無い（DB を開けない）
    with pytest.raises(access.AccessConfigError), TestClient(app):
        pass


def test_the_app_starts_with_membership_off_and_reads_no_membership(required, monkeypatch):
    monkeypatch.setattr(access, "get_pool", lambda: pytest.fail("MEMBERSHIP_MODE が未設定・off のとき、DB は読まない"))
    with TestClient(app) as started:
        assert started.get("/health").status_code == 200


# ---- 6. 運用コマンドと表（本物の Postgres） --------------------------------------------------------------


def _seed(url: str) -> None:
    with SyncPg(url) as db:
        db.batch(
            [
                ("INSERT INTO roles (name, label) VALUES ('default', 'D')", None),
                (
                    "INSERT INTO \"user\" (id, name, email) VALUES ('u1', 'U1', 'One@Example.com'), ('u2', 'U2', 'two@example.com')",
                    None,
                ),
            ]
        )


def _admin(monkeypatch, url: str, *argv: str) -> int:
    monkeypatch.setattr(access_admin, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DB_BACKEND", "pg")
    monkeypatch.setenv("DATABASE_URL", url)
    return access_admin.main(list(argv))


def _rows(url: str, sql: str) -> list[dict[str, Any]]:
    with SyncPg(url) as db:
        return db.execute(sql)


def test_the_migration_creates_an_empty_table_and_no_user_id_is_in_it():
    text = next(p for p in MIGRATIONS if p.name == "0003_app_members.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE app_members" in text
    assert "INSERT" not in text.upper().replace(
        "INSERTED", ""
    )  # 資格の付与は運用コマンド。本番の利用者 ID を埋め込まない


@needs_pg
def test_app_members_starts_empty_and_follows_the_user_row(pg_url):
    _seed(pg_url)
    assert _rows(pg_url, "SELECT count(*) AS n FROM app_members")[0]["n"] == 0
    with SyncPg(pg_url) as db:
        db.batch([("INSERT INTO app_members (user_id) VALUES ('u1')", None)])
    assert _rows(pg_url, "SELECT note FROM app_members") == [{"note": ""}]
    with SyncPg(pg_url) as db:
        db.batch([("DELETE FROM \"user\" WHERE id = 'u1'", None)])
    assert _rows(pg_url, "SELECT count(*) AS n FROM app_members")[0]["n"] == 0  # ユーザー行を消せば、資格も消える


@needs_pg
def test_is_member_against_a_real_postgres(pg_url, monkeypatch):
    _seed(pg_url)
    with SyncPg(pg_url) as db:
        db.batch([("INSERT INTO app_members (user_id) VALUES ('u1')", None)])

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            monkeypatch.setattr(access, "get_pool", lambda: pool)
            return [await access.is_member(u) for u in ("u1", "u2", "nobody")]
        finally:
            await pool.close()

    assert asyncio.run(run()) == [True, False, False]


@needs_pg
def test_member_command_is_a_dry_run_by_default_and_idempotent(pg_url, monkeypatch, capsys):
    _seed(pg_url)
    assert _admin(monkeypatch, pg_url, "member", "add", "--email", "one@example.com") == 0
    out = capsys.readouterr().out
    assert "なし → あり" in out and "書き込みはしていません" in out
    assert _rows(pg_url, "SELECT count(*) AS n FROM app_members")[0]["n"] == 0  # --apply が無ければ何も書かない

    assert _admin(monkeypatch, pg_url, "member", "add", "--email", "ONE@example.com", "--note", "owner", "--apply") == 0
    assert _rows(pg_url, "SELECT user_id, note FROM app_members") == [{"user_id": "u1", "note": "owner"}]
    audit = _rows(pg_url, "SELECT actor, table_name, op, row_data FROM access_audit")
    assert [(a["actor"], a["table_name"], a["op"]) for a in audit] == [("access_admin_cli", "app_members", "INSERT")]
    assert "One@Example.com" not in str(audit)  # 監査にメールは入れない

    capsys.readouterr()
    assert _admin(monkeypatch, pg_url, "member", "add", "--email", "one@example.com", "--apply") == 0  # 再実行
    assert "変更なし" in capsys.readouterr().out
    assert _rows(pg_url, "SELECT count(*) AS n FROM app_members")[0]["n"] == 1
    assert len(_rows(pg_url, "SELECT 1 FROM access_audit")) == 1  # 変更がなければ監査も増えない

    assert _admin(monkeypatch, pg_url, "member", "show", "--user-id", "u1") == 0
    assert "利用資格: あり" in capsys.readouterr().out
    assert _admin(monkeypatch, pg_url, "member", "list") == 0
    out = capsys.readouterr().out
    assert "1 人" in out and "u1" not in out and "example.com" not in out

    assert _admin(monkeypatch, pg_url, "member", "remove", "--user-id", "u1") == 0  # 確認だけ
    assert _rows(pg_url, "SELECT count(*) AS n FROM app_members")[0]["n"] == 1
    capsys.readouterr()
    assert _admin(monkeypatch, pg_url, "member", "remove", "--user-id", "u1", "--apply") == 0
    assert "TTL" in capsys.readouterr().out
    assert _rows(pg_url, "SELECT count(*) AS n FROM app_members")[0]["n"] == 0
    audit = _rows(pg_url, "SELECT op, row_data FROM access_audit ORDER BY id")
    assert [a["op"] for a in audit] == ["INSERT", "DELETE"]  # 変更と監査は同じ束で書かれ、別の接続から見ても揃っている
    assert all(a["row_data"] == {"user_id": "u1"} for a in audit)


@needs_pg
def test_the_cache_and_a_real_postgres_work_together(pg_url, monkeypatch):
    """実際の DB の判定が、控え（TTL）を通して返ること。書き込みは別の接続（運用コマンド）から行い、保存されたものを読む。"""
    _seed(pg_url)

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            monkeypatch.setattr(access, "get_pool", lambda: pool)
            first = await access.is_member("u1")  # 資格なし（控える）
            assert _admin(monkeypatch, pg_url, "member", "add", "--user-id", "u1", "--apply") == 0  # 別の接続で付与
            within_ttl = await access.is_member("u1")  # TTL 内は、控えの古い答え
            _expire("u1")
            after_expiry = await access.is_member("u1")  # 期限切れ後は、DB に保存された新しい答え
            assert _admin(monkeypatch, pg_url, "member", "remove", "--user-id", "u1", "--apply") == 0
            _expire("u1")
            after_revoke = await access.is_member("u1")
            return first, within_ttl, after_expiry, after_revoke
        finally:
            await pool.close()

    assert asyncio.run(run()) == (False, False, True, False)


def _audit_blocks_members(url: str, blocked: bool) -> None:
    """監査表への書き込みを、`app_members` の行についてだけ失敗させる（または解除する）。監査の書き込みが失敗した状況を作る。"""
    with SyncPg(url) as db:
        if blocked:
            db.batch(
                [
                    (
                        "ALTER TABLE access_audit ADD CONSTRAINT audit_blocks_members CHECK (table_name <> 'app_members') NOT VALID",
                        None,
                    )
                ]
            )
        else:
            db.batch([("ALTER TABLE access_audit DROP CONSTRAINT audit_blocks_members", None)])


@needs_pg
def test_when_the_audit_write_fails_the_membership_change_is_rolled_back(pg_url, monkeypatch, capsys):
    """変更と監査は 1 つの束（1 トランザクション）。監査が書けなければ、資格の付与も取り消しも、書かれない（別の接続から確かめる）。"""
    _seed(pg_url)

    _audit_blocks_members(pg_url, True)
    assert _admin(monkeypatch, pg_url, "member", "add", "--user-id", "u1", "--apply") == 1
    assert "失敗" in capsys.readouterr().err
    assert _rows(pg_url, "SELECT count(*) AS n FROM app_members")[0]["n"] == 0  # 付与されていない
    assert _rows(pg_url, "SELECT count(*) AS n FROM access_audit")[0]["n"] == 0

    _audit_blocks_members(pg_url, False)
    assert _admin(monkeypatch, pg_url, "member", "add", "--user-id", "u1", "--apply") == 0
    assert _rows(pg_url, "SELECT user_id FROM app_members") == [{"user_id": "u1"}]

    _audit_blocks_members(pg_url, True)
    capsys.readouterr()
    assert _admin(monkeypatch, pg_url, "member", "remove", "--user-id", "u1", "--apply") == 1
    assert "失敗" in capsys.readouterr().err
    assert _rows(pg_url, "SELECT user_id FROM app_members") == [{"user_id": "u1"}]  # 取り消されていない
    assert [a["op"] for a in _rows(pg_url, "SELECT op FROM access_audit")] == ["INSERT"]  # 成功した付与の分だけ


@needs_pg
def test_member_command_leaves_roles_alone_and_refuses_unknown_users(pg_url, monkeypatch, capsys):
    _seed(pg_url)
    assert _admin(monkeypatch, pg_url, "member", "add", "--email", "one@example.com", "--apply") == 0
    assert _rows(pg_url, "SELECT count(*) AS n FROM user_roles")[0]["n"] == 0  # 資格を付けても、ロールは変わらない
    capsys.readouterr()
    assert _admin(monkeypatch, pg_url, "member", "add", "--email", "ghost@example.com", "--apply") == 1
    assert "見つかりません" in capsys.readouterr().err
    assert _rows(pg_url, "SELECT count(*) AS n FROM app_members")[0]["n"] == 1  # 存在しない利用者には付けない


def test_member_command_needs_a_user_except_for_list():
    with pytest.raises(SystemExit) as exc:
        access_admin.main(["member", "add"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        access_admin.main(["member", "grant", "--email", "a@example.com"])
    assert exc.value.code == 2


def test_the_auth_module_is_unchanged_by_membership():
    """資格はバックエンド（`access.py`）の関門で確かめる。トークンの検証（`app/auth.py`）は、資格を知らない。"""
    assert not hasattr(auth, "is_member") and not hasattr(auth, "enforce_membership")
