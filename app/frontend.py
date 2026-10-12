"""ビルド済みフロントエンド（frontend/dist）を FastAPI から配信する。

配信は「どのルートにも当たらなかったリクエスト」だけを受ける `router.default` に差し込む。
そのため API ルート（/chat, /health, /docs など）は常に優先され、メソッド違いの 405 や
末尾スラッシュのリダイレクトも従来どおりで、ルートの登録順にも依存しない。

- dist 直下のファイル（index.html、/assets/* のハッシュ付きファイルなど）はそのまま返す。
- 見つからないパスは SPA のルートとみなして index.html を返す。ただし拡張子付きのパスと
  /assets 配下は「無いファイル」なので、HTML ではなく 404 にする。
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException  # StaticFiles が送出するのはこちら
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# app/ の親（Docker では /code）の下の frontend/dist。ローカルでも Docker でも同じ相対配置になる
DEFAULT_DIST_DIR = Path(__file__).resolve().parents[1] / "frontend" / "dist"

_INDEX = "index.html"
_ASSETS_DIR = "assets"
# index.html は毎回再検証させる（デプロイで参照するハッシュ付きファイルが変わるため）
_CACHE_INDEX = "no-cache"
# /assets/* は Vite がファイル名に内容のハッシュを入れるので、長期キャッシュしてよい
_CACHE_ASSET = "public, max-age=31536000, immutable"

# 応答の本文に載せるので固定文にする（サーバー内のパスはログにだけ残す）
_MSG_NOT_BUILT = (
    "フロントエンドの成果物（frontend/dist/index.html）が見つかりません。"
    "UI は配信されていません（API は利用できます）。"
    "frontend/ で `npm run build` を実行するか、Docker の場合はイメージを再ビルド"
    "（`docker compose up --build`）してください。"
)


def _in_assets(path: str) -> bool:
    return path == _ASSETS_DIR or path.startswith(f"{_ASSETS_DIR}/")


def _is_spa_route(path: str) -> bool:
    """画面遷移のパスか。ファイル名らしい（拡張子付き）パスや /assets 配下は違う。"""
    return not _in_assets(path) and "." not in path.rsplit("/", 1)[-1]


class _FrontendFiles(StaticFiles):
    """StaticFiles に SPA フォールバックとキャッシュヘッダを足したもの。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        served = path
        try:
            try:
                response = await super().get_response(path, scope)
            except HTTPException as exc:
                if exc.status_code != 404 or not _is_spa_route(path):
                    raise
                response = await super().get_response(_INDEX, scope)
                served = _INDEX
        except (OSError, ValueError) as exc:
            # NUL 文字を含むパス（ValueError）、シンボリックリンクのループや I/O 障害（OSError）。
            # 利用者には「無いファイル」として 404 を返すが、後者は運用側が気付けるようログに残す
            if isinstance(exc, OSError):
                logger.warning("静的ファイルを読み込めません: %s", path, exc_info=exc)
            raise HTTPException(status_code=404) from exc

        if response.status_code >= 400:
            # html=True だと 404.html は例外ではなく応答として返ってくる。エラーに長期キャッシュは付けない
            return response
        if _in_assets(served):
            response.headers["Cache-Control"] = _CACHE_ASSET
        elif served in (_INDEX, "."):  # "." は `/`（html=True で index.html を返す）
            response.headers["Cache-Control"] = _CACHE_INDEX
        return response


def mount_frontend(app: FastAPI, dist_dir: Path = DEFAULT_DIST_DIR) -> bool:
    """dist_dir のビルド成果物を、どのルートにも当たらなかった GET/HEAD への応答として配信する。

    成果物の有無はリクエストごとに見るので、起動後に `npm run build` しても再起動は要らない。
    未ビルドでも API は動き、`/` だけがビルドを促す 404 になる。戻り値は「今ビルド済みか」。
    """
    dist = dist_dir.resolve()
    index = dist / _INDEX

    # 未ビルドのまま起動するとアクセス時に素の 404 しか出ないため、起動時にも理由を残す
    # （INFO では uvicorn の既定設定だと表示されないので WARNING にする）
    built = index.is_file()
    if built:
        logger.info("フロントエンド (%s) を配信します。", dist)
    else:
        logger.warning("%s（探した場所: %s）", _MSG_NOT_BUILT, index)

    files = _FrontendFiles(directory=dist, html=True, check_dir=False)
    not_found: ASGIApp = app.router.default  # 配信対象外のリクエストは、従来の 404 のまま

    async def serve_frontend(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            await not_found(scope, receive, send)
        elif index.is_file():
            await files(scope, receive, send)
        elif scope["path"] == "/":
            # ブラウザで開いた入口だけは、素の "Not Found" ではなく原因が分かる 404 にする
            await JSONResponse({"detail": _MSG_NOT_BUILT}, status_code=404)(scope, receive, send)
        else:
            await not_found(scope, receive, send)

    app.router.default = serve_frontend
    return built
