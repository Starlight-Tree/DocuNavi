"""コマンドライン。`python -m evals.synth <サブコマンド>`（README に流れと上限の説明がある）。

終了コード: 0 = 正常、2 = 入力・設定の誤り、3 = 上限に達して止まった（途中の結果は台帳に残る）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from evals.synth import batch, bench, measure, pipeline
from evals.synth.backend import BackendError, FakeBackend, OpenAICompatBackend
from evals.synth.bible import BibleError, load_all
from evals.synth.budget import BudgetExceeded, Limits
from evals.synth.ledger import Ledger, LockedError
from evals.synth.plan import Brief, PlanError, build_plan
from evals.synth.render import DEFAULT_FONT, RenderUnavailable, render_pdf
from evals.synth.verify import verify_output

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def default_references() -> dict[str, str]:
    """既存の資料の本文（類似の検査用）。facts.md（事実の一覧）は除く。"""
    refs: dict[str, str] = {}
    paths = [p for p in sorted((REPO / "evals/corpora").glob("*/source/*.md")) if p.name != "facts.md"]
    paths += sorted((REPO / "evals/fixtures/zahard_txt").glob("*.txt"))
    for p in paths:
        refs[f"existing:{p.name}"] = p.read_text(encoding="utf-8")
    return refs


def _briefs(args: argparse.Namespace) -> tuple[dict, list[Brief]]:
    bibles = load_all(Path(args.bibles))
    briefs = build_plan(bibles, args.per_target, args.target or None)
    if getattr(args, "sample_per_target", None):
        picked: list[Brief] = []
        counts: Counter[str] = Counter()
        for b in briefs:
            if counts[b.target] < args.sample_per_target:
                picked.append(b)
                counts[b.target] += 1
        briefs = picked
    return bibles, briefs


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--bibles", default=str(HERE / "bibles"), help="対象ごとの共通設定（bible）のディレクトリー")
    p.add_argument("--out-dir", default=str(HERE / "out"), help="出力先（storage/ や evals/corpora/ の中は拒否される）")
    p.add_argument("--per-target", type=int, default=20, help="対象ごとの目標件数（既存の資料を含む）")
    p.add_argument("--target", action="append", help="対象を絞る（繰り返し可）")
    p.add_argument(
        "--sample-per-target",
        type=int,
        default=None,
        help="計画の先頭から、対象ごとにこの件数だけ使う（試験用。例: 2）",
    )


def cmd_plan(args: argparse.Namespace) -> int:
    bibles, briefs = _briefs(args)
    out = Path(args.out_dir)
    pipeline.check_out_dir(out)
    from evals.synth.ledger import atomic_write

    atomic_write(
        out / "plan.jsonl", "".join(json.dumps(b.to_dict(), ensure_ascii=False) + "\n" for b in briefs).encode()
    )
    by_target: Counter[str] = Counter(b.target for b in briefs)
    by_type: Counter[tuple[str, str]] = Counter((b.target, b.doc_type) for b in briefs)
    for target in sorted(by_target):
        types = ", ".join(f"{t}×{n}" for (tg, t), n in sorted(by_type.items()) if tg == target)
        print(f"{target}: 新規 {by_target[target]} 件（既存 {bibles[target].existing_documents} 件）  {types}")
    print(f"計 {len(briefs)} 件 → {out / 'plan.jsonl'}")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    bibles, briefs = _briefs(args)
    report = batch.prepare_round(
        batch.PrepareConfig(
            out_dir=Path(args.out_dir),
            bibles=bibles,
            briefs=briefs,
            max_attempts=args.max_attempts,
            max_requests=args.max_requests,
            max_tokens_per_doc=args.max_tokens_per_doc,
            retry_failed=args.retry_failed,
            regenerate_stale=args.regenerate_stale,
            requeue_queued=args.requeue_queued,
            usd_per_hour=args.usd_per_hour,
            assumed_tokens_per_second=args.assumed_tokens_per_second,
            assumed_overhead_seconds=args.assumed_overhead_seconds,
            max_total_cost_usd=args.max_total_cost_usd,
        )
    )
    print(
        f"依頼 {report.requests} 件 / 受け入れ済み {len(report.already_done)} / 依頼済み（結果待ち）{len(report.skipped_queued)} / "
        f"失敗で停止 {len(report.skipped_failed)} / stale {len(report.stale)} / 上限で見送り {len(report.cut_by_limit)}"
    )
    if report.requests:
        print(f"回 {report.round_no}: {report.path}")
        if report.projected_cost_usd:
            print(
                f"見積もり（未測定の仮定）: {report.projected_seconds / 60:.0f} 分、${report.projected_cost_usd:.2f}"
                f"（これまでの費用 ${report.spent_usd:.2f}）"
            )
    else:
        print("新しい依頼はありません")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    bibles, briefs = _briefs(args)
    report = batch.ingest_round(
        batch.IngestConfig(
            out_dir=Path(args.out_dir),
            bibles=bibles,
            briefs=briefs,
            round_no=args.round,
            max_attempts=args.max_attempts,
            dedupe_threshold=args.dedupe_threshold,
            usd_per_hour=args.usd_per_hour,
            job_seconds=args.job_seconds,
            reference_texts=default_references(),
        )
    )
    print(
        f"回 {args.round}: 合格 {len(report.accepted)} / 再試行へ {len(report.rejected)} / 失敗で停止 {len(report.failed)} / "
        f"結果なし {len(report.lost)} / 無視 {len(report.ignored)}"
    )
    if report.rejected_codes:
        print("落ちた理由: " + ", ".join(f"{k}×{v}" for k, v in sorted(report.rejected_codes.items())))
    if report.round_record:
        r = report.round_record
        print(f"所要 {r['job_seconds']:.0f} 秒（{r['job_seconds_source']}）、費用 ${r['cost_usd']:.2f}")
    return 0


def _backend(args: argparse.Namespace):
    if args.backend == "fake":
        return FakeBackend()
    if not (args.base_url and args.model):
        raise BackendError("--backend openai には --base-url と --model が要ります")
    return OpenAICompatBackend(args.base_url, args.model, token_env=args.token_env, temperature=args.temperature)


def cmd_run(args: argparse.Namespace) -> int:
    bibles, briefs = _briefs(args)
    if args.backend != "fake" and None in (args.max_docs, args.max_cost_usd, args.max_tokens):
        print(
            "fake 以外のバックエンドは、--max-docs --max-cost-usd --max-tokens をすべて指定してください",
            file=sys.stderr,
        )
        return 2
    limits = Limits(
        max_docs=args.max_docs if args.max_docs is not None else len(briefs),
        max_tokens=args.max_tokens if args.max_tokens is not None else 10**9,
        max_cost_usd=args.max_cost_usd if args.max_cost_usd is not None else 10**6,
        max_total_cost_usd=args.max_total_cost_usd,
        price_in_per_m=args.price_in,
        price_out_per_m=args.price_out,
        usd_per_hour=args.usd_per_hour,
    )
    report = pipeline.run(
        pipeline.RunConfig(
            out_dir=Path(args.out_dir),
            bibles=bibles,
            briefs=briefs,
            backend=_backend(args),
            limits=limits,
            max_attempts=args.max_attempts,
            max_tokens_per_doc=args.max_tokens_per_doc,
            concurrency=args.concurrency,
            dedupe_threshold=args.dedupe_threshold,
            retry_failed=args.retry_failed,
            regenerate_stale=args.regenerate_stale,
            reference_texts=default_references(),
        )
    )
    print(
        f"合格 {len(report.accepted)} / 済み {len(report.already_done)} / 失敗 {len(report.failed)} / 未着手 {len(report.not_started)} / "
        f"stale {len(report.stale)} / 失敗で停止済み {len(report.skipped_failed)} / トークン {report.tokens} / 費用 ${report.cost_usd:.4f}"
    )
    if report.stopped:
        print(f"停止: {report.stopped}")
        return 3
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    font = Path(args.font)

    def renderer(md: str, out: Path, doc_id: str, title: str) -> None:
        render_pdf(md, out, font_path=font, doc_id=doc_id, title=title)

    done = pipeline.render_pending(Path(args.out_dir), renderer)
    rows = pipeline.write_manifest(Path(args.out_dir))
    print(f"PDF を作成 {len(done)} 件。マニフェスト {len(rows)} 件 → {Path(args.out_dir) / 'manifest.jsonl'}")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    rows = pipeline.write_manifest(Path(args.out_dir))
    print(f"マニフェスト {len(rows)} 件")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    problems = verify_output(Path(args.out_dir))
    for p in problems:
        print(f"問題: {p}")
    print("問題なし" if not problems else f"問題 {len(problems)} 件")
    return 0 if not problems else 2


def cmd_status(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.out_dir) / "ledger.jsonl")
    counts: Counter[tuple[str, str]] = Counter(
        (s.get("target", "?"), s.get("status", "?")) for s in ledger.state.values()
    )
    for (target, status), n in sorted(counts.items()):
        print(f"{target:16} {status:10} {n}")
    print(f"費用の累計（見積もり）: ${pipeline.total_spent_usd(Path(args.out_dir), ledger):.2f}")
    return 0


def cmd_bench_prepare(args: argparse.Namespace) -> int:
    bibles, briefs = _briefs(args)
    requests = [batch.make_request(bibles, b, 1, args.max_tokens_per_doc) for b in briefs]
    path = bench.prepare_bench(Path(args.out_dir), requests, force=args.force)
    print(f"ベンチの依頼 {len(requests)} 件 → {path}（全部の試行で共有する。同時数だけを変えて GPU ジョブを流す）")
    return 0


def cmd_bench_ingest(args: argparse.Namespace) -> int:
    bibles, briefs = _briefs(args)
    r = bench.evaluate_trial(
        out_dir=Path(args.out_dir),
        bibles=bibles,
        briefs=briefs,
        label=args.label,
        usd_per_hour=args.usd_per_hour,
        job_seconds=args.job_seconds,
        dedupe_threshold=args.dedupe_threshold,
        reference_texts=default_references(),
    )
    print(
        f"{r['label']}（同時数 {r['concurrency']}）: 合格 {r['valid']} / 不合格 {r['invalid']} {r['invalid_codes']} / 欠落 {r['missing']} / "
        f"エラー {r['error']} / 全体 {r['job_seconds']:.0f} 秒 / 費用 ${r['cost_usd']:.2f}"
    )
    return 0


def cmd_bench_report(args: argparse.Namespace) -> int:
    print(bench.format_report(bench.latest_trials(Path(args.out_dir)), args.tolerance))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    m = measure.measure(batch.recorded_rounds(out), batch.accepted_count(out))
    print(
        measure.format_report(
            m,
            usd_per_hour=args.usd_per_hour,
            task_timeout_seconds=args.task_timeout,
            max_attempts=args.max_attempts,
            docs=args.docs,
            gains=args.gain,
            gemini_prices=(args.gemini_in, args.gemini_out) if args.gemini_in is not None else None,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.synth", description="テスト用の架空資料（PDF）の一括生成")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=fn)
        _common(p)
        return p

    add("plan", cmd_plan, "依頼票の計画を作る（外部に接続しない）")

    p = add("prepare", cmd_prepare, "一括: GPU ジョブへ渡す依頼を書く（CPU）")
    p.add_argument("--max-requests", type=int, default=8, help="1 回のジョブに入れる依頼の上限")
    p.add_argument("--max-attempts", type=int, default=2, help="1 つの資料の試行（= 回）の上限")
    p.add_argument("--max-tokens-per-doc", type=int, default=3000)
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--regenerate-stale", action="store_true")
    p.add_argument("--requeue-queued", action="store_true", help="結果が戻らなかった依頼を出し直す")
    p.add_argument(
        "--usd-per-hour",
        type=float,
        default=0.0,
        help="GPU ジョブ（GPU + CPU + メモリ）の時間単価。費用の事前の見積もりに使う",
    )
    p.add_argument("--assumed-tokens-per-second", type=float, default=300.0, help="未測定の仮定。測定後に差し替える")
    p.add_argument(
        "--assumed-overhead-seconds", type=float, default=900.0, help="起動 + 読み込みの仮定。測定後に差し替える"
    )
    p.add_argument(
        "--max-total-cost-usd", type=float, default=None, help="累計の費用の上限。収まる件数まで依頼を減らす"
    )

    p = add("ingest", cmd_ingest, "一括: GPU ジョブの結果を取り込む（検証・受け入れ。CPU）")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--max-attempts", type=int, default=2)
    p.add_argument("--dedupe-threshold", type=float, default=0.5)
    p.add_argument("--usd-per-hour", type=float, default=0.0)
    p.add_argument(
        "--job-seconds",
        type=float,
        default=None,
        help="Cloud Run の実行の開始〜終了の秒数（起動を含む）。無ければ meta の経過時間",
    )

    p = add("run", cmd_run, "逐次: バックエンドを 1 件ずつ呼ぶ（fake・OpenAI 互換）")
    p.add_argument("--backend", choices=["fake", "openai"], default="fake")
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--token-env", help="認証トークンを入れた環境変数の名前（値は渡さない）")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--max-docs", type=int)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--max-cost-usd", type=float)
    p.add_argument("--max-total-cost-usd", type=float)
    p.add_argument("--price-in", type=float, default=0.0)
    p.add_argument("--price-out", type=float, default=0.0)
    p.add_argument("--usd-per-hour", type=float, default=0.0)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--max-tokens-per-doc", type=int, default=3000)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--dedupe-threshold", type=float, default=0.5)
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--regenerate-stale", action="store_true")

    p = add("render", cmd_render, "受け入れ済みの本文を PDF にし、マニフェストを書く（CPU。reportlab が要る）")
    p.add_argument("--font", default=str(DEFAULT_FONT))
    add("manifest", cmd_manifest, "分類マニフェストを書く")
    add("verify", cmd_verify, "台帳・本文・PDF・マニフェストの整合と、全ページの「テスト用の架空資料」を点検する")
    add("status", cmd_status, "対象ごとの状態の件数と費用の累計")

    p = add("bench-prepare", cmd_bench_prepare, "同時数の比較: 全試行で共有する依頼を書く（台帳には触れない）")
    p.add_argument("--max-tokens-per-doc", type=int, default=3000, help="出力の上限。全試行で同じにする")
    p.add_argument("--force", action="store_true", help="既にある依頼と違っても作り直す（比較をやり直すとき）")

    p = add("bench-ingest", cmd_bench_ingest, "同時数の比較: 1 つの試行の結果を検証して集計する（CPU）")
    p.add_argument("--label", required=True, help="試行の名前（例: c04）。bench/results.<label>.jsonl を読む")
    p.add_argument("--usd-per-hour", type=float, required=True)
    p.add_argument("--job-seconds", type=float, default=None, help="Cloud Run の実行の開始〜終了の秒数（起動を含む）")
    p.add_argument("--dedupe-threshold", type=float, default=0.5)

    p = add("bench-report", cmd_bench_report, "同時数の比較: 試行を並べて、推奨の同時数を出す")
    p.add_argument("--tolerance", type=float, default=0.05, help="費用の差がこの割合以内なら、同時数が小さい方を選ぶ")

    p = add("report", cmd_report, "GPU ジョブの実測の集計と、80 件・1,000 件への見積もりの補正")
    p.add_argument("--usd-per-hour", type=float, required=True, help="GPU ジョブの時間単価（GPU + CPU + メモリ）")
    p.add_argument(
        "--task-timeout", type=float, default=3000.0, help="ジョブの 1 タスクの上限（秒）。超える分はタスクを分ける"
    )
    p.add_argument("--max-attempts", type=int, default=2)
    p.add_argument("--docs", type=int, nargs="+", default=[80, 1000])
    p.add_argument(
        "--gain",
        type=float,
        nargs="+",
        default=[1.0],
        help="大きなバッチでの 1 件あたりの速さの倍率（未測定なら 1.0 だけ）",
    )
    p.add_argument(
        "--gemini-in", type=float, default=None, help="比較用: Gemini バッチの入力単価（USD / 100 万トークン）"
    )
    p.add_argument("--gemini-out", type=float, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (
        BibleError,
        PlanError,
        pipeline.UnsafeOutDir,
        BackendError,
        LockedError,
        FileNotFoundError,
        RenderUnavailable,
        bench.BenchError,
        ValueError,
    ) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except BudgetExceeded as exc:
        print(f"停止: {exc}", file=sys.stderr)
        return 3
