"""scripts/check_docs.py（Markdown の死んだ参照の検査）のテスト。

本物のリポジトリは読まず、tmp_path に作った小さな偽のリポジトリだけを検査する。
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_docs.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_docs = load("check_docs", SCRIPT)


def make(root: Path, files: dict[str, str]) -> Path:
    """files（相対パス → 内容）を root に作る。"""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def problems(root: Path, files: list[str] | None = None) -> list[str]:
    paths = None if files is None else [root / f for f in files]
    return check_docs.check_files(root, paths)


def test_clean_repo_has_no_problems(tmp_path: Path):
    make(
        tmp_path,
        {
            "README.md": "# T\n\n[docs](docs/a.md) と `scripts/x.sh` と `docs/` を参照する。\n",
            "docs/a.md": "# A\n\n## 設計\n\n[戻る](../README.md)\n",
            "scripts/x.sh": "#!/bin/sh\n",
        },
    )
    assert problems(tmp_path) == []


# --- 1. 相対リンク ---


def test_dead_relative_link_is_reported_with_file_and_line(tmp_path: Path):
    make(tmp_path, {"docs/a.md": "# A\n\n[消えた](missing.md)\n"})
    assert problems(tmp_path) == ["docs/a.md:3: リンク先が存在しません: missing.md"]


def test_existing_link_with_fragment_and_query_is_ok(tmp_path: Path):
    make(tmp_path, {"docs/a.md": "[b](b.md#no-such-anchor) [c](b.md?x=1)\n", "docs/b.md": "# B\n"})
    assert problems(tmp_path) == []


def test_external_and_anchor_links_are_skipped(tmp_path: Path):
    text = "[a](https://example.com/x) [b](http://example.com) [c](mailto:a@example.com) [d](#top)\n"
    make(tmp_path, {"docs/a.md": text})
    assert problems(tmp_path) == []


def test_link_inside_inline_code_is_not_a_link(tmp_path: Path):
    make(tmp_path, {"docs/a.md": "書き方の例: `[text](missing.md)`\n"})
    assert problems(tmp_path) == []


def test_link_path_is_normalized_in_messages(tmp_path: Path):
    make(tmp_path, {"README.md": "# T\n", "docs/a.md": "[x](../README.md) の「無い見出し」\n"})
    assert problems(tmp_path) == ["docs/a.md:1: README.md に見出し「無い見出し」がありません"]


# --- 2. バッククォートのパス ---


def test_dead_backticked_path_with_line_suffix_is_reported(tmp_path: Path):
    make(tmp_path, {"docs/a.md": "# A\n見る: `app/gone.py:123` と `app/gone2.py:12-34`\n", "app/real.py": ""})
    assert problems(tmp_path) == [
        "docs/a.md:2: パスが存在しません: app/gone.py",
        "docs/a.md:2: パスが存在しません: app/gone2.py",
    ]


def test_existing_backticked_paths_are_ok(tmp_path: Path):
    text = "`app/real.py` `app/real.py:10` `app/real.py:10-20` `app/real.py::test_x` `app/sub/` `.claude/rules/r.md`\n"
    make(tmp_path, {"docs/a.md": text, "app/real.py": "", "app/sub/m.py": "", ".claude/rules/r.md": ""})
    assert problems(tmp_path) == []


def test_dead_backticked_directory_is_reported(tmp_path: Path):
    make(tmp_path, {"docs/a.md": "`ops/gone/`\n"})
    assert problems(tmp_path) == ["docs/a.md:1: パスが存在しません: ops/gone/"]


@pytest.mark.parametrize(
    "span",
    [
        "app/*.py",
        "docs/<name>.md",
        "docs/decisions/NNNN-title.md",
        "app/{a,b}.py",
        "app/$HOME.py",
        "app/…/x.py",
        "app/x y.py",
        "pytest tests/gone.py",  # 空白を含む（コマンド）
        "app/search",  # 拡張子も末尾の `/` も無い
        "other/gone.py",  # 先頭が既知のディレクトリーではない
        "~/.claude/gone.md",
        "https://example.com/docs/gone.md",
    ],
)
def test_placeholders_and_non_paths_are_skipped(tmp_path: Path, span: str):
    make(tmp_path, {"docs/a.md": f"`{span}`\n", "app/real.py": ""})
    assert problems(tmp_path) == []


def test_path_relative_to_the_file_directory_is_accepted(tmp_path: Path):
    make(tmp_path, {"frontend/CLAUDE.md": "`app/page.tsx` と `app/gone.tsx`\n", "frontend/app/page.tsx": ""})
    assert problems(tmp_path) == ["frontend/CLAUDE.md:1: パスが存在しません: app/gone.tsx"]


def test_git_ignored_local_file_is_not_reported(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    make(tmp_path, {".gitignore": ".dev.vars\n", "docs/a.md": "`workers/api/.dev.vars` と `workers/api/gone.ts`\n"})
    assert problems(tmp_path, ["docs/a.md"]) == ["docs/a.md:1: パスが存在しません: workers/api/gone.ts"]


# --- 3. 節の参照 ---

SECTIONS = {"docs/b.md": "# B\n\n## 資料の検索\n\n### 上位件数（`TOP_K`）の現状\n\n- **監視設定:** 4 つ\n"}


def test_section_reference_found_by_path_link_and_bare_name(tmp_path: Path):
    text = "`docs/b.md` の「資料の検索」と [b.md](b.md) の「上位件数（TOP_K）」と `b.md` の「監視設定」\n"
    make(tmp_path, {**SECTIONS, "docs/a.md": text})
    assert problems(tmp_path) == []


def test_section_reference_not_found_is_reported(tmp_path: Path):
    text = "# A\n\n`docs/b.md` の「消えた節」と [b.md](b.md#x) の「これも無い」と `b.md` の「3 つ目」\n"
    make(tmp_path, {**SECTIONS, "docs/a.md": text})
    assert problems(tmp_path) == [
        "docs/a.md:3: docs/b.md に見出し「消えた節」がありません",
        "docs/a.md:3: docs/b.md に見出し「これも無い」がありません",
        "docs/a.md:3: docs/b.md に見出し「3 つ目」がありません",
    ]


def test_heading_text_only_in_body_or_fence_does_not_count(tmp_path: Path):
    make(
        tmp_path,
        {
            "docs/b.md": "# B\n\n本文に「本文だけ」と書く。\n\n```\n## 囲いの中\n```\n",
            "docs/a.md": "`docs/b.md` の「本文だけ」と `docs/b.md` の「囲いの中」\n",
        },
    )
    assert len(problems(tmp_path)) == 2


@pytest.mark.parametrize("tail", ["の「無い節」など", "の「…」", "の「無い…節」"])
def test_section_reference_with_etc_or_ellipsis_is_skipped(tmp_path: Path, tail: str):
    quote = tail.removesuffix("など")
    suffix = "など" if tail.endswith("など") else ""
    make(tmp_path, {**SECTIONS, "docs/a.md": f"`docs/b.md` {quote}{suffix}\n"})
    assert problems(tmp_path) == []


def test_ambiguous_or_unknown_bare_file_name_is_skipped(tmp_path: Path):
    make(
        tmp_path,
        {
            "docs/CLAUDE.md": "# 見出し\n",
            "app/CLAUDE.md": "# 別の見出し\n",
            "docs/a.md": "`CLAUDE.md` の「どこにも無い」と `nowhere.md` の「無い」と `~/.claude/CLAUDE.md` の「無い」\n",
        },
    )
    assert problems(tmp_path) == []


def test_section_reference_to_a_missing_file_is_reported_once_by_the_link_check(tmp_path: Path):
    make(tmp_path, {"docs/a.md": "[x](gone.md) の「節」\n"})
    assert problems(tmp_path) == ["docs/a.md:1: リンク先が存在しません: gone.md"]


# --- 4. 例外 ---


def test_ignore_marker_skips_the_whole_line(tmp_path: Path):
    text = "[x](gone.md) `app/gone.py` `docs/b.md` の「無い」 <!-- check-docs: ignore -->\n[y](gone2.md)\n"
    make(tmp_path, {**SECTIONS, "docs/a.md": text})
    assert problems(tmp_path) == ["docs/a.md:2: リンク先が存在しません: gone2.md"]


def test_ignore_targets_skips_listed_references(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(check_docs, "IGNORE_TARGETS", {"ops/old/", "gone.md"})
    make(tmp_path, {"docs/a.md": "`ops/old/` と [x](gone.md) と `ops/other/`\n"})
    assert problems(tmp_path) == ["docs/a.md:1: パスが存在しません: ops/other/"]


def test_fenced_blocks_are_skipped(tmp_path: Path):
    text = (
        "```\n`app/gone.py` [x](gone.md) `docs/b.md` の「無い」\n```\n\n~~~md\n`app/gone2.py`\n~~~\n\n`app/gone3.py`\n"
    )
    make(tmp_path, {**SECTIONS, "docs/a.md": text})
    assert problems(tmp_path) == ["docs/a.md:9: パスが存在しません: app/gone3.py"]


def test_longer_fence_is_closed_only_by_a_matching_fence(tmp_path: Path):
    text = "````\n```\n`app/gone.py`\n```\n````\n`app/gone2.py`\n"
    make(tmp_path, {"docs/a.md": text})
    assert problems(tmp_path) == ["docs/a.md:6: パスが存在しません: app/gone2.py"]


# --- 既定の対象 ---


def test_default_files_cover_the_expected_places_and_skip_vendor_dirs(tmp_path: Path):
    wanted = [
        "README.md",
        "CLAUDE.md",
        "docs/a.md",
        "docs/decisions/0001-x.md",
        ".claude/rules/r.md",
        ".claude/skills/s/SKILL.md",
        ".devcontainer/README.md",
        "app/CLAUDE.md",
        "ops/sse-relay/CLAUDE.md",
        "frontend/README.md",
    ]
    skipped = [
        "app/deep/er/CLAUDE.md",  # 深さ 3
        "app/other.md",  # CLAUDE.md / README.md 以外
        "frontend/node_modules/pkg/README.md",
        "frontend/node_modules/README.md",
        ".venv/lib/README.md",
        ".venv-django/README.md",
        "evals/corpora/cafe/README.md",
        "storage/README.md",
        "frontend/.next-e2e/README.md",
        "scripts/README.md",  # 対象の領域ではない
    ]
    make(tmp_path, {name: "# x\n" for name in wanted + skipped})
    found = [p.relative_to(tmp_path).as_posix() for p in check_docs.default_files(tmp_path)]
    assert sorted(found) == sorted(wanted)


# --- コマンドライン ---


def test_main_exits_zero_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    make(tmp_path, {"docs/a.md": "# A\n", "README.md": "[a](docs/a.md)\n"})
    assert check_docs.main([], root=tmp_path) == 0
    assert capsys.readouterr().out == ""


def test_main_exits_one_and_prints_one_line_per_problem(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    make(tmp_path, {"docs/a.md": "# A\n[x](gone.md)\n`app/gone.py`\n"})
    assert check_docs.main([], root=tmp_path) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "docs/a.md:2: リンク先が存在しません: gone.md",
        "docs/a.md:3: パスが存在しません: app/gone.py",
    ]
    assert "2 件" in captured.err


def test_main_with_explicit_files_checks_only_those(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    make(tmp_path, {"docs/bad.md": "[x](gone.md)\n", "docs/good.md": "# G\n"})
    assert check_docs.main([str(tmp_path / "docs/good.md")], root=tmp_path) == 0
    assert check_docs.main([str(tmp_path / "docs/bad.md")], root=tmp_path) == 1
    assert capsys.readouterr().out == "docs/bad.md:1: リンク先が存在しません: gone.md\n"


def test_main_ignores_non_markdown_arguments_and_reports_missing_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    make(tmp_path, {"app/x.py": "# [x](gone.md)\n"})
    assert check_docs.main([str(tmp_path / "app/x.py")], root=tmp_path) == 0
    assert check_docs.main([str(tmp_path / "docs/none.md")], root=tmp_path) == 1
    assert capsys.readouterr().out == "docs/none.md:0: ファイルが存在しません\n"
