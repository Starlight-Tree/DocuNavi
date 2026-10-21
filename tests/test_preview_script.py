"""scripts/preview.sh（Cloud Run のタグ付きプレビューの書き込み）。PREVIEW_DRY_RUN で、gcloud を実行せずにコマンドだけ確かめる。"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "preview.sh"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PREVIEW_DRY_RUN": "1"}
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def test_deploy_never_moves_traffic_or_touches_settings():
    result = run("deploy", "dc969b9", "preview-x")
    assert result.returncode == 0
    deploy = next(line for line in result.stdout.splitlines() if "gcloud run deploy" in line)
    assert "--no-traffic" in deploy and "--tag preview-x" in deploy
    assert "docunavi:dc969b9" in deploy
    # 付けると有料キーの Secret など、いまの設定が消える
    assert "--set-env-vars" not in deploy and "--set-secrets" not in deploy


def test_promote_uses_the_tag_and_rollback_uses_the_revision():
    promote = run("promote", "preview-x")
    assert promote.returncode == 0
    assert "--to-tags preview-x=100" in promote.stdout
    rollback = run("rollback", "docunavi-00005-zl7")
    assert rollback.returncode == 0
    assert "--to-revisions=docunavi-00005-zl7=100" in rollback.stdout


def test_bad_arguments_are_refused_before_anything_runs():
    for args in (("deploy",), ("deploy", "zzz"), ("promote", "Bad_Tag"), ("rollback", "foo"), ("nope",)):
        result = run(*args)
        assert result.returncode == 2, args
        assert "gcloud run" not in result.stdout, args


def test_build_refuses_a_commit_that_is_not_head():
    # ビルドの入力は作業ツリーなので、HEAD 以外のコミット名でイメージを作ると、中身と名前がずれる
    result = run("build", "0000000")
    assert result.returncode == 2
    assert "gcloud builds" not in result.stdout
