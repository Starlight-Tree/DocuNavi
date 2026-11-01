"""承認して課金を再開する（Cloud Run functions、HTTP）。`budget-guard` が課金を外した後に、人が呼ぶ。

呼び出しは 2 つの鍵が要る。Cloud Run の IAM（`run.invoker`。サービスは非公開）と、使い捨てのトークン（`X-Disarm-Token`）。
トークンは Secret Manager のバージョンに、ハッシュと期限（JSON）だけを入れる（`issue.py` が作る）。使うと、そのバージョンを無効にする（再利用できない）。
`DRY_RUN`（既定 true）の間は、課金を変えない。トークンの検査と消費、監査ログは、本番と同じに通す。
トークンを先に消費してから課金を変える。変更に失敗したら、新しいトークンを作り直す（黙って再試行できる状態にしない）。

注意: 課金が外れると、同じプロジェクトの Cloud Run と Secret Manager も止まる。この関数は、課金が有効な別のプロジェクトに置く
（`TARGET_PROJECT_ID` が、課金を戻す対象。docs/deploy.md の G）。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:
    import functions_framework

    _register = functions_framework.http
except ImportError:  # テスト（CI）には入れていない。本番の関数では requirements.txt で入る

    def _register(func: Any) -> Any:
        return func


log = logging.getLogger("budget-guard-disarm")

TOKEN_HEADER = "X-Disarm-Token"
_PATHS = ("/", "/disarm")
_BILLING_ACCOUNT = re.compile(r"^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$")
_SECRET = re.compile(r"^projects/[^/]+/secrets/[^/]+$")


class Denied(Exception):
    """トークンの検査に通らなかった。理由は監査ログにだけ出し、応答には出さない。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TokenRecord:
    version: str  # projects/<番号>/secrets/<名前>/versions/<n>
    etag: str
    payload: dict[str, Any]


class SecretStore:
    """Secret Manager。google のライブラリは、本番の関数にだけある。"""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def latest_enabled(self) -> TokenRecord | None:
        from google.api_core import exceptions
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        try:
            version = client.get_secret_version(request={"name": f"{self._secret}/versions/latest"})
        except (exceptions.NotFound, exceptions.FailedPrecondition):
            return None
        if version.state != secretmanager.SecretVersion.State.ENABLED:
            return None
        data = client.access_secret_version(request={"name": version.name}).payload.data
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:
            payload = {}  # 読めない中身は、検査で「不正」として落ちる
        return TokenRecord(version.name, version.etag, payload if isinstance(payload, dict) else {})

    def disable(self, record: TokenRecord) -> bool:
        """etag つきで無効にする。同時に別の呼び出しが先に使っていたら、etag が合わず False。"""
        from google.api_core import exceptions
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        try:
            client.disable_secret_version(request={"name": record.version, "etag": record.etag})
        except (exceptions.FailedPrecondition, exceptions.Aborted, exceptions.NotFound):
            return False
        return True


def _store(secret: str) -> SecretStore:
    return SecretStore(secret)


def _relink_billing(project_id: str, billing_account: str) -> None:
    from google.cloud import billing_v1  # 本番の関数にだけある

    # 先に現在の課金を読まない（読む権限が別に要る。budget-guard と同じ理由）。同じ請求先への再紐付けは、すでに紐づいていても害がない。
    client = billing_v1.CloudBillingClient()
    client.update_project_billing_info(
        name=f"projects/{project_id}",
        project_billing_info={"billing_account_name": f"billingAccounts/{billing_account}"},
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _config() -> dict[str, str]:
    cfg = {
        "project": os.environ.get("TARGET_PROJECT_ID", ""),
        "billing_account": os.environ.get("BILLING_ACCOUNT_ID", ""),
        "secret": os.environ.get("DISARM_SECRET", ""),
    }
    missing = [name for name, value in cfg.items() if not value]
    if missing:
        raise RuntimeError(
            f"missing settings: {', '.join(missing)}"
        )  # 黙って何もしないより、失敗として見えるようにする
    if not _BILLING_ACCOUNT.match(cfg["billing_account"]):
        raise RuntimeError("BILLING_ACCOUNT_ID is malformed")
    if not _SECRET.match(cfg["secret"]):
        raise RuntimeError("DISARM_SECRET is malformed (projects/<project>/secrets/<name>)")
    return cfg


def _audit(severity: str, event: str, **fields: Any) -> None:
    """Cloud Logging の構造化ログ（標準出力の 1 行の JSON。severity と message は特別なキー）。トークンは入れない。"""
    entry = {"severity": severity, "message": event, "event": event, "time": _now().isoformat(), **fields}
    sys.stdout.write(json.dumps(entry, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _caller(request: Any) -> str:
    """呼んだ人。Cloud Run が検証した ID トークンの `email`（検証は IAM が済ませている。公開にしていると信用できない）。"""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return "unknown"
    try:
        segment = auth.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        return str(claims.get("email") or claims.get("sub") or "unknown")[:200]
    except (IndexError, ValueError):
        return "unknown"


def _respond(status: int, error: str | None = None, headers: dict[str, str] | None = None, **body: Any) -> tuple:
    content = {"error": error} if error else {"status": "ok", **body}
    return json.dumps(content), status, {"Content-Type": "application/json", **(headers or {})}


def verify(record: TokenRecord | None, presented: str, now: datetime) -> str:
    """トークンが有効なら、その識別子（ハッシュの先頭 8 文字）を返す。そうでなければ Denied。"""
    if not presented:
        raise Denied("missing_token")
    if record is None:
        raise Denied("no_active_token")
    stored, expires = record.payload.get("sha256"), record.payload.get("expires_at")
    if not isinstance(stored, str) or not isinstance(expires, str):
        raise Denied("bad_secret_payload")
    try:
        expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        raise Denied("bad_secret_payload") from None
    if expires_at.tzinfo is None:
        raise Denied("bad_secret_payload")
    if not hmac.compare_digest(hashlib.sha256(presented.encode("utf-8")).hexdigest(), stored.lower()):
        raise Denied("token_mismatch")
    if now >= expires_at:
        raise Denied("expired")
    return stored[:8]


@_register
def disarm(request: Any) -> tuple:
    cfg = _config()
    context = {
        "project": cfg["project"],
        "billing_account": cfg["billing_account"],
        "budget": os.environ.get("BUDGET_ID", ""),  # ログに残すだけ（予算の API は呼ばない）
        "caller": _caller(request),
        "source_ip": request.headers.get("X-Forwarded-For", "").split(",")[0].strip()[:64],
    }
    if request.path not in _PATHS:
        return _respond(404, "not_found")
    if request.method != "POST":
        return _respond(405, "method_not_allowed", {"Allow": "POST"})

    dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
    token_id = ""
    try:
        store = _store(cfg["secret"])
        record = store.latest_enabled()
        if record is not None:
            token_id = str(record.payload.get("sha256", ""))[:8]
        token_id = verify(record, request.headers.get(TOKEN_HEADER, "").strip(), _now())
        if record is None or not store.disable(record):  # 先に使い切る。同時に使われたら、etag が合わず落ちる
            raise Denied("already_used")
    except Denied as denied:
        _audit("WARNING", "disarm.denied", reason=denied.reason, token_id=token_id, **context)
        return _respond(403, "forbidden")
    except Exception as exc:
        _audit("ERROR", "disarm.error", stage="token", error_type=type(exc).__name__, token_id=token_id, **context)
        return _respond(500, "internal")

    if dry_run:
        _audit("NOTICE", "disarm.dry_run", changed=False, token_id=token_id, **context)
        return _respond(200, dry_run=True, changed=False)
    try:
        _relink_billing(cfg["project"], cfg["billing_account"])
    except Exception as exc:
        _audit("ERROR", "disarm.error", stage="relink", error_type=type(exc).__name__, token_id=token_id, **context)
        return _respond(500, "internal")
    _audit("NOTICE", "disarm.applied", changed=True, token_id=token_id, **context)
    return _respond(200, dry_run=False, changed=True)
