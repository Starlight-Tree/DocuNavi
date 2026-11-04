"""ログと未処理の例外の出力に混ざる秘密情報を伏せる。

接続文字列が不正だと、psycopg のエラー文に接続文字列がそのまま入り、`psycopg.pool` が WARNING で標準エラーへ出す
（2026-10-19 に、パスワード付きの値が Cloud Logging に残った）。例外の文字列には何が入るか分からないので、
出力の最後の段（`logging.Formatter.format` と未処理の例外の表示）で、次の形を伏せる:

- URL の資格情報: `scheme://user:パスワード@host` → `scheme://user:***@host`（`ostgresql://` のように scheme が崩れていても）
- `password=...`（libpq の key=value 形式。クエリ文字列も）
- Gemini の API キー（`AIza…`）
- `Bearer <トークン>`

`app` パッケージを import した時点で入る（サーバーも、`python -m app.ingestion` などのジョブも通る）。
ログの書式を持つのは標準の `logging.Formatter` だけなので、ハンドラーを足さず、ルートロガーにも触れない。
"""

import logging
import re
import sys
import traceback
from types import TracebackType

MASK = "***"

_URL_CREDENTIALS = re.compile(r"""([A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@"'<>]+:)[^\s/"'<>]+(@)""")
_PASSWORD_PARAM = re.compile(r"""(\bpassword\s*=\s*)('[^']*'|"[^"]*"|[^\s&;,'"]+)""", re.IGNORECASE)
_GEMINI_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_BEARER = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=\-]{8,}", re.IGNORECASE)


def redact(text: str) -> str:
    """秘密情報の形を伏せた文字列。該当がなければ、そのまま返す。"""
    if "://" in text:
        text = _URL_CREDENTIALS.sub(rf"\g<1>{MASK}\g<2>", text)
    if "assword" in text or "ASSWORD" in text:
        text = _PASSWORD_PARAM.sub(rf"\g<1>{MASK}", text)
    if "AIza" in text:
        text = _GEMINI_KEY.sub(f"AIza{MASK}", text)
    if "earer" in text or "EARER" in text:
        text = _BEARER.sub(rf"\g<1>{MASK}", text)
    return text


def install() -> None:
    """ログの出力と未処理の例外の表示に `redact` を通す。何度呼んでもよい。"""
    if not getattr(logging.Formatter.format, "_redacting", False):
        original = logging.Formatter.format

        def format(self: logging.Formatter, record: logging.LogRecord) -> str:  # noqa: A001 - 元のメソッドと同じ名前で差し替える
            return redact(original(self, record))

        format._redacting = True  # type: ignore[attr-defined]
        logging.Formatter.format = format  # type: ignore[method-assign]
    if not getattr(sys.excepthook, "_redacting", False):
        previous = sys.excepthook

        def excepthook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
            if previous is not sys.__excepthook__:
                previous(exc_type, exc, tb)  # 別の処理が差し替えていたら、その出力は触らない
                return
            sys.stderr.write(redact("".join(traceback.format_exception(exc_type, exc, tb))))

        excepthook._redacting = True  # type: ignore[attr-defined]
        sys.excepthook = excepthook
