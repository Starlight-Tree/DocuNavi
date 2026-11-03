"""生成した本文の検証。LLM を使わない規則だけ（無料・決定的）。落ちた本文は再試行へ回す。

文書間の矛盾は、「数値の突合」で防ぐ: 本文に出る数量・日付・時刻は、bible の事実（と `extra_numbers`）の文字列に
含まれていなければならない。数値を伴わない矛盾（言い換えで事実を変える、固有名の取り違え）は、この検証では見つからない
（README の「限界」）。
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from dataclasses import dataclass

from evals.synth import markdown_subset
from evals.synth.bible import Bible


@dataclass(frozen=True)
class Issue:
    code: str
    message: str


_DATE = r"\d{4}年\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月|\d{1,2}月\d{1,2}日|\d{4}年|\d{1,2}月"
_QTY = r"\d+(?:\.\d+)?(?:kg|mm|mL|MPa|kW|km|cm|dB|Hz|MB|GB|°C|%|g|m|L|W|V|円|時間|分|秒|か月|ヶ月|日|席|台|名|棟|室|店舗|店|歳|倍)"
_TIME = r"\d{1,2}:\d{2}|\d{1,2}時\d{1,2}分|\d{1,2}時"
_TOKEN = re.compile(f"{_DATE}|{_QTY}|{_TIME}")

GLOBAL_FORBIDDEN = ("Google", "Amazon", "Apple", "Microsoft", "OpenAI", "Anthropic", "Gemini", "ChatGPT", "Wikipedia")
META_PHRASES = (
    "承知しました",
    "承知いたしました",
    "作成しました",
    "As an AI",
    "<think>",
    "</think>",
    "プロンプト",
    "使ってよい事実",
)
_FENCE = re.compile(r"```|~~~")
_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_IMAGE_OR_LINK = re.compile(r"!\[|\]\(")
_PHONE = re.compile(r"(?<!\d)(?:\+81[-\s]?)?0\d{1,4}[-ー−\s]\d{1,4}[-ー−\s]\d{3,4}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_URL = re.compile(r"https?://([\w.-]+)", re.I)
_SAFE_DOMAINS = ("example.com", "example.org", "example.net", ".example", ".invalid", ".test")
_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿]")


def normalize(text: str) -> str:
    """全角・半角をそろえ、数値の桁区切りと、数字に接する空白（`6 時 30 分` など）を消す。"""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    return re.sub(r"(?<=\d) +| +(?=\d)", "", text)  # NFKC のあとなので、全角の空白は半角になっている


def quantity_tokens(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))


def allowed_haystack(bible: Bible) -> str:
    return normalize("\n".join([f.text for f in bible.facts] + list(bible.extra_numbers) + [bible.revision]))


def _known(token: str, haystack: str) -> bool:
    """トークンが、事実の文字列の中に、数字の途中でない位置で現れるか（`2kg` が `12kg` に一致しない）。"""
    return re.search(rf"(?<![\d.]){re.escape(token)}", haystack) is not None


def _safe_domain(domain: str) -> bool:
    domain = domain.lower()
    return any(domain == d.lstrip(".") or domain.endswith(d) for d in _SAFE_DOMAINS)


def validate_body(md: str, bible: Bible, *, min_chars: int, max_chars: int) -> list[Issue]:
    issues: list[Issue] = []
    text = md.strip()
    plain = markdown_subset.plain_text(text)
    n = len(plain.replace("\n", "").replace(" ", ""))

    if n < min_chars:
        issues.append(Issue("too_short", f"本文が短すぎます（{n} 文字 < {min_chars}）"))
    if n > max_chars:
        issues.append(Issue("too_long", f"本文が長すぎます（{n} 文字 > {max_chars}）"))

    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if not first.startswith("# "):
        issues.append(Issue("no_title", "先頭が `# 題名` ではありません"))
    if sum(1 for ln in text.splitlines() if ln.startswith("## ")) < 2:
        issues.append(Issue("few_headings", "`## 見出し` が 2 つ未満です"))
    if (
        _FENCE.search(text)
        or _HTML.search(text)
        or _IMAGE_OR_LINK.search(text)
        or "**" in text
        or re.search(r"^#{3,} ", text, re.M)
    ):
        issues.append(
            Issue(
                "format",
                "Markdown の小集合の外の記法（コードブロック・HTML・画像・リンク・太字・### 見出し）があります",
            )
        )
    for phrase in META_PHRASES:
        if phrase in text:
            issues.append(Issue("meta_text", f"前置きや指示の語が混ざっています: {phrase}"))
            break

    letters = [c for c in plain if not c.isspace()]
    if letters and sum(1 for c in letters if _JAPANESE.match(c)) / len(letters) < 0.4:
        issues.append(Issue("language", "日本語の文字が少なすぎます"))

    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 12]
    if any(lines.count(ln) > 2 for ln in set(lines)):
        issues.append(Issue("repetition", "同じ行が 3 回以上あります"))
    raw = plain.encode()
    if len(raw) > 600 and len(zlib.compress(raw)) / len(raw) < 0.12:
        issues.append(Issue("repetition", "同じ語句の繰り返しが多すぎます（圧縮率）"))

    if (
        text
        and not text.rstrip().endswith(("。", "」", "）", ")", "|", "』", "。」"))
        and not re.search(r"\[\^\d+\]:.*$", text.splitlines()[-1])
    ):
        issues.append(Issue("truncated", "文が途中で終わっています"))

    haystack = allowed_haystack(bible)
    unknown = sorted({t for t in quantity_tokens(plain) if not _known(t, haystack)})
    if unknown:
        issues.append(Issue("unknown_number", f"事実にない数値・日付・時刻: {', '.join(unknown[:8])}"))

    allowed_contacts = {normalize(c) for c in bible.contacts}
    for m in _PHONE.finditer(normalize(plain)):
        if m.group(0) not in allowed_contacts:
            issues.append(Issue("contact", "電話番号らしい文字列があります"))
            break
    if any(
        not _safe_domain(m.group(1)) and normalize(m.group(0)) not in allowed_contacts for m in _EMAIL.finditer(plain)
    ):
        issues.append(Issue("contact", "実在しうるメールアドレスがあります（example.com 等だけ許可）"))
    if any(not _safe_domain(m.group(1)) for m in _URL.finditer(plain)):
        issues.append(Issue("contact", "実在しうる URL があります（example.com 等だけ許可）"))
    if "〒" in plain:
        issues.append(Issue("contact", "郵便番号の記号があります"))

    for term in (*GLOBAL_FORBIDDEN, *bible.forbidden):
        if term and term.lower() in plain.lower():
            issues.append(Issue("forbidden", f"使えない語があります: {term}"))
            break
    return issues


def shingles(text: str, n: int = 5) -> set[str]:
    compact = re.sub(r"\s+", "", normalize(markdown_subset.plain_text(text)))
    return {compact[i : i + n] for i in range(max(len(compact) - n + 1, 0))}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicate(candidate: set[str], others: dict[str, set[str]], threshold: float) -> tuple[str, float] | None:
    """既に受け入れた本文のうち、類似度（5 文字の連続の Jaccard）が閾値以上のもの。最も似ているものを返す。"""
    best: tuple[str, float] | None = None
    for doc_id, other in others.items():
        score = similarity(candidate, other)
        if score >= threshold and (best is None or score > best[1]):
            best = (doc_id, score)
    return best
