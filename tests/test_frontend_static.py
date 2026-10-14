"""app/frontend.py（静的配信 + SPA フォールバック）の検証。

分離したアプリ（tmp の dist）と、実際の app.main.app の両方を確認する。
モックモードへの固定は tests/conftest.py が行う。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

from app.api.routes import router
from app.frontend import DEFAULT_DIST_DIR, mount_frontend

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = "<!doctype html><html><body><div id='root'>FRONTEND-INDEX</div></body></html>"
ASSET_JS = "console.log('asset');"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text(ASSET_JS, encoding="utf-8")
    (root / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")
    # dist の外にある、漏れてはいけないファイル
    (tmp_path / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
    return root


def make_app(dist_dir: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    mount_frontend(app, dist_dir)
    return app


@pytest.fixture
def client(dist: Path) -> TestClient:
    return TestClient(make_app(dist))


# --- フロントエンドの配信 -----------------------------------------------------


def test_root_serves_index_html_without_cache(client):
    res = client.get("/")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "FRONTEND-INDEX" in res.text
    assert res.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_served_with_long_cache(client):
    res = client.get("/assets/index-abc123.js")

    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    assert res.text == ASSET_JS
    assert res.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_root_level_files_are_served(client):
    res = client.get("/robots.txt")

    assert res.status_code == 200
    assert res.text.startswith("User-agent")


def test_index_html_by_name_is_not_cached_either(client):
    res = client.get("/index.html")

    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/chat-history", "/a/b/c", "/settings/", "/threads/abc-123"])
def test_unknown_paths_fall_back_to_index_html(client, path):
    res = client.get(path)

    assert res.status_code == 200
    assert "FRONTEND-INDEX" in res.text


def test_head_is_supported(client):
    res = client.head("/")

    assert res.status_code == 200
    assert res.content == b""


# --- 無いファイルは HTML にしない ----------------------------------------------


@pytest.mark.parametrize(
    "path", ["/assets/missing.js", "/assets/", "/assets", "/missing.js", "/favicon.ico", "/x/y.css"]
)
def test_missing_files_return_404_not_html(client, path):
    res = client.get(path)

    assert res.status_code == 404
    assert "FRONTEND-INDEX" not in res.text


# --- API との干渉が無い --------------------------------------------------------


def test_health_is_not_shadowed(client):
    res = client.get("/health")

    assert res.status_code == 200
    assert res.json() == {"status": "ok", "mode": "mock", "history": "memory"}


def test_chat_post_is_not_shadowed(client):
    res = client.post("/chat", json={"message": "ザーハードの大気について教えて"})

    assert res.status_code == 200
    assert set(res.json()) == {"reply", "thread_id", "context", "mode"}


@pytest.mark.parametrize("path", ["/chat", "/chat/"])
def test_get_on_chat_stays_405_with_allow_header(client, path):
    res = client.get(path)

    assert res.status_code == 405
    assert "POST" in res.headers["allow"]
    assert "FRONTEND-INDEX" not in res.text


def test_post_validation_on_chat_is_unchanged(client):
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_docs_and_openapi_still_work(client):
    assert client.get("/openapi.json").json()["info"]["title"]
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "FRONTEND-INDEX" not in docs.text


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_non_get_on_unknown_path_is_404(client, method):
    res = getattr(client, method)("/no-such-endpoint")

    assert res.status_code == 404


def test_openapi_lists_only_the_api_routes(client):
    assert set(client.get("/openapi.json").json()["paths"]) == {"/health", "/chat", "/chat/stream"}


# --- パストラバーサル ----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/%2e%2e%2fsecret.txt",
        "/assets/../../secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/..%5csecret.txt",
        "/%00",
        "/robots.txt%00.png",
    ],
)
def test_path_traversal_does_not_leak_files(client, path):
    res = client.get(path)

    assert "TOP-SECRET" not in res.text
    assert res.status_code in (200, 404)


def test_symlink_pointing_outside_dist_is_not_served(dist, tmp_path):
    (dist / "leak.txt").symlink_to(tmp_path / "secret.txt")
    client = TestClient(make_app(dist))

    res = client.get("/leak.txt")

    assert "TOP-SECRET" not in res.text
    assert res.status_code == 404


# --- 未ビルド ------------------------------------------------------------------


def test_without_build_output_the_api_still_works(tmp_path):
    app = FastAPI()
    app.include_router(router)

    assert mount_frontend(app, tmp_path / "does-not-exist") is False

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.post("/chat", json={"message": "ザーハードの大気について教えて"}).status_code == 200
    assert client.get("/chat").status_code == 405


def test_root_explains_how_to_build_when_frontend_is_missing(tmp_path):
    app = FastAPI()
    app.include_router(router)
    mount_frontend(app, tmp_path / "does-not-exist")
    client = TestClient(app)

    res = client.get("/")

    assert res.status_code == 404
    detail = res.json()["detail"]
    assert "npm run build" in detail
    assert "docker compose up --build" in detail
    # 他のパスは従来どおりの素の 404（API の誤 URL にビルド手順を返さない）
    other = client.get("/no/such/page")
    assert other.status_code == 404
    assert other.json() == {"detail": "Not Found"}


def test_missing_frontend_is_reported_in_the_startup_log(tmp_path, caplog):
    import logging

    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger="app.frontend"):
        mount_frontend(app, tmp_path / "nope")

    assert any(r.levelno == logging.WARNING and "npm run build" in r.getMessage() for r in caplog.records)


def test_build_output_created_after_startup_is_served_without_restart(tmp_path):
    dist = tmp_path / "dist"  # 起動時点では存在しない
    app = FastAPI()
    app.include_router(router)
    assert mount_frontend(app, dist) is False
    client = TestClient(app)
    assert client.get("/").status_code == 404

    # 起動後に npm run build した状態
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text(ASSET_JS, encoding="utf-8")

    res = client.get("/")
    assert res.status_code == 200 and "FRONTEND-INDEX" in res.text
    assert client.get("/assets/index-abc123.js").status_code == 200
    assert client.get("/some/route").status_code == 200

    # ビルド成果物を消す（再ビルド中など）と、再び API だけの状態に戻る
    (dist / "index.html").unlink()
    assert client.get("/").status_code == 404
    assert client.get("/health").status_code == 200


def test_dist_without_assets_dir_is_fine(tmp_path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    client = TestClient(make_app(root))

    assert "FRONTEND-INDEX" in client.get("/").text
    assert client.get("/assets/x.js").status_code == 404


# --- 実際のアプリ（app.main.app）-----------------------------------------------


@pytest.mark.skipif(
    not (DEFAULT_DIST_DIR / "index.html").is_file(), reason="frontend がビルドされていない"
)
def test_real_app_serves_built_frontend_and_keeps_api():
    from app.main import app

    client = TestClient(app)
    built_index = (DEFAULT_DIST_DIR / "index.html").read_text(encoding="utf-8")

    assert client.get("/").text == built_index
    assert client.get("/some/client/route").text == built_index
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/chat").status_code == 405

    asset = next((DEFAULT_DIST_DIR / "assets").glob("*.js"), None)
    assert asset is not None
    res = client.get(f"/assets/{asset.name}")
    assert res.status_code == 200
    assert res.headers["cache-control"].endswith("immutable")


# --- 回帰: レビューで見つかった「テストが無いと壊れても気付けない」挙動 ---------------


def test_spa_fallback_response_is_revalidated_like_index_html(client):
    res = client.get("/threads/abc-123")

    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/assets-guide", "/assetsfoo/bar", "/v1.2/settings", "/docs.v2/overview"])
def test_spa_routes_that_only_look_like_assets_or_files_still_get_index(client, path):
    res = client.get(path)

    assert res.status_code == 200
    assert "FRONTEND-INDEX" in res.text


def test_symlink_loop_is_404_not_500(dist):
    (dist / "loop").symlink_to(dist / "loop")
    client = TestClient(make_app(dist), raise_server_exceptions=False)

    assert client.get("/loop").status_code == 404
    assert client.get("/loop/page").status_code == 404


def test_non_404_errors_are_not_masked_by_the_spa_fallback(dist, monkeypatch):
    original = StaticFiles.get_response

    async def forbidden(self, path, scope):
        if path == "index.html":  # フォールバック先は読める。元のパスだけ拒否する
            return await original(self, path, scope)
        raise HTTPException(status_code=401)

    monkeypatch.setattr(StaticFiles, "get_response", forbidden)

    res = TestClient(make_app(dist)).get("/some/page")

    assert res.status_code == 401
    assert "FRONTEND-INDEX" not in res.text


def test_dist_dir_without_index_html_is_treated_as_not_built(tmp_path):
    partial = tmp_path / "dist"
    partial.mkdir()
    (partial / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(router)

    assert mount_frontend(app, partial) is False

    client = TestClient(app)
    assert "npm run build" in client.get("/").json()["detail"]
    assert client.get("/robots.txt").status_code == 404


def test_conditional_get_keeps_cache_control(client):
    etag = client.get("/assets/index-abc123.js").headers["etag"]

    res = client.get("/assets/index-abc123.js", headers={"If-None-Match": etag})

    assert res.status_code == 304
    assert res.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_not_built_hint_does_not_expose_server_paths(tmp_path):
    app = FastAPI()
    mount_frontend(app, tmp_path / "secret-build-dir" / "dist")

    res = TestClient(app).get("/")

    assert res.status_code == 404
    assert str(tmp_path) not in res.text
    assert "secret-build-dir" not in res.text


def test_custom_404_page_in_dist_never_gets_the_immutable_header(dist):
    # Vite の public/404.html など。html=True だと例外でなく応答として返ってくる
    (dist / "404.html").write_text("CUSTOM-404", encoding="utf-8")
    client = TestClient(make_app(dist))

    res = client.get("/assets/missing.js")

    assert res.status_code == 404
    assert "immutable" not in res.headers.get("cache-control", "")


def test_app_main_mounts_the_dist_next_to_the_app_package(tmp_path):
    """frontend/dist をビルドしていない環境でも、main.py の配線を確かめる（別プロセスで実施）。"""
    shutil.copytree(REPO_ROOT / "app", tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO_ROOT / "storage", tmp_path / "storage")
    fake_dist = tmp_path / "frontend" / "dist"
    fake_dist.mkdir(parents=True)
    (fake_dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    script = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app)\n"
        "assert 'FRONTEND-INDEX' in c.get('/some/client/route').text\n"
        "assert c.get('/health').json()['status'] == 'ok'\n"
    )
    env = {**os.environ, "GEMINI_API_KEY": "", "GOOGLE_GENAI_USE_VERTEXAI": "false"}

    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, check=True, timeout=120)
