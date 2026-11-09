"""scripts/prod_smoke_chat.py の純粋な部分（SSE の解析と、引用の検査）。ネットワークは使わない。"""

import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "prod_smoke_chat", Path(__file__).resolve().parent.parent / "scripts" / "prod_smoke_chat.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)

CONTEXT = "[1] a.pdf\n入寮の申込みは 3月1日 までです。\n\n[2] b.pdf\n退寮の届出は 30日前 です。"


def sse(*chunks: dict) -> str:
    return "\n\n".join(f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks) + "\n\ndata: [DONE]\n"


def test_parse_stream_collects_text_and_metadata():
    raw = sse(
        {"type": "start", "messageMetadata": {"thread_id": "t"}},
        {"type": "text-delta", "id": "x", "delta": "申込みは"},
        {"type": "text-delta", "id": "x", "delta": "3月1日[1]"},
        {"type": "message-metadata", "messageMetadata": {"hit_count": 2, "sources": [{"collection": "cafe"}]}},
        {"type": "finish"},
    )
    text, meta, error = smoke.parse_stream(raw)
    assert text == "申込みは3月1日[1]" and meta["hit_count"] == 2 and error is None


def test_parse_stream_reports_an_error_chunk():
    _, _, error = smoke.parse_stream(sse({"type": "error", "errorText": "固定文"}))
    assert error == "固定文"


def test_check_citations_matches_the_screen_rule():
    text = "A[1「3月1日」]。B[2「4月10日」]。C[3]。D[1]。E[2「30日 前」]"
    got = smoke.check_citations(text, CONTEXT, 2)
    assert got == [
        (1, "3月1日", "原文どおり"),
        (2, "4月10日", "原文に無い"),
        (3, None, "番号が無い"),
        (1, None, "番号のみ"),
        (2, "30日 前", "原文どおり"),  # 空白を除いて比べる
    ]
