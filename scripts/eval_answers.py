"""回答の評価（実 API）。evals/answers/questions.yaml の質問を、app と同じグラフで Gemini に答えさせて記録する。

検索の評価（scripts/eval_retrieval.py）と違い、**LLM を呼ぶ**（課金される）。呼び出し回数に上限を置く。
採点は、このスクリプトでは自動化しない（回答の文を人が読んで、README の基準で付ける）。ここでは、回答の文・検索の
クエリと結果・呼び出し回数を JSON に残し、「資料に無い」と言っているかの機械的な目印（says_no_info）だけを付ける。

    python -m scripts.eval_answers                 # 質問の一覧と上限を表示するだけ（API は呼ばない）
    python -m scripts.eval_answers --run           # 実 API で測る（既定の上限: 全体 30 回、1 問 3 回）
    python -m scripts.eval_answers --run --min-similarity 0.65 \\
        --questions evals/answers/questions_vector.yaml --max-calls 40 --out evals/results/answers_vector_<日付>.json
                                                   # しきい値をこの実行の中だけ差し替える（付けなければ、アプリの MIN_SIMILARITY）。DB の接続は .env の DATABASE_URL

ヘッジ（追加の試行）とフォールバックは切る。切らないと、1 回の呼び出しが何本の試行になるか数えられない。
キーは .env から app と同じ経路で読み、表示しない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from scripts.eval_retrieval import CORPORA_DIR, JST, QUESTIONS_PATH, ROOT, Question, load_questions

ANSWERS_DIR = ROOT / "evals" / "answers"
ANSWERS_QUESTIONS_PATH = ANSWERS_DIR / "questions.yaml"
RESULTS_DIR = ROOT / "evals" / "results"
ROLES = ("absent_spill", "on_topic_absent", "off_topic", "answerable")
DEFAULT_MAX_CALLS = 30
DEFAULT_PER_QUESTION = 3

# 「資料に無い」と言っている文の目印。機械的な目印にすぎず、採点は回答の文を読んで付ける
_NO_INFO = re.compile(
    r"記載がありません|記載されていません|見つかりませんでした|書かれていません|情報がありません|資料にはありません"
)
_SOURCE_NAME = re.compile(r"(?m)^\[([^\]\n]+\.(?:txt|pdf))\]$")


class BudgetExceeded(RuntimeError):
    """呼び出しの上限に達した（上限を超える呼び出しは、送る前に止める）。"""


class CallBudget(AsyncCallbackHandler):
    """LLM を呼ぶ直前に数え、上限を超える呼び出しを送らずに止める。1 問の上限と、全体の上限がある。"""

    raise_error = True

    def __init__(self, max_total: int, max_per_question: int) -> None:
        self.max_total = max_total
        self.max_per_question = max_per_question
        self.total = 0
        self.in_question = 0

    def start_question(self) -> None:
        self.in_question = 0

    async def on_chat_model_start(self, serialized: dict[str, Any], messages: Any, **kwargs: Any) -> None:
        if self.total >= self.max_total:
            raise BudgetExceeded(f"全体の上限 {self.max_total} 回に達しました")
        if self.in_question >= self.max_per_question:
            raise BudgetExceeded(f"1 問の上限 {self.max_per_question} 回に達しました")
        self.total += 1
        self.in_question += 1


def load_plan(path: Path = ANSWERS_QUESTIONS_PATH) -> list[tuple[Question, str]]:
    """(質問, 役) の列。質問の本文と正解は、承認済みの評価セットから引く。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("質問のリストが空です")
    by_id: dict[str, Question] = {}
    for source in (QUESTIONS_PATH, *sorted(CORPORA_DIR.glob("*/questions.yaml"))):
        for question in load_questions(source):
            by_id[question.id] = question
    plan: list[tuple[Question, str]] = []
    for item in raw:
        qid, role = item.get("id"), item.get("role")
        if qid not in by_id:
            raise ValueError(f"{qid}: 評価セットにありません")
        if role not in ROLES:
            raise ValueError(f"{qid}: role は {ROLES} のどれかにしてください")
        plan.append((by_id[qid], role))
    return plan


def says_no_info(text: str) -> bool:
    return bool(_NO_INFO.search(text))


def summarize_messages(messages: Sequence[Any]) -> dict[str, Any]:
    """今ターンの経過（検索のクエリ・返ったファイル・返った本文・最後の回答）を、メッセージ列から取り出す。"""
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    turn = messages[last_human + 1 :]
    searches: list[dict[str, Any]] = []
    queries: dict[str, str] = {}
    for message in turn:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                queries[call["id"]] = str(call["args"].get("query", ""))
        elif isinstance(message, ToolMessage):
            content = str(message.content)
            searches.append(
                {
                    "query": queries.get(message.tool_call_id, ""),
                    "files": _SOURCE_NAME.findall(content),
                    "result": content,
                }
            )
    answer = next((m.text for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), None)
    return {"searches": searches, "answer": answer}


async def run_one(graph: Any, question: Question, budget: CallBudget) -> dict[str, Any]:
    thread_id = f"eval-answers-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}, "callbacks": [budget]}
    budget.start_question()
    started = time.monotonic()
    error: str | None = None
    try:
        await graph.ainvoke({"messages": [HumanMessage(content=question.question)]}, config)
    except BudgetExceeded as exc:
        error = f"budget_exceeded: {exc}"
    except Exception as exc:  # noqa: BLE001 - 詳細（本文・URL）は残さず、型とステータスだけ記録する
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        error = f"{type(exc).__name__}" + (f" (status={status})" if status else "")
    elapsed = time.monotonic() - started
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    summary = summarize_messages(state.values.get("messages", []))
    return {
        "calls": budget.in_question,
        "elapsed_seconds": round(elapsed, 1),
        "error": error,
        "says_no_info": says_no_info(summary["answer"]) if summary["answer"] else None,
        **summary,
    }


@asynccontextmanager
async def vector_search_session(min_similarity: float | None) -> AsyncIterator[None]:
    """測るあいだだけ、DB の接続プールを開き、しきい値を差し替える（min_similarity が None ならアプリの値のまま。アプリのコードは変えない）。

    app の lifespan と違い、履歴の checkpointer は作らない（テーブルの作成も書き込みもしない）。検索（app/search_vector.py）が借りる
    プールだけを、history の _pool に入れる。起動時の検査（資料と埋め込みが DB と合っているか）は app と同じものを通す。
    """
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from app import history, search_vector

    url = history.database_url()
    if not url:
        raise RuntimeError("ベクトル検索の評価には DATABASE_URL が要ります（未設定です）")
    pool = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=2,
        timeout=10.0,
        open=False,
        check=AsyncConnectionPool.check_connection,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 10},
    )
    await pool.open(wait=True, timeout=10.0)
    original = search_vector.MIN_SIMILARITY
    history._pool = pool
    if min_similarity is not None:
        search_vector.MIN_SIMILARITY = min_similarity
    try:
        await search_vector.verify()
        yield
    finally:
        search_vector.MIN_SIMILARITY = original
        history._pool = None
        await search_vector.close()
        await pool.close()


async def run_with_search(
    plan: list[tuple[Question, str]], budget: CallBudget, min_similarity: float | None
) -> tuple[list[dict[str, Any]], str]:
    async with vector_search_session(min_similarity):
        return await run_all(plan, budget)


async def run_all(plan: list[tuple[Question, str]], budget: CallBudget) -> tuple[list[dict[str, Any]], str]:
    from app.agents.graph import build_graph, create_llm

    llm = create_llm()
    model = getattr(llm, "model", type(llm).__name__)
    graph = build_graph()
    records: list[dict[str, Any]] = []
    for question, role in plan:
        result = await run_one(graph, question, budget)
        records.append(
            {
                "id": question.id,
                "role": role,
                "question": question.question,
                "expected_answer": question.answer,
                **result,
            }
        )
        print(
            f"{question.id} ({role}): calls={result['calls']} total={budget.total}/{budget.max_total}"
            f" error={result['error']} says_no_info={result['says_no_info']}",
            flush=True,
        )
        if result["error"] and result["error"].startswith("budget_exceeded: 全体"):
            break
    return records, str(model)


def search_floor() -> float:
    from app import search_vector

    return search_vector.MIN_SIMILARITY


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回答の評価（実 API）")
    parser.add_argument("--run", action="store_true", help="実 API を呼ぶ（付けなければ質問の一覧を表示するだけ）")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, help="LLM 呼び出しの全体の上限")
    parser.add_argument("--per-question", type=int, default=DEFAULT_PER_QUESTION, help="1 問あたりの上限")
    parser.add_argument(
        "--only", help="測る質問の id（カンマ区切り）。API の一時的な失敗で取れなかった質問をやり直すとき"
    )
    parser.add_argument(
        "--out", type=Path, help="出力先（既定 evals/results/answers_<日付>.json。あるファイルは上書きしない）"
    )
    parser.add_argument(
        "--questions", type=Path, default=ANSWERS_QUESTIONS_PATH, help="質問の一覧（既定 evals/answers/questions.yaml）"
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        help="検索の類似度のしきい値（この実行の中だけ差し替える。省略はアプリの MIN_SIMILARITY）",
    )
    args = parser.parse_args(argv)

    plan = load_plan(args.questions)
    if args.only:
        wanted = [qid.strip() for qid in args.only.split(",") if qid.strip()]
        unknown = [qid for qid in wanted if qid not in {q.id for q, _ in plan}]
        if unknown:
            print(f"質問にありません: {', '.join(unknown)}", file=sys.stderr)
            return 2
        plan = [(q, role) for q, role in plan if q.id in wanted]
    print(
        f"質問 {len(plan)} 問、上限: 全体 {args.max_calls} 回、1 問 {args.per_question} 回、検索: ベクトル（しきい値 {args.min_similarity if args.min_similarity is not None else 'アプリの値'}）"
    )
    for question, role in plan:
        print(f"  {question.id} [{role}] {question.question}")
    if not args.run:
        print("（--run を付けると実 API を呼びます）")
        return 0

    now = datetime.now(JST)
    out = args.out or RESULTS_DIR / f"answers_{now:%Y%m%d}.json"
    if out.exists():
        print(f"出力先がすでにあります（上書きしません。--out で別の名前にしてください）: {out.name}", file=sys.stderr)
        return 2
    load_dotenv()
    # 追加の試行（ヘッジ）・再試行・フォールバックは切る。1 回の呼び出しを 1 本にして、回数を数えられるようにする
    os.environ["LLM_HEDGE_DELAY_SECONDS"] = "0"
    os.environ["LLM_MAX_ATTEMPTS"] = "1"
    os.environ.pop("GEMINI_FALLBACK_MODEL", None)
    from app.agents.graph import is_mock_mode

    if is_mock_mode():
        print("本物の LLM の認証情報がありません（モックモード）。実 API の評価はできません。", file=sys.stderr)
        return 2

    budget = CallBudget(args.max_calls, args.per_question)
    records, model = asyncio.run(run_with_search(plan, budget, args.min_similarity))
    now = datetime.now(JST)
    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "model": model,
        "max_calls": args.max_calls,
        "max_calls_per_question": args.per_question,
        "calls_used": budget.total,
        "hedge": "off",
        "search": {
            "backend": "vector",
            "min_similarity": args.min_similarity if args.min_similarity is not None else search_floor(),
        },
        "path": "ainvoke（/chat と同じ。画面が使う /chat/stream ではない）",
        "results": records,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"呼び出し {budget.total}/{args.max_calls} 回。結果: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
