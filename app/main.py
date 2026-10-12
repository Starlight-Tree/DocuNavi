import os

from dotenv import load_dotenv

load_dotenv()  # ルーター import より前に .env を読み込む

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.api.routes import router  # noqa: E402
from app.frontend import mount_frontend  # noqa: E402

app = FastAPI(title="Zahard Chatbot")

origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 想定外の例外でも内部情報を返さず、他のエラーと同じ JSON 形式にそろえる。
    # Starlette はこの後も例外を再送出するので、ログは uvicorn 側に残る
    return JSONResponse(status_code=500, content={"detail": "サーバー内部でエラーが発生しました。"})


# ビルド済みフロントエンド（frontend/dist）があれば配信する。無ければ API だけで動く。
# どのルートにも当たらなかったリクエストだけを受けるので、API ルートが隠れることはない
mount_frontend(app)
