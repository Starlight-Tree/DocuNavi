"""R2 の資料を DB に揃えるコマンド（突き合わせ方式。docs/r2-ingest-design.md の第 1 版）。

R2 の資料群（バケット直下のプレフィックス）ごとに取り込み（`python -m app.ingestion --input r2://<資料群>`）を呼び、
取り込みが終わったら埋め込み（`python -m app.ingestion.embed`）を呼ぶ。どちらも冪等なので、何度起動しても結果は同じ。
新しい処理は持たない（資料群を巡る順序と、終了コードの判断だけ）。定期実行は Cloud Run Job + Cloud Scheduler を想定する。

    python -m app.ingestion.sync            # 確認だけ（取り込みも埋め込みも、何も保存せず API も呼ばない）
    python -m app.ingestion.sync --apply    # 取り込んで保存し、実 API で埋め込む（埋め込みは課金される）

`R2_ACCOUNT_ID` `R2_BUCKET` `R2_CREDENTIALS_JSON`（または `R2_CREDENTIALS_FILE`）、`DATABASE_URL`、`GEMINI_API_KEY` が要る。

埋め込みは、取り込みの終了コードが 0 または 3 のときだけ実行する。3 は壊れた PDF が混ざったとき（壊れた文書にはチャンクが無く、
埋め込みの対象にもならない）。壊れた PDF は取り込むたびに同じく失敗するので、3 で止めると、他の資料が検索に出なくなる。
1・2（DB・R2 の失敗、設定の誤り）のときは、取り込みが済んでいないので埋め込まない。

終了コード: 0 = 完了（壊れた PDF が混ざっていても 0。Job の再試行を無駄に起こさない。該当の資料群は標準エラーに出す）、
1 = 取り込み・埋め込み・R2 の一時的な失敗、2 = 設定の誤り・使い方の誤り。
"""

import argparse
import sys
from collections.abc import Callable, Sequence

from app.ingestion.embedding import DEFAULT_MAX_CALLS, DEFAULT_MAX_CHUNKS
from app.ingestion.service import COLLECTION_PATTERN

Command = Callable[[Sequence[str]], int]

# 取り込みの終了コード（app/ingestion/__main__.py）。0 = 完了、3 = 一部の PDF が取り込めなかった（他は処理した）
_INGEST_OK = {0, 3}
_PARTIAL = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingestion.sync",
        description="R2 の資料を DB に取り込み、埋め込む（既定は確認だけ。--apply で保存し、実 API を呼ぶ）",
    )
    parser.add_argument("--apply", action="store_true", help="DB に保存し、実 API で埋め込む（課金される）")
    parser.add_argument("--max-chunks", type=int, default=DEFAULT_MAX_CHUNKS, help="埋め込みの対象の上限")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, help="埋め込みの API への送信回数の上限")
    return parser


def run_sync(
    collections: Sequence[str],
    ingest_main: Command,
    embed_main: Command,
    *,
    apply: bool,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    max_calls: int = DEFAULT_MAX_CALLS,
) -> int:
    """資料群ごとに取り込み、全部終わってから 1 回だけ埋め込む。取り込みと埋め込みは、引数の関数で呼ぶ（テストでは偽物）。"""
    names: list[str] = []
    for name in collections:
        if COLLECTION_PATTERN.fullmatch(name):
            names.append(name)
        else:  # 取り込みが受け付けない名前（2 で終わる）。これで埋め込みを止めない
            print("資料群の名前の形が合わないので飛ばしました（英数字・_・- の 40 文字まで）。", file=sys.stderr)
    if not names:
        print("取り込む資料群が R2 にありません。", file=sys.stderr)
        return 0

    codes = {name: ingest_main(["--input", f"r2://{name}", *(["--apply"] if apply else [])]) for name in names}

    failed = {name: code for name, code in codes.items() if code not in _INGEST_OK}
    if failed:
        listing = "、".join(f"{name}（終了コード {code}）" for name, code in failed.items())
        print(f"取り込みが終わらなかった資料群があるので、埋め込みはしません: {listing}", file=sys.stderr)
        return 2 if all(code == 2 for code in failed.values()) else 1

    embed_code = embed_main(
        [*(["--apply"] if apply else []), "--max-chunks", str(max_chunks), "--max-calls", str(max_calls)]
    )
    if embed_code != 0:
        print(f"埋め込みが終わりませんでした（終了コード {embed_code}）。", file=sys.stderr)
        return embed_code if embed_code in (1, 2) else 1

    broken = [name for name, code in codes.items() if code == _PARTIAL]
    if broken:
        print(
            f"取り込めなかった PDF がある資料群: {'、'.join(broken)}（documents.failure_reason に理由が残っています）",
            file=sys.stderr,
        )
    return 0


def _call_ingest(argv: Sequence[str]) -> int:
    from app.ingestion.__main__ import main

    return main(argv)


def _call_embed(argv: Sequence[str]) -> int:
    from app.ingestion.embed import main

    return main(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_chunks < 1 or args.max_calls < 1:
        print("--max-chunks と --max-calls は 1 以上にしてください。", file=sys.stderr)
        return 2

    from app import r2_store

    try:
        names = r2_store.default_store().collections()
    except r2_store.R2ConfigError as exc:
        print(f"R2 の設定が足りません（{exc}）。", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 例外の文字列に接続先・鍵の ID が入りうる。クラス名だけを出す
        print(f"R2 の資料群を取得できませんでした（{type(exc).__name__}）。", file=sys.stderr)
        return 1

    return run_sync(
        names,
        _call_ingest,
        _call_embed,
        apply=args.apply,
        max_chunks=args.max_chunks,
        max_calls=args.max_calls,
    )


if __name__ == "__main__":
    raise SystemExit(main())
