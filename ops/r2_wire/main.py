"""アプリの Cloud Run サービスを R2 参照に切り替える Job（段階的に反映し、直前の状態へ戻せる）。

手順と設計は docs/r2-rollout.md。Job は用途ごとに引数を固定して作り（実行時の上書きは使わない）、
このファイルの `MODES` のどれか 1 つだけを実行する。

| モード | 何をするか | 本番のトラフィック |
|---|---|---|
| stage | 新しいリビジョン（R2 参照の設定つき）を作り、タグ `r2new` を付けて `/health` を確かめる。直前に配信していたリビジョンには `r2prev` を付ける | 変えない（直前のリビジョンが 100%） |
| promote | `r2new` が `/health` に答えるのを確かめ、100% を `r2new` へ移す。移した後に本番の `/health` が通らなければ、`r2prev` へ自動で戻す | 移す |
| rollback | 100% を `r2prev` へ戻す | 戻す |

「直前の状態」は、Cloud Run のトラフィックのタグ（`r2prev`）に残す。リビジョン名をコードや文書に固定しない。

守ること:
- 変えるのは、コンテナの環境変数 `PDF_STORE` `R2_ACCOUNT_ID` `R2_BUCKET` `R2_CREDENTIALS_FILE`、Secret `r2-app-read` のボリューム（`latest`）とそのマウント
  （`/secrets/r2/credentials.json`）、トラフィックの配分だけ。鍵は環境変数に入れない（起動時の値に固定されて、入れ替えに追従できない。
  `R2_CREDENTIALS_JSON` が残っていれば取り除く）。ファイルなら、アプリが 401/403 のときに読み直す（`app/r2_store.py`）。
  イメージ・他の環境変数・Secret・CPU やメモリは、いまのサービスの設定をそのまま引き継ぐ。
- **アプリのイメージは変えない。** 配線の前に、R2 を読むコードを含むイメージが配備されている必要がある（docs/r2-rollout.md の 1）。
- 環境変数と Secret の値は、ログに出さない。出すのはリビジョン名、イメージ、配分、HTTP ステータスだけ。例外はクラス名と HTTP ステータスだけを出す。
- `/health` は DB と Gemini の状態しか見ない。R2 から PDF が読めることの確認は、別の Job（`r2-verify`。同じボリュームの形で読む）と、
  切り替えの後に画面で PDF を開くことで行う。

終了コード: 0 成功（または、すでにその状態）/ 2 設定・前提の誤り（何も変えていない）/ 3 Cloud Run API の失敗 /
4 新しいリビジョンが準備できない、または `/health` が通らない（本番は直前のリビジョンのまま、または戻した）/
5 想定外のトラフィックの動き（戻そうとした。ログを見る）。
"""

import copy
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("r2-wire")

MODES = ("stage", "promote", "rollback")
TAG_NEW = "r2new"
TAG_PREV = "r2prev"
REVISION = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
LATEST = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
READY_TIMEOUT_SECONDS = 300
HEALTH_ATTEMPTS = 4


class RunApiError(Exception):
    """Cloud Run API の失敗。本文は持たない（HTTP ステータスと、Google の status 文字列だけ）。"""

    def __init__(self, status: int | None, code: str = "") -> None:
        super().__init__(f"status={status} code={code}")
        self.status, self.code = status, code


class NotReady(Exception):
    """新しいリビジョンが準備できなかった（失敗、またはタイムアウト）。"""


class Refused(Exception):
    """前提を満たさないので、何も変えずに止める。メッセージは固定の文言。"""


class Run(Protocol):
    def get_service(self) -> dict[str, Any]: ...
    def patch_service(self, body: dict[str, Any], update_mask: str) -> None: ...


Health = Callable[[str], int]


@dataclass(frozen=True)
class Config:
    project: str
    region: str
    service: str
    account_id: str
    bucket: str
    secret: str = "r2-app-read"


# --- サービスの JSON（Cloud Run Admin API v2）を読む ------------------------------------------------------------


def short(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def serving(service: dict[str, Any]) -> dict[str, int]:
    """いま配信しているリビジョンと配分（%）。0% は含めない。"""
    out: dict[str, int] = {}
    for status in service.get("trafficStatuses", []):
        percent = int(status.get("percent", 0) or 0)
        if percent > 0 and status.get("revision"):
            out[short(status["revision"])] = out.get(short(status["revision"]), 0) + percent
    return out


def single_serving(service: dict[str, Any]) -> str:
    current = serving(service)
    if len(current) != 1 or sum(current.values()) != 100:
        raise Refused("traffic is not on exactly one revision")
    return next(iter(current))


def tagged(service: dict[str, Any], tag: str) -> tuple[str, str] | None:
    """タグが付いたリビジョンと、そのタグの URL。無ければ None。"""
    for status in service.get("trafficStatuses", []):
        if status.get("tag") == tag and status.get("revision"):
            return short(status["revision"]), str(status.get("uri", ""))
    return None


VOLUME_NAME = "r2-app-read"
MOUNT_DIR = "/secrets/r2"
CREDENTIALS_FILE = "credentials.json"
OLD_ENV = "R2_CREDENTIALS_JSON"  # 起動時に固定される渡し方。残っていたら取り除く


def desired_env(cfg: Config) -> list[dict[str, Any]]:
    return [
        {"name": "PDF_STORE", "value": "r2"},
        {"name": "R2_ACCOUNT_ID", "value": cfg.account_id},
        {"name": "R2_BUCKET", "value": cfg.bucket},
        {"name": "R2_CREDENTIALS_FILE", "value": f"{MOUNT_DIR}/{CREDENTIALS_FILE}"},
    ]


def desired_volume(cfg: Config) -> dict[str, Any]:
    return {
        "name": VOLUME_NAME,
        "secret": {"secret": cfg.secret, "items": [{"path": CREDENTIALS_FILE, "version": "latest"}]},
    }


def desired_mount() -> dict[str, Any]:
    return {"name": VOLUME_NAME, "mountPath": MOUNT_DIR}


def _volume_ok(volume: dict[str, Any], cfg: Config) -> bool:
    secret = volume.get("secret") or {}
    items = [(i.get("path"), i.get("version")) for i in secret.get("items", [])]
    return secret.get("secret") == cfg.secret and items == [(CREDENTIALS_FILE, "latest")]


def is_wired(template: dict[str, Any], cfg: Config) -> bool:
    containers = template.get("containers") or [{}]
    env = {e.get("name"): e for e in containers[0].get("env", [])}
    mounts = [m for m in containers[0].get("volumeMounts", []) if m.get("name") == VOLUME_NAME]
    volumes = [v for v in template.get("volumes", []) if v.get("name") == VOLUME_NAME]
    return (
        all(env.get(want["name"]) == want for want in desired_env(cfg))
        and OLD_ENV not in env
        and len(mounts) == 1
        and mounts[0].get("mountPath") == MOUNT_DIR
        and len(volumes) == 1
        and _volume_ok(volumes[0], cfg)
    )


def with_wiring(template: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """いまのテンプレートを写し、R2 の環境変数 4 つ・ボリューム・マウントだけを入れ替える。リビジョン名は自動に任せる（`revision` を消す）。"""
    out = copy.deepcopy(template)
    out.pop("revision", None)
    containers = out.get("containers")
    if not containers:
        raise Refused("service has no container")
    names = {want["name"] for want in desired_env(cfg)} | {OLD_ENV}
    containers[0]["env"] = [e for e in containers[0].get("env", []) if e.get("name") not in names] + desired_env(cfg)
    mounts = containers[0].get("volumeMounts", [])
    if any(m.get("mountPath") == MOUNT_DIR and m.get("name") != VOLUME_NAME for m in mounts):
        raise Refused("another volume is already mounted at the credentials directory")
    containers[0]["volumeMounts"] = [m for m in mounts if m.get("name") != VOLUME_NAME] + [desired_mount()]
    out["volumes"] = [v for v in out.get("volumes", []) if v.get("name") != VOLUME_NAME] + [desired_volume(cfg)]
    return out


def latest_entry(tag: str) -> dict[str, Any]:
    """最新のリビジョンへ、0% でタグだけを付ける。"""
    return {"type": LATEST, "percent": 0, "tag": tag}


def entry(revision: str, percent: int, tag: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"type": REVISION, "revision": revision, "percent": percent}
    if tag:
        out["tag"] = tag
    return out


# --- 待つ・確かめる -------------------------------------------------------------------------------------------


def wait_settled(run: Run, sleep: Callable[[float], None], clock: Callable[[], float]) -> dict[str, Any]:
    """更新が反映されるまで待つ（`reconciling` が止まり、最新の世代が観測され、Ready）。失敗か時間切れなら NotReady。"""
    deadline = clock() + READY_TIMEOUT_SECONDS
    while True:
        service = run.get_service()
        state = (service.get("terminalCondition") or {}).get("state", "")
        observed = service.get("observedGeneration")
        generation = service.get("generation")
        caught_up = observed is None or generation is None or str(observed) == str(generation)
        if caught_up and not service.get("reconciling"):
            if state == "CONDITION_SUCCEEDED":
                return service
            if state == "CONDITION_FAILED":
                raise NotReady("service reports a failed condition")
        if clock() >= deadline:
            raise NotReady("timed out waiting for the service")
        sleep(5)


def wait_ready(run: Run, revision: str, sleep: Callable[[float], None], clock: Callable[[], float]) -> dict[str, Any]:
    """そのリビジョンが「最新の準備済み」になるまで待つ。配信もタグも無いリビジョンは Cloud Run が退役させて準備済みにならないので、先にタグを付けておく。"""
    deadline = clock() + READY_TIMEOUT_SECONDS
    while True:
        service = run.get_service()
        if short(service.get("latestReadyRevision", "")) == revision and not service.get("reconciling"):
            return service
        if clock() >= deadline:
            raise NotReady("timed out waiting for the new revision")
        sleep(5)


def healthy(url: str, health: Health, sleep: Callable[[float], None]) -> bool:
    """`<url>/health` が 200 になるか（起動直後の遅れを見て、数回まで）。"""
    for attempt in range(HEALTH_ATTEMPTS):
        status = health(url)
        log.info("health %s -> %s", "tag" if url.endswith("---") else "url", status)
        if status == 200:
            return True
        if attempt < HEALTH_ATTEMPTS - 1:
            sleep(5)
    return False


def _traffic(run: Run, entries: list[dict[str, Any]]) -> None:
    run.patch_service({"traffic": entries}, "traffic")


# --- モード --------------------------------------------------------------------------------------------------


def stage(run: Run, cfg: Config, health: Health, sleep: Callable[[float], None], clock: Callable[[], float]) -> int:
    service = run.get_service()
    current = single_serving(service)
    if (
        is_wired(service.get("template", {}), cfg)
        and short(service.get("latestReadyRevision", "")) == current
        and short(service.get("latestCreatedRevision", "")) == current
    ):
        log.info("the serving revision %s already has the R2 settings; nothing to stage", current)
        return 0
    image = ((service.get("template") or {}).get("containers") or [{}])[0].get("image", "")
    log.info("serving revision before the update: %s (image %s)", current, image.rsplit("/", 1)[-1])

    # 配信中のリビジョンに 100% を固定し、新しいリビジョンには 0% でタグを付けて、テンプレートを更新する
    # （新しいリビジョンに本番の流れを移さないため。タグが無いと、新しいリビジョンは配信されないまま退役して、準備済みにならない）
    run.patch_service(
        {
            "template": with_wiring(service["template"], cfg),
            "traffic": [entry(current, 100, TAG_PREV), latest_entry(TAG_NEW)],
        },
        "template,traffic",
    )
    service = wait_settled(run, sleep, clock)
    if serving(service) != {current: 100}:
        log.error("traffic moved unexpectedly after the update: %s", serving(service))
        _traffic(run, [entry(current, 100, TAG_PREV)])
        return 5
    new = short(service.get("latestCreatedRevision", ""))
    if not new or new == current:
        log.error("no new revision was created")
        return 4
    try:
        wait_ready(run, new, sleep, clock)
    except NotReady:
        log.error("the new revision %s is not ready; production is still on %s", new, current)
        return 4

    # タグを、いま準備できたリビジョンに固定する（「最新」に付けたままだと、次の更新で動く）
    _traffic(run, [entry(current, 100, TAG_PREV), entry(new, 0, TAG_NEW)])
    service = wait_settled(run, sleep, clock)
    if serving(service) != {current: 100}:
        log.error("traffic moved unexpectedly after tagging: %s", serving(service))
        _traffic(run, [entry(current, 100, TAG_PREV), entry(new, 0, TAG_NEW)])
        return 5
    found = tagged(service, TAG_NEW)
    if not found or not found[1]:
        log.error("the new revision has no tag URL")
        return 4
    if not healthy(found[1], health, sleep):
        log.error("the new revision %s did not answer /health; production is still on %s", new, current)
        return 4
    log.info("staged: new revision %s (0%%, tag %s), production stays on %s (tag %s)", new, TAG_NEW, current, TAG_PREV)
    return 0


def _staged(service: dict[str, Any]) -> tuple[str, str, str]:
    """(いま配信中, r2prev のリビジョン, r2new のリビジョン)。段階的に反映する前提を満たさなければ Refused。"""
    prev, new = tagged(service, TAG_PREV), tagged(service, TAG_NEW)
    if not prev or not new:
        raise Refused("the revisions are not staged (tags are missing)")
    return single_serving(service), prev[0], new[0]


def promote(run: Run, cfg: Config, health: Health, sleep: Callable[[float], None], clock: Callable[[], float]) -> int:
    service = run.get_service()
    current, prev, new = _staged(service)
    if current != prev or new == prev:
        raise Refused("production is not on the revision tagged r2prev")
    found = tagged(service, TAG_NEW)
    assert found  # _staged が確かめた
    if not healthy(found[1], health, sleep):
        log.error("the staged revision %s does not answer /health; nothing was changed", new)
        return 4
    _traffic(run, [entry(new, 100, TAG_NEW), entry(prev, 0, TAG_PREV)])
    service = wait_settled(run, sleep, clock)
    if serving(service) != {new: 100}:
        log.error("traffic is not on the new revision after the switch: %s; restoring %s", serving(service), prev)
        _traffic(run, [entry(prev, 100, TAG_PREV), entry(new, 0, TAG_NEW)])
        return 5
    if not healthy(str(service.get("uri", "")), health, sleep):
        log.error("production did not answer /health after the switch; restoring %s", prev)
        _traffic(run, [entry(prev, 100, TAG_PREV), entry(new, 0, TAG_NEW)])
        return 4
    log.info("promoted: production is on %s (previous: %s, tag %s)", new, prev, TAG_PREV)
    return 0


def rollback(run: Run, cfg: Config, health: Health, sleep: Callable[[float], None], clock: Callable[[], float]) -> int:
    service = run.get_service()
    prev = tagged(service, TAG_PREV)
    if not prev:
        raise Refused("there is no revision tagged r2prev")
    new = tagged(service, TAG_NEW)
    if serving(service) == {prev[0]: 100}:
        log.info("production is already on %s", prev[0])
        return 0
    entries = [entry(prev[0], 100, TAG_PREV)] + ([entry(new[0], 0, TAG_NEW)] if new else [])
    _traffic(run, entries)
    service = wait_settled(run, sleep, clock)
    if serving(service) != {prev[0]: 100}:
        log.error("traffic is not on %s after the rollback: %s", prev[0], serving(service))
        return 5
    ok = healthy(str(service.get("uri", "")), health, sleep)
    log.info(
        "rolled back to %s; /health %s",
        prev[0],
        "ok" if ok else "NOT ok (the previous revision itself is unhealthy; check DB/Gemini)",
    )
    return 0 if ok else 4


RUNNERS = {"stage": stage, "promote": promote, "rollback": rollback}


# --- 本物の接続 -----------------------------------------------------------------------------------------------


class RestRun:
    """Cloud Run Admin API v2 の REST。Job の実行用サービスアカウントで呼ぶ（キーは持たない）。"""

    def __init__(self, cfg: Config) -> None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self._session = AuthorizedSession(credentials)
        self._url = (
            f"https://run.googleapis.com/v2/projects/{cfg.project}/locations/{cfg.region}/services/{cfg.service}"
        )

    @staticmethod
    def _check(response: Any) -> Any:
        if response.status_code >= 300:
            try:
                code = str(response.json().get("error", {}).get("status", ""))
            except ValueError:
                code = ""
            raise RunApiError(response.status_code, code)
        return response

    def get_service(self) -> dict[str, Any]:
        return self._check(self._session.get(self._url, timeout=30)).json()

    def patch_service(self, body: dict[str, Any], update_mask: str) -> None:
        self._check(self._session.patch(self._url, params={"updateMask": update_mask}, json=body, timeout=60))


def http_health(url: str) -> int:
    import requests

    try:
        return requests.get(url.rstrip("/") + "/health", timeout=15).status_code
    except requests.RequestException:
        return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    mode = argv[1] if len(argv) == 2 else ""
    if mode not in MODES:
        log.error("unknown mode (expected one of: %s)", ", ".join(MODES))
        return 2
    names = ("GCP_PROJECT_ID", "REGION", "SERVICE_NAME", "R2_ACCOUNT_ID", "R2_BUCKET")
    values = [os.environ.get(name, "").strip() for name in names]
    if not all(values):
        log.error("%s are required", ", ".join(names))
        return 2
    cfg = Config(*values)
    try:
        return RUNNERS[mode](RestRun(cfg), cfg, http_health, time.sleep, time.monotonic)
    except Refused as e:
        log.error("refused: %s", e)
        return 2
    except NotReady as e:
        log.error("not ready: %s", e)
        return 4
    except RunApiError as e:
        log.error("Cloud Run API failed (status=%s code=%s)", e.status, e.code)
        return 3
    except Exception as e:
        log.error("failed (%s)", type(e).__name__)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
