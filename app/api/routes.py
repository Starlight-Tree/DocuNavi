import uuid
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.agents.graph import build_graph, is_mock_mode

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="ユーザーの発言")
    thread_id: str | None = Field(
        default=None, description="会話ID。同じ値を送ると会話が継続する"
    )


class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    context: str = Field(description="回答の根拠として検索された設定資料（未検索なら空）")
    mode: str = Field(description="実行モード。mock = ダミー応答 / gemini = 本物の LLM")


@lru_cache(maxsize=2)
def get_graph(mock: bool):
    # mock をキーにキャッシュするので、キー設定後は自動で Gemini 側のグラフに切り替わる
    return build_graph()


def current_mode() -> str:
    return "mock" if is_mock_mode() else "gemini"


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "mode": current_mode()}


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    mode = current_mode()
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    try:
        # context は直近のターンの検索結果だけを返したいので、毎ターン空に戻す
        result = await get_graph(mode == "mock").ainvoke(
            {"messages": [HumanMessage(content=req.message)], "context": ""},
            config,
        )
    except Exception as exc:  # Gemini 側のエラー等
        raise HTTPException(status_code=502, detail=f"LLM 呼び出しに失敗しました: {exc}") from exc

    return ChatResponse(
        reply=str(result["messages"][-1].text),
        thread_id=thread_id,
        context=result.get("context", ""),
        mode=mode,
    )
