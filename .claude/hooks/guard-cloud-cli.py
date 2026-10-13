#!/usr/bin/env python3
"""PreToolUse フック: クラウド CLI（gcloud など）と GitHub CLI（gh）の危険な呼び出しを、実行前に止める。

この開発コンテナの gcloud には、ユーザー自身のアカウントでログインする（README の「開発環境の gcloud」）。
gh には、このリポジトリだけの読み取り専用の細粒度トークンを置く（README の「開発環境の GitHub CLI（gh）」）。
認証情報があるので、誤操作や、取り込んだ文書に埋め込まれた指示による破壊的な操作を、ここで防ぐ。

  終了コード 2  … 拒否（理由を標準エラーへ。Claude Code はこの呼び出しを実行せず、理由をモデルに返す）
  JSON を標準出力 … 確認を強制する（permissionDecision: "ask"）
  何も出さず 0   … この検査では判断しない（通常の権限ルールに任せる）

方針:
  - 読み取り（list / describe / get / read / logs / view ...）は通す。
  - 認証情報の取得・ID の切り替え・IAM や課金の変更・削除は拒否する。
  - それ以外の gcloud / gh などの操作（deploy / create / update ...）は確認を強制する。
  - これらの CLI に関係の無いコマンドは、何もしない。

コマンド文字列は、シェルの構文（引用符・改行・; & | ( )・リダイレクト・ヒアドキュメント・コマンド置換）を
近似的に読んで、1 つずつの単純なコマンドに分けてから調べる。ラッパー（sudo / env / xargs ...）、
`bash -c`、`eval`、`find -exec`、シェルへ渡す文字列（ヒアドキュメント・パイプ）の中身も調べる。

これはセキュリティ境界ではない（コマンド文字列の検査なので、変数やファイルに隠した操作、スクリプトの中身、
パスの難読化までは追えない）。gcloud は日常の ID を閲覧専用にする設定と GCP 側の保護、gh はトークンの
権限（読み取り専用）と組み合わせて使う。異常終了・タイムアウトは settings.json の onFailure: "block" で
拒否にする。
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import sys

MAX_DEPTH = 5  # bash -c や $(...) の入れ子を追う深さ

CLIS = {"gcloud", "gsutil", "bq", "terraform", "tofu", "kubectl", "gh"}
CLI_WORD = re.compile(r"\b(gcloud|gsutil|bq|terraform|tofu|kubectl|gh)\b")
# 標準入力のスクリプトを実行するシェル（-c の文字列も調べる）。su / script は -c の文字列だけ調べる
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh", "mksh", "rbash"}
C_ONLY_SHELLS = {"su", "script"}
SOURCERS = {"source", "."}
INTERPRETER = re.compile(r"^(python[\d.]*|node|nodejs|deno|bun|perl|ruby|php|awk|gawk|mawk|nawk|lua|luajit)$")
# 引数のスクリプト・タスクの中でコマンドを呼びうるランナー（中身までは追えないので、CLI の名前が見えたら確認する）
RUNNERS = {"uv", "uvx", "npx", "pnpm", "yarn", "npm", "poetry", "pipx", "make", "just", "tox", "nox"}
# 標準入力（ヒアドキュメント・ヒアストリング）で受け取ったコマンドを、後で実行するもの
SCHEDULERS = {"at", "batch"}
# 他のコマンドを起動するラッパー: 名前 -> (値を取るオプション, 先頭で飛ばす位置引数の数)
WRAPPERS: dict[str, tuple[set[str], int]] = {
    "sudo": (
        {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t", "--user", "--group", "--host", "--prompt",
         "--close-from", "--chdir", "--chroot", "--command-timeout", "--other-user", "--role", "--type"},
        0,
    ),
    "doas": ({"-u", "-C"}, 0),
    "env": ({"-u", "-C", "-S", "-a", "--unset", "--chdir", "--split-string", "--argv0"}, 0),
    "time": ({"-f", "-o", "--format", "--output"}, 0),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1),
    "nohup": (set(), 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"}, 0),
    "command": (set(), 0),
    "builtin": (set(), 0),
    "exec": ({"-a"}, 0),
    # -i / -l / --replace / --eof / --max-lines の値は、-i{} のように続けて書く形だけ（別の引数にはならない）
    "xargs": (
        {"-I", "-d", "-a", "-E", "-L", "-n", "-P", "-s", "--delimiter", "--arg-file", "--max-args", "--max-procs",
         "--max-chars"},
        0,
    ),
    "parallel": ({"-j", "-n", "-N", "-a", "-P", "-S", "-L", "-l", "--jobs", "--arg-file", "--sshlogin"}, 0),
    "watch": ({"-n", "--interval"}, 0),  # -d / --differences の値は、続けて書く形だけ
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    "setsid": (set(), 0),
    "flock": ({"-w", "-E", "--timeout", "--conflict-exit-code"}, 1),
    "busybox": (set(), 0),
    "taskset": ({"-c"}, 0),
    "strace": ({"-e", "-o", "-p", "-s", "-u"}, 0),
    "unbuffer": (set(), 0),
}
# コマンドの先頭に来うる予約語（if / while / do ... の直後のコマンドを見落とさないよう飛ばす）
RESERVED = {
    "if", "then", "elif", "else", "fi", "do", "done", "while", "until", "for", "in", "case", "esac", "select",
    "function", "{", "}", "!", "coproc",
}
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=")
ASSIGNING_BUILTINS = {"export", "declare", "typeset", "readonly", "local"}

# シェルの演算子（長いものを先に）。REDIRECTS は直後の語を対象として取り、それ以外は単純コマンドを区切る
OPERATORS = [
    "<<<", "<<-", "&>>", ";;&", "<<", ">>", ">&", "<&", "&>", ">|", "<>", "&&", "||", "|&", ";;", ";&", "<(", ">(",
    ";", "&", "|", "(", ")", "<", ">",
]
REDIRECTS = {"<", ">", ">>", ">&", "<&", "&>", "&>>", ">|", "<>"}
PIPES = {"|", "|&"}
CONTINUING = {"|", "|&", "&&", "||"}  # 直後の改行で、コマンドが続く

# 認証情報の置き場・トークンの取得・ID の切り替え。これらを含む操作は拒否する
CREDENTIAL_TEXT = re.compile(
    r"\.config/gcloud|application_default_credentials|credentials\.db|access_tokens\.db"
    r"|legacy_credentials|\.gcloud-token|print-access-token|print-identity-token"
    r"|\.config/gh(?![\w-])|\.gh-config|\bGH_CONFIG_DIR\b|\bCLOUDSDK_CONFIG\b|\.git-credentials"
)
# 認証情報のディレクトリ（~ や $HOME を展開し、// ・ /./ ・ .. を畳み、* や ? を含む指定も照合する）
HOME_DIRS = {os.path.expanduser("~"), "/home/vscode"}
PROTECTED_DIRS = [h + "/" + d for h in sorted(HOME_DIRS) for d in (".config/gcloud", ".config/gh", ".gh-config")]
PROTECTED_FILES = ("hosts.yml", "config.yml", "credentials.db", "access_tokens.db", "legacy_credentials")
PROTECTED_PATHS = PROTECTED_DIRS + [d + "/" + f for d in PROTECTED_DIRS for f in PROTECTED_FILES]
# 認証・ID を変える環境変数（gh の GH_PAGER / GH_EDITOR / GH_BROWSER は、任意のコマンドを起動させられるので含める）
IDENTITY_ENV = re.compile(
    r"^(CLOUDSDK_[A-Z0-9_]+|GOOGLE_APPLICATION_CREDENTIALS|GOOGLE_OAUTH_ACCESS_TOKEN"
    r"|GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN|GH_HOST|GH_CONFIG_DIR"
    r"|GH_DEBUG|GH_PAGER|GH_EDITOR|GH_BROWSER)$"
)
# gh が、専用の変数が無いときに代わりに使う環境変数（任意のコマンドを起動させられる）。CLI の呼び出しに付けたら拒否する
LAUNCHER_ENV = re.compile(r"^(BROWSER|PAGER|EDITOR|VISUAL)$")
# 通信の経路や、信頼する証明書を変える環境変数（トークンを別の経路へ流せる）。CLI の呼び出しに付けたら拒否する
ROUTING_ENV = re.compile(
    r"^(?i:https?_proxy|all_proxy|ssl_cert_file|ssl_cert_dir|curl_ca_bundle|requests_ca_bundle"
    r"|node_extra_ca_certs|sslkeylogfile)$"
)
IDENTITY_FLAGS = ("--impersonate-service-account", "--account", "--access-token-file", "--credential-file-override")
# 文章（コミットメッセージなど）に書いただけで拒否しないよう、検査から外すプログラム
TEXT_ONLY = {"echo", "printf"}
GREP_PROGRAMS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "zgrep"}
# 認証情報のディレクトリを含む上位のディレクトリを渡されると、まとめて読む・写す・開くプログラム
PARENT_SENSITIVE = GREP_PROGRAMS | {"find", "cp", "mv", "tar", "zip", "rsync", "scp", "cd", "pushd"}

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


CREDENTIAL_DENY = Finding(
    "deny", "認証情報（~/.config/gcloud、~/.config/gh、gh の設定ディレクトリ、トークンの出力）には触れません。"
)
GIT_CREDENTIAL_DENY = Finding(
    "deny", "git credential（保存された認証情報の取り出し）は許可していません。push / pull はそのまま使えます。"
)


# --- 字句解析（シェルの構文の近似）---


class Word:
    """引用符を外した 1 つの語。subs は、引用符の中・バッククォートの中のコマンド置換の中身。"""

    __slots__ = ("text", "subs", "quoted")

    def __init__(self, text: str, subs: list[str], quoted: bool) -> None:
        self.text, self.subs, self.quoted = text, subs, quoted


class Heredoc:
    __slots__ = ("delim", "quoted", "strip_tabs", "body")

    def __init__(self, delim: str, quoted: bool, strip_tabs: bool) -> None:
        self.delim, self.quoted, self.strip_tabs, self.body = delim, quoted, strip_tabs, ""

    def read(self, text: str, i: int) -> int:
        """区切り行までを本文として読み、次の位置を返す（終端が無ければ末尾まで）。"""
        lines: list[str] = []
        n = len(text)
        while i < n:
            j = text.find("\n", i)
            line = text[i:] if j < 0 else text[i:j]
            i = n if j < 0 else j + 1
            if line.strip() == self.delim:
                break
            lines.append(line)
        self.body = "\n".join(lines)
        return i


_ANSI_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "v": "\v",
    "\\": "\\", "'": "'", '"': '"', "?": "?",
}


def _ansi_escape(text: str, i: int) -> tuple[str, int]:
    """$'...' の中の、text[i] == '\\' から始まるエスケープを解釈して (文字, 次の位置) を返す。"""
    c = text[i + 1 : i + 2]
    if c in _ANSI_ESCAPES:
        return _ANSI_ESCAPES[c], i + 2
    if c == "x":
        m = re.match(r"[0-9A-Fa-f]{1,2}", text[i + 2 :])
        if m:
            return chr(int(m.group(), 16)), i + 2 + len(m.group())
    if c and c in "01234567":
        m = re.match(r"[0-7]{1,3}", text[i + 1 :])
        if m:
            return chr(int(m.group(), 8) & 0xFF), i + 1 + len(m.group())
    if c in ("u", "U"):
        m = re.match(r"[0-9A-Fa-f]{1,%d}" % (4 if c == "u" else 8), text[i + 2 :])
        if m:
            try:
                return chr(int(m.group(), 16)), i + 2 + len(m.group())
            except (ValueError, OverflowError):
                pass
    return "\\" + c, i + 2


def _skip_backtick(text: str, i: int) -> int:
    """text[i] == '`' に対応する閉じの次の位置を返す（閉じが無ければ末尾）。"""
    j, n = i + 1, len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
        elif text[j] == "`":
            return j + 1
        else:
            j += 1
    return n


MAX_NESTING = 40  # $( ... ) と引用符の入れ子を読む深さ（病的な入力で再帰が深くならないように）


def _skip_double_quote(text: str, i: int, level: int = 0) -> int:
    """text[i] は開きの '"' の次。閉じの次の位置を返す（閉じが無ければ末尾）。"""
    n = len(text)
    if level > MAX_NESTING:
        return n
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c == "$" and text[i + 1 : i + 2] == "(":
            i = _match_paren(text, i + 1, level + 1)
        elif c == "`":
            i = _skip_backtick(text, i)
        else:
            i += 1
    return n


def _match_paren(text: str, i: int, level: int = 0) -> int:
    """text[i] == '(' に対応する ')' の次の位置を返す（引用符・エスケープを考慮。閉じが無ければ末尾）。"""
    depth, n = 0, len(text)
    if level > MAX_NESTING:
        return n
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i = _skip_double_quote(text, i + 1, level + 1)
            continue
        if c == "`":
            i = _skip_backtick(text, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _inner(text: str, start: int, end: int) -> str:
    """text[start:end]（閉じを含む）から、閉じの 1 文字を除いた中身を返す。"""
    return text[start : end - 1] if end - 1 >= start and text[end - 1 : end] in (")", "`") else text[start:end]


def substitutions_in(text: str) -> list[str]:
    """文章（引用符なしのヒアドキュメントの本文など）の中の $(...) とバッククォートの中身を返す。"""
    found: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "$" and text[i + 1 : i + 2] == "(":
            end = _match_paren(text, i + 1)
            found.append(_inner(text, i + 2, end))
            i = end
        elif c == "`":
            end = _skip_backtick(text, i)
            found.append(_inner(text, i + 1, end))
            i = end
        else:
            i += 1
    return found


class _Lexer:
    def __init__(self, text: str) -> None:
        self.text, self.n = text, len(text)
        self.toks: list[Word | Heredoc | str] = []
        self.buf: list[str] = []
        self.subs: list[str] = []
        self.has = False
        self.quoted = False
        self.want: str | None = None  # 直前の演算子が << / <<- のとき、次の語は区切りの語
        self.pending: list[Heredoc] = []

    def add(self, s: str, quoted: bool = False) -> None:
        self.buf.append(s)
        self.has = True
        if quoted:
            self.quoted = True

    def flush(self) -> None:
        if self.has:
            word = "".join(self.buf)
            if self.want is not None:
                heredoc = Heredoc(word, self.quoted, self.want == "<<-")
                self.toks.append(heredoc)
                self.pending.append(heredoc)
                self.want = None
            else:
                self.toks.append(Word(word, self.subs, self.quoted))
        self.buf, self.subs, self.has, self.quoted = [], [], False, False

    def double_quote(self, i: int) -> int:
        text, n = self.text, self.n
        j = i + 1
        while j < n:
            d = text[j]
            if d == "\\":
                nxt = text[j + 1 : j + 2]
                if nxt == "\n":
                    j += 2
                elif nxt in ("$", "`", '"', "\\"):
                    self.buf.append(nxt)
                    j += 2
                else:
                    self.buf.append(d)
                    j += 1
            elif d == '"':
                break
            elif d == "$" and text[j + 1 : j + 2] == "(":
                end = _match_paren(text, j + 1)
                self.subs.append(_inner(text, j + 2, end))
                self.buf.append(text[j:end])
                j = end
            elif d == "`":
                end = _skip_backtick(text, j)
                self.subs.append(_inner(text, j + 1, end))
                self.buf.append(text[j:end])
                j = end
            else:
                self.buf.append(d)
                j += 1
        self.has = self.quoted = True
        return min(j + 1, n)

    def ansi_quote(self, i: int) -> int:
        text, n = self.text, self.n
        j = i + 2
        while j < n and text[j] != "'":
            if text[j] == "\\" and j + 1 < n:
                char, j = _ansi_escape(text, j)
                self.buf.append(char)
            else:
                self.buf.append(text[j])
                j += 1
        self.has = self.quoted = True
        return min(j + 1, n)

    def operator(self, i: int) -> int:
        text = self.text
        # 2>/dev/null の 2 は、ファイル記述子の番号であって語ではない
        if text[i] in "<>" and self.has and not self.quoted and "".join(self.buf).isdigit():
            self.buf, self.subs, self.has = [], [], False
        else:
            self.flush()
        op = next(o for o in OPERATORS if text.startswith(o, i))
        self.toks.append(op)
        self.want = op if op in ("<<", "<<-") else None
        return i + len(op)

    def run(self) -> list[Word | Heredoc | str]:
        text, n = self.text, self.n
        i = 0
        while i < n:
            c = text[i]
            if c in " \t\r":
                self.flush()
                i += 1
            elif c == "\n":
                self.flush()
                self.toks.append("\n")
                self.want = None
                i += 1
                for heredoc in self.pending:
                    i = heredoc.read(text, i)
                self.pending = []
            elif c == "#" and not self.has:  # 語の先頭の # は、行末までコメント
                j = text.find("\n", i)
                i = n if j < 0 else j
            elif c == "\\":
                nxt = text[i + 1 : i + 2]
                if nxt != "\n":  # 行継続（\ + 改行）は無かったことにする
                    self.add(nxt, quoted=True)
                i += 2
            elif c == "'":
                j = text.find("'", i + 1)
                end = n if j < 0 else j
                self.add(text[i + 1 : end], quoted=True)
                i = min(end + 1, n)
            elif c == '"':
                i = self.double_quote(i)
            elif c == "$" and text[i + 1 : i + 2] == "'":
                i = self.ansi_quote(i)
            elif c == "$" and text[i + 1 : i + 2] == '"':
                i += 1
            elif c == "`":
                end = _skip_backtick(text, i)
                self.subs.append(_inner(text, i + 1, end))
                self.add(text[i:end])
                i = end
            elif c in ";&|()<>":
                i = self.operator(i)
            else:
                self.add(c)
                i += 1
        self.flush()
        return self.toks


class Unit:
    """1 つの単純コマンド。words はリダイレクトを除いた語、targets はリダイレクトの対象。"""

    def __init__(self, before: str) -> None:
        self.before = before  # 直前の区切り（"" は先頭）
        self.after = ""  # 直後の区切り
        self.words: list[str] = []
        self.dynamic: list[bool] = []  # words の各語に、実行時に決まる部分（コマンド置換）があるか
        self.subs: list[str] = []
        self.targets: list[str] = []
        self.heredocs: list[Heredoc] = []
        self.herestrings: list[str] = []

    def has_content(self) -> bool:
        return bool(self.words or self.targets or self.heredocs or self.herestrings or self.subs)


def build_units(toks: list[Word | Heredoc | str]) -> list[Unit]:
    units: list[Unit] = []
    cur = Unit("")
    pending: str | None = None  # 直前の演算子が、直後の語を取る種類のとき、その演算子
    for tok in toks:
        if isinstance(tok, str):
            pending = None
            if tok in REDIRECTS or tok in ("<<<", "<<", "<<-"):
                pending = tok
                continue
            if tok == "\n" and not cur.has_content() and cur.before in CONTINUING:
                continue
            if cur.has_content():
                cur.after = tok
                units.append(cur)
            cur = Unit(tok)
        elif isinstance(tok, Heredoc):
            cur.heredocs.append(tok)
            pending = None
        else:
            cur.subs.extend(tok.subs)
            if pending in REDIRECTS:
                cur.targets.append(tok.text)
            elif pending == "<<<":
                cur.herestrings.append(tok.text)
            else:
                cur.words.append(tok.text)
                cur.dynamic.append(bool(tok.subs))
            pending = None
    if cur.has_content():
        units.append(cur)
    return units


def tokenize(text: str) -> list[Word | Heredoc | str]:
    return _Lexer(text).run()


# --- コマンドの特定 ---


def resolve_program(words: list[str]) -> tuple[int | None, list[str], list[str]]:
    """環境変数の代入・予約語・ラッパー（sudo / env / timeout ...）を飛ばして、実際のプログラムの位置を返す。

    ほかに、ラッパーが引数で受け取るコマンド文字列（env -S ...）と、通ったラッパーの名前を返す。
    """
    scripts: list[str] = []
    wrappers: list[str] = []
    n = len(words)
    i = 0
    while i < n:
        word = words[i]
        base = os.path.basename(word)
        if word == "function":  # function name { ...; } の name を飛ばす
            i += 2
            continue
        if ASSIGNMENT.match(word) or word in RESERVED:
            i += 1
            continue
        if base not in WRAPPERS:
            return i, scripts, wrappers
        wrappers.append(base)
        value_flags, positional_count = WRAPPERS[base]
        i += 1
        while i < n:
            w = words[i]
            if w == "--":
                i += 1
                break
            if len(w) < 2 or not w.startswith("-"):
                break
            if w.startswith("--"):
                name, eq, value = w.partition("=")
                if name in value_flags and not eq:
                    value = words[i + 1] if i + 1 < n else ""
                    i += 1
                if base == "env" and name == "--split-string":
                    scripts.append(value)
            else:
                body = w[1:]
                for k, ch in enumerate(body):
                    if "-" + ch not in value_flags:
                        continue
                    glued = body[k + 1 :]
                    if glued:
                        value = glued.lstrip("=")
                    else:
                        value = words[i + 1] if i + 1 < n else ""
                        i += 1
                    if base == "env" and ch == "S":
                        scripts.append(value)
                    break
            i += 1
        skipped = 0
        while skipped < positional_count and i < n and not words[i].startswith("-"):
            i += 1
            skipped += 1
        if base == "flock" and i + 1 < n and words[i] in {"-c", "--command"}:
            scripts.append(words[i + 1])
            return None, scripts, wrappers
    return None, scripts, wrappers


def shell_script(args: list[str]) -> tuple[int | None, bool]:
    """シェルの引数から、-c のスクリプトの位置を取り出す。戻り値は (args の位置または None, 標準入力から読むか)。

    -c は -lc / -ec のように、他のオプションと束ねて書ける。-o / -O は値を取る。
    """
    i, n = 0, len(args)
    has_c = has_s = False
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            i += 2 if a in {"--rcfile", "--init-file"} else 1
            continue
        if a == "-":  # 標準入力から読む
            has_s = True
            i += 1
            continue
        if len(a) > 1 and a[0] in "-+":
            body = a[1:]
            if a[0] == "-":
                has_c = has_c or "c" in body
                has_s = has_s or "s" in body
            i += 2 if body[-1] in "oO" else 1
            continue
        break
    if has_c:
        return (i if i < n else None), False
    return None, (i >= n or has_s)


def c_option_script(args: list[str]) -> int | None:
    """su -c 'cmd' user / script -qc 'cmd' file の、-c のコマンド文字列の位置。"""
    for k, a in enumerate(args):
        is_c = a == "--command" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:])
        if is_c and k + 1 < len(args):
            return k + 1
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


def drop_option_values(args: list[str], shorts: set[str], longs: set[str]) -> list[str]:
    """文章を受け取るオプション（-m / --message など）とその値を除いた引数を返す。"""
    kept: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.startswith("--") and len(arg) > 2:
            name, eq, _ = arg.partition("=")
            if name[2:] in longs:
                skip = not eq
                continue
        elif arg.startswith("-") and len(arg) > 1:
            body = arg[1:]
            hit = next((k for k, ch in enumerate(body) if ch in shorts), None)
            if hit is not None:
                skip = hit == len(body) - 1  # 値が次の引数のとき
                continue
        kept.append(arg)
    return kept


def strip_grep_pattern(args: list[str]) -> list[str]:
    """grep / rg の引数から、検索パターンを除く（パターンはファイルではなく、文章）。"""
    value_short = set("efmABCdDgtTjM")
    value_long = {"file", "max-count", "after-context", "before-context", "context", "directories", "devices",
                  "include", "exclude", "exclude-dir", "glob", "type", "type-not"}
    kept: list[tuple[str, bool]] = []  # (引数, 位置引数か)
    has_pattern_option = False
    skip: str | None = None  # 次の引数の扱い: "keep"（残す値）/ "drop"（パターンなので捨てる）
    for arg in args:
        if skip is not None:
            if skip == "keep":
                kept.append((arg, False))
            skip = None
            continue
        if arg.startswith("--") and len(arg) > 2:
            name, eq, _ = arg.partition("=")
            name = name[2:]
            if name == "regexp":
                has_pattern_option = True
                skip = None if eq else "drop"
                continue
            if name == "file":
                has_pattern_option = True
            if name in value_long and not eq:
                skip = "keep"
            kept.append((arg, False))
        elif arg.startswith("-") and len(arg) > 1:
            body = arg[1:]
            hit = next((k for k, ch in enumerate(body) if ch in value_short), None)
            if hit is None:
                kept.append((arg, False))
                continue
            ch, glued = body[hit], hit < len(body) - 1
            if ch in "ef":
                has_pattern_option = True
            if ch == "e":  # -e の値はパターン（ファイル名ではない）
                skip = None if glued else "drop"
                continue
            kept.append((arg, False))
            skip = None if glued else "keep"
        else:
            kept.append((arg, True))
    result: list[str] = []
    dropped = has_pattern_option
    for arg, is_positional in kept:
        if is_positional and not dropped:
            dropped = True  # 最初の位置引数が、検索パターン
            continue
        result.append(arg)
    return result


# --- gh（GitHub CLI）---
# グループごとの「読み取りだけ」のサブコマンド。ここに無いサブコマンドは確認を強制する
GH_READ = {
    "run": {"list", "view", "watch"},
    "workflow": {"list", "view"},
    "pr": {"list", "view", "status", "diff", "checks"},
    "issue": {"list", "view", "status"},
    "repo": {"view", "list"},
    "release": {"list", "view"},
    "ruleset": {"list", "view", "check"},
    "cache": {"list"},
    "label": {"list"},
    "secret": {"list"},
    "variable": {"list", "get"},
    "gist": {"list", "view"},
    "project": {"list", "view"},
    "org": {"list"},
    "ssh-key": {"list"},
    "gpg-key": {"list"},
    "alias": {"list"},
    "extension": {"list", "search", "browse"},
    "codespace": {"list", "view"},
}
# 読み取りだけの入口（サブコマンドは見ない）
GH_READ_GROUPS = {"status", "version", "help", "browse", "completion", "search"}
# 読み取り以外は拒否するグループ。コードの実行（extension / alias / codespace）、設定・認証情報・秘密の書き換え
GH_DENY_GROUPS = {"extension", "codespace", "alias", "config", "secret", "variable", "ssh-key", "gpg-key"}
# 組み込みの別名
GH_ALIASES = {"cs": "codespace", "ext": "extension", "extensions": "extension", "rs": "ruleset"}
# 削除・アーカイブ系の動詞（item-delete / delete-asset のように、ハイフンでつながったものを含む）
GH_DESTRUCTIVE = re.compile(r"(?:^|-)(?:delete|remove|archive|transfer|destroy)(?:-|$)")
# 3 階層のコマンド（gh repo deploy-key delete など）を持つグループ
GH_NESTED_GROUPS = {"repo", "project", "release", "codespace", "extension"}
# 値を取るフラグ（グループ・サブコマンドの位置を数えるときに、その値を飛ばす）
GH_VALUE_FLAGS = {
    "-R", "--repo", "-q", "--jq", "-t", "--template", "--json", "-L", "--limit", "-b", "--branch", "-w", "--workflow",
    "-e", "--event", "-u", "--user", "-s", "--status", "-c", "--commit", "-j", "--job", "-X", "--method",
    "-H", "--header", "-f", "-F", "--field", "--raw-field", "--input", "--cache", "-p", "--preview",
}
GH_SHORT_VALUE = {f[1] for f in GH_VALUE_FLAGS if len(f) == 2}
# gh api のフラグのうち、値を取るもの
GH_API_SHORT_VALUE = {"X", "f", "F", "H", "q", "t", "p"}
GH_API_LONG_VALUE = {
    "--method", "--field", "--raw-field", "--header", "--input", "--jq", "--template", "--preview", "--cache",
    "--hostname",
}
GH_API_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field"}
# 文章を受け取るオプション（認証情報の置き場の検査から外す）
GH_TEXT_SHORT = {"S", "b"}
GH_TEXT_LONG = {"search", "body", "title", "subject", "notes", "comment"}


def gh_positionals(args: list[str]) -> list[tuple[int, str]]:
    """フラグとその値を除いた語を、(args の位置, 語) で返す。"""
    words: list[tuple[int, str]] = []
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg.startswith("--"):
            if "=" not in arg and arg in GH_VALUE_FLAGS:
                skip = True
        elif arg.startswith("-") and len(arg) > 1:
            body = arg[1:]
            hit = next((k for k, ch in enumerate(body) if ch in GH_SHORT_VALUE), None)
            if hit is not None and hit == len(body) - 1:
                skip = True
        else:
            words.append((index, arg))
    return words


def graphql_code(query: str) -> str:
    """GraphQL の文書から、文字列リテラルとコメントを除く（その中の mutation という語は、書き込みではない）。"""
    query = re.sub(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"', '""', query)
    return re.sub(r"#[^\n]*", "", query)


def classify_gh_api(rest: list[str]) -> Finding | None:
    """gh api: GET（と読み取りの GraphQL）だけ通す。書き込みは確認、削除・任意の URL・HTTP の詳細出力は拒否する。"""
    method: str | None = None
    has_fields = has_input = False
    fields: list[str] = []
    headers: list[str] = []
    endpoint: str | None = None
    n = len(rest)
    i = 0
    while i < n:
        arg = rest[i]
        name: str | None = None
        value = ""
        if arg == "--verbose":
            return Finding("deny", "gh api --verbose（HTTP の詳細出力）は、認証ヘッダを含みうるので使えません。")
        if arg.startswith("--") and len(arg) > 2:
            flag, eq, inline = arg.partition("=")
            if flag in GH_API_LONG_VALUE:
                name = flag
                if eq:
                    value = inline
                else:
                    value = rest[i + 1] if i + 1 < n else ""
                    i += 1
        elif arg.startswith("-") and len(arg) > 1:
            body = arg[1:]  # -i のような真偽値のフラグは、-iXDELETE のように値を取るフラグと束ねて書ける
            for k, ch in enumerate(body):
                if ch not in GH_API_SHORT_VALUE:
                    continue
                name = "-" + ch
                glued = body[k + 1 :]
                if glued:
                    value = glued[1:] if glued.startswith("=") else glued  # -XDELETE / -X=DELETE
                else:
                    value = rest[i + 1] if i + 1 < n else ""
                    i += 1
                break
        elif endpoint is None:
            endpoint = arg
        i += 1

        if name in {"-X", "--method"}:
            method = value.upper()
        elif name in GH_API_FIELD_FLAGS:
            has_fields = True
            fields.append(value)
        elif name in {"-H", "--header"}:
            headers.append(value)
        elif name == "--input":
            has_input = True

    if any(re.search(r"(?i)method-override|x-http-method", h) for h in headers):
        return Finding("ask", "gh api の HTTP メソッドを上書きするヘッダがあります。内容を確認してください。")

    if endpoint is None:
        return None
    if re.match(r"(?i)https?://", endpoint):
        return Finding("deny", "gh api に URL は指定できません（認証つきで任意のホストへ送りうるため）。パスで指定してください。")
    path = endpoint.split("?", 1)[0].strip().removeprefix("./").strip("/").lower()
    if path == "graphql":
        queries = [v.partition("=")[2] for v in fields if v.partition("=")[0] == "query"]
        if any(re.search(r"\bmutation\b", graphql_code(q), re.I) for q in queries):
            return Finding("deny", "GraphQL の mutation（書き込み）は許可していません。必要ならユーザーが実行します。")
        dynamic = any(re.search(r"\$\(|`", q) or re.fullmatch(r"\$\{?\w+\}?", q) for q in queries)
        if has_input or dynamic or any(q.startswith("@") for q in queries):
            return Finding("ask", "GraphQL のクエリを、ファイルやコマンドの出力から読んでいます。内容を確認してください。")
        return None
    if method == "DELETE":
        return Finding("deny", "gh api の DELETE は許可していません。必要ならユーザーが実行します。")
    if method and method != "GET":
        return Finding("ask", f"gh api の書き込み（{method}）です。内容を確認してください。")
    if method is None and (has_fields or has_input):
        return Finding("ask", "gh api にフィールド（-f / -F / --input）があるので、POST になります。内容を確認してください。")
    return None


def classify_gh(args: list[str]) -> Finding | None:
    for arg in args:
        if arg == "--hostname" or arg.startswith("--hostname="):
            return Finding("deny", "gh の --hostname は、トークンを別のホストへ送りうるので使えません。")
    positional = gh_positionals(args)
    words = [w for _, w in positional]
    if not words:
        return None  # gh --version など
    group = GH_ALIASES.get(words[0], words[0])
    sub = words[1] if len(words) > 1 else ""
    if sub == "ls":
        sub = "list"
    if group == "api":
        return classify_gh_api(args[positional[0][0] + 1 :])
    if group == "auth":
        # gh auth status は、引数なし（と --active）だけ通す。-t / --show-token（トークンの表示）、-h（ホストの指定）、
        # --json などは、-at のように他のフラグと束ねて書けるので、許可するものだけを数える
        extra = [a for a in args if a not in {"auth", "status", "-a", "--active"}]
        if sub == "status" and not extra:
            return None
        return Finding(
            "deny", "gh auth の操作（ログイン・トークンの取得・切り替え）は私には許可していません。必要ならユーザーが実行します。"
        )
    if group in GH_READ_GROUPS:
        return None
    scope = words[:2]
    if group in GH_NESTED_GROUPS and sub not in GH_READ.get(group, ()):
        scope = words[:3]
    if any(GH_DESTRUCTIVE.search(w) for w in scope):
        return Finding("deny", f"gh の削除・アーカイブ系の操作（gh {group} {sub}）は許可していません。必要ならユーザーが実行します。")
    if sub in GH_READ.get(group, ()):
        return None
    if group in GH_DENY_GROUPS:
        return Finding(
            "deny", f"gh {group} {sub} は、コードの実行や、設定・認証情報・秘密の書き換えにつながるため許可していません。"
        )
    return Finding("ask", f"gh の書き込み系、または未知の操作の可能性があります。内容を確認してください: gh {group} {sub}".rstrip())


def git_subcommand(args: list[str]) -> str | None:
    """git のグローバルオプション（-C / -c ...）を飛ばして、最初のサブコマンドを返す。"""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg
    return None


def classify_cli(cli: str, args: list[str]) -> Finding | None:
    for arg in args:
        name = arg.split("=", 1)[0]
        if name in IDENTITY_FLAGS:
            return Finding("deny", f"ID を切り替えるフラグ（{name}）は使えません。日常の ID は閲覧専用に固定しています。")
    if "--help" in args:
        return None  # ヘルプの表示は、何も実行しない
    if cli == "gh":
        return classify_gh(args)
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


# --- 解析 ---


def text_arguments(args: list[str]) -> list[str]:
    """echo / printf の引数のうち、先頭のオプション（-e / -n）を除いた文章。"""
    k = 0
    while k < len(args) and re.fullmatch(r"-[neE]+", args[k]):
        k += 1
    return args[k:]


def produced_texts(unit: Unit) -> list[str]:
    """このコマンドが標準出力へ流す文章（echo / printf の引数、ヒアドキュメント、ヒアストリング）。"""
    texts = [h.body for h in unit.heredocs] + list(unit.herestrings)
    index, _, _ = resolve_program(unit.words)
    if index is not None and os.path.basename(unit.words[index]) in TEXT_ONLY:
        args = text_arguments(unit.words[index + 1 :])
        texts += args
        if len(args) > 1:
            texts.append(" ".join(args))
    return [t.replace("\\n", "\n") for t in texts]


def normalized_path(word: str) -> str | None:
    """絶対パス・~・$HOME で始まる語を、展開して正規化する（// ・ /./ ・ .. を畳む）。パスでなければ None。"""
    w = word
    for ref in ("${HOME}", "$HOME"):
        if w.startswith(ref):
            w = "~" + w[len(ref):]
    if not (w.startswith("/") or w.startswith("~")):
        return None
    return posixpath.normpath(os.path.expanduser(w))


def touches_protected_path(word: str) -> bool:
    """語が、認証情報のディレクトリ・ファイルを指すか（~ / $HOME の展開、パスの正規化、グロブも照合）。"""
    w = normalized_path(word)
    if w is None:
        return False
    if any(ch in w for ch in "*?["):
        return any(fnmatch.fnmatchcase(path, w) for path in PROTECTED_PATHS)
    return any(w == d or w.startswith(d + "/") for d in PROTECTED_DIRS)


def covers_protected_dirs(program: str, word: str) -> bool:
    """コピー・検索・移動の対象が、認証情報のディレクトリを含む上位のディレクトリ（~/.config など）か。"""
    w = normalized_path(word)
    if w is None:
        return False
    config_dirs = {h + "/.config" for h in HOME_DIRS}
    if program in {"cd", "pushd"}:  # cd ~ は普通に使う。~/.config から相対パスで開くのを防ぐ
        return w in config_dirs
    return w in config_dirs | HOME_DIRS | {"/", "/home"}


def credential_words(program: str, args: list[str]) -> list[str]:
    """認証情報の置き場の検査にかける引数。文章（検索パターン・コミットメッセージ）は除く。"""
    if program in TEXT_ONLY:
        return []
    if program in GREP_PROGRAMS:
        return strip_grep_pattern(args)
    if program == "git":
        sub = git_subcommand(args)
        kept = drop_option_values(args, {"m", "S", "G"}, {"message", "grep"})
        if sub == "grep" and sub in kept:
            kept.remove(sub)
            return [sub] + strip_grep_pattern(kept)
        return kept
    if program == "gh":
        if "search" in args:
            return []
        return drop_option_values(args, GH_TEXT_SHORT, GH_TEXT_LONG)
    return args


def analyze_unit(unit: Unit, units: list[Unit], idx: int, text: str, depth: int, findings: list[Finding]) -> None:
    def sub(script: str) -> None:
        if depth < MAX_DEPTH:
            findings.extend(analyze(script, depth + 1))
        elif CLI_WORD.search(script):
            findings.append(Finding("ask", "コマンドの入れ子が深く、検査できません。内容を確認してください。"))

    for script in unit.subs:
        sub(script)
    for heredoc in unit.heredocs:
        if not heredoc.quoted:  # 引用符なしの区切りでは、本文の $(...) とバッククォートが実行される
            for script in substitutions_in(heredoc.body):
                sub(script)
    if any(CREDENTIAL_TEXT.search(t) or touches_protected_path(t) for t in unit.targets):
        findings.append(CREDENTIAL_DENY)

    words = unit.words
    index, scripts, wrappers = resolve_program(words)
    for script in scripts:
        sub(script)
    prefix = words if index is None else words[:index]
    for word in prefix:
        m = ASSIGNMENT.match(word)
        if m and IDENTITY_ENV.match(m.group(1)):
            findings.append(Finding("deny", f"認証・ID を変える環境変数（{m.group(1)}）は設定できません。"))
    if index is None:
        if CREDENTIAL_TEXT.search(" ".join(prefix)):
            findings.append(CREDENTIAL_DENY)
        return

    program_word = words[index]
    program = os.path.basename(program_word)
    args = words[index + 1 :]
    dynamic = unit.dynamic[index + 1 :] if len(unit.dynamic) == len(words) else [False] * len(args)

    if re.search(r"[\s;&|]", program_word):  # watch 'gh run list' のように、コマンドが 1 つの文字列で渡された
        sub(" ".join(words[index:]))
        return

    def runs_dynamic_text(position: int) -> bool:
        """実行する文字列が、コマンド置換や変数で実行時に決まるか（検査できないので、確認にする）。"""
        return dynamic[position] or bool(re.fullmatch(r"\$\{?\w+\}?", args[position]))

    ask_dynamic = Finding("ask", "実行する文字列が実行時に決まります（コマンド置換・変数）。内容を確認してください。")

    if program in ASSIGNING_BUILTINS:
        for word in args:
            m = ASSIGNMENT.match(word)
            if m and IDENTITY_ENV.match(m.group(1)):
                findings.append(Finding("deny", f"認証・ID を変える環境変数（{m.group(1)}）は設定できません。"))

    reads_stdin_script = False
    inner_scripts: list[str] = []  # 再帰で調べるので、認証情報の置き場の検査からは外す
    if program in CLIS:
        for word in prefix:
            m = ASSIGNMENT.match(word)
            if m and (LAUNCHER_ENV.match(m.group(1)) or ROUTING_ENV.match(m.group(1))):
                findings.append(
                    Finding(
                        "deny",
                        f"外部コマンドの起動や通信の経路を変えられる環境変数（{m.group(1)}）は、{program} には付けられません。",
                    )
                )
        finding = classify_cli(program, args)
        if finding:
            findings.append(finding)
        if "xargs" in wrappers:  # echo 'auth token' | xargs gh のように、標準入力の語がこの CLI の引数になる
            j = idx
            while j > 0 and units[j].before in PIPES:
                j -= 1
                for produced in produced_texts(units[j])[-1:]:
                    piped = classify_cli(program, args + produced.split())
                    if piped:
                        findings.append(piped)
    elif program in SHELLS:
        position, reads_stdin_script = shell_script(args)
        if position is not None:
            if runs_dynamic_text(position):
                findings.append(ask_dynamic)
            sub(args[position])
            inner_scripts.append(args[position])
    elif program in C_ONLY_SHELLS:
        position = c_option_script(args)
        if position is not None:
            if runs_dynamic_text(position):
                findings.append(ask_dynamic)
            sub(args[position])
            inner_scripts.append(args[position])
    elif program == "eval":
        if any(runs_dynamic_text(k) for k in range(len(args))):
            findings.append(ask_dynamic)
        sub(" ".join(args))
        inner_scripts += args
    elif program in SCHEDULERS:  # at now <<< 'gh run delete 1'
        for payload in [h.body for h in unit.heredocs] + unit.herestrings:
            sub(payload)
    elif program in RUNNERS:
        if any(CLI_WORD.search(w) for w in args):
            findings.append(Finding("ask", "タスクやスクリプトの中でクラウド CLI（gcloud / gh など）を呼ぶ可能性があります。内容を確認してください。"))
    elif program == "find":
        k = 0
        while k < len(args):
            if args[k] in {"-exec", "-execdir", "-ok", "-okdir"}:
                end = k + 1
                while end < len(args) and args[end] not in {";", "+"}:
                    end += 1
                inner = Unit("")
                inner.words = args[k + 1 : end]
                if depth < MAX_DEPTH:
                    analyze_unit(inner, [inner], 0, text, depth + 1, findings)
                k = end
            k += 1
    elif INTERPRETER.match(program):
        fed = [h.body for h in unit.heredocs] + unit.herestrings
        if any(CLI_WORD.search(w) for w in args) or any(CLI_WORD.search(t) for t in fed):
            findings.append(Finding("ask", "スクリプトの中でクラウド CLI（gcloud / gh など）を呼んでいます。内容を確認してください。"))

    # シェルへ渡される文章（ヒアドキュメント・ヒアストリング・パイプ・プロセス置換）も調べる
    if program in SHELLS or program in SOURCERS:
        analyzed = False
        if unit.after == "<(" and idx + 1 < len(units) and units[idx + 1].before == "<(":
            for produced in produced_texts(units[idx + 1]):
                sub(produced)
            analyzed = True
        if reads_stdin_script:
            for heredoc in unit.heredocs:
                sub(heredoc.body)
                analyzed = True
            for string in unit.herestrings:
                sub(string)
                analyzed = True
            j = idx
            while j > 0 and units[j].before in PIPES:
                j -= 1
                texts = produced_texts(units[j])
                for produced in texts:
                    sub(produced)
                analyzed = analyzed or bool(texts)
            if not analyzed and CLI_WORD.search(text):
                findings.append(
                    Finding(
                        "ask",
                        "標準入力から読んだスクリプトの中でクラウド CLI（gcloud / gh など）を呼ぶ可能性があります。"
                        "内容を確認してください。",
                    )
                )

    if program == "git":
        sub_command = git_subcommand(args)
        if sub_command and (sub_command == "credential" or sub_command.startswith("credential-")):
            findings.append(GIT_CREDENTIAL_DENY)
        if any(re.match(r"alias\.[^=]*=.*credential", a) for a in args):  # git -c alias.c=credential c fill
            findings.append(GIT_CREDENTIAL_DENY)
    elif program.startswith("git-credential"):
        findings.append(GIT_CREDENTIAL_DENY)

    scanned = prefix + [program_word] + credential_words(program, [a for a in args if a not in inner_scripts])
    if CREDENTIAL_TEXT.search(" ".join(scanned)) or any(touches_protected_path(w) for w in scanned):
        findings.append(CREDENTIAL_DENY)
    elif program in PARENT_SENSITIVE and any(covers_protected_dirs(program, w) for w in scanned[len(prefix) + 1 :]):
        findings.append(CREDENTIAL_DENY)


def analyze(text: str, depth: int = 0) -> list[Finding]:
    findings: list[Finding] = []
    units = build_units(tokenize(text))
    for idx, unit in enumerate(units):
        analyze_unit(unit, units, idx, text, depth, findings)
    return findings


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
        if not isinstance(tool_input, dict) or payload.get("tool_name") not in {"Bash", "Monitor"}:
            return 0
        command = tool_input.get("command") or ""
        if not isinstance(command, str):
            raise TypeError("command is not a string")
        findings = analyze(command)
    except Exception:  # noqa: BLE001  入力が読めない・検査に失敗したときは、通さない
        print("ガード: フックの入力を検査できませんでした。実行を止めます。", file=sys.stderr)
        return 2
    if not findings:
        return 0

    denies = [f.reason for f in findings if f.level == "deny"]
    if denies:
        print("ガード（クラウド CLI・gh）: 実行を止めました。\n- " + "\n- ".join(dict.fromkeys(denies)), file=sys.stderr)
        return 2
    asks = list(dict.fromkeys(f.reason for f in findings))
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": "ガード（クラウド CLI・gh）: " + " / ".join(asks),
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
