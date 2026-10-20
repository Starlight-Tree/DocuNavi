"""R2 キーの発行 Job（ops/r2_key_issuer/main.py）。Cloudflare・Secret Manager・R2 には接続しない（すべて模擬）。"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ops.r2_key_issuer import main as issuer

PARENT_VALUE = "PARENT-TOKEN-VALUE-DO-NOT-LOG"
NEW_VALUE = "NEW-TOKEN-VALUE-DO-NOT-LOG"
ACCOUNT = "acct123"
BUCKET = "docunavi-documents"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
OLD = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
NEW = (NOW - timedelta(minutes=3)).isoformat().replace("+00:00", "Z")


class FakeCloudflare:
    def __init__(self) -> None:
        self.tokens: list[dict[str, Any]] = []
        self.groups = [
            {"id": "g-read", "name": "Workers R2 Storage Bucket Item Read", "is_selectable": True},
            {"id": "g-write", "name": "Workers R2 Storage Bucket Item Write", "is_selectable": True},
            {"id": "g-other", "name": "Workers R2 Storage Write"},
        ]
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.create_error: issuer.CloudflareError | None = None
        self.create_then_error = False  # 作成は通ったが、応答が届かなかった
        self.delete_failures = 0
        self.expires_on: str | None = None
        self._n = 0

    def verify(self) -> dict[str, Any]:
        return {"id": "parent-id", "status": "active", "expires_on": self.expires_on}

    def permission_groups(self) -> list[dict[str, Any]]:
        return self.groups

    def list_tokens(self) -> list[dict[str, Any]]:
        return [{"id": "parent-id", "name": "docunavi-r2-issuer-parent", "issued_on": OLD}, *self.tokens]

    def create_token(self, name: str, policies: list[dict[str, Any]]) -> dict[str, Any]:
        if self.create_error and not self.create_then_error:
            raise self.create_error
        self._n += 1
        token = {"id": f"tok{self._n}", "name": name, "issued_on": NEW, "policies": policies}
        self.tokens.append(token)
        self.created.append({"name": name, "policies": policies})
        if self.create_then_error:
            assert self.create_error
            raise self.create_error
        return {"id": token["id"], "value": NEW_VALUE}

    def delete_token(self, token_id: str) -> None:
        if self.delete_failures:
            self.delete_failures -= 1
            raise issuer.CloudflareError(500, [], ambiguous=True)
        self.tokens = [t for t in self.tokens if t["id"] != token_id]
        self.deleted.append(token_id)


class FakeSecrets:
    def __init__(self) -> None:
        self.store: dict[str, list[dict[str, str]]] = {}
        self.add_error: Exception | None = None
        self.add_then_error = False  # 保存はされたが、応答が届かなかった
        self.read_error: Exception | None = None

    def read_json(self, name: str) -> dict[str, str] | None:
        if self.read_error:
            raise self.read_error
        versions = self.store.get(name)
        return versions[-1] if versions else None

    def read_text(self, name: str) -> str:
        return PARENT_VALUE

    def add_json(self, name: str, value: dict[str, str]) -> str:
        if self.add_error and not self.add_then_error:
            raise self.add_error
        self.store.setdefault(name, []).append(value)
        if self.add_then_error:
            assert self.add_error
            raise self.add_error
        return str(len(self.store[name]))


class FakeProbe:
    def __init__(self, failures: int = 0) -> None:
        self.failures, self.calls = failures, []

    def __call__(self, creds: dict[str, str], can_write: bool, execution: str) -> list[str]:
        self.calls.append((creds["access_key_id"], can_write))
        if self.failures:
            self.failures -= 1
            return ["list failed (ClientError)"]
        return []


@pytest.fixture
def cfg() -> issuer.Config:
    return issuer.Config(ACCOUNT, BUCKET, "exec-1")


@pytest.fixture
def cf() -> FakeCloudflare:
    return FakeCloudflare()


@pytest.fixture
def sm() -> FakeSecrets:
    return FakeSecrets()


def run_issue(purpose: str, force: bool, cfg, cf, sm, probe=None) -> int:
    return issuer.issue(
        issuer.PURPOSES[purpose],
        force=force,
        cfg=cfg,
        api=cf,
        secrets=sm,
        probe=probe or FakeProbe(),
        sleep=lambda s: None,
    )


def test_secret_access_key_is_sha256_of_the_token_value() -> None:
    assert issuer.secret_access_key("abc") == hashlib.sha256(b"abc").hexdigest()


@pytest.mark.parametrize(("purpose", "group"), [("app-read", "g-read"), ("ingest-rw", "g-write")])
def test_issue_creates_a_bucket_scoped_token_and_stores_one_version(purpose, group, cfg, cf, sm) -> None:
    assert run_issue(purpose, False, cfg, cf, sm) == 0
    (created,) = cf.created
    assert created["name"] == f"docunavi-r2-{purpose}-exec-1"
    (policy,) = created["policies"]
    assert policy["resources"] == {f"com.cloudflare.edge.r2.bucket.{ACCOUNT}_default_{BUCKET}": "*"}  # このバケットだけ
    assert policy["permission_groups"] == [{"id": group}]
    secret = issuer.PURPOSES[purpose].secret
    assert sm.store[secret] == [
        {"access_key_id": "tok1", "secret_access_key": hashlib.sha256(NEW_VALUE.encode()).hexdigest()}
    ]


def test_issue_is_a_noop_when_a_working_key_is_recorded(cfg, cf, sm) -> None:
    run_issue("app-read", False, cfg, cf, sm)
    assert run_issue("app-read", False, cfg, cf, sm) == 0
    assert len(cf.created) == 1 and len(sm.store["r2-app-read"]) == 1


def test_issue_replaces_a_recorded_key_whose_token_is_gone(cfg, cf, sm) -> None:
    run_issue("app-read", False, cfg, cf, sm)
    cf.tokens.clear()
    assert run_issue("app-read", False, cfg, cf, sm) == 0
    assert len(cf.created) == 2 and sm.store["r2-app-read"][-1]["access_key_id"] == "tok2"


def test_rotate_adds_a_new_key_and_keeps_the_old_token_until_revoked(cfg, cf, sm) -> None:
    run_issue("app-read", False, cfg, cf, sm)
    assert run_issue("app-read", True, cfg, cf, sm) == 0
    assert [t["id"] for t in cf.tokens] == ["tok1", "tok2"]  # 古い方は、アプリを入れ替えるまで残す
    assert sm.store["r2-app-read"][-1]["access_key_id"] == "tok2"
    assert cf.deleted == []


@pytest.mark.parametrize("groups", [[], [{"id": "a", "name": "Workers R2 Storage Bucket Item Read"}] * 2])
def test_issue_stops_before_creating_when_the_permission_group_is_not_unique(groups, cfg, cf, sm) -> None:
    cf.groups = groups
    assert run_issue("app-read", False, cfg, cf, sm) == 2
    assert cf.created == []


def test_issue_stops_when_the_parent_cannot_select_the_group(cfg, cf, sm) -> None:
    cf.groups = [{"id": "g", "name": "Workers R2 Storage Bucket Item Read", "is_selectable": False}]
    assert run_issue("app-read", False, cfg, cf, sm) == 2
    assert cf.created == []


def test_definite_rejection_creates_nothing(cfg, cf, sm) -> None:
    cf.create_error = issuer.CloudflareError(403, [9109], ambiguous=False)
    assert run_issue("app-read", False, cfg, cf, sm) == 2
    assert cf.tokens == [] and sm.store == {}


def test_unknown_create_result_looks_for_the_token_by_name_and_revokes_it(cfg, cf, sm) -> None:
    cf.create_error = issuer.CloudflareError(None, [], ambiguous=True)
    cf.create_then_error = True
    assert run_issue("app-read", False, cfg, cf, sm) == 3
    assert cf.tokens == [] and sm.store == {}  # 値を受け取れていない作成済みのトークンは、取り消す


def test_unknown_create_result_with_nothing_found_deletes_nothing(cfg, cf, sm) -> None:
    cf.create_error = issuer.CloudflareError(None, [], ambiguous=True)
    assert run_issue("app-read", False, cfg, cf, sm) == 3
    assert cf.deleted == []


def test_probe_that_never_passes_revokes_the_token_and_stores_nothing(cfg, cf, sm) -> None:
    assert run_issue("app-read", False, cfg, cf, sm, FakeProbe(failures=99)) == 4
    assert cf.tokens == [] and sm.store == {}


def test_probe_is_retried_while_the_new_token_propagates(cfg, cf, sm) -> None:
    probe = FakeProbe(failures=2)
    assert run_issue("app-read", False, cfg, cf, sm, probe) == 0
    assert len(probe.calls) == 3


def test_failed_save_revokes_the_token(cfg, cf, sm) -> None:
    sm.add_error = RuntimeError("boom")
    assert run_issue("app-read", False, cfg, cf, sm) == 5
    assert cf.tokens == [] and sm.store == {}


def test_save_that_succeeded_despite_an_error_keeps_the_token(cfg, cf, sm) -> None:
    sm.add_error, sm.add_then_error = RuntimeError("timeout"), True
    assert run_issue("app-read", False, cfg, cf, sm) == 0
    assert [t["id"] for t in cf.tokens] == ["tok1"] and sm.store["r2-app-read"][0]["access_key_id"] == "tok1"


def test_failed_rollback_reports_the_orphan_name(cfg, cf, sm, caplog) -> None:
    sm.add_error = RuntimeError("boom")
    cf.delete_failures = 99
    with caplog.at_level(logging.INFO):
        assert run_issue("app-read", False, cfg, cf, sm) == 5
    assert "ORPHAN token name=docunavi-r2-app-read-exec-1" in caplog.text


def test_unreadable_secret_stops_before_creating(cfg, cf, sm) -> None:
    sm.read_error = RuntimeError("denied")
    assert run_issue("app-read", False, cfg, cf, sm) == 5
    assert cf.created == []


def test_concurrent_issues_leave_a_consistent_last_writer(cfg, cf, sm) -> None:
    """2 つの実行が重なっても、各版は ID と Secret Access Key の組で書かれる。負けた方のトークンは revoke-stale が片付ける。"""
    other = issuer.Config(ACCOUNT, BUCKET, "exec-2")
    run_issue("app-read", True, cfg, cf, sm)
    run_issue("app-read", True, other, cf, sm)
    latest = sm.store["r2-app-read"][-1]
    assert latest["access_key_id"] == "tok2"
    assert {t["id"] for t in cf.tokens} == {"tok1", "tok2"}


def _managed(cf, token_id, purpose, issued=OLD) -> None:
    cf.tokens.append({"id": token_id, "name": f"docunavi-r2-{purpose}-{token_id}", "issued_on": issued})


def test_revoke_stale_removes_only_unrecorded_old_managed_tokens(cf, sm) -> None:
    _managed(cf, "active", "app-read")
    _managed(cf, "old", "app-read")
    _managed(cf, "fresh", "app-read", issued=NEW)
    cf.tokens.append({"id": "hand", "name": "my-own-token", "issued_on": OLD})
    sm.store["r2-app-read"] = [{"access_key_id": "active", "secret_access_key": "x"}]
    assert issuer.revoke(stale_only=True, api=cf, secrets=sm, now=lambda: NOW, sleep=lambda s: None) == 0
    assert cf.deleted == ["old"]  # 記録されたもの・作ったばかりのもの・手で作ったもの・親は残る


def test_revoke_stale_skips_tokens_with_unknown_age(cf, sm) -> None:
    cf.tokens.append({"id": "t", "name": "docunavi-r2-app-read-t"})
    assert issuer.revoke(stale_only=True, api=cf, secrets=sm, now=lambda: NOW, sleep=lambda s: None) == 0
    assert cf.deleted == []


def test_revoke_stale_deletes_nothing_when_the_recorded_keys_cannot_be_read(cf, sm) -> None:
    _managed(cf, "old", "app-read")
    sm.read_error = RuntimeError("denied")
    assert issuer.revoke(stale_only=True, api=cf, secrets=sm, now=lambda: NOW, sleep=lambda s: None) == 5
    assert cf.deleted == []


def test_revoke_all_removes_the_recorded_keys_too_but_never_the_parent(cf, sm) -> None:
    _managed(cf, "active", "ingest-rw", issued=NEW)
    _managed(cf, "old", "app-read")
    sm.store["r2-ingest-rw"] = [{"access_key_id": "active", "secret_access_key": "x"}]
    assert issuer.revoke(stale_only=False, api=cf, secrets=sm, now=lambda: NOW, sleep=lambda s: None) == 0
    assert sorted(cf.deleted) == ["active", "old"]
    assert [t["id"] for t in cf.list_tokens()] == ["parent-id"]


def test_revoke_reports_failures_and_keeps_going(cf, sm) -> None:
    _managed(cf, "a", "app-read")
    _managed(cf, "b", "app-read")
    cf.delete_failures = 3  # 1 つ目だけ、3 回とも失敗
    assert issuer.revoke(stale_only=False, api=cf, secrets=sm, now=lambda: NOW, sleep=lambda s: None) == 4
    assert cf.deleted == ["b"]


def test_check_changes_nothing(cfg, cf, sm) -> None:
    run_issue("app-read", False, cfg, cf, sm)
    before = (list(cf.tokens), json.dumps(sm.store))
    assert issuer.check(cfg=cfg, api=cf, secrets=sm, probe=FakeProbe()) == 0
    assert (cf.tokens, json.dumps(sm.store)) == before and cf.deleted == []


def test_check_reports_days_left_on_the_parent_token(cfg, cf, sm, caplog) -> None:
    cf.expires_on = (datetime.now(UTC) + timedelta(days=5, hours=1)).isoformat()
    with caplog.at_level(logging.INFO):
        assert issuer.check(cfg=cfg, api=cf, secrets=sm, probe=FakeProbe()) == 0
    assert "parent token: 5 days left" in caplog.text


def test_check_treats_an_unusable_parent_as_idle_and_still_probes_the_keys(cfg, cf, sm, caplog) -> None:
    run_issue("app-read", False, cfg, cf, sm)
    probe = FakeProbe()
    with caplog.at_level(logging.INFO):
        assert issuer.check(cfg=cfg, api=issuer.NoParent(), secrets=sm, probe=probe) == 0
    assert "parent token is not usable" in caplog.text and probe.calls == [("tok1", False)]
    assert (
        issuer.check(cfg=cfg, api=issuer.NoParent(), secrets=sm, probe=FakeProbe(failures=1)) == 1
    )  # キーの不具合は異常のまま


def test_check_is_clean_when_everything_is_in_order(cfg, cf, sm) -> None:
    run_issue("app-read", False, cfg, cf, sm)
    run_issue("ingest-rw", False, cfg, cf, sm)
    assert issuer.check(cfg=cfg, api=cf, secrets=sm, probe=FakeProbe()) == 0


def test_check_flags_a_recorded_key_without_a_token(cfg, cf, sm) -> None:
    run_issue("app-read", False, cfg, cf, sm)
    cf.tokens.clear()
    assert issuer.check(cfg=cfg, api=cf, secrets=sm, probe=FakeProbe()) == 1


def test_no_secret_value_reaches_the_logs_or_output(cfg, cf, sm, caplog, capsys) -> None:
    """あらゆる経路（成功・失敗・取り消し）で、親トークンの値・新しい値・Secret Access Key をログに出さない。"""
    secret_key = hashlib.sha256(NEW_VALUE.encode()).hexdigest()
    with caplog.at_level(logging.DEBUG):
        run_issue("app-read", False, cfg, cf, sm)
        run_issue("ingest-rw", True, cfg, cf, sm)
        sm.add_error = RuntimeError(f"leaky {NEW_VALUE} {secret_key} {PARENT_VALUE}")
        run_issue("app-read", True, cfg, cf, sm)
        cf.create_error = issuer.CloudflareError(500, [1], ambiguous=True)
        run_issue("app-read", True, cfg, cf, sm)
        issuer.check(cfg=cfg, api=cf, secrets=sm, probe=FakeProbe())
        issuer.revoke(stale_only=False, api=cf, secrets=sm, now=lambda: NOW, sleep=lambda s: None)
    out = capsys.readouterr()
    for text in (caplog.text, out.out, out.err):
        for secret in (NEW_VALUE, secret_key, PARENT_VALUE):
            assert secret not in text


def test_main_rejects_unknown_modes_and_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert issuer.main(["x"]) == 2
    assert issuer.main(["x", "delete-everything"]) == 2
    assert issuer.main(["x", "check", "--extra"]) == 2
    for name in ("CLOUDFLARE_ACCOUNT_ID", "R2_BUCKET", "GCP_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert issuer.main(["x", "check"]) == 2


def test_modes_match_the_purposes() -> None:
    assert set(issuer.MODES) == {f"{a}-{p}" for a in ("issue", "rotate") for p in issuer.PURPOSES} | {
        "revoke-stale",
        "revoke-all",
        "check",
    }


def test_managed_name_pattern_does_not_match_other_tokens() -> None:
    assert issuer.MANAGED_NAME.match("docunavi-r2-app-read-abc-123")
    for name in ("docunavi-r2-issuer-parent", "docunavi-r2-app-read-", "my-token", "docunavi-r2-other-x"):
        assert not issuer.MANAGED_NAME.match(name)


def test_revoke_never_touches_the_parent_even_if_its_name_looks_managed(cf, sm) -> None:
    """親トークンに名前を付け間違えても（docunavi-r2-… にしても）、ID で除外する。"""
    cf.list_tokens = lambda: [  # type: ignore[method-assign]
        {"id": "parent-id", "name": "docunavi-r2-app-read-parent", "issued_on": OLD},
        {"id": "old", "name": "docunavi-r2-app-read-old", "issued_on": OLD},
    ]
    assert issuer.revoke(stale_only=False, api=cf, secrets=sm, now=lambda: NOW, sleep=lambda s: None) == 0
    assert cf.deleted == ["old"]


def _http(handler) -> issuer.HttpCloudflare:
    httpx = pytest.importorskip("httpx")
    return issuer.HttpCloudflare(ACCOUNT, PARENT_VALUE, transport=httpx.MockTransport(handler))


def _json(status: int, body: dict) -> Any:
    httpx = pytest.importorskip("httpx")
    return httpx.Response(status, json=body)


def test_http_create_token_sends_the_parent_token_and_returns_the_value() -> None:
    seen = {}

    def handler(request):
        seen["auth"], seen["url"], seen["body"] = (
            request.headers["authorization"],
            str(request.url),
            json.loads(request.content),
        )
        return _json(200, {"success": True, "errors": [], "result": {"id": "t1", "value": NEW_VALUE}})

    api = _http(handler)
    assert api.create_token("n", [{"effect": "allow"}]) == {"id": "t1", "value": NEW_VALUE}
    assert seen["auth"] == f"Bearer {PARENT_VALUE}"
    assert seen["url"] == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/tokens"
    assert seen["body"] == {"name": "n", "policies": [{"effect": "allow"}]}


@pytest.mark.parametrize(
    ("status", "ambiguous"), [(400, False), (403, False), (404, False), (429, True), (500, True), (503, True)]
)
def test_http_errors_are_definite_for_4xx_and_ambiguous_for_429_and_5xx(status, ambiguous) -> None:
    api = _http(lambda r: _json(status, {"success": False, "errors": [{"code": 9109, "message": "leak " + NEW_VALUE}]}))
    with pytest.raises(issuer.CloudflareError) as e:
        api.create_token("n", [])
    assert (e.value.status, e.value.codes, e.value.ambiguous) == (status, [9109], ambiguous)
    assert NEW_VALUE not in str(e.value)  # 応答の本文は、例外に載せない


def test_http_timeout_is_ambiguous() -> None:
    httpx = pytest.importorskip("httpx")

    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(issuer.CloudflareError) as e:
        _http(handler).create_token("n", [])
    assert e.value.ambiguous and e.value.status is None


def test_http_list_tokens_follows_pages() -> None:
    pages = {"1": [{"id": str(i)} for i in range(50)], "2": [{"id": "last"}]}
    api = _http(lambda r: _json(200, {"success": True, "errors": [], "result": pages[r.url.params["page"]]}))
    assert len(api.list_tokens()) == 51
