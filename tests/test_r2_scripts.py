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
        "rotate-ingest-rw",
        "revoke-stale",
        "check",
        "revoke-all",
    ]
    all_deploys = [line for line in log if line.startswith("run jobs deploy")]
    deploys = [line for line in all_deploys if " --service-account r2-key-issuer@" in line]
    assert [line.split()[3] for line in deploys] == [f"r2-{m}" for m in modes]
    for mode, line in zip(modes, deploys, strict=True):
        assert f"--args={mode}" in line  # 引数は Job の定義に固定する（実行時の上書きは使わない）
        assert "--max-retries=0" in line and "--tasks=1" in line and "--parallelism=1" in line
        assert "@sha256:abc123" in line  # イメージは digest で固定
        assert "--service-account r2-key-issuer@proj-x.iam.gserviceaccount.com" in line

    invokers = [line for line in log if "run jobs add-iam-policy-binding" in line]
    assert {line.split()[3] for line in invokers} == {f"r2-{m}" for m in modes if m != "revoke-all"} | {
        "r2-upload-plan",
        "r2-upload",
        "r2-verify",
    }  # revoke-all は本人だけ
    assert all("--role=roles/run.invoker" in line and "docunavi-viewer@" in line for line in invokers)

    adders = [line for line in log if "secretVersionAdder" in line]
    assert {line.split()[2] for line in adders} == {"r2-app-read", "r2-ingest-rw"}  # 親トークンには版を足せない
    issuer = "--member=serviceAccount:r2-key-issuer@proj-x.iam.gserviceaccount.com"
    accessors = [line for line in log if "secretAccessor" in line]
    assert {line.split()[2] for line in accessors if issuer in line} == {
        "cloudflare-issuer-token",
        "r2-app-read",
        "r2-ingest-rw",
    }
    assert all(issuer in line for line in adders)
    # PDF の配置・確認の Job: 置く SA は取り込み用キーだけ、確認はアプリの実行用 SA が読み取りキーだけ
    others = {line.split()[2]: line for line in accessors if issuer not in line}
    assert set(others) == {"r2-ingest-rw", "r2-app-read"}
    assert "--member=serviceAccount:r2-uploader@proj-x.iam.gserviceaccount.com" in others["r2-ingest-rw"]
    assert "--member=serviceAccount:docunavi-run@proj-x.iam.gserviceaccount.com" in others["r2-app-read"]

    tools = {line.split()[3]: line for line in all_deploys if line not in deploys}
    assert set(tools) == {"r2-upload-plan", "r2-upload", "r2-verify"}
    for line in tools.values():
        assert "@sha256:abc123" in line and "--command=python" in line and "--max-retries=0" in line
    assert "--args=-m,app.r2_store,upload " in tools["r2-upload-plan"] and "--apply" not in tools["r2-upload-plan"]
    assert "--args=-m,app.r2_store,upload,--apply" in tools["r2-upload"]
    assert "--args=-m,app.r2_store,verify" in tools["r2-verify"]
    assert "--service-account r2-uploader@proj-x.iam.gserviceaccount.com" in tools["r2-upload"]
    assert "--service-account docunavi-run@proj-x.iam.gserviceaccount.com" in tools["r2-verify"]
    assert "R2_CREDENTIALS_JSON=r2-app-read:latest,DATABASE_URL=database-url:latest" in tools["r2-verify"]
    assert "R2_CREDENTIALS_JSON=r2-ingest-rw:latest" in tools["r2-upload"]


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
