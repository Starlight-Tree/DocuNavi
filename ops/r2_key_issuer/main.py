"""R2 の S3 互換キー（アプリ用の読み取り、取り込み用の読み書き）の発行と取り消しをする Cloud Run Job。

手順と設計は docs/r2-key-issuer.md。Job は用途ごとに引数を固定して作り（実行時の上書きは使わない）、
このファイルの `MODES` のどれか 1 つだけを実行する。

守ること:
- Cloudflare の親トークンと、発行したキーの値は、標準出力・標準エラー・ログに出さない。出すのはトークンの名前、
  版の番号、件数、固定の文言だけ。例外も、クラス名と HTTP ステータスとエラー番号だけを出す（本文は出さない）。
- 発行したキーは Secret Manager の 1 つの版に（キー ID と Secret Access Key をまとめて）書く。ID だけ新しい、
  という食い違いを作らない。「いま有効なキー」は、その Secret の最新の版に入っている ID。
- 先にトークンを作り、動作を確かめ、保存に成功してから終わる。途中で失敗したら、作ったトークンをその場で取り消す。
  取り消しも失敗したら、残ったトークンの名前を出して異常終了する（`revoke-stale` が片付ける）。

自動ローテーション（Cloud Scheduler が月 1 回 `rotate-if-due-*` を起動する）:
- `rotate-if-due-*` は、記録したキーの発行から `DUE_DAYS`（60）日たっていなければ、何も変えずに終わる（キーが動くことだけ確かめる。
  動かなければ、その場で入れ替える）。入れ替えるときは、新しいキーの動作確認と保存に成功してから終わり、**古いキーは取り消さない**。
- 古いキーは、`revoke-stale` が、置き換えた新しいキーの発行から `GRACE_DAYS`（7）日たってから取り消す。アプリは Secret を読み直して
  新しいキーに切り替わる（app/r2_store.py）が、動いているインスタンスが古いキーを持っている間は取り消さない。
- 失敗したときに取り消すのは、そのとき作った新しいトークンだけ。記録された有効なキーには触れない。
- `check` は、親トークンの残りが `PARENT_WARN_DAYS`（21）日を切ると（または使えないと）、固定の文言 `PARENT_TOKEN_EXPIRING` を出す
  （Cloud Monitoring のログベースのアラートが拾う）。記録したキーが `OVERDUE_DAYS`（100）日より古ければ `KEY_ROTATION_OVERDUE` を出す
  （Scheduler や `rotate-if-due` が止まっていることの検知。`check` 自身が止まったことは、Job の完了数の欠落アラートが拾う）。

終了コード: 0 成功 / 1 `check` で問題あり / 2 設定・入力の誤り（何も作っていない）/ 3 作成の結果が不明 /
4 作ったトークンの動作確認に失敗、または取り消しに失敗 / 5 保存または読み出しに失敗。
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger("r2-key-issuer")

PARENT_SECRET = "cloudflare-issuer-token"
TOKEN_PREFIX = "docunavi-r2-"
# 取り消しの対象にする名前。親トークンや、手で作ったトークンには一致しない
MANAGED_NAME = re.compile(r"^docunavi-r2-(app-read|ingest-rw)-[A-Za-z0-9_-]+$")
# 作った直後のトークンは、他の実行が保存する前かもしれないので、`revoke-stale` は触れない
MIN_AGE_SECONDS = 15 * 60
PROBE_ATTEMPTS = 6
DUE_DAYS = 60  # 記録したキーがこれより新しければ、`rotate-if-due` は何もしない
GRACE_DAYS = 7  # 入れ替えたあと、古いキーを取り消さずに待つ日数（置き換えた新しいキーの発行から数える）
GRACE_SECONDS = GRACE_DAYS * 24 * 3600
PARENT_WARN_DAYS = 21  # 親トークンの残りがこれを切ったら警告する
# 入れ替えは月 1 回の起動なので、正常でもキーは最大 91 日（60 日 + 次の起動まで）ほど使われる。これを超えたら入れ替えが止まっている
OVERDUE_DAYS = 100


@dataclass(frozen=True)
class Purpose:
    key: str  # トークン名に入れる
    secret: str  # 保存先の Secret（キー ID と Secret Access Key の JSON）
    permission: str  # Cloudflare の権限グループの名前（バケット単位）
    can_write: bool


PURPOSES = {
    "app-read": Purpose("app-read", "r2-app-read", "Workers R2 Storage Bucket Item Read", can_write=False),
    "ingest-rw": Purpose("ingest-rw", "r2-ingest-rw", "Workers R2 Storage Bucket Item Write", can_write=True),
}
MODES = (
    "issue-app-read",
    "issue-ingest-rw",
    "rotate-app-read",
    "rotate-ingest-rw",
    "rotate-if-due-app-read",
    "rotate-if-due-ingest-rw",
    "revoke-stale",
    "revoke-all",
    "check",
)


class CloudflareError(Exception):
    """Cloudflare API の失敗。`ambiguous` は、サーバー側で処理された可能性が残る失敗（タイムアウト・5xx など）。"""

    def __init__(self, status: int | None, codes: list[int], ambiguous: bool) -> None:
        super().__init__(f"status={status} codes={codes}")
        self.status, self.codes, self.ambiguous = status, codes, ambiguous


class Cloudflare(Protocol):
    def verify(self) -> dict[str, Any]: ...
    def permission_groups(self) -> list[dict[str, Any]]: ...
    def list_tokens(self) -> list[dict[str, Any]]: ...
    def create_token(self, name: str, policies: list[dict[str, Any]]) -> dict[str, Any]: ...
    def delete_token(self, token_id: str) -> None: ...


class Secrets(Protocol):
    def read_json(self, name: str) -> dict[str, str] | None:
        """最新の版を JSON として読む。版が無ければ None。それ以外の失敗は例外。"""
        ...

    def read_text(self, name: str) -> str: ...
    def add_json(self, name: str, value: dict[str, str]) -> str:
        """新しい版を追加して、版の番号を返す。"""
        ...


class Probe(Protocol):
    def __call__(self, creds: dict[str, str], can_write: bool, execution: str) -> list[str]:
        """キーの動作を確かめる。問題があれば、固定の文言の一覧を返す（空なら問題なし）。"""
        ...


@dataclass
class Config:
    account_id: str
    bucket: str
    execution: str


def secret_access_key(token_value: str) -> str:
    """R2 の Secret Access Key は、API トークンの値の SHA-256（Cloudflare の公式の説明）。"""
    return hashlib.sha256(token_value.encode()).hexdigest()


def bucket_resource(cfg: Config) -> str:
    return f"com.cloudflare.edge.r2.bucket.{cfg.account_id}_default_{cfg.bucket}"


def token_name(purpose: Purpose, execution: str) -> str:
    return f"{TOKEN_PREFIX}{purpose.key}-{execution}"[:120]


def purpose_of(name: object) -> str | None:
    """管理しているトークンの名前から、用途（app-read / ingest-rw）を取り出す。"""
    match = MANAGED_NAME.match(str(name or ""))
    return match.group(1) if match else None


def find_permission_group(api: Cloudflare, name: str) -> str:
    """権限グループを名前から ID にする。Cloudflare は ID を使うよう勧めるので、ID は実行のたびに引く（ID を書き込まない）。"""
    found = [g for g in api.permission_groups() if g.get("name") == name]
    if len(found) != 1 or not found[0].get("id"):
        raise LookupError(f"permission group {name!r}: {len(found)} found")
    group = found[0]
    if group.get("is_selectable") is False:
        raise PermissionError(f"permission group {name!r} is not selectable by the parent token")
    return str(group["id"])


def delete_with_retry(api: Cloudflare, token_id: str, sleep: Callable[[float], None]) -> bool:
    for attempt in range(3):
        try:
            api.delete_token(token_id)
            return True
        except CloudflareError as e:
            if e.status == 404:  # もう無い
                return True
            sleep(2**attempt)
    return False


def rollback(api: Cloudflare, token_id: str, name: str, sleep: Callable[[float], None]) -> bool:
    if delete_with_retry(api, token_id, sleep):
        log.warning("rolled back token name=%s", name)
        return True
    log.error("ORPHAN token name=%s could not be revoked; run revoke-stale (or revoke it in the dashboard)", name)
    return False


def token_exists(api: Cloudflare, token_id: str) -> bool:
    return any(t.get("id") == token_id for t in api.list_tokens())


def issue(
    purpose: Purpose,
    *,
    force: bool,
    cfg: Config,
    api: Cloudflare,
    secrets: Secrets,
    probe: Probe,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """キーを発行して Secret に保存する。force=False（issue）は、有効なキーが既にあれば何もしない。"""
    try:
        current = secrets.read_json(purpose.secret)
    except Exception as e:
        log.error("cannot read secret %s (%s)", purpose.secret, type(e).__name__)
        return 5

    if current and not force:
        try:
            alive = token_exists(api, current.get("access_key_id", ""))
        except CloudflareError as e:
            log.error("cannot list tokens (status=%s codes=%s)", e.status, e.codes)
            return 3
        if alive and not probe(current, purpose.can_write, cfg.execution):
            log.info("already issued: purpose=%s (nothing changed)", purpose.key)
            return 0
        log.warning("recorded key for %s is not usable; issuing a new one", purpose.key)

    try:
        group_id = find_permission_group(api, purpose.permission)
    except (LookupError, PermissionError) as e:
        log.error("%s", e)
        return 2
    except CloudflareError as e:
        log.error("cannot list permission groups (status=%s codes=%s)", e.status, e.codes)
        return 2

    name = token_name(purpose, cfg.execution)
    policies = [
        {
            "effect": "allow",
            "resources": {bucket_resource(cfg): "*"},
            "permission_groups": [{"id": group_id}],
        }
    ]
    try:
        created = api.create_token(name, policies)
    except CloudflareError as e:
        if not e.ambiguous:
            log.error("create token rejected (status=%s codes=%s); nothing was created", e.status, e.codes)
            return 2
        return recover_unknown_create(api, name, sleep)

    token_id, value = str(created["id"]), str(created["value"])
    # issued_at は `rotate-if-due` が古さを知るために使う（親トークンが使えなくても読める）。アプリは ID と Secret Access Key だけを読む
    creds = {
        "access_key_id": token_id,
        "secret_access_key": secret_access_key(value),
        "issued_at": now().isoformat(),
    }
    del value, created

    problems: list[str] = []
    for attempt in range(PROBE_ATTEMPTS):  # 新しいトークンが効くまで、少し遅れることがある
        problems = probe(creds, purpose.can_write, cfg.execution)
        if not problems:
            break
        sleep(min(2**attempt, 10))
    if problems:
        log.error("new key failed the check: %s", "; ".join(problems))
        rollback(api, token_id, name, sleep)
        return 4

    try:
        version = secrets.add_json(purpose.secret, creds)
    except Exception as e:
        log.error("saving to %s failed (%s); checking whether it was stored", purpose.secret, type(e).__name__)
        try:
            latest = secrets.read_json(purpose.secret)
        except Exception:
            latest = None
        if latest and latest.get("access_key_id") == token_id:
            log.warning("the version was stored despite the error: purpose=%s name=%s", purpose.key, name)
            return 0
        rollback(api, token_id, name, sleep)
        return 5

    log.info("issued: purpose=%s name=%s secret=%s version=%s", purpose.key, name, purpose.secret, version)
    return 0


def rotate_if_due(
    purpose: Purpose,
    *,
    cfg: Config,
    api: Cloudflare,
    secrets: Secrets,
    probe: Probe,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """記録したキーが `DUE_DAYS` 日より古い（または古さが分からない・動かない）ときだけ入れ替える。古いキーは取り消さない。"""
    try:
        current = secrets.read_json(purpose.secret)
    except Exception as e:
        log.error("cannot read secret %s (%s)", purpose.secret, type(e).__name__)
        return 5
    issued = parse_time(current.get("issued_at")) if current else None
    if current and issued is not None and (now() - issued).days < DUE_DAYS:
        if not probe(current, purpose.can_write, cfg.execution):
            log.info("not due: purpose=%s age_days=%d (nothing changed)", purpose.key, (now() - issued).days)
            return 0
        log.warning("recorded key for %s is not usable; rotating now", purpose.key)
    elif current:
        log.info("due: purpose=%s (age is unknown or over %d days)", purpose.key, DUE_DAYS)
    return issue(purpose, force=True, cfg=cfg, api=api, secrets=secrets, probe=probe, sleep=sleep, now=now)


def recover_unknown_create(api: Cloudflare, name: str, sleep: Callable[[float], None]) -> int:
    """作成の結果が不明（タイムアウトなど）。名前で探し、あれば取り消す（値は受け取れていないので使えない）。"""
    log.warning("create token result unknown; looking for name=%s", name)
    for attempt in range(3):
        try:
            leftovers = [t for t in api.list_tokens() if t.get("name") == name]
            break
        except CloudflareError:
            sleep(2**attempt)
    else:
        log.error("state unknown: could not list tokens; run revoke-stale later (name=%s)", name)
        return 3
    for t in leftovers:
        rollback(api, str(t["id"]), name, sleep)
    log.error("create token did not complete: found=%d (nothing is recorded as active)", len(leftovers))
    return 3


def recorded_by_purpose(secrets: Secrets) -> dict[str, str]:
    """用途ごとの、いま有効なキーの ID（Secret の最新の版）。"""
    ids: dict[str, str] = {}
    for p in PURPOSES.values():
        current = secrets.read_json(p.secret)
        if current:
            ids[p.key] = current["access_key_id"]
    return ids


def managed_tokens(api: Cloudflare, parent_id: str) -> list[dict[str, Any]]:
    return [t for t in api.list_tokens() if MANAGED_NAME.match(str(t.get("name", ""))) and t.get("id") != parent_id]


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def stale_verdict(issued: datetime | None, successor_issued: datetime | None, current: datetime) -> str:
    """記録されていないトークンを、いま取り消してよいか。revoke（取り消す）/ grace（猶予中）/ new（作ったばかり）/ unknown（発行時刻が不明）。

    - 記録されたキーに置き換えられた古いキー（発行が新しいキーより前）: 新しいキーの発行から `GRACE_SECONDS` たつまで残す。
      古いキーの発行からの日数ではない（60 日使ったキーを、入れ替えの直後に取り消してしまうため）
    - 置き換え先が分からない（記録が無い・トークンが無い）: 自分の発行から `GRACE_SECONDS` たつまで残す（使われているかもしれない）
    - 記録されたキーより新しい: 保存に失敗して残ったもの。他の実行が保存中かもしれないので、`MIN_AGE_SECONDS` たったら取り消す
    """
    if issued is None:
        return "unknown"
    if successor_issued is None:
        return "revoke" if (current - issued).total_seconds() >= GRACE_SECONDS else "grace"
    if issued < successor_issued:
        return "revoke" if (current - successor_issued).total_seconds() >= GRACE_SECONDS else "grace"
    return "revoke" if (current - issued).total_seconds() >= MIN_AGE_SECONDS else "new"


def revoke(
    *,
    stale_only: bool,
    api: Cloudflare,
    secrets: Secrets,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """stale_only=True: 記録されたキー以外のうち、猶予を過ぎたものだけを取り消す（`stale_verdict`）。False: 全部取り消す。"""
    try:
        parent_id = str(api.verify().get("id", ""))
        recorded = recorded_by_purpose(secrets) if stale_only else {}
        keep = set(recorded.values())
        if stale_only:
            log.info("recorded keys: %d", len(keep))
        tokens = managed_tokens(api, parent_id)
        issued_by_id = {str(t["id"]): parse_time(t.get("issued_on")) for t in tokens}
        targets = [t for t in tokens if t["id"] not in keep]
    except CloudflareError as e:
        log.error("cannot list tokens (status=%s codes=%s)", e.status, e.codes)
        return 3
    except Exception as e:
        log.error("cannot determine the recorded keys (%s); nothing was revoked", type(e).__name__)
        return 5

    failed = 0
    skipped = 0
    for t in targets:
        if stale_only:
            successor = recorded.get(purpose_of(t.get("name")) or "")
            verdict = stale_verdict(parse_time(t.get("issued_on")), issued_by_id.get(successor or ""), now())
            if verdict != "revoke":
                skipped += 1  # 猶予中・作ったばかり・発行時刻が読めないものは触らない
                log.info("skipped (%s): name=%s", verdict, t.get("name"))
                continue
        if delete_with_retry(api, str(t["id"]), sleep):
            log.info("revoked: name=%s", t.get("name"))
        else:
            failed += 1
            log.error("could not revoke: name=%s", t.get("name"))
    log.info("done: targets=%d skipped=%d failed=%d", len(targets), skipped, failed)
    return 4 if failed else 0


def check(*, cfg: Config, api: Cloudflare, secrets: Secrets, probe: Probe) -> int:
    """読み取りだけの点検（トークンも Secret も作らない・変えない）。結果は固定の文言で出す。"""
    bad = 0
    tokens: dict[str, dict[str, Any]] | None = None
    try:
        info = api.verify()
        log.info("parent token: status=%s expires_on=%s", info.get("status"), info.get("expires_on", "none"))
        expires = parse_time(info.get("expires_on"))
        if expires is not None:
            days_left = (expires - datetime.now(UTC)).days
            log.info("parent token: %d days left", days_left)
            if days_left < PARENT_WARN_DAYS:
                log.warning(
                    "PARENT_TOKEN_EXPIRING: %d days left; create a new parent token and store it "
                    "(scripts/store-cloudflare-parent-token.sh)",
                    days_left,
                )
        for p in PURPOSES.values():
            try:
                find_permission_group(api, p.permission)
                log.info("permission group ok: %s", p.permission)
            except (LookupError, PermissionError) as e:
                bad += 1
                log.error("%s", e)
        tokens = {t["id"]: t for t in managed_tokens(api, str(info.get("id", "")))}
    except CloudflareError as e:
        # 自動ローテーションでは、親トークンが使えないと次の入れ替えができない。終了コードは変えず、アラート用の文言で知らせる
        log.warning(
            "PARENT_TOKEN_EXPIRING: parent token is not usable (status=%s codes=%s): issue/rotate/revoke need a new one; "
            "checking the recorded keys only",
            e.status,
            e.codes,
        )
    recorded: set[str] = set()
    for p in PURPOSES.values():
        try:
            current = secrets.read_json(p.secret)
        except Exception as e:
            bad += 1
            log.error("%s: cannot read secret (%s)", p.key, type(e).__name__)
            continue
        if not current:
            log.warning("%s: no key recorded yet", p.key)
            continue
        recorded.add(current["access_key_id"])
        issued = parse_time(current.get("issued_at"))
        if issued is not None and (datetime.now(UTC) - issued).days > OVERDUE_DAYS:
            log.warning(
                "KEY_ROTATION_OVERDUE: %s is %d days old (rotation runs at %d days; check the Scheduler and the rotate-if-due Jobs)",
                p.key,
                (datetime.now(UTC) - issued).days,
                DUE_DAYS,
            )
        if tokens is not None and current["access_key_id"] not in tokens:
            bad += 1
            log.error("%s: the recorded key has no matching token", p.key)
            continue
        problems = probe(current, p.can_write, cfg.execution)
        if problems:
            bad += 1
            log.error("%s: %s", p.key, "; ".join(problems))
        else:
            log.info("%s: ok", p.key)
    if tokens is not None:
        log.info("managed tokens: %d (not recorded: %d)", len(tokens), len([i for i in tokens if i not in recorded]))
    return 1 if bad else 0


# --- 実際の接続（テストでは使わない。必要なライブラリは Job のイメージにだけ入れる） ---


class HttpCloudflare:
    def __init__(self, account_id: str, parent_token: str, transport: Any = None) -> None:
        import httpx

        self._http = httpx.Client(
            base_url=f"https://api.cloudflare.com/client/v4/accounts/{account_id}",
            headers={"Authorization": f"Bearer {parent_token}"},
            timeout=20,
            transport=transport,  # テストで差し替える
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        import httpx

        try:
            r = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:  # 接続・タイムアウト。サーバーで処理された可能性が残る
            raise CloudflareError(None, [], ambiguous=True) from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        codes = [int(x["code"]) for x in body.get("errors", []) if isinstance(x, dict) and "code" in x]
        if r.status_code >= 500 or r.status_code == 429:
            raise CloudflareError(r.status_code, codes, ambiguous=True)
        if r.status_code >= 400 or not body.get("success", False):
            raise CloudflareError(r.status_code, codes, ambiguous=False)
        return body.get("result")

    def verify(self) -> dict[str, Any]:
        return self._call("GET", "/tokens/verify")

    def permission_groups(self) -> list[dict[str, Any]]:
        return self._call("GET", "/tokens/permission_groups")

    def list_tokens(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 50):
            batch = self._call("GET", "/tokens", params={"page": page, "per_page": 50})
            out.extend(batch)
            if len(batch) < 50:
                break
        return out

    def create_token(self, name: str, policies: list[dict[str, Any]]) -> dict[str, Any]:
        return self._call("POST", "/tokens", json={"name": name, "policies": policies})

    def delete_token(self, token_id: str) -> None:
        self._call("DELETE", f"/tokens/{token_id}")


class NoParent:
    """親トークンを読めないとき（`check` だけ）。Cloudflare を使う操作は、使えない扱いにする。"""

    def _unusable(self, *args: Any, **kwargs: Any) -> Any:
        raise CloudflareError(None, [], ambiguous=False)

    verify = permission_groups = list_tokens = create_token = delete_token = _unusable


class SecretManager:
    def __init__(self, project: str) -> None:
        from google.cloud import secretmanager

        self._client = secretmanager.SecretManagerServiceClient()
        self._project = project

    def _path(self, name: str) -> str:
        return f"projects/{self._project}/secrets/{name}"

    def read_text(self, name: str) -> str:
        r = self._client.access_secret_version(name=f"{self._path(name)}/versions/latest")
        return r.payload.data.decode().strip()

    def read_json(self, name: str) -> dict[str, str] | None:
        from google.api_core.exceptions import NotFound

        try:
            data = json.loads(self.read_text(name))
        except NotFound:
            return None
        if not isinstance(data, dict) or not {"access_key_id", "secret_access_key"} <= data.keys():
            raise ValueError("unexpected secret format")
        return {k: str(data[k]) for k in ("access_key_id", "secret_access_key", "issued_at") if k in data}

    def add_json(self, name: str, value: dict[str, str]) -> str:
        import google_crc32c

        data = json.dumps(value).encode()
        r = self._client.add_secret_version(
            request={
                "parent": self._path(name),
                "payload": {
                    "data": data,
                    "data_crc32c": int(google_crc32c.value(data)),
                },  # 書き込みの時点で、内容の破損を検出する
            }
        )
        return r.name.rsplit("/", 1)[-1]


def s3_probe(cfg: Config) -> Probe:
    """実際の R2 に、そのキーで接続して確かめる。read 用は「読める・書けない」、読み書き用は「書く・読む・消す」。"""

    def run(creds: dict[str, str], can_write: bool, execution: str) -> list[str]:
        import boto3
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import ClientError

        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{cfg.account_id}.r2.cloudflarestorage.com",
            region_name="auto",
            aws_access_key_id=creds["access_key_id"],
            aws_secret_access_key=creds["secret_access_key"],
            config=BotoConfig(retries={"max_attempts": 2}, connect_timeout=5, read_timeout=10),
        )
        key = f"_probe/{execution}"
        problems: list[str] = []

        def status(e: ClientError) -> int:
            return int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))

        try:
            s3.list_objects_v2(Bucket=cfg.bucket, MaxKeys=1)
        except Exception as e:
            problems.append(f"list failed ({type(e).__name__})")
            return problems
        if can_write:
            try:
                s3.put_object(Bucket=cfg.bucket, Key=key, Body=b"probe")
                s3.get_object(Bucket=cfg.bucket, Key=key)["Body"].read()
                s3.delete_object(Bucket=cfg.bucket, Key=key)
            except Exception as e:
                problems.append(f"write/read/delete failed ({type(e).__name__})")
        else:
            try:
                s3.put_object(Bucket=cfg.bucket, Key=key, Body=b"probe")
                problems.append("read-only key was able to write")
                s3.delete_object(Bucket=cfg.bucket, Key=key)
            except ClientError as e:
                if status(e) != 403:
                    problems.append(f"write was rejected with an unexpected status ({status(e)})")
            except Exception as e:
                problems.append(f"write check failed ({type(e).__name__})")
        return problems

    return run


def execution_id() -> str:
    """トークン名に入れる実行の識別子。Cloud Run Job の実行名（再試行でも同じ）。"""
    raw = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{int(time.time())}"
    return re.sub(r"[^A-Za-z0-9_-]", "-", raw)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    mode = argv[1] if len(argv) == 2 else ""
    if mode not in MODES:
        log.error("unknown mode (expected one of: %s)", ", ".join(MODES))
        return 2
    account, bucket, project = (os.environ.get(k, "") for k in ("CLOUDFLARE_ACCOUNT_ID", "R2_BUCKET", "GCP_PROJECT_ID"))
    if not (account and bucket and project):
        log.error("CLOUDFLARE_ACCOUNT_ID, R2_BUCKET and GCP_PROJECT_ID are required")
        return 2
    cfg = Config(account, bucket, execution_id())
    try:
        secrets = SecretManager(project)
        api: Cloudflare
        try:
            api = HttpCloudflare(account, secrets.read_text(PARENT_SECRET))
        except Exception as e:
            if mode != "check":
                raise
            log.warning("parent token secret cannot be read (%s)", type(e).__name__)
            api = NoParent()
        probe = s3_probe(cfg)
        if mode.startswith("rotate-if-due-"):
            purpose = PURPOSES[mode.removeprefix("rotate-if-due-")]
            return rotate_if_due(purpose, cfg=cfg, api=api, secrets=secrets, probe=probe)
        if mode.startswith(("issue-", "rotate-")):
            action, key = mode.split("-", 1)
            return issue(PURPOSES[key], force=action == "rotate", cfg=cfg, api=api, secrets=secrets, probe=probe)
        if mode == "check":
            return check(cfg=cfg, api=api, secrets=secrets, probe=probe)
        return revoke(stale_only=mode == "revoke-stale", api=api, secrets=secrets)
    except CloudflareError as e:
        log.error("Cloudflare API failed (status=%s codes=%s)", e.status, e.codes)
        return 3
    except Exception as e:
        log.error("failed (%s)", type(e).__name__)
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv))
