"""追質問の検索語の補完（`CHAT_RETRIEVAL_MODE=direct` の検索の前処理）。

通常は、利用者の質問をそのまま検索語にする（検索の評価も、質問文そのままで行っている）。会話の 2 回目以降で、質問が
前の話題に頼っているとき（「それはいつ？」「営業時間は？」のような短い質問・指示語）だけ、前の発言を踏まえた検索語に直す。
直すのは LLM の短い 1 回（`rewrite_messages`）で、失敗・遅延のときは前の質問を足した検索語（`rule_query`）で済ませる。

ここは文字列を扱うだけの純粋な関数で、LLM もログも持たない（呼び出しは graph.py）。
"""

import re
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

# 質問がこの文字数より短いとき、2 回目以降の会話では前の話題に頼っているとみなす
SHORT_QUESTION_CHARS = 12
# 補完に渡す前の質問の数と、直前の回答の長さ（長いと遅く、無関係な語も混ざる）
MAX_PRIOR_QUESTIONS = 3
MAX_ANSWER_CHARS = 300
# 検索語の長さの上限（これを超える出力は、検索語ではなく回答のような文とみなして使わない）
MAX_QUERY_CHARS = 120

# 指示語・継続を表す語。「では」「なぜ」のように単独の質問にも普通に出る語は入れない（不要な補完は遅くなるだけ）
_DEPENDENT = re.compile(
    r"それ|これ|あれ|どれ|その|この|あの|同じ|前述|上記|先ほど|さっき|他に|ほかに|もっと|詳しく|続き|さらに|じゃあ|それなら"
)

REWRITE_INSTRUCTION = (
    "あなたは、資料検索のための検索語を作る係です。会話の流れを踏まえ、最後の質問を、会話を見ていない人にも通じる"
    "1 つの短い検索文（日本語）に書き換えてください。指示語は、会話に出てきた具体的な名前に置き換えます。"
    "会話に無い事実は足さず、質問に答えず、検索文だけを 1 行で出力してください。"
)


def needs_completion(question: str) -> bool:
    """この質問は、前の発言を見ないと検索語として足りないか（2 回目以降の会話で呼ぶ）。"""
    text = question.strip()
    return len(text) < SHORT_QUESTION_CHARS or _DEPENDENT.search(text) is not None


def prior_context(messages: Sequence[BaseMessage], skip_texts: frozenset[str] = frozenset()) -> tuple[list[str], str]:
    """最後のユーザー発言より前の、(ユーザーの質問（古い順）, 直前の最終回答)。

    skip_texts は、伏せ字に差し替わった発言・回答の定型文（これらは話題の手がかりにならない）。
    """
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    before = messages[:last_human] if last_human >= 0 else []
    questions = [m.text.strip() for m in before if isinstance(m, HumanMessage) and m.text.strip() not in skip_texts]
    answers = [
        m.text.strip()
        for m in before
        if isinstance(m, AIMessage) and not m.tool_calls and m.text.strip() and m.text.strip() not in skip_texts
    ]
    return [q for q in questions if q], (answers[-1] if answers else "")


def rule_query(question: str, prior_questions: Sequence[str]) -> str:
    """LLM を使えないときの検索語。直前の質問を前に足す（話題の手がかりが検索に残る）。"""
    return f"{prior_questions[-1]} {question}".strip() if prior_questions else question


def rewrite_messages(question: str, prior_questions: Sequence[str], last_answer: str) -> list[BaseMessage]:
    """検索語を書き換えさせる LLM への入力（ツールは付けない）。"""
    lines = [f"- {q}" for q in prior_questions[-MAX_PRIOR_QUESTIONS:]]
    parts = ["これまでの質問:", *lines]
    if last_answer:
        parts += ["", f"直前の回答（冒頭）: {last_answer[:MAX_ANSWER_CHARS]}"]
    parts += ["", f"最後の質問: {question}"]
    return [SystemMessage(content=REWRITE_INSTRUCTION), HumanMessage(content="\n".join(parts))]


def parse_rewrite(text: str) -> str | None:
    """LLM の出力から検索語を取り出す。空・長すぎる出力は None（使わない）。"""
    for line in text.strip().splitlines():
        line = line.strip().strip("「」\"'`").strip()
        for prefix in ("検索文:", "検索文：", "検索語:", "検索語："):
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
        if line:
            return line if len(line) <= MAX_QUERY_CHARS else None
    return None
