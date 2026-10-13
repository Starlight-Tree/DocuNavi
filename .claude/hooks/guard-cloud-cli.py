#!/usr/bin/env python3
"""PreToolUse フック: クラウド CLI（gcloud など）の危険な呼び出しを、実行前に止める。

この開発コンテナの gcloud には、ユーザー自身のアカウントでログインする（README の「開発環境の gcloud」）。
ログイン情報があるので、誤操作や、取り込んだ文書に埋め込まれた指示による破壊的な操作を、ここで防ぐ。

  終了コード 2  … 拒否（理由を標準エラーへ。Claude Code はこの呼び出しを実行せず、理由をモデルに返す）
  JSON を標準出力 … 確認を強制する（permissionDecision: "ask"）
  何も出さず 0   … この検査では判断しない（通常の権限ルールに任せる）

方針:
  - 読み取り（list / describe / get / read / logs ...）は通す。
  - 認証情報の取得・ID の切り替え・IAM や課金の変更・削除は拒否する。
  - それ以外の gcloud などの操作（deploy / create / update ...）は確認を強制する。
  - クラウド CLI に関係の無いコマンドは、何もしない。

これはセキュリティ境界ではない（コマンド文字列の検査なので、スクリプトの中身までは追えない）。
日常の ID を閲覧専用にする設定と、GCP 側の保護と組み合わせて使う。異常終了・タイムアウトは
settings.json の onFailure: "block" で拒否にする。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

CLIS = {"gcloud", "gsutil", "bq", "terraform", "tofu", "kubectl"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
WRAPPERS = {
    "sudo", "env", "time", "timeout", "nohup", "nice", "command", "exec", "xargs",
    "watch", "stdbuf", "setsid", "ionice", "flock", "builtin",
}
INTERPRETER = re.compile(r"^(python[\d.]*|node|nodejs|deno|bun|perl|ruby|php)$")
OPERATOR = re.compile(r"^[;&|()<>]+$")
ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
HEREDOC = re.compile(
    r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1([^\n]*)\n.*?\n[ \t]*\2[ \t]*(?:\n|$)", re.S
)

# 認証情報の置き場・トークンの取得・ID の切り替え。これらを含む操作は拒否する
CREDENTIAL_TEXT = re.compile(
    r"\.config/gcloud|application_default_credentials|credentials\.db|access_tokens\.db"
    r"|legacy_credentials|\.gcloud-token|print-access-token|print-identity-token"
)
IDENTITY_ENV = re.compile(r"^(CLOUDSDK_[A-Z0-9_]+|GOOGLE_APPLICATION_CREDENTIALS|GOOGLE_OAUTH_ACCESS_TOKEN)$")
IDENTITY_FLAGS = ("--impersonate-service-account", "--account", "--access-token-file", "--credential-file-override")
# 文章（コミットメッセージなど）に書いただけで拒否しないよう、検査から外すプログラム
TEXT_ONLY = {"echo", "printf"}

DESTRUCTIVE = {
    "delete", "destroy", "undelete", "purge", "wipe", "disable", "remove", "rm", "rb", "reset",
    "set-iam-policy", "add-iam-policy-binding", "remove-iam-policy-binding", "set-iam", "set-iam-policy-binding",
}
SENSITIVE_GROUPS = {"iam", "billing", "organizations", "projects", "resource-manager", "kms"}
READ_VERBS = {
    "list", "describe", "get", "read", "tail", "log", "logs", "get-iam-policy", "get-value", "versions", "info",
    "version", "help", "show", "ls", "cat", "stat", "du", "head", "plan", "validate", "fmt", "output", "graph",
    "top", "explain", "api-resources", "cluster-info",
}
VALUE_FLAGS = {
    "--project", "--region", "--zone", "--location", "--format", "--filter", "--limit", "--sort-by",
    "--page-size", "--configuration", "--billing-project", "--verbosity", "--service", "--freshness", "--order",
}


class Finding:
    def __init__(self, level: str, reason: str) -> None:
        self.level, self.reason = level, reason


def strip_heredocs(text: str) -> str:
    """ヒアドキュメントの本文（コミットメッセージなどの文章）を除く。同じ行の続き（| gcloud ... など）は残す。"""
    return HEREDOC.sub(lambda m: m.group(3) + "\n", text)


def tokenize(text: str) -> list[str]:
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""  # '#' 以降も検査する（コメントに隠した操作を見逃さない）
        return list(lex)
    except ValueError:  # 閉じていない引用符など
        return text.split()


def split_simple_commands(tokens: list[str]) -> list[list[str]]:
    commands: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if OPERATOR.match(token):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token.strip("`"))
    if current:
        commands.append(current)
    return commands


def program_index(words: list[str]) -> int | None:
    """環境変数の代入とラッパー（sudo / env / timeout ...）を飛ばして、実際のプログラムの位置を返す。"""
    i = 0
    while i < len(words):
        word = words[i]
        base = os.path.basename(word)
        if ASSIGNMENT.match(word):
            i += 1
        elif base in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", words[i])):
                i += 1
        else:
            return i
    return None


def positionals(args: list[str]) -> list[str]:
    result: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            if "=" not in arg and arg in VALUE_FLAGS:
                skip = True
            continue
        result.append(arg)
    return result


def classify_cli(cli: str, args: list[str]) -> Finding | None:
    for arg in args:
        name = arg.split("=", 1)[0]
        if name in IDENTITY_FLAGS:
            return Finding("deny", f"ID を切り替えるフラグ（{name}）は使えません。日常の ID は閲覧専用に固定しています。")
    pos = positionals(args)
    if not pos:
        return None
    group, verbs = pos[0], set(pos)

    if cli == "gcloud":
        if group == "auth":
            return None if pos[:2] == ["auth", "list"] else Finding(
                "deny", "gcloud auth の操作（ログイン・トークンの取得・切り替え）は私には許可していません。必要ならユーザーが実行します。"
            )
        if group == "config":
            ok = pos[1:2] and pos[1] in {"list", "get-value", "get"} or pos[1:3] in (["configurations", "list"], ["configurations", "describe"])
            return None if ok else Finding("deny", "gcloud config の変更（アカウント・プロジェクト・なりすましの設定）は許可していません。")
        if group in {"version", "info", "help", "topic", "feedback"}:
            return None
        if group == "components":
            return None if pos[1:2] == ["list"] else Finding("ask", "gcloud components の変更は確認が必要です。")
        if "secrets" in verbs and "access" in verbs:
            return Finding("deny", "Secret Manager の値の取得は許可していません。")
        if "keys" in verbs and "create" in verbs:
            return Finding("deny", "サービスアカウントキーの作成は許可していません。")
    elif cli == "gsutil":
        if group in {"rm", "rb", "rsync", "acl", "iam", "defacl", "setmeta"}:
            return Finding("deny", f"gsutil {group} は許可していません。必要ならユーザーが実行します。")
        return None if group in {"ls", "cat", "stat", "du", "version", "help", "hash"} else Finding("ask", "gsutil の書き込み系は確認が必要です。")
    elif cli == "bq":
        if group in {"rm", "mk", "update", "cp", "load", "insert"}:
            return Finding("deny" if group == "rm" else "ask", f"bq {group} は確認が必要です。")
        return None if group in {"ls", "show", "head", "version", "help", "info"} else Finding("ask", "bq の操作は確認が必要です。")
    elif cli in {"terraform", "tofu"}:
        if group == "destroy":
            return Finding("deny", "terraform destroy は許可していません。必要ならユーザーが実行します。")
        return None if group in {"plan", "validate", "fmt", "show", "version", "output", "graph"} else Finding(
            "ask", f"{cli} {group} は確認が必要です。"
        )
    elif cli == "kubectl":
        if group in {"delete", "drain", "replace"}:
            return Finding("deny", f"kubectl {group} は許可していません。必要ならユーザーが実行します。")
        return None if group in {"get", "describe", "logs", "top", "version", "explain", "api-resources", "cluster-info"} else Finding(
            "ask", f"kubectl {group} は確認が必要です。"
        )

    # gcloud の一般則
    if verbs & DESTRUCTIVE:
        return Finding("deny", f"破壊的・権限変更の操作（{', '.join(sorted(verbs & DESTRUCTIVE))}）は許可していません。必要ならユーザーが実行します。")
    if cli == "gcloud" and group in SENSITIVE_GROUPS:
        reads = any(v in READ_VERBS or v.startswith(("list", "describe", "get-")) for v in pos)
        return None if reads else Finding("deny", f"gcloud {group} の変更は許可していません。必要ならユーザーが実行します。")
    if any(v in READ_VERBS or v.startswith(("list", "describe", "get-")) for v in pos):
        return None
    return Finding("ask", f"{cli} の書き込み系の可能性がある操作です。内容を確認してください: {cli} {' '.join(pos[:4])}")


def analyze(text: str, depth: int = 0) -> list[Finding]:
    findings: list[Finding] = []
    text = strip_heredocs(text)
    for words in split_simple_commands(tokenize(text)):
        for word in words:
            m = ASSIGNMENT.match(word)
            if m and IDENTITY_ENV.match(m.group(1)):
                findings.append(Finding("deny", f"認証・ID を変える環境変数（{m.group(1)}）は設定できません。"))
        index = program_index(words)
        if index is None:
            continue
        program = os.path.basename(words[index])
        rest = words[index + 1:]

        if program in CLIS:
            finding = classify_cli(program, rest)
            if finding:
                findings.append(finding)
        elif program in SHELLS and "-c" in rest and rest.index("-c") + 1 < len(rest) and depth < 3:
            findings += analyze(rest[rest.index("-c") + 1], depth + 1)
        elif program == "eval" and depth < 3:
            findings += analyze(" ".join(rest), depth + 1)
        elif INTERPRETER.match(program) and any(re.search(r"\b(gcloud|gsutil|bq|terraform|kubectl)\b", w) for w in rest):
            findings.append(Finding("ask", "スクリプトの中でクラウド CLI を呼んでいます。内容を確認してください。"))

        if program not in TEXT_ONLY and not (program == "git" and {"commit", "tag", "merge"} & set(rest)):
            if CREDENTIAL_TEXT.search(" ".join(words)):
                findings.append(Finding("deny", "gcloud の認証情報（~/.config/gcloud、トークン、アクセストークンの出力）には触れません。"))
    return findings


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001  入力が読めないときは、通さない
        print("ガード: フックの入力を読めませんでした。実行を止めます。", file=sys.stderr)
        return 2
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    findings = analyze(command)
    if not findings:
        return 0

    denies = [f.reason for f in findings if f.level == "deny"]
    if denies:
        print("ガード（クラウド CLI）: 実行を止めました。\n- " + "\n- ".join(dict.fromkeys(denies)), file=sys.stderr)
        return 2
    asks = list(dict.fromkeys(f.reason for f in findings))
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": "ガード（クラウド CLI）: " + " / ".join(asks),
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
