"""Cloud Run サービスを R2 参照に切り替える Job（ops/r2_wire/main.py）。Cloud Run には接続しない（サービスの状態は偽物）。"""

import copy
import logging
from typing import Any

import pytest

from ops.r2_wire import main as wire

ACCOUNT = "acct-DO-NOT-LOG"
BUCKET = "bucket-DO-NOT-LOG"
CFG = wire.Config(project="p", region="r", service="docunavi", account_id=ACCOUNT, bucket=BUCKET)
SECRET_ENV_REF = {
    "name": "DATABASE_URL",
    "valueSource": {"secretKeyRef": {"secret": "database-url", "version": "latest"}},
}
OTHER_VOLUME = {"name": "other", "secret": {"secret": "other", "items": [{"path": "x", "version": "latest"}]}}
OTHER_MOUNT = {"name": "other", "mountPath": "/secrets/other"}
PROD = "https://docunavi-x.run.app"


def base_template() -> dict[str, Any]:
    return {
        "revision": "docunavi-00003-b9t",
        "serviceAccount": "docunavi-run@p.iam.gserviceaccount.com",
        "containers": [
            {
                "image": "r/p/repo/docunavi:abc1234",
                "env": [{"name": "AUTH_MODE", "value": "required"}, SECRET_ENV_REF],
                "volumeMounts": [OTHER_MOUNT],
                "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
            }
        ],
        "volumes": [OTHER_VOLUME],
    }


class FakeRun:
    """Cloud Run Admin API v2 のサービスの動きを真似る。テンプレートの更新は新しいリビジョンを作り、`traffic` を渡さなければ最新へ 100% 流す。"""

    def __init__(self, template: dict[str, Any] | None = None) -> None:
        self.template = template or base_template()
        self.revisions = {"docunavi-00003-b9t": copy.deepcopy(self.template)}
        self.latest = "docunavi-00003-b9t"
        self.entries: list[dict[str, Any]] = [{"revision": "docunavi-00003-b9t", "percent": 100}]
        self.patches: list[tuple[dict[str, Any], str]] = []
        self.ready = "docunavi-00003-b9t"
        self.ready_lag = 0  # 「最新の準備済み」に反映されるまでの get_service の回数
        self._lag_left = 0
        self.new_revision_fails = False
        self.moves_traffic_on_template_update = False  # 想定外: 更新で本番の流れが新しいリビジョンに移る
        self._n = 3

    def get_service(self) -> dict[str, Any]:
        statuses = []
        for e in self.entries:
            status: dict[str, Any] = {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": e["revision"],
                "percent": e["percent"],
            }
            if e.get("tag"):
                status["tag"] = e["tag"]
                status["uri"] = f"https://{e['tag']}---docunavi-x.run.app"
            statuses.append(status)
        if (
            self.latest != self.ready
            and not self.new_revision_fails
            and self.latest in {e["revision"] for e in self.entries}
        ):
            if self._lag_left > 0:
                self._lag_left -= 1
            else:
                self.ready = self.latest  # タグか配信が付いたリビジョンだけが準備済みになる（付かなければ退役する）
        ready = self.ready
        return {
            "template": copy.deepcopy(self.revisions[self.latest]),
            "trafficStatuses": statuses,
            "latestCreatedRevision": f"projects/p/locations/r/services/docunavi/revisions/{self.latest}",
            "latestReadyRevision": f"projects/p/locations/r/services/docunavi/revisions/{ready}",
            "uri": PROD,
            "reconciling": False,
            "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
        }

    def patch_service(self, body: dict[str, Any], update_mask: str) -> None:
        self.patches.append((copy.deepcopy(body), update_mask))
        if "template" in body:
            self._n += 1
            self.latest = f"docunavi-{self._n:05d}-new"
            self._lag_left = self.ready_lag
            self.revisions[self.latest] = copy.deepcopy(body["template"])
            if "traffic" not in body or self.moves_traffic_on_template_update:
                self.entries = [{"revision": self.latest, "percent": 100}]
        if "traffic" in body and not self.moves_traffic_on_template_update:
            self.entries = [
                {
                    "revision": self.latest if e["type"] == wire.LATEST else e["revision"],
                    "percent": e["percent"],
                    **({"tag": e["tag"]} if e.get("tag") else {}),
                }
                for e in body["traffic"]
            ]


class Health:
    """URL ごとの `/health` の答え。既定は 200。"""

    def __init__(self, **by_fragment: int) -> None:
        self.by_fragment = by_fragment
        self.asked: list[str] = []

    def __call__(self, url: str) -> int:
        self.asked.append(url)
        for fragment, status in self.by_fragment.items():
            if fragment in url:
                return status
        return 200


def no_sleep(_: float) -> None:
    return None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1
        return self.now


def serving(run: FakeRun) -> dict[str, int]:
    return wire.serving(run.get_service())


# --- テンプレートの書き換え ------------------------------------------------------------------------------------


def test_wiring_adds_the_r2_settings_and_keeps_everything_else() -> None:
    template = base_template()
    out = wire.with_wiring(template, CFG)
    env = {e["name"]: e for e in out["containers"][0]["env"]}
    assert env["PDF_STORE"]["value"] == "r2"
    assert env["R2_ACCOUNT_ID"]["value"] == ACCOUNT and env["R2_BUCKET"]["value"] == BUCKET
    assert env["R2_CREDENTIALS_FILE"]["value"] == "/secrets/r2/credentials.json"
    assert env["AUTH_MODE"]["value"] == "required" and env["DATABASE_URL"] == SECRET_ENV_REF
    assert out["containers"][0]["image"] == "r/p/repo/docunavi:abc1234"
    assert out["containers"][0]["resources"] == template["containers"][0]["resources"]
    assert out["serviceAccount"] == template["serviceAccount"]
    assert "revision" not in out  # リビジョン名は自動
    assert OTHER_VOLUME in out["volumes"] and OTHER_MOUNT in out["containers"][0]["volumeMounts"]
    assert {"name": "r2-app-read", "mountPath": "/secrets/r2"} in out["containers"][0]["volumeMounts"]
    (volume,) = [v for v in out["volumes"] if v["name"] == "r2-app-read"]
    assert volume["secret"] == {"secret": "r2-app-read", "items": [{"path": "credentials.json", "version": "latest"}]}
    assert template == base_template()  # 元を変えない


def test_the_key_is_never_passed_as_an_environment_variable() -> None:
    template = base_template()
    template["containers"][0]["env"].append(
        {"name": "R2_CREDENTIALS_JSON", "valueSource": {"secretKeyRef": {"secret": "r2-app-read", "version": "latest"}}}
    )
    out = wire.with_wiring(template, CFG)
    assert "R2_CREDENTIALS_JSON" not in {e["name"] for e in out["containers"][0]["env"]}
    assert not wire.is_wired(template, CFG)


def test_wiring_is_idempotent_and_detected() -> None:
    once = wire.with_wiring(base_template(), CFG)
    assert wire.is_wired(once, CFG) and not wire.is_wired(base_template(), CFG)
    assert wire.with_wiring(once, CFG) == once


def test_a_partial_wiring_is_not_wired() -> None:
    once = wire.with_wiring(base_template(), CFG)
    no_mount = copy.deepcopy(once)
    no_mount["containers"][0]["volumeMounts"] = [OTHER_MOUNT]
    pinned = copy.deepcopy(once)
    pinned["volumes"][-1]["secret"]["items"][0]["version"] = "2"  # 版を固定すると入れ替えに追従しない
    assert not wire.is_wired(no_mount, CFG) and not wire.is_wired(pinned, CFG)


def test_another_volume_at_the_credentials_directory_is_refused() -> None:
    template = base_template()
    template["containers"][0]["volumeMounts"] = [{"name": "other", "mountPath": "/secrets/r2"}]
    with pytest.raises(wire.Refused):
        wire.with_wiring(template, CFG)


# --- stage -----------------------------------------------------------------------------------------------------


def test_stage_makes_a_wired_revision_at_zero_percent_and_keeps_production(caplog) -> None:
    run, health = FakeRun(), Health()
    caplog.set_level(logging.INFO)
    assert wire.stage(run, CFG, health, no_sleep, Clock()) == 0
    first_body, first_mask = run.patches[0]
    assert first_mask == "template,traffic"
    # 本番は直前のリビジョンに固定し、新しいリビジョンには 0% でタグを付ける（付けないと退役して、準備済みにならない）
    assert first_body["traffic"] == [
        {"type": wire.REVISION, "revision": "docunavi-00003-b9t", "percent": 100, "tag": "r2prev"},
        {"type": wire.LATEST, "percent": 0, "tag": "r2new"},
    ]
    assert wire.is_wired(first_body["template"], CFG)
    assert serving(run) == {"docunavi-00003-b9t": 100}  # 本番は直前のまま
    service = run.get_service()
    assert wire.tagged(service, "r2prev")[0] == "docunavi-00003-b9t"
    assert wire.tagged(service, "r2new")[0] == run.latest
    assert health.asked == ["https://r2new---docunavi-x.run.app"]
    text = caplog.text
    assert ACCOUNT not in text and BUCKET not in text and "docunavi:abc1234" in text


def test_stage_does_nothing_when_the_serving_revision_is_already_wired() -> None:
    run = FakeRun(wire.with_wiring(base_template(), CFG))
    assert wire.stage(run, CFG, Health(), no_sleep, Clock()) == 0
    assert run.patches == []


def test_stage_waits_until_the_new_revision_is_reported_ready() -> None:
    """実機では、タグを付けても「最新の準備済み」への反映が少し遅れる。"""
    run = FakeRun()
    run.ready_lag = 3
    assert wire.stage(run, CFG, Health(), no_sleep, Clock()) == 0
    assert wire.tagged(run.get_service(), "r2new")[0] == run.latest


def test_a_new_revision_without_a_tag_never_becomes_ready() -> None:
    """実機で起きた失敗（1 回目の stage は、タグ無しで更新して、終了コード 4 で止まった）の再現。"""
    run = FakeRun()
    run.patch_service(
        {"template": wire.with_wiring(base_template(), CFG), "traffic": [wire.entry("docunavi-00003-b9t", 100)]},
        "template,traffic",
    )
    assert wire.short(run.get_service()["latestReadyRevision"]) == "docunavi-00003-b9t" != run.latest


def test_stage_stops_when_the_new_revision_is_not_ready() -> None:
    run = FakeRun()
    run.new_revision_fails = True
    assert wire.stage(run, CFG, Health(), no_sleep, Clock()) == 4
    assert serving(run) == {"docunavi-00003-b9t": 100}


def test_stage_leaves_production_alone_when_the_new_revision_fails_health() -> None:
    run = FakeRun()
    assert wire.stage(run, CFG, Health(r2new=503), no_sleep, Clock()) == 4
    assert serving(run) == {"docunavi-00003-b9t": 100}


def test_stage_puts_production_back_if_traffic_moves_unexpectedly() -> None:
    run = FakeRun()
    run.moves_traffic_on_template_update = True
    assert wire.stage(run, CFG, Health(), no_sleep, Clock()) == 5
    assert run.patches[-1][0]["traffic"][0]["revision"] == "docunavi-00003-b9t"


def test_stage_refuses_to_start_when_traffic_is_split() -> None:
    run = FakeRun()
    run.entries = [{"revision": "docunavi-00003-b9t", "percent": 50}, {"revision": "docunavi-00002-x", "percent": 50}]
    with pytest.raises(wire.Refused):
        wire.stage(run, CFG, Health(), no_sleep, Clock())
    assert run.patches == []


# --- promote / rollback ----------------------------------------------------------------------------------------


def staged() -> FakeRun:
    run = FakeRun()
    assert wire.stage(run, CFG, Health(), no_sleep, Clock()) == 0
    return run


def test_promote_moves_production_to_the_staged_revision_and_keeps_the_previous_tag() -> None:
    run, health = staged(), Health()
    new = run.latest
    assert wire.promote(run, CFG, health, no_sleep, Clock()) == 0
    assert serving(run) == {new: 100}
    assert wire.tagged(run.get_service(), "r2prev")[0] == "docunavi-00003-b9t"
    assert health.asked[-1] == PROD  # 切り替えの後は本番の URL を確かめる


def test_promote_refuses_when_nothing_is_staged() -> None:
    with pytest.raises(wire.Refused):
        wire.promote(FakeRun(), CFG, Health(), no_sleep, Clock())


def test_promote_changes_nothing_when_the_staged_revision_is_unhealthy() -> None:
    run = staged()
    before = len(run.patches)
    assert wire.promote(run, CFG, Health(r2new=500), no_sleep, Clock()) == 4
    assert len(run.patches) == before and serving(run) == {"docunavi-00003-b9t": 100}


def test_promote_goes_back_automatically_when_production_fails_health_after_the_switch() -> None:
    run = staged()

    def health(url: str) -> int:
        return 200 if "r2new---" in url else 503  # 本番の URL だけが答えない

    assert wire.promote(run, CFG, health, no_sleep, Clock()) == 4
    assert serving(run) == {"docunavi-00003-b9t": 100}


def test_rollback_returns_production_to_the_tagged_previous_revision() -> None:
    run = staged()
    assert wire.promote(run, CFG, Health(), no_sleep, Clock()) == 0
    assert wire.rollback(run, CFG, Health(), no_sleep, Clock()) == 0
    assert serving(run) == {"docunavi-00003-b9t": 100}
    assert wire.rollback(run, CFG, Health(), no_sleep, Clock()) == 0  # 二度目は何もしない
    assert len(run.patches) == 4


def test_rollback_without_a_previous_tag_is_refused() -> None:
    with pytest.raises(wire.Refused):
        wire.rollback(FakeRun(), CFG, Health(), no_sleep, Clock())


# --- main ------------------------------------------------------------------------------------------------------


ENV = {"GCP_PROJECT_ID": "p", "REGION": "r", "SERVICE_NAME": "docunavi", "R2_ACCOUNT_ID": ACCOUNT, "R2_BUCKET": BUCKET}


def test_main_rejects_an_unknown_mode_and_missing_settings(monkeypatch) -> None:
    for name in ENV:
        monkeypatch.setenv(name, ENV[name])
    assert wire.main(["main.py", "delete"]) == 2
    assert wire.main(["main.py"]) == 2
    monkeypatch.delenv("R2_BUCKET")
    assert wire.main(["main.py", "stage"]) == 2


def test_main_maps_failures_to_exit_codes_without_leaking_the_message(monkeypatch, caplog) -> None:
    for name in ENV:
        monkeypatch.setenv(name, ENV[name])
    monkeypatch.setattr(wire, "RestRun", lambda cfg: FakeRun())

    def refuse(*_: Any) -> int:
        raise wire.Refused("fixed")

    def api(*_: Any) -> int:
        raise wire.RunApiError(403, "PERMISSION_DENIED")

    def boom(*_: Any) -> int:
        raise RuntimeError(f"secret detail {ACCOUNT}")

    for runner, code in ((refuse, 2), (api, 3), (boom, 3)):
        monkeypatch.setitem(wire.RUNNERS, "stage", runner)
        assert wire.main(["main.py", "stage"]) == code
    assert ACCOUNT not in caplog.text and "secret detail" not in caplog.text
