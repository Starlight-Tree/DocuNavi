"""app/log_redaction.py: ログと未処理の例外の出力から、秘密情報の形を伏せる。

値は、このテスト用の作り物（本物の資格情報ではない）。
"""

import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.log_redaction import MASK, install, redact

FAKE_PASSWORD = "Zq7-fake-Password-0123456789"
FAKE_KEY = "AIza" + "x" * 35


@pytest.mark.parametrize(
    "text",
    [
        f"postgresql://chatbot_app.projref:{FAKE_PASSWORD}@aws-0-ap-northeast-1.pooler.example.com:6543/postgres",
        # 先頭の 1 文字が欠けた接続文字列（2026-10-19 に実際に起きた形）
        f'error connecting in \'pool-1\': missing "=" after "ostgresql://chatbot_app.projref:{FAKE_PASSWORD}@aws-0.pooler.example.com:6543/postgres" in connection info string',
        f"redis://default:{FAKE_PASSWORD}@cache.example.com:6379",
        # パスワードに @ が入っている（URL として不正だが、エラー文には出る）
        f"postgresql://user:{FAKE_PASSWORD}@x@host:5432/db",
        f"host=db.example.com user=app password={FAKE_PASSWORD} dbname=postgres",
        f"host=db.example.com password='{FAKE_PASSWORD} with space' dbname=postgres",
        f"postgresql://host/db?user=app&password={FAKE_PASSWORD}&sslmode=require",
        f"GET https://generativelanguage.googleapis.com/v1/models?key={FAKE_KEY}",
        f"Authorization: Bearer {FAKE_PASSWORD}",
    ],
)
def test_redact_hides_the_secret(text: str) -> None:
    redacted = redact(text)
    assert FAKE_PASSWORD not in redacted
    assert FAKE_KEY not in redacted
    assert MASK in redacted


def test_redact_keeps_the_rest_readable() -> None:
    text = f"postgresql://chatbot_app.projref:{FAKE_PASSWORD}@aws-0.pooler.example.com:6543/postgres"
    assert redact(text) == f"postgresql://chatbot_app.projref:{MASK}@aws-0.pooler.example.com:6543/postgres"


@pytest.mark.parametrize(
    "text",
    [
        "会話履歴の保存先: Postgres（接続プールの上限 5）",
        "https://example.com/path?x=1",  # 資格情報のない URL
        "postgresql://user@host/db",  # パスワードのない URL
        "host:6543 に接続できません",
        "password を入力してください",
        "Bearer の意味",
    ],
)
def test_redact_leaves_ordinary_text_alone(text: str) -> None:
    assert redact(text) == text


def test_install_is_idempotent() -> None:
    install()
    first = logging.Formatter.format
    install()
    assert logging.Formatter.format is first


def test_formatter_masks_message_and_traceback() -> None:
    """ハンドラーの書式を通る出力は、メッセージも例外の文字列も伏せる。"""
    install()
    formatter = logging.Formatter("%(levelname)s %(message)s")
    try:
        raise ValueError(f'bad conninfo "postgresql://u:{FAKE_PASSWORD}@h/db"')
    except ValueError:
        import sys as _sys

        record = logging.LogRecord(
            "psycopg.pool",
            logging.WARNING,
            __file__,
            1,
            "error connecting: %s",
            (f"password={FAKE_PASSWORD}",),
            _sys.exc_info(),
        )
    out = formatter.format(record)
    assert FAKE_PASSWORD not in out
    assert out.count(MASK) >= 2


def test_logger_without_handlers_goes_through_last_resort_masked(capsys: pytest.CaptureFixture[str]) -> None:
    """ハンドラーの無いロガーの WARNING は、標準エラーに出る（今回漏れた経路）。そこでも伏せる。"""
    install()
    logger = logging.getLogger("test_log_redaction.no_handler")
    logger.propagate = False
    logger.warning(
        "error connecting in 'pool-1': %s", f"missing '=' after \"ostgresql://u:{FAKE_PASSWORD}@h:6543/postgres\""
    )
    err = capsys.readouterr().err
    assert "error connecting" in err
    assert FAKE_PASSWORD not in err
    assert MASK in err


def test_unhandled_exception_traceback_is_masked() -> None:
    """未処理の例外（`python -m …` のジョブが落ちたとき）の表示も伏せる。別プロセスで確かめる。"""
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        import app  # noqa: F401  (import だけで入る)
        raise RuntimeError('cannot connect to postgresql://u:{FAKE_PASSWORD}@h:6543/postgres')
        """
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 1
    assert "RuntimeError" in result.stderr
    assert FAKE_PASSWORD not in result.stderr
    assert MASK in result.stderr
