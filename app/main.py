import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # ルーター import より前に .env を読み込む

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app import search_vector  # noqa: E402
from app.agents.graph import is_mock_mode  # noqa: E402
from app.api.documents import router as documents_router  # noqa: E402
from app.api.graph import router as graph_router  # noqa: E402
from app.api.routes import router  # noqa: E402
from app.api.threads import router as threads_router  # noqa: E402
from app.auth import verify_auth_config  # noqa: E402
from app.frontend import mount_frontend  # noqa: E402
from app.history import history_store  # noqa: E402

_APP_LOG_HANDLER = "app-log"


def _configure_app_logging() -> None:
    """app.* のログを INFO から標準エラーへ出す。ルートロガーには触れない。

    uvicorn は uvicorn.* しか設定せずルートは WARNING のままなので、何もしないと app.* の INFO
    （切断による LLM 呼び出しの取り消しなど）が捨てられる。app ロガーだけにレベルとハンドラを付け、
    他のライブラリのログ量は変えない。propagate は既定のまま（テストの caplog はルートで拾う）。
    再 import（--reload・テスト）でハンドラが重ならないよう、名前で判定する。
    """
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    if any(h.get_name() == _APP_LOG_HANDLER for h in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.set_name(_APP_LOG_HANDLER)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)


_configure_app_logging()  # mount_frontend の起動ログより前に有効にする


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # AUTH_MODE=required のとき、JWKS の公開鍵を取得できるかを確かめる。使えなければ、検証なしで動き出さないよう、起動を失敗させる
    await verify_auth_config()
    # DATABASE_URL があれば、会話履歴の接続プールを起動時に開いて終了時に閉じる（無ければ何もしない）。
    # 開けなければ、履歴がメモリに切り替わらないよう、起動を失敗させる
    async with history_store():
        # 本物の LLM のときは、DB の資料と埋め込みが使える状態かを確かめる。使えなければ、検索のない状態で動き出さず、起動を失敗させる
        # （接続プールは history_store が開く。検索はそれを借りる）。モックモードは資料を検索しないので、DB も API も要らない
        if not is_mock_mode():
            await search_vector.verify()
        try:
            yield
        finally:
            await search_vector.close()


app = FastAPI(title="DocuNavi", description="資料を聞く・探す", lifespan=lifespan)

origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(documents_router)
app.include_router(threads_router)
app.include_router(graph_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 想定外の例外でも内部情報を返さず、他のエラーと同じ JSON 形式にそろえる。
    # Starlette はこの後も例外を再送出するので、ログは uvicorn 側に残る
    return JSONResponse(status_code=500, content={"detail": "サーバー内部でエラーが発生しました。"})


# ビルド済みフロントエンド（frontend/dist）があれば配信する。無ければ API だけで動く。
# どのルートにも当たらなかったリクエストだけを受けるので、API ルートが隠れることはない
mount_frontend(app)
