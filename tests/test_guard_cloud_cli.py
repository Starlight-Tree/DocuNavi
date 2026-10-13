"""PreToolUse フック .claude/hooks/guard-cloud-cli.py の判定のテスト。

フックは Claude Code が子プロセスとして呼ぶので、ここでも同じく標準入力に JSON を渡して実行する。
  終了コード 2 = 拒否 / 標準出力に permissionDecision "ask" = 確認の強制 / 何も出さず 0 = 判断しない
GCP には一切触れない（コマンド文字列を検査するだけ）。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "guard-cloud-cli.py"


def run_hook(command: str, tool_name: str = "Bash") -> tuple[int, str, str]:
    payload = {"tool_name": tool_name, "tool_input": {"command": command}, "hook_event_name": "PreToolUse"}
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=20
    )
    return proc.returncode, proc.stdout, proc.stderr


def decision(command: str) -> str:
    code, out, _ = run_hook(command)
    if code == 2:
        return "deny"
    assert code == 0
    if not out.strip():
        return "pass"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


@pytest.mark.parametrize(
    "command",
    [
        # 破壊的な操作
        "gcloud run services delete chatbot --region asia-northeast1",
        "gcloud projects delete my-project",
        "gcloud compute instances delete vm-1 --quiet",
        "echo y | gcloud compute instances delete vm-1",
        "gcloud storage rm -r gs://bucket",
        "gsutil rm -r gs://bucket",
        "bq rm -r -f dataset",
        "terraform destroy -auto-approve",
        "kubectl delete deployment web",
        # 権限・課金・秘密の変更や取得
        "gcloud projects add-iam-policy-binding p --member=user:a@b.c --role=roles/owner",
        "gcloud iam service-accounts keys create k.json --iam-account=a@p.iam.gserviceaccount.com",
        "gcloud secrets versions access latest --secret=api-key",
        "gcloud billing projects link my-proj --billing-account=0000-0000-0000",
        # 認証・ID の切り替え
        "gcloud auth login",
        "gcloud auth print-access-token",
        "gcloud auth application-default print-access-token",
        "gcloud auth revoke --all",
        "gcloud config set auth/impersonate_service_account ''",
        "gcloud config unset auth/impersonate_service_account",
        "gcloud builds list --impersonate-service-account=a@p.iam.gserviceaccount.com",
        "gcloud builds list --account=other@example.com",
        "CLOUDSDK_AUTH_ACCESS_TOKEN=abc gcloud builds list",
        "export GOOGLE_APPLICATION_CREDENTIALS=/tmp/k.json",
        # 認証情報の置き場
        "cat ~/.config/gcloud/credentials.db",
        "ls ~/.config/gcloud",
        "python3 -c \"open('/home/vscode/.config/gcloud/access_tokens.db').read()\"",
        # 形を変えた呼び出し
        "/usr/bin/gcloud run services delete chatbot",
        "sudo gcloud run services delete chatbot",
        "echo chatbot | xargs gcloud run services delete",
        "bash -c 'gcloud run services delete chatbot'",
        "sh -c \"cd /tmp && gcloud projects delete p\"",
        "eval 'gcloud run services delete chatbot'",
        "(gcloud run services delete chatbot)",
        "true && gcloud run services delete chatbot",
        "FOO=1 timeout 30 gcloud run services delete chatbot",
        "gcloud --quiet run services delete chatbot",
    ],
)
def test_dangerous_commands_are_denied(command):
    code, _, err = run_hook(command)
    assert code == 2, f"拒否されていない: {command}"
    assert err.strip(), "理由が返っていない"


@pytest.mark.parametrize(
    "command",
    [
        "gcloud run deploy chatbot --source .",
        "gcloud builds submit --tag gcr.io/p/app",
        "gcloud run services update chatbot --memory 1Gi",
        "gcloud components update",
        "terraform apply",
        "gsutil cp file gs://bucket/",
        "python3 -c \"import subprocess; subprocess.run(['gcloud','run','services','list'])\"",
        "node -e \"require('child_process').execSync('gcloud run deploy x')\"",
    ],
)
def test_write_like_commands_force_a_confirmation(command):
    assert decision(command) == "ask", f"確認が強制されていない: {command}"


@pytest.mark.parametrize(
    "command",
    [
        "gcloud --version",
        "gcloud version",
        "gcloud auth list",
        "gcloud config list",
        "gcloud config get-value project",
        "gcloud projects list --format='table(projectId,name)'",
        "gcloud builds triggers list --project my-proj --region=asia-northeast1",
        "gcloud builds triggers list --project my-proj",
        "gcloud builds list --limit 5",
        "gcloud builds describe 1234-abcd",
        "gcloud run services list --region asia-northeast1",
        "gcloud run services describe chatbot --format=json",
        "gcloud run services logs read chatbot --limit 5",
        "gcloud logging read 'severity>=ERROR AND textPayload:\"delete\"' --limit 20",
        "gcloud iam service-accounts list",
        "gcloud projects get-iam-policy my-proj",
        "gcloud billing accounts list",
        "gsutil ls gs://bucket",
        "bq ls",
        "terraform plan",
        "kubectl get pods",
        "gcloud builds list | head -5",
        # クラウド CLI に関係しないコマンド（文章に語が出るだけのものを含む）
        "ls -la",
        "pytest -q",
        "git status",
        "grep -rn gcloud README.md",
        "echo 'gcloud run services delete x'",
        'git commit -m "docs: mention gcloud auth login and ~/.config/gcloud"',
        "git commit -q -F - <<'EOF'\nfix: x\n\ngcloud auth login is not used.\ngcloud run services delete is blocked by the hook.\nSee ~/.config/gcloud\nEOF",
        "cat <<'EOF' > notes.txt\ngcloud projects delete p\nEOF",
    ],
)
def test_reads_and_unrelated_commands_pass_through(command):
    assert decision(command) == "pass", f"通されていない: {command}"


def test_a_command_after_a_heredoc_on_the_same_line_is_still_checked():
    command = "cat <<'EOF' | gcloud run services delete chatbot\nhello\nEOF"
    assert decision(command) == "deny"


def test_other_tools_are_ignored():
    assert run_hook("gcloud projects delete p", tool_name="Read")[0] == 0


def test_unreadable_input_fails_closed():
    proc = subprocess.run([sys.executable, "-I", str(HOOK)], input="not json", capture_output=True, text=True, timeout=20)
    assert proc.returncode == 2


def test_the_deny_reason_does_not_echo_secrets_or_the_whole_command():
    _, _, err = run_hook("gcloud auth print-access-token")
    assert "ya29" not in err
    assert len(err) < 600
