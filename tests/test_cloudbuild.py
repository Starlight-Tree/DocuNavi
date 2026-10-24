"""cloudbuild.yaml（scripts/preview.sh の build が使う）の守りを確かめる。

Docker を使わずに確かめられるのは、設定の中身まで。実際のビルドと配備は、Cloud Build で実行するまで未確認。
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _build_step() -> tuple[dict, dict]:
    config = yaml.safe_load((ROOT / "cloudbuild.yaml").read_text(encoding="utf-8"))
    (step,) = config["steps"]
    return config, step


def test_builds_the_api_image_without_build_args() -> None:
    # 画面は Vercel の frontend-next が配信する。イメージに画面の設定（ビルド引数）は要らない
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"^ARG ", dockerfile, flags=re.MULTILINE)
    _, step = _build_step()
    assert "--build-arg" not in step["args"]


def test_no_secrets_in_the_config() -> None:
    # secret key や DB の URL を、ビルドの設定（= ログ）に載せない。コメントは対象外
    lines = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8").splitlines()
    code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert not re.search(r"secret|DATABASE_URL|GEMINI_API_KEY|SUPABASE", code, flags=re.IGNORECASE)


def test_pushes_the_image_that_was_built() -> None:
    config, step = _build_step()
    assert "${_IMAGE}" in step["args"]
    assert config["images"] == ["${_IMAGE}"]
