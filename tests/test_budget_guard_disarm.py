"""承認して課金を再開する関数（ops/budget_guard_disarm/main.py）。Secret Manager と Cloud Billing の API は呼ばない（偽物に差し替える）。"""

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ops.budget_guard_disarm import issue
from ops.budget_guard_disarm import main as disarm

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
ACCOUNT = "AAAAAA-BBBBBB-CCCCCC"


def _request(method: str = "POST", path: str = "/disarm", **headers: str) -> SimpleNamespace:
    return SimpleNamespace(method=method, path=path, headers={k.replace("_", "-"): v for k, v in headers.items()})


def _jwt(email: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"Bearer h.{body}.s"


class FakeStore:
    """Secret Manager の偽物。無効にできるのは 1 回だけ（etag の競合の代わり）。"""

    def __init__(self, payload: dict | None) -> None:
        self.payload = payload
        self.disabled = payload is None
        self.disable_calls = 0

    def latest_enabled(self) -> disarm.TokenRecord | None:
        if self.disabled or self.payload is None:
            return None
        return disarm.TokenRecord("projects/1/secrets/s/versions/1", "etag", self.payload)

    def disable(self, record: disarm.TokenRecord) -> bool:
        self.disable_calls += 1
        if self.disabled:
            return False
        self.disabled = True
        return True


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    relinks: list[tuple[str, str]] = []
    monkeypatch.setenv("TARGET_PROJECT_ID", "p")
    monkeypatch.setenv("BILLING_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("DISARM_SECRET", "projects/ops/secrets/disarm")
    monkeypatch.setenv("BUDGET_ID", "b-1")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(disarm, "_now", lambda: NOW)
    monkeypatch.setattr(disarm, "_relink_billing", lambda project, account: relinks.append((project, account)))
    return relinks


def _issue(monkeypatch: pytest.MonkeyPatch, minutes: int = 15) -> tuple[str, FakeStore]:
    token, payload = issue.new_token(NOW, minutes)
    store = FakeStore(payload)
    monkeypatch.setattr(disarm, "_store", lambda secret: store)
    return token, store


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_no_token_is_403(env, monkeypatch, capsys) -> None:
    _issue(monkeypatch)
    body, status, _ = disarm.disarm(_request())
    assert status == 403
    assert json.loads(body) == {"error": "forbidden"}
    assert _events(capsys)[0]["reason"] == "missing_token"
    assert env == []


def test_wrong_token_is_403_and_does_not_consume(env, monkeypatch, capsys) -> None:
    token, store = _issue(monkeypatch)
    _, status, _ = disarm.disarm(_request(X_Disarm_Token="not-the-token"))
    assert status == 403
    assert _events(capsys)[0]["reason"] == "token_mismatch"
    assert store.disable_calls == 0  # 間違った呼び出しで、本物のトークンを失効させない
    assert disarm.disarm(_request(X_Disarm_Token=token))[1] == 200


def test_correct_token_is_200_in_dry_run_and_changes_nothing(env, monkeypatch, capsys) -> None:
    token, store = _issue(monkeypatch)
    body, status, headers = disarm.disarm(_request(X_Disarm_Token=token, Authorization=_jwt("op@example.com")))
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"status": "ok", "dry_run": True, "changed": False}
    assert env == []  # 課金は変えない
    assert store.disabled  # トークンは、検査の通し稽古でも使い切る
    event = _events(capsys)[0]
    assert event["event"] == "disarm.dry_run"
    assert event["severity"] == "NOTICE"


def test_token_is_single_use(env, monkeypatch, capsys) -> None:
    token, _ = _issue(monkeypatch)
    assert disarm.disarm(_request(X_Disarm_Token=token))[1] == 200
    assert disarm.disarm(_request(X_Disarm_Token=token))[1] == 403
    assert _events(capsys)[-1]["reason"] == "no_active_token"


def test_expired_token_is_403(env, monkeypatch, capsys) -> None:
    token, payload = issue.new_token(NOW - timedelta(minutes=16), 15)
    monkeypatch.setattr(disarm, "_store", lambda secret: FakeStore(payload))
    assert disarm.disarm(_request(X_Disarm_Token=token))[1] == 403
    assert _events(capsys)[0]["reason"] == "expired"


def test_lost_race_is_403_and_does_not_relink(env, monkeypatch, capsys) -> None:
    token, store = _issue(monkeypatch)
    real_latest = store.latest_enabled

    def read_then_lose() -> disarm.TokenRecord | None:
        record = real_latest()
        store.disabled = True  # 読んだ直後に、別の呼び出しが使い切る
        return record

    monkeypatch.setattr(store, "latest_enabled", read_then_lose)
    monkeypatch.setenv("DRY_RUN", "false")
    assert disarm.disarm(_request(X_Disarm_Token=token))[1] == 403  # 読んだ後に、別の呼び出しが先に使った
    assert _events(capsys)[0]["reason"] == "already_used"
    assert env == []


def test_live_mode_relinks_once_with_the_configured_target(env, monkeypatch, capsys) -> None:
    token, _ = _issue(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    body, status, _ = disarm.disarm(_request(X_Disarm_Token=token, Authorization=_jwt("op@example.com")))
    assert status == 200
    assert json.loads(body)["changed"] is True
    assert env == [("p", ACCOUNT)]
    event = _events(capsys)[0]
    assert event["event"] == "disarm.applied"
    assert disarm.disarm(_request(X_Disarm_Token=token))[1] == 403
    assert env == [("p", ACCOUNT)]  # 2 回目は課金に触れない


def test_audit_log_has_who_when_what_and_never_the_token(env, monkeypatch, capsys) -> None:
    token, payload = (lambda t: (t[0], t[1]))(issue.new_token(NOW, 15))
    monkeypatch.setattr(disarm, "_store", lambda secret: FakeStore(payload))
    disarm.disarm(
        _request(X_Disarm_Token=token, Authorization=_jwt("op@example.com"), X_Forwarded_For="203.0.113.9, 10.0.0.1")
    )
    out = capsys.readouterr().out
    assert token not in out
    assert payload["sha256"] not in out  # ハッシュの全体も出さない（先頭 8 文字だけ）
    event = json.loads(out.splitlines()[0])
    assert event["caller"] == "op@example.com"
    assert event["project"] == "p"
    assert event["billing_account"] == ACCOUNT
    assert event["budget"] == "b-1"
    assert event["source_ip"] == "203.0.113.9"
    assert event["time"] == NOW.isoformat()
    assert event["token_id"] == payload["sha256"][:8]


def test_failed_calls_are_logged_with_a_reason(env, monkeypatch, capsys) -> None:
    _issue(monkeypatch)
    disarm.disarm(_request(X_Disarm_Token="x", Authorization=_jwt("op@example.com")))
    event = _events(capsys)[0]
    assert (event["event"], event["severity"], event["caller"]) == ("disarm.denied", "WARNING", "op@example.com")


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "OPTIONS"])
def test_only_post_is_allowed(env, monkeypatch, method: str) -> None:
    _issue(monkeypatch)
    _, status, headers = disarm.disarm(_request(method=method))
    assert status == 405
    assert headers["Allow"] == "POST"


def test_unknown_path_is_404(env, monkeypatch) -> None:
    _issue(monkeypatch)
    assert disarm.disarm(_request(path="/other"))[1] == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"sha256": "x"},
        {"sha256": "x", "expires_at": "not a date"},
        {"sha256": "x", "expires_at": "2026-12-01T00:00:00"},
    ],
)
def test_bad_secret_payload_is_denied(env, monkeypatch, capsys, payload: dict) -> None:
    monkeypatch.setattr(disarm, "_store", lambda secret: FakeStore(payload))
    assert disarm.disarm(_request(X_Disarm_Token="x"))[1] == 403
    assert _events(capsys)[0]["reason"] == "bad_secret_payload"


def test_backend_errors_are_500_with_a_fixed_message(env, monkeypatch, capsys) -> None:
    class Broken:
        def latest_enabled(self):
            raise ConnectionError("secret detail /srv/path")

    monkeypatch.setattr(disarm, "_store", lambda secret: Broken())
    body, status, _ = disarm.disarm(_request(X_Disarm_Token="x"))
    assert status == 500
    assert json.loads(body) == {"error": "internal"}
    out = capsys.readouterr().out
    assert "secret detail" not in out
    assert json.loads(out.splitlines()[0])["error_type"] == "ConnectionError"


def test_relink_failure_is_500_and_the_token_stays_used(env, monkeypatch, capsys) -> None:
    token, store = _issue(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")

    def boom(project: str, account: str) -> None:
        raise PermissionError("403 detail")

    monkeypatch.setattr(disarm, "_relink_billing", boom)
    assert disarm.disarm(_request(X_Disarm_Token=token))[1] == 500
    assert store.disabled
    assert _events(capsys)[0]["stage"] == "relink"


@pytest.mark.parametrize(
    ("name", "value"),
    [("TARGET_PROJECT_ID", ""), ("BILLING_ACCOUNT_ID", "bad"), ("DISARM_SECRET", "no-slash")],
)
def test_bad_settings_fail_loudly(env, monkeypatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        disarm.disarm(_request())


def test_issue_stores_only_the_hash_and_a_bounded_expiry() -> None:
    token, payload = issue.new_token(NOW, 15)
    assert token not in json.dumps(payload)
    assert payload["expires_at"] == (NOW + timedelta(minutes=15)).isoformat()
    for bad in (0, issue.MAX_MINUTES + 1):
        with pytest.raises(ValueError):
            issue.new_token(NOW, bad)
