"""生成の指示（プロンプト）。本文はモデルが書く。ここは、根拠にする事実と書式を渡すだけ。

`PROMPT_VERSION` は、指示の文面を変えたら上げる（依頼票の key に入るので、古い生成物は stale になる）。
FakeBackend（backend.py）は、この書式の「題名」「文字数」「使ってよい事実」の行を読む。書式を変えたら両方を直す。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evals.synth.bible import Bible
    from evals.synth.plan import Brief

PROMPT_VERSION = "1"

SYSTEM = """あなたは、架空の設定資料を書く編集者です。依頼された 1 つの資料の本文だけを、日本語で書きます。
守ること:
- 「使ってよい事実」に書かれたことだけを根拠にする。そこに無い数値・日付・時刻・金額・固有名・連絡先は、書かない。
- 事実と食い違うことは書かない。事実が足りないところは、数値を使わない一般的な説明・手順・注意で補う。
- 実在の企業・商品・人物・地名・電話番号・メールアドレス・URL は書かない。
- 本文の書式は Markdown の小集合だけ: 先頭の `# 題名`、`## 見出し`、段落、`|` で区切る表、脚注（`[^1]` と `[^1]: 本文`）。
  箇条書きの記号・コードブロック・太字・画像・リンク・HTML は使わない。
- 前置き・あいさつ・説明・「以下は」などは書かない。本文だけを出す。「架空の資料です」といった断り書きも書かない（別に付く）。
- 文章は途中で終わらせない。最後は句点か閉じ括弧で終える。"""


def build_messages(bible: Bible, brief: Brief) -> tuple[str, str]:
    kind = next(k for k in bible.kinds if k.id == brief.kind)
    facts = bible.facts_for(kind)
    lines = [
        f"対象: {bible.label}",
        f"設定の概要: {bible.premise}",
        f"資料の種類: {brief.doc_type}（{kind.id}）",
        f"題名: {brief.title}",
        f"この資料の焦点: {brief.variant}",
        f"文字数: {brief.min_chars}〜{brief.max_chars}",
    ]
    if bible.revision:
        lines.append(f"改訂: {bible.revision}")
    lines.append("構成（この順で見出しを立てる）:")
    lines.extend(f"- {o}" for o in kind.outline)
    if kind.style:
        lines.append(f"文体: {kind.style}")
    lines.append("## 使ってよい事実")
    lines.extend(f"- [{f.id}] {f.text}" for f in facts)
    lines.append("上の事実だけを根拠に、この資料の本文を書いてください。")
    return SYSTEM, "\n".join(lines)


def prompt_sha256(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\n---\n{user}".encode()).hexdigest()
