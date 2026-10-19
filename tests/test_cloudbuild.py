"""cloudbuild.yaml が、Dockerfile の VITE_* の ARG と食い違っていないことを確かめる。

Docker を使わずに確かめられるのは、設定の名前の対応まで。実際のビルドと配備は、Cloud Build で実行するまで未確認。
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _dockerfile_vite_args() -> set[str]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    return set(re.findall(r"^ARG (VITE_\w+)", text, flags=re.MULTILINE))


def _build_step() -> tuple[dict, dict]:
    config = yaml.safe_load((ROOT / "cloudbuild.yaml").read_text(encoding="utf-8"))
    (step,) = config["steps"]
    return config, step


def test_build_args_match_the_dockerfile_args() -> None:
    _, step = _build_step()
    args = step["args"]
    passed = {a.split("=", 1)[0] for prev, a in zip(args, args[1:]) if prev == "--build-arg"}
    assert (
        passed
        == _dockerfile_vite_args()
        == {
            "VITE_SUPABASE_URL",
            "VITE_SUPABASE_PUBLISHABLE_KEY",
        }
    )


def test_only_public_values_are_passed() -> None:
    # secret key や DB の URL を、ビルド引数（= バンドルとログ）に渡さない。コメントは対象外
    lines = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8").splitlines()
    code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert not re.search(r"secret|DATABASE_URL|GEMINI_API_KEY", code, flags=re.IGNORECASE)


def test_pushes_the_image_that_was_built() -> None:
    config, step = _build_step()
    assert "${_IMAGE}" in step["args"]
    assert config["images"] == ["${_IMAGE}"]
