"""チャンクの埋め込みを作って保存するコマンド。既定は確認だけ（API を呼ばず、何も書かない）。

    python -m app.ingestion.embed                  # 対象の件数・文字数・費用の見積り・必要な呼び出し回数を表示するだけ
    python -m app.ingestion.embed --apply          # 実 API で埋め込みを作り、chunks.embedding に保存する（課金される）

対象は、埋め込みが無い（または別のモデル・次元の）チャンクだけ。作り済みの行は再生成しない。途中で失敗しても、
そこまでの保存は残り、次の実行は続きから始まる。上限は `--max-chunks`（既定 300）と `--max-calls`（既定 10。
再試行はしないので、API への送信 1 回が 1 回）。`GEMINI_API_KEY` と `DATABASE_URL` を .env から読み、表示しない。
列は supabase/migrations/20261017184901_pgvector_embeddings.sql で足したもの（Supabase の履歴の版 20261017184901）。

終了コード: 0 = 完了（確認だけ、または全て保存）、1 = DB・埋め込みの失敗（一部は保存済みのことがある）、2 = 使い方の誤り。
"""

import argparse
import os
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from app.history import database_url
from app.ingestion.embedding import (
    BATCH_SIZE,
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_CHUNKS,
    DIMENSIONS,
    MAX_INPUT_CHARS,
    MODEL,
    VERTEX_ENV,
    ApiBudget,
    EmbedReport,
    GeminiEmbedder,
    PostgresEmbeddingStore,
    embed_pending,
    use_vertex,
)

_PLACEHOLDER_KEYS = {"", "your_api_key_here"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingestion.embed",
        description="チャンクの埋め込みを作る（既定は確認だけ。--apply で実 API を呼んで保存する）",
    )
    parser.add_argument("--apply", action="store_true", help="実 API を呼び、DB に保存する（課金される）")
    parser.add_argument("--max-chunks", type=int, default=DEFAULT_MAX_CHUNKS, help="今回の対象の上限")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, help="API への送信回数の上限")
    return parser


def _print_report(report: EmbedReport, max_chunks: int, max_calls: int) -> None:
    print(f"モデル: {MODEL}、{DIMENSIONS} 次元、1 回に最大 {BATCH_SIZE} 件")
    print(f"埋め込みの無いチャンク: {report.pending_total} 件、今回の対象: {report.selected} 件")
    print(f"送る文字数: {report.characters}（前置きを含む）、必要な呼び出し: {report.calls_needed} 回")
    print(
        f"費用の概算: 約 ${report.estimated_cost_usd:.4f}（文字数 × 1 文字 1 トークンの推計。保証された上限ではない。"
        f"強制している上限は、チャンク {max_chunks} 件・呼び出し {max_calls} 回・1 件 {MAX_INPUT_CHARS} 文字）"
    )
    if report.applied:
        print(f"呼び出し {report.calls} 回、保存 {report.saved} 件")
    if report.error:
        print(f"中断: {report.error}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_chunks < 1 or args.max_calls < 1:
        print("--max-chunks と --max-calls は 1 以上にしてください。", file=sys.stderr)
        return 2

    load_dotenv()
    url = database_url()
    if not url:
        print("DATABASE_URL が要ります（未設定です）。", file=sys.stderr)
        return 2
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if args.apply and api_key in _PLACEHOLDER_KEYS and not use_vertex():
        print(
            f"--apply には GEMINI_API_KEY が要ります（未設定です）。Vertex AI で呼ぶなら {VERTEX_ENV}=true と ADC が要ります。",
            file=sys.stderr,
        )
        return 2

    import psycopg

    budget = ApiBudget(args.max_calls)
    try:
        # prepare_threshold=None: transaction mode のプーラーでも動かす（app/history.py と同じ）
        with psycopg.connect(url, prepare_threshold=None, connect_timeout=10) as conn:
            store = PostgresEmbeddingStore(conn)
            embedder = GeminiEmbedder(api_key, budget) if args.apply else None
            report = embed_pending(store, embedder, budget, max_chunks=args.max_chunks, apply=args.apply)  # type: ignore[arg-type]
    except psycopg.Error as exc:
        # 例外の文字列には接続先が入りうる。クラス名だけを出す
        print(f"DB の操作に失敗しました（{type(exc).__name__}）。", file=sys.stderr)
        return 1

    _print_report(report, args.max_chunks, args.max_calls)
    if not args.apply:
        print("（確認だけ。API は呼ばず、何も保存していません。--apply で実行します）")
    return 1 if report.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
