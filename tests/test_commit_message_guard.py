"""コミットの件名の検査（scripts/check_commit_messages.py）と、それを使うフックの判定のテスト。

git の実行は、一時のリポジトリだけ（このリポジトリの履歴・実際のコミットには触れない）。
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_commit_messages.py"
HOOK = ROOT / ".claude" / "hooks" / "guard-git-write.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load("check_commit_messages", CHECKER)
guard = load("guard_git_write", HOOK)


def no_diff(*_args: str, **_kwargs: str) -> str:
    return ""


@pytest.mark.parametrize(
    "subject",
    [
        "feat(frontend): add light/dark theme toggle",
        "fix: keep the topic when the follow-up rewrite falls back",
        "docs(claude-md): raise the cleanup threshold to 200 lines",
        "refactor(sse-relay)!: drop the JWT sub reading",
        "chore(devcontainer): update host paths after home rename",
        "Merge pull request #7 from Starlight-Tree/dependabot/x",
        'Revert "feat: add x"',
        "fixup! feat: add x",
        "a" * 0 + "ci: " + "x" * 68,  # ちょうど 72 文字
    ],
)
def test_valid_subjects(subject: str) -> None:
    assert checker.check_subject(subject) == []


@pytest.mark.parametrize(
    ("subject", "fragment"),
    [
        ("feat: 開発コンテナの起動時にサーバーを自動で起こす", "英語"),
        ("Exclude hero image prefix from R2 collections", "type(scope)"),
        ("feature(frontend): add x", "type(scope)"),
        ("feat(Frontend): add x", "type(scope)"),
        ("feat:add x", "type(scope)"),
        ("feat: Add x", "小文字"),
        ("feat: add x.", "ピリオド"),
        ("ci: " + "x" * 69, "72 文字"),
    ],
)
def test_invalid_subjects(subject: str, fragment: str) -> None:
    problems = checker.check_subject(subject)
    assert problems and any(fragment in p for p in problems)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('git commit -m "feat: add x"', "feat: add x"),
        ("git commit -m 'fix: y' -m 'body'", "fix: y"),
        ('git commit -am "docs: z"', "docs: z"),
        ('git commit --message="chore: w"', "chore: w"),
        ('git add a && git commit -m "test: v" && git push', "test: v"),
        ('git -C /tmp commit -m "build: u"', "build: u"),
        ("git commit -m \"$(cat <<'EOF'\nfeat: heredoc subject\n\nbody\nEOF\n)\"", "feat: heredoc subject"),
        ("git commit -F - <<'EOF'\nfix: from stdin\n\nbody\nEOF", "fix: from stdin"),
        ("git commit --amend", None),
        ("git commit -F msg.txt", None),
        ("git log --grep commit", None),
    ],
)
def test_commit_subject_extraction(command: str, expected: str | None) -> None:
    assert guard.commit_subject(command) == expected


def test_hook_denies_bad_subjects_and_allows_good_ones() -> None:
    bad = guard.decide('git commit -m "feat: 日本語の件名"', diff_for=no_diff)
    assert bad and bad[0] == "deny" and "規約" in bad[1]
    assert guard.decide('git commit -m "Update things"', diff_for=no_diff)[0] == "deny"
    assert guard.decide('git commit -m "feat(scope): add a thing"', diff_for=no_diff) is None
    # 件名が取れない形は判断しない
    assert guard.decide("git commit --amend --no-edit", diff_for=no_diff) is None
    assert guard.decide("git status", diff_for=no_diff) is None


def run_git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd),
    }  # fmt: skip
    out = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_range_checks_only_new_commits_and_skips_bots(tmp_path: Path) -> None:
    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "古い件名（検査しない）")
    base = run_git(tmp_path, "rev-parse", "HEAD")
    run_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "feat: good one")
    run_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "Bad One")
    run_git(tmp_path, "commit", "-q", "--allow-empty", "--author=dependabot[bot] <d@example.com>", "-m", "Bump x")
    head = run_git(tmp_path, "rev-parse", "HEAD")

    def run(spec: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(CHECKER), "--range", spec], cwd=tmp_path, capture_output=True, text=True
        )

    result = run(f"{base}..{head}")
    assert result.returncode == 1
    assert "Bad One" in result.stdout and "good one" not in result.stdout and "Bump x" not in result.stdout
    assert "検査したコミット: 2 件、違反: 1 件" in result.stdout
    # 起点が無い（強制プッシュの直後など）ときは、終点の 1 件だけを見る
    fallback = run(f"{'0' * 40}..{head}")
    assert "検査したコミット" in fallback.stdout
    ok = run(f"{base}..{run_git(tmp_path, 'rev-parse', 'HEAD~2')}")
    assert ok.returncode == 0


# ---- Git の commit-msg フック（.githooks/commit-msg）。どの経路でも、メッセージのファイルを読む ----

GITHOOKS = ROOT / ".githooks"
LONG_SUBJECT = "feat(documents): " + "x" * 80


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(GITHOOKS)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    return repo


def _commit(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # pre-commit（固定語の検査）は、検査語が無いと警告だけで通る。ここで見るのは commit-msg だけ
    return subprocess.run(["git", "-C", str(repo), "commit", "-q", *args], capture_output=True, text=True, check=False)


def test_commit_msg_hook_accepts_a_good_subject(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _commit(repo, "-m", "feat: add a thing").returncode == 0


@pytest.mark.parametrize("via", ["-m", "-F"])
def test_commit_msg_hook_rejects_a_long_subject(tmp_path: Path, via: str) -> None:
    repo = _repo(tmp_path)
    if via == "-F":
        message = tmp_path / "msg.txt"
        message.write_text(LONG_SUBJECT + "\n\nbody\n")
        result = _commit(repo, "-F", str(message))
    else:
        result = _commit(repo, "-m", LONG_SUBJECT)
    assert result.returncode != 0
    assert "72" in result.stdout + result.stderr
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True).returncode != 0


def test_commit_msg_hook_rejects_a_bad_amend(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _commit(repo, "-m", "feat: add a thing").returncode == 0
    assert _commit(repo, "--amend", "-m", LONG_SUBJECT).returncode != 0


def test_commit_msg_hook_rejects_an_unreadable_message(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(GITHOOKS / "commit-msg"), str(tmp_path / "missing")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "読み取れません" in result.stdout


def test_message_file_skips_comment_lines(tmp_path: Path) -> None:
    message = tmp_path / "msg.txt"
    message.write_text("\n# comment\nfix: real subject\n\nbody\n")
    assert checker.subject_from_message_file(str(message)) == "fix: real subject"
    message.write_text("# only a comment\n")
    assert checker.subject_from_message_file(str(message)) is None
