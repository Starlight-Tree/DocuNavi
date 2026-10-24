"""cloudbuild-deploy.yaml（deploy/* のタグ push で 0% 配備するトリガー用）の守りを確かめる。

Docker も Cloud Build も使わずに確かめられるのは、設定の中身まで。トリガーとビルドの実行は、動かすまで未確認。
"""

import re
import subprocess
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


def test_build_args_match_the_dockerfile_and_are_public_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerfile_args = set(re.findall(r"^ARG (VITE_\w+)", dockerfile, flags=re.MULTILINE))
    args = _step("build")["args"]
    passed = {a.split("=", 1)[0] for prev, a in zip(args, args[1:]) if prev == "--build-arg"}
    assert passed == dockerfile_args == {"VITE_SUPABASE_URL", "VITE_SUPABASE_PUBLISHABLE_KEY"}
    assert not re.search(r"secret|DATABASE_URL|GEMINI_API_KEY", _code(), flags=re.IGNORECASE)


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


def test_tag_check_accepts_deploy_tags_and_writes_the_run_tag(tmp_path: Path) -> None:
    result = _run_tag_check(tmp_path, "deploy/h2c-20261024")
    assert result.returncode == 0
    assert (tmp_path / "run-tag").read_text() == "h2c-20261024"


def test_tag_check_rejects_other_names(tmp_path: Path) -> None:
    for name in ["", "main", "deploy/", "deploy/H2C", "deploy/1abc", "deploy/a;rm", "deploy/a/b", "v1.0"]:
        assert _run_tag_check(tmp_path, name).returncode != 0, name
