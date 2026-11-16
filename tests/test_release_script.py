"""scripts/release.sh（タグ push 配備のあとの切り替えの一本化）。引数の検査は、何かを実行する前に終わること（gcloud を呼ばない）。"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release.sh"
GOOD_ID = "4820224e-4976-4444-85ed-f6710c994736"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PREVIEW_DRY_RUN": "1"}
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def test_bad_arguments_are_refused_before_anything_runs():
    for args in ((), ("not-a-build-id",), (GOOD_ID, "extra"), (GOOD_ID, "--nope"), ("../etc/passwd",)):
        result = run(*args)
        assert result.returncode == 2, args
        # 書き込みのコマンド（DRY_RUN では「+ gcloud …」と表示される）も、読み取りの結果（ビルドの状態）も出ていない
        assert "+ gcloud" not in result.stdout, args
        assert "ビルド:" not in result.stdout, args


def test_help_explains_the_single_confirmation_and_the_host_only_rule():
    result = run("--help")
    assert result.returncode == 0
    assert "ホストのターミナル" in result.stdout
    assert "1 回だけ" in result.stdout


def test_script_never_moves_traffic_without_the_confirmation_and_delegates_the_check():
    text = SCRIPT.read_text(encoding="utf-8")
    # 切り替え（preview.sh promote）は、確認のあとにだけ置く
    assert text.index('confirm "切り替えてよいですか?"') < text.index('preview.sh" promote')
    # 資格確認（テスト用ログインの有効化と削除）は prod_check.sh に任せる。ここで Worker の Secret を書かない
    assert 'prod_check.sh" --chat -y' in text
    assert "secret put" not in text
