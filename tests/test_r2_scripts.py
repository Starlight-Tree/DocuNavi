"""R2 キー発行の土台を作るスクリプト（scripts/setup-r2-key-issuer.sh、scripts/store-cloudflare-parent-token.sh）のテスト。

**本物の gcloud には届かない作りにしてある。** 子プロセスの環境は空から作り（親の環境変数・HOME・認証情報を引き継がない）、PATH は
`gcloud` を含まないディレクトリだけ。テストの冒頭で `gcloud` が PATH から見つかったら、何も実行せず失敗する。
スクリプトに gcloud を渡すのは、`GCLOUD_BIN`（絶対パス・実行できる・`# STUB-GCLOUD` の行がある偽物）だけで、スクリプト自身が検査する。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "scripts" / "setup-r2-key-issuer.sh"
STORE = ROOT / "scripts" / "store-cloudflare-parent-token.sh"
TOOLS = ("sha256sum", "cut", "seq", "dirname", "cat", "awk", "tail", "head", "grep", "wc", "tr", "date")
PARENT = "PARENT-TOKEN-VALUE-DO-NOT-LOG"

STUB = """#!/bin/bash
# STUB-GCLOUD
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$*" in
  "config get-value auth/impersonate_service_account") exit 0 ;;
  *image_summary.digest*) echo "sha256:abc123"; exit 0 ;;
  *"secrets versions add"*) n=$(wc -c); printf 'stdin-bytes=%s\\n' "$n" >> "$STUB_LOG"; echo "projects/p/secrets/s/versions/5"; exit 0 ;;
  *"secrets versions list"*) printf 'projects/p/secrets/s/versions/4\\nprojects/p/secrets/s/versions/5\\n'; exit 0 ;;
  *"jobs update r2-check"*"--update-labels=negative-test=1"*) exit 1 ;;
  *"jobs execute"*"--impersonate-service-account"*) exit 1 ;;
  *describe*) exit 1 ;;
esac
exit 0
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """gcloud の無い PATH、偽の gcloud、ログの置き場。本物の gcloud が見つかる状態なら、ここで失敗させる。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    (bin_dir / "sleep").write_text("#!/bin/bash\nexit 0\n")  # 反映待ちの待機は、テストでは待たない
    (bin_dir / "sleep").chmod(0o755)
    assert shutil.which("gcloud", path=str(bin_dir)) is None, "本物の gcloud に届く状態でテストを始めない"
    stub = tmp_path / "stub-gcloud"
    stub.write_text(STUB)
    stub.chmod(0o755)
    poison = tmp_path / "poison"
    poison.mkdir()
    marker = tmp_path / "REAL-GCLOUD-CALLED"
    (poison / "gcloud").write_text(f"#!/bin/bash\ntouch {marker}\nexit 1\n")
    (poison / "gcloud").chmod(0o755)
    return {
        "bin": bin_dir,
        "stub": stub,
        "poison": poison,
        "marker": marker,
        "log": tmp_path / "stub.log",
        "home": tmp_path / "home",
    }


def run(
    sandbox: dict, script: Path, *args: str, stub: bool, poison: bool = False, stdin: str = ""
) -> subprocess.CompletedProcess:
    path = f"{sandbox['poison']}:{sandbox['bin']}" if poison else str(sandbox["bin"])
    env = {"PATH": path, "HOME": str(sandbox["home"]), "STUB_LOG": str(sandbox["log"]), "LANG": "C.UTF-8"}
    if stub:
        env["GCLOUD_BIN"] = str(sandbox["stub"])
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603 - 引数はテストの固定値
        [bash, str(script), *args],
        env=env,
        input=stdin or None,
        stdin=None if stdin else subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def calls(sandbox: dict) -> list[str]:
    return sandbox["log"].read_text().splitlines() if sandbox["log"].exists() else []


SETUP_ARGS = ("proj-x", "--cloudflare-account", "ACCT")


# --- 安全装置 ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("script_args", [SETUP_ARGS + ("-y",), SETUP_ARGS + ("--verify",), SETUP_ARGS])
def test_setup_refuses_without_a_terminal_and_never_calls_gcloud(sandbox, script_args) -> None:
    result = run(sandbox, SETUP, *script_args, stub=False, poison=True)  # PATH に「呼ばれたら印を残す」gcloud を置く
    assert result.returncode == 3 and "gcloud は呼んでいません" in result.stderr
    assert not sandbox["marker"].exists() and calls(sandbox) == []


def test_store_refuses_without_a_terminal_and_never_calls_gcloud(sandbox) -> None:
    result = run(sandbox, STORE, "proj-x", stub=False, poison=True, stdin=PARENT + "\n")
    assert result.returncode == 3
    assert not sandbox["marker"].exists() and calls(sandbox) == []
    assert PARENT not in result.stdout + result.stderr


@pytest.mark.parametrize("bad", ["relative/path", "/nonexistent/gcloud", "gcloud"])
def test_a_gcloud_override_that_is_not_a_marked_stub_is_refused(sandbox, tmp_path, bad) -> None:
    env = {"PATH": f"{sandbox['poison']}:{sandbox['bin']}", "HOME": str(sandbox["home"]), "GCLOUD_BIN": bad}
    bash = shutil.which("bash")
    assert bash
    result = subprocess.run(
        [bash, str(SETUP), *SETUP_ARGS, "-y"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )  # noqa: S603
    assert result.returncode == 2 and not sandbox["marker"].exists()


def test_an_executable_without_the_stub_marker_is_refused(sandbox, tmp_path) -> None:
    fake = tmp_path / "looks-like-gcloud"
    fake.write_text(f"#!/bin/bash\ntouch {sandbox['marker']}\n")  # 目印の行が無い
    fake.chmod(0o755)
    sandbox["stub"] = fake
    result = run(sandbox, SETUP, *SETUP_ARGS, "-y", stub=True)
    assert result.returncode == 2 and not sandbox["marker"].exists()


# --- セットアップが出す gcloud コマンド（偽の gcloud で記録）------------------------------------------------------


def test_setup_issues_only_the_intended_commands(sandbox) -> None:
    result = run(sandbox, SETUP, *SETUP_ARGS, "-y", stub=True)
    assert result.returncode == 0, result.stderr
    log = calls(sandbox)
    text = "\n".join(log)

    for forbidden in (
        "keys create",
        "--allow-unauthenticated",
        "projects add-iam-policy-binding",
        "--impersonate-service-account",
        "set-iam-policy",
    ):
        assert forbidden not in text, forbidden
    assert "--role=roles/owner" not in text and "--role=roles/editor" not in text
    assert (
        "serviceAccountUser" not in text and "serviceAccountTokenCreator" not in text
    )  # agent-deployer に Job を触らせない

    modes = [
        "issue-app-read",
        "issue-ingest-rw",
        "rotate-app-read",
        "rotate-if-due-app-read",
        "revoke-stale",
        "check",
        "revoke-all",
    ]  # r2-ingest-rw は、発行の Job だけ作る（入れ替えの Job と Scheduler は作らない）
    all_deploys = [line for line in log if line.startswith("run jobs deploy")]
    deploys = [line for line in all_deploys if " --service-account r2-key-issuer@" in line]
    assert deploys == all_deploys  # 取り込み・一致確認の Job は作らない・変えない（r2-verify は作成済み）
    assert [line.split()[3] for line in deploys] == [f"r2-{m}" for m in modes]
    for mode, line in zip(modes, deploys, strict=True):
        assert f"--args={mode}" in line  # 引数は Job の定義に固定する（実行時の上書きは使わない）
        assert "--max-retries=0" in line and "--tasks=1" in line and "--parallelism=1" in line
        assert "@sha256:abc123" in line  # イメージは digest で固定
        assert "--service-account r2-key-issuer@proj-x.iam.gserviceaccount.com" in line

    all_invokers = [line for line in log if "run jobs add-iam-policy-binding" in line]
    assert all("--role=roles/run.invoker" in line for line in all_invokers)
    invokers = [line for line in all_invokers if "docunavi-viewer@" in line]
    assert {line.split()[3] for line in invokers} == {
        f"r2-{m}" for m in modes if m not in ("revoke-all", "issue-ingest-rw")
    }  # revoke-all と、書き込める鍵を作る issue-ingest-rw は本人だけ
    # Scheduler の SA が起動できるのは、自動で動かす 3 つだけ（発行・手動の入れ替え・revoke-all は起動できない）
    scheduled = [line for line in all_invokers if "r2-scheduler@proj-x.iam.gserviceaccount.com" in line]
    assert {line.split()[3] for line in scheduled} == {
        "r2-rotate-if-due-app-read",
        "r2-revoke-stale",
        "r2-check",
    }
    assert len(scheduled) + len(invokers) == len(all_invokers)

    adders = [line for line in log if "secretVersionAdder" in line]
    assert {line.split()[2] for line in adders} == {
        "r2-app-read",
        "r2-ingest-rw",
    }  # 親トークンには版を足せない
    issuer = "--member=serviceAccount:r2-key-issuer@proj-x.iam.gserviceaccount.com"
    accessors = [line for line in log if "secretAccessor" in line]
    assert {line.split()[2] for line in accessors if issuer in line} == {
        "cloudflare-issuer-token",
        "r2-app-read",
        "r2-ingest-rw",
    }
    assert all(issuer in line for line in adders)
    assert not any("r2-scheduler@" in line for line in log if "secrets " in line)  # Scheduler は Secret に触れない
    # 取り込み用の SA は r2-ingest-rw だけ、アプリの実行用 SA は r2-app-read だけ読める（Job は無い）
    others = {line.split()[2]: line for line in accessors if issuer not in line}
    assert set(others) == {"r2-ingest-rw", "r2-app-read"}
    assert "--member=serviceAccount:r2-uploader@proj-x.iam.gserviceaccount.com" in others["r2-ingest-rw"]
    assert "--member=serviceAccount:docunavi-run@proj-x.iam.gserviceaccount.com" in others["r2-app-read"]
    # 作らないもの: 通知（Cloud Monitoring）、取り込み・配置の Job、アプリのイメージ、r2-ingest-rw の入れ替えの Job と Scheduler
    assert "monitoring" not in text and "r2-upload" not in text.replace("r2-uploader", "") and "r2tools" not in text
    assert [line.split()[3] for line in all_deploys if "ingest-rw" in line] == ["r2-issue-ingest-rw"]
    assert not any("ingest-rw" in line and "scheduler" in line for line in log)
    created = [line for line in log if "secrets create" in line]
    assert {line.split()[2] for line in created} == {"cloudflare-issuer-token", "r2-app-read", "r2-ingest-rw"}
    assert not any(
        "versions add" in line and "r2-ingest-rw" in line for line in log
    )  # 値は入れない（発行 Job が入れる）


def test_verify_changes_nothing_and_only_impersonates_for_the_negative_tests(sandbox) -> None:
    result = run(sandbox, SETUP, *SETUP_ARGS, "--verify", stub=True)
    log = calls(sandbox)
    assert not any(
        w in line
        for line in log
        for w in (" create ", "add-iam-policy-binding", "run jobs deploy", "versions add", " enable ")
    )
    impersonating = [line for line in log if "--impersonate-service-account" in line]
    assert len(impersonating) == 2 and all(
        ("jobs update r2-check" in line) or ("jobs execute r2-issue-app-read" in line) for line in impersonating
    )
    assert "拒否された（正しい）" in result.stdout


# --- 親トークンの保存 -------------------------------------------------------------------------------------------


def test_store_never_puts_the_token_in_arguments_or_output(sandbox) -> None:
    result = run(sandbox, STORE, "proj-x", stub=True, stdin=PARENT + "\n")
    assert result.returncode == 0, result.stderr
    everything = result.stdout + result.stderr + "\n".join(calls(sandbox)) + sandbox["log"].read_text()
    assert PARENT not in everything
    log = calls(sandbox)
    assert f"stdin-bytes={len(PARENT)}" in "\n".join(log)  # 値は標準入力で渡した（長さだけ記録される）
    assert any("secrets versions add cloudflare-issuer-token --data-file=-" in line for line in log)
    disabled = [line for line in log if "versions disable" in line]
    assert len(disabled) == 1 and disabled[0].startswith(
        "secrets versions disable 4 "
    )  # 新しい版（5）より前の有効な版だけ無効にする
    assert f"{len(PARENT)} 文字" in result.stdout


def test_store_rejects_an_empty_token(sandbox) -> None:
    result = run(sandbox, STORE, "proj-x", stub=True, stdin="\n")
    assert result.returncode == 1 and not any("versions add" in line for line in calls(sandbox))


# --- 自動ローテーション（Scheduler）---------------------------------------------------------------------------------


def _setup_log(sandbox) -> list[str]:
    result = run(sandbox, SETUP, *SETUP_ARGS, "-y", stub=True)
    assert result.returncode == 0, result.stderr
    return calls(sandbox)


def test_scheduler_jobs_call_the_fixed_jobs_without_a_body_or_retries(sandbox) -> None:
    log = _setup_log(sandbox)
    created = {line.split()[4]: line for line in log if line.startswith("scheduler jobs create http")}
    expected = {
        "r2-sched-rotate-app-read": ("0 4 1 * *", "r2-rotate-if-due-app-read"),
        "r2-sched-revoke-stale": ("30 4 * * 1", "r2-revoke-stale"),
        "r2-sched-check": ("0 5 * * 1", "r2-check"),  # 週 1 回
    }
    assert set(created) == set(expected)
    for name, (cron, job) in expected.items():
        line = created[name]
        assert f"--schedule={cron} " in line and "--time-zone=Asia/Tokyo" in line
        assert f"--uri=https://run.googleapis.com/v2/projects/proj-x/locations/asia-northeast1/jobs/{job}:run" in line
        assert "--oauth-service-account-email=r2-scheduler@proj-x.iam.gserviceaccount.com" in line
        assert "--max-retry-attempts=0" in line  # 失敗しても自動では再試行しない（二重に入れ替えない）
        for forbidden in ("--message-body", "--headers", "--oidc"):
            assert forbidden not in line  # 引数は渡さない（Job の固定の引数のまま）


def test_the_scheduler_service_account_has_no_project_role_and_no_secret_access(sandbox) -> None:
    log = _setup_log(sandbox)
    created = [line for line in log if line.startswith("iam service-accounts create r2-scheduler")]
    assert len(created) == 1
    mentions = [line for line in log if "r2-scheduler@" in line and "run jobs add-iam-policy-binding" not in line]
    assert all(line.startswith(("iam service-accounts describe", "scheduler jobs create http")) for line in mentions), (
        mentions
    )


def test_setup_needs_no_alert_address_and_the_check_phrases_match_the_code(sandbox) -> None:
    result = run(sandbox, SETUP, *SETUP_ARGS, "-y", stub=True)  # ALERT_EMAIL なしで動く（通知は作らない）
    assert result.returncode == 0, result.stderr
    # 通知は無いので、警告の文言は r2-check のログで読む。コードが出す文言であること
    source = (ROOT / "ops" / "r2_key_issuer" / "main.py").read_text()
    for phrase in ("PARENT_TOKEN_EXPIRING", "KEY_ROTATION_OVERDUE"):
        assert f'"{phrase}:' in source


def test_the_alert_address_is_not_in_the_repository() -> None:
    for path in (SETUP, ROOT / "docs" / "r2-key-issuer.md"):
        assert "@gmail.com" not in path.read_text()


# --- scripts/setup-r2-wire.sh ---------------------------------------------------------------------------------

WIRE = ROOT / "scripts" / "setup-r2-wire.sh"


@pytest.mark.parametrize("script_args", [SETUP_ARGS + ("-y",), SETUP_ARGS + ("--verify",), SETUP_ARGS])
def test_wire_setup_refuses_without_a_terminal_and_never_calls_gcloud(sandbox, script_args) -> None:
    result = run(sandbox, WIRE, *script_args, stub=False, poison=True)
    assert result.returncode == 3 and "gcloud は呼んでいません" in result.stderr
    assert not sandbox["marker"].exists() and calls(sandbox) == []


def test_wire_setup_creates_three_jobs_and_least_privilege_and_does_not_touch_the_service(sandbox) -> None:
    result = run(sandbox, WIRE, *SETUP_ARGS, "-y", stub=True)
    assert result.returncode == 0, result.stderr
    log = calls(sandbox)
    deploys = [line for line in log if line.startswith("run jobs deploy")]
    assert [line.split()[3] for line in deploys] == ["r2-wire-stage", "r2-wire-promote", "r2-wire-rollback"]
    for line, mode in zip(deploys, ("stage", "promote", "rollback"), strict=True):
        assert f"--args={mode}" in line and "--service-account r2-wirer@proj-x.iam.gserviceaccount.com" in line
        assert "@sha256:abc123" in line and "--max-retries=0" in line and "R2_ACCOUNT_ID=ACCT" in line
    # サービスを変える操作は、スクリプトの中に無い（Job を実行したときだけ変わる）
    assert not any(line.startswith(("run deploy", "run services update", "run jobs execute")) for line in log)
    # 権限は 3 つだけ。閲覧専用の ID に Job の実行権限は付けない
    bindings = [line for line in log if "add-iam-policy-binding" in line]
    assert len(bindings) == 4
    assert any("run services add-iam-policy-binding docunavi" in b and "roles/run.developer" in b for b in bindings)
    assert any(
        "service-accounts add-iam-policy-binding docunavi-run@proj-x" in b and "roles/iam.serviceAccountUser" in b
        for b in bindings
    )
    assert any(
        "secrets add-iam-policy-binding r2-app-read" in b and "roles/secretmanager.viewer" in b for b in bindings
    )
    # サービスの更新時に、Cloud Run が呼び出し側のイメージ読み取りを確かめる（実際に 403 になった）。リポジトリ単位の reader だけ
    assert any(
        "artifacts repositories add-iam-policy-binding cloud-run-source-deploy" in b
        and "roles/artifactregistry.reader" in b
        for b in bindings
    )
    assert not any("docunavi-viewer" in line or "roles/run.invoker" in line for line in log)
    assert not any("secretAccessor" in line or "versions add" in line for line in log)


def test_wire_setup_verify_changes_nothing(sandbox) -> None:
    run(sandbox, WIRE, *SETUP_ARGS, "--verify", stub=True)
    writes = ("create", "deploy", "add-iam-policy-binding", "update", "submit", "enable", "execute")
    assert not any(
        any(w in line.split()[:5] for w in writes) for line in calls(sandbox) if not line.startswith("config get-value")
    )
