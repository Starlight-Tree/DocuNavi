"""PreToolUse フック .claude/hooks/guard-cloud-cli.py の、認証情報の拒否理由のテスト。

拒否したとき、どの語に一致したかと、次の行動（語を外す / ユーザーに伝える）を理由に含める。
判定そのもの（何を拒否するか）は tests/test_guard_cloud_cli.py が持つ。ここでは理由の中身だけを見る。
GCP には一切触れない（コマンド文字列を検査するだけ）。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "guard-cloud-cli.py"


def run_hook(command: str) -> tuple[int, str, str]:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "hook_event_name": "PreToolUse"}
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=20
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.mark.parametrize(
    ("command", "shown"),
    [
        # 一致した部分だけを出す（語全体ではない）
        ("findmnt -T /home/vscode/.config/gcloud", ".config/gcloud"),
        ("cat ~/.config/gh/hosts.yml", ".config/gh"),
        ("ls /home/vscode/.gh-config", ".gh-config"),
        ("gcloud auth print-access-token", "print-access-token"),
        ("GH_CONFIG_DIR=/tmp/x gh run list", "GH_CONFIG_DIR"),
        # パスとして解決できた語は、その語を出す（一致する部分文字列が無いため）
        ("cat ~/.config/./gcloud/properties", "~/.config/./gcloud/properties"),
        ("ls ~/.c*/gcloud", "~/.c*/gcloud"),
        # 認証情報を含む上位のディレクトリを、まとめて検索・コピーする
        ("grep -r token ~/.config", "~/.config"),
        # リダイレクトの先
        ("echo x > ~/.config/gcloud/properties", ".config/gcloud"),
    ],
)
def test_deny_reason_names_the_matched_word(command, shown):
    code, _, err = run_hook(command)
    assert code == 2, f"拒否されていない: {command!r}"
    assert f"一致した語: {shown}。" in err, err
    # 次の行動は 2 通り: 認証情報が要らないなら語を外す / 要るなら、別の書き方で試さずユーザーに伝える
    assert "この語を命令から外してください" in err
    assert "別の書き方で試さず、ユーザーに伝えてください" in err


def test_deny_reason_keeps_the_original_sentence():
    """従来の文は、そのまま先頭に残す（それを手がかりに読む人・テストを壊さない）。"""
    _, _, err = run_hook("cat ~/.config/gh/hosts.yml")
    assert "認証情報（~/.config/gcloud、~/.config/gh、gh の設定ディレクトリ、トークンの出力）には触れません。" in err


def test_long_word_is_truncated():
    command = "ls ~/.c*/gcloud/" + "a" * 200
    code, _, err = run_hook(command)
    assert code == 2
    assert "..." in err
    assert "a" * 100 not in err, "長い語がそのまま写っている"


@pytest.mark.parametrize(
    "command",
    [
        "gcloud projects delete my-project",
        "gcloud config set auth/impersonate_service_account ''",
    ],
)
def test_other_denies_do_not_claim_a_matched_word(command):
    """認証情報以外の拒否には、「一致した語」を付けない。"""
    code, _, err = run_hook(command)
    assert code == 2
    assert "一致した語" not in err


@pytest.mark.parametrize(
    "command",
    [
        "findmnt",  # パスを含まない確認は、これまでどおり通る
        "findmnt -T /workspaces/docunavi",
        'echo ".config/gcloud"',  # 文章に書くだけなら止めない
        "git commit -m 'document ~/.config/gcloud handling'",
    ],
)
def test_commands_without_credentials_still_pass(command):
    code, out, err = run_hook(command)
    assert code == 0, err
    assert not out.strip()
