"""scripts/check-sensitive-terms.sh のテスト。

実際の検索語（.git/sensitive-terms）には触れず、使い捨ての Git リポジトリで動かす。
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-sensitive-terms.sh"
TERM = "zz-fake-term-123"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def run(repo: Path, *args: str, terms: str | None = TERM) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "SENSITIVE_TERMS"}
    if terms is not None:
        env["SENSITIVE_TERMS"] = terms
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=repo, env=env, capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    git(tmp_path, "add", "a.txt")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_staged_hit_reports_file_and_line_but_not_the_term(repo: Path):
    (repo / "a.txt").write_text(f"one\ntwo\nid = {TERM}\n")
    git(repo, "add", "a.txt")

    result = run(repo)

    assert result.returncode == 1
    assert "a.txt:3:" in result.stdout
    assert TERM not in result.stdout + result.stderr


def test_staged_clean_passes(repo: Path):
    (repo / "a.txt").write_text("one\ntwo\nthree\n")
    git(repo, "add", "a.txt")

    assert run(repo).returncode == 0


def test_removed_lines_are_not_hits(repo: Path):
    (repo / "a.txt").write_text(f"one\ntwo\n{TERM}\n")
    git(repo, "commit", "-q", "-am", "add")
    (repo / "a.txt").write_text("one\ntwo\n")
    git(repo, "add", "a.txt")

    assert run(repo).returncode == 0


def test_terms_file_ignores_comments_blank_lines_and_case(repo: Path):
    (repo / ".git" / "sensitive-terms").write_text(f"# note\r\n\r\n  {TERM.upper()}  # inline\r\n")
    (repo / "a.txt").write_text(f"one\ntwo\n{TERM}\n")
    git(repo, "add", "a.txt")

    result = run(repo, terms=None)

    assert result.returncode == 1
    assert "a.txt:3:" in result.stdout


def test_range_catches_a_term_added_and_removed_in_later_commit(repo: Path):
    git(repo, "tag", "base")
    (repo / "a.txt").write_text(f"one\ntwo\n{TERM}\n")
    git(repo, "commit", "-q", "-am", "add")
    (repo / "a.txt").write_text("one\ntwo\n")
    git(repo, "commit", "-q", "-am", "remove")

    assert run(repo, "base..HEAD").returncode == 1
    assert run(repo, "HEAD~2..HEAD~1").returncode == 1
    assert run(repo, "HEAD~1..HEAD").returncode == 0


def test_history_and_tracked_modes(repo: Path):
    (repo / "b.txt").write_text(f"x\n{TERM}\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "add b")

    history = run(repo, "--history")
    tracked = run(repo, "--tracked")

    assert history.returncode == 1 and "b.txt:2:" in history.stdout
    assert tracked.returncode == 1 and "b.txt:2:" in tracked.stdout
    assert TERM not in history.stdout + tracked.stdout


def test_no_terms_warns_and_passes(repo: Path):
    result = run(repo, terms=None)

    assert result.returncode == 0
    assert "何も検査していません" in result.stderr


def test_bad_flag_and_bad_range_fail(repo: Path):
    assert run(repo, "--nope").returncode == 2
    assert run(repo, "no-such-ref..HEAD").returncode != 0
