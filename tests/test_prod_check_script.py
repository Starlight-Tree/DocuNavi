"""scripts/prod_check.sh（本番の確認をテスト専用ユーザーで 1 コマンドにまとめる）。引数の検査は、何かを実行する前に終わること。"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prod_check.sh"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PREVIEW_DRY_RUN": "1"}
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def test_bad_arguments_are_refused_before_anything_runs():
    for args in ((), ("--nope",), ("--chat", "extra"), ("--stream", "--question"), ("--screen", "--question", "")):
        result = run(*args)
        assert result.returncode == 2, args
        # 書き込みの表示（DRY_RUN では「+ …」）も、計画の表示も出ていない
        assert "+ " not in result.stdout, args
        assert "=== 本番の確認" not in result.stdout, args


def test_help_lists_the_three_modes_and_the_host_only_rule():
    result = run("--help")
    assert result.returncode == 0
    for word in ("--chat", "--stream", "--screen", "ホストのターミナル", "trap"):
        assert word in result.stdout, word


def test_cleanup_always_removes_the_test_login_and_the_membership():
    text = SCRIPT.read_text(encoding="utf-8")
    # 失敗・中断でも、資格とテスト用ログインを戻す（trap）。テスト用ログインを有効にする前に、trap を置く
    assert "trap cleanup EXIT" in text
    assert text.index("trap cleanup EXIT") < text.index("secret put")
    assert "secret delete" in text and "remove --email" in text
    # Infisical の値は表示しない（標準入力で wrangler に渡す）
    assert 'echo "$TEST_LOGIN' not in text
