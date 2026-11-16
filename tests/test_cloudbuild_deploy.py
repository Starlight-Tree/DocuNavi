"""cloudbuild-deploy.yaml（deploy/* のタグ push で 0% 配備するトリガー用）の守りを確かめる。

Docker も Cloud Build も使わずに確かめられるのは、設定の中身まで。トリガーとビルドの実行は、動かすまで未確認。
"""

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _config() -> dict:
    return yaml.safe_load((ROOT / "cloudbuild-deploy.yaml").read_text(encoding="utf-8"))


def _step(step_id: str) -> dict:
    (step,) = [s for s in _config()["steps"] if s["id"] == step_id]
    return step


def _code() -> str:
    # コメントは対象外
    lines = (ROOT / "cloudbuild-deploy.yaml").read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_deploys_without_taking_traffic() -> None:
    script = _step("deploy-no-traffic")["args"][1]
    assert "gcloud run deploy" in script
    assert "--no-traffic" in script
    assert "--tag" in script


def test_never_changes_traffic_or_service_settings() -> None:
    # 100% への切り替えはユーザーの promote だけ。環境変数・Secret・ポート名は、サービスの最新の設定を引き継ぐ
    assert not re.search(r"update-traffic|--to-tags|--to-latest|--to-revisions", _code())
    assert not re.search(r"--set-env-vars|--set-secrets|--update-env-vars|--update-secrets|--use-http2", _code())


def test_pushes_the_image_before_deploying() -> None:
    ids = [s["id"] for s in _config()["steps"]]
    assert ids.index("build") < ids.index("push") < ids.index("deploy-no-traffic")
    assert ids[0] == "check-tag"


def test_builds_the_api_image_without_build_args() -> None:
    # 画面は Cloudflare の Worker `docunavi-web`（frontend）が配信する。イメージに画面の設定（ビルド引数）は要らず、秘密も渡さない
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"^ARG ", dockerfile, flags=re.MULTILINE)
    assert "--build-arg" not in _step("build")["args"]
    assert not re.search(r"secret|DATABASE_URL|GEMINI_API_KEY|SUPABASE", _code(), flags=re.IGNORECASE)


def _run_tag_check(tmp_path: Path, tag_name: str) -> subprocess.CompletedProcess[str]:
    # Cloud Build は `$$` を `$` に戻してから bash に渡す。同じ変換をして、検査のシェルを実際に動かす
    script = _step("check-tag")["args"][1].replace("$$", "$")
    run_tag = tmp_path / "run-tag"
    script = script.replace("/workspace/run-tag", str(run_tag))
    return subprocess.run(
        ["bash", "-c", script],
        env={"TAG_NAME": tag_name, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def _today_jst() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")


def test_tag_check_accepts_new_format_and_writes_the_run_tag(tmp_path: Path) -> None:
    # Git のタグ deploy/vYYYYMMDD[-N] → Cloud Run のタグ vYYYYMMDD[-N]（先頭の deploy/ を外すだけ。1 回目は日付だけ、2 回目以降が -2 から）
    today = _today_jst()
    for name in [
        "deploy/v20260101",
        "deploy/v20261114-2",
        f"deploy/v{today}",
        f"deploy/v{today}-2",
        f"deploy/v{today}-12",
    ]:
        (tmp_path / "run-tag").unlink(missing_ok=True)
        result = _run_tag_check(tmp_path, name)
        assert result.returncode == 0, (name, result.stderr)
        assert (tmp_path / "run-tag").read_text() == name.removeprefix("deploy/")


def test_tag_check_rejects_malformed_names(tmp_path: Path) -> None:
    for name in [
        "",
        "main",
        "deploy/",
        "deploy/H2C",
        "deploy/1abc",
        "deploy/a;rm",
        "deploy/a/b",
        "v1.0",
        "deploy/v20261114-1",  # 1 回目は日付だけ。-1 は付けない
        "deploy/v20261114-0",
        "deploy/v20261114-02",  # 先頭ゼロなし
        "deploy/v20261114-",
        "deploy/v20261114-2-fix",  # 内容は名前に入れない
        "deploy/v20261114-2-3",
        "deploy/V20261114",  # 小文字の v
        "deploy/20261114",  # v が無い（Cloud Run のタグは英字始まり）
        "deploy/v2026114",  # 日付が 7 桁
        "deploy/v202611140",  # 日付が 9 桁
        "deploy/v20261114/x",
        "deploy/v20261114\n",
        "deploy/v20261114-2\n",
    ]:
        assert _run_tag_check(tmp_path, name).returncode != 0, name
        assert not (tmp_path / "run-tag").exists(), name


def test_tag_check_rejects_old_format_tags(tmp_path: Path) -> None:
    # 2026-11-17 より前の 4 つ。再配備で同じ名前を push しても止まる（既存のタグ自体は変更しない）
    for name in [
        "deploy/py314-1116",
        "deploy/suggest-20261117",
        "deploy/suggest-hook-1117",
        "deploy/membership-1117",
        "deploy/h2c-20261024",
    ]:
        assert _run_tag_check(tmp_path, name).returncode != 0, name


def test_tag_check_rejects_impossible_and_future_dates(tmp_path: Path) -> None:
    for name in [
        "deploy/v20261340",
        "deploy/v20260230-2",
        "deploy/v20261100",
        "deploy/v00000000",
        "deploy/v20991231",
    ]:
        result = _run_tag_check(tmp_path, name)
        assert result.returncode != 0, name
        assert not (tmp_path / "run-tag").exists(), name
    tomorrow = (datetime.now(timezone.utc) + timedelta(hours=9, days=1)).strftime("%Y%m%d")
    assert _run_tag_check(tmp_path, f"deploy/v{tomorrow}").returncode != 0
    assert _run_tag_check(tmp_path, f"deploy/v{tomorrow}-2").returncode != 0
