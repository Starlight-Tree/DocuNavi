"""テスト用の架空資料の生成（evals/synth/）: 一括の経路（prepare → GPU ジョブ → ingest）、逐次の経路、上限、再開。

GPU ジョブの本体（gpu_worker.py）は、偽のエンジンで動かす。実機（vLLM・GPU）は動かさない。外部に接続しない。
"""

import json
from pathlib import Path

import pytest

from evals.synth import batch, bench, measure, pipeline
from evals.synth.backend import BackendError, Completion, FakeBackend
from evals.synth.bible import load_all
from evals.synth.budget import Budget, BudgetExceeded, Limits
from evals.synth.gpu_worker import FakeEngine, VllmEngine, WorkerLimitError, read_requests, run_worker
from evals.synth.ledger import Ledger, LockedError, atomic_write, exclusive
from evals.synth.plan import build_plan
from evals.synth.render import MARKER, NOTICE

BIBLES = load_all(Path(__file__).resolve().parents[1] / "evals" / "synth" / "bibles")


def sample_briefs(per_target: int = 2):
    briefs, counts = [], {}
    for b in build_plan(BIBLES, 20):
        if b.target.startswith(("lune-", "hoshizora-", "arca-", "oasis-")):
            continue  # 8 件の固定の試料（4 対象 × 2）。リューネ工房の新製品は、別の bible のテストで見る
        counts[b.target] = counts.get(b.target, 0) + 1
        if counts[b.target] <= per_target:
            briefs.append(b)
    return briefs


def prepare(out: Path, briefs, **kw):
    return batch.prepare_round(batch.PrepareConfig(out_dir=out, bibles=BIBLES, briefs=briefs, **kw))


def ingest(out: Path, briefs, round_no: int, **kw):
    return batch.ingest_round(batch.IngestConfig(out_dir=out, bibles=BIBLES, briefs=briefs, round_no=round_no, **kw))


def worker(out: Path, round_no: int, **kw):
    requests = read_requests(batch.requests_path(out, round_no), max_requests=100)
    return run_worker(
        FakeEngine(),
        requests,
        batch.results_path(out, round_no),
        batch.meta_path(out, round_no),
        deadline_seconds=1e9,
        chunk_size=3,
        **kw,
    )


def test_end_to_end_eight_documents_one_gpu_job(tmp_path):
    briefs = sample_briefs(2)
    assert len(briefs) == 8
    out = tmp_path / "out"

    report = prepare(out, briefs, max_requests=8, usd_per_hour=3.0)
    assert (report.round_no, report.requests) == (1, 8)
    lines = [json.loads(x) for x in batch.requests_path(out, 1).read_text().splitlines()]
    assert {r["attempt"] for r in lines} == {1}
    assert len({r["seed"] for r in lines}) == 8

    # 結果が戻る前に prepare し直しても、同じ依頼を二重に出さない（GPU の二重実行を防ぐ）
    again = prepare(out, briefs)
    assert again.requests == 0 and len(again.skipped_queued) == 8

    meta = worker(out, 1)
    assert meta["completed"] == 8 and meta["stopped"] is None and meta["load_seconds"] is not None

    ingested = ingest(out, briefs, 1, usd_per_hour=3.0, job_seconds=1800)
    assert len(ingested.accepted) == 8, ingested.rejected_codes
    assert ingested.round_record["cost_usd"] == pytest.approx(1.5)  # 1800 秒 × $3/時
    assert ingested.round_record["job_seconds_source"] == "given"

    # 再開: 受け入れ済みは、もう依頼にならない。取り込みの二重実行は、何も変えず費用も数え直さない
    assert prepare(out, briefs).requests == 0
    assert len(ingest(out, briefs, 1, usd_per_hour=3.0).ignored) == 8
    assert len(batch.recorded_rounds(out)) == 1

    # PDF 化（CPU）とマニフェスト。ここでは偽の描画（reportlab は要らない）
    def fake_renderer(md: str, target: Path, doc_id: str, title: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"{MARKER} {NOTICE} {doc_id}".encode())

    assert len(pipeline.render_pending(out, fake_renderer)) == 8
    assert pipeline.render_pending(out, fake_renderer) == []  # 作り直さない
    rows = pipeline.write_manifest(out)
    assert [r["id"] for r in rows] == sorted(b.id for b in briefs)
    assert {r["collection"] for r in rows} == {"synthetic-test"} and all(r["synthetic"] for r in rows)
    assert {(r["target"], r["doc_type"]) for r in rows} >= {("minato-coffee", "store-guide"), ("zahard", "setting")}
    # 分類の付け替え（bible の classify_as）。target だけが変わり、元の識別名は bible_target に残る
    regrouped = pipeline.build_manifest(out, {"zahard": "school"})
    assert {(r["target"], r["bible_target"]) for r in regrouped} >= {
        ("school", "zahard"),
        ("minato-coffee", "minato-coffee"),
    }
    assert [r["path"] for r in regrouped] == [r["path"] for r in rows]  # PDF の置き場所は変わらない
    assert (out / "manifest.jsonl").read_bytes() == b"".join(
        (json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode() for r in rows
    )

    m = measure.measure(batch.recorded_rounds(out), batch.accepted_count(out))
    assert m.accepted == 8 and m.first_pass_rate == 1.0 and m.usd_per_accepted == pytest.approx(1.5 / 8)


def test_a_rejected_document_is_retried_in_the_next_round_then_stops(tmp_path):
    briefs = sample_briefs(1)  # 4 件
    out = tmp_path / "out"
    prepare(out, briefs, max_attempts=2)
    worker(out, 1)
    # 1 件だけ、検証で落ちる本文（短すぎ）に書き換える
    victim = briefs[0].id
    rows = [json.loads(x) for x in batch.results_path(out, 1).read_text().splitlines()]
    for r in rows:
        if r["id"] == victim:
            r["text"] = "# 題\n\n## 一\n\n短い。\n\n## 二\n\n以上です。"
    batch.results_path(out, 1).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))

    first = ingest(out, briefs, 1)
    assert first.rejected == [victim] and first.rejected_codes == {"too_short": 1}
    assert len(first.accepted) == 3

    second = prepare(out, briefs, max_attempts=2)  # 落ちた 1 件だけが、2 回目の試行として次の回に入る
    assert second.requests == 1 and second.round_no == 2
    req = json.loads(batch.requests_path(out, 2).read_text())
    assert (req["id"], req["attempt"]) == (victim, 2) and req["seed"] == briefs[0].seed + 1  # 試行ごとに種を変える

    worker(out, 2)
    rows = [json.loads(x) for x in batch.results_path(out, 2).read_text().splitlines()]
    rows[0]["text"] = "# 題\n\n本文"
    batch.results_path(out, 2).write_text(json.dumps(rows[0], ensure_ascii=False) + "\n")
    final = ingest(out, briefs, 2)
    assert final.failed == [victim]  # 試行の上限（2）に達した

    assert prepare(out, briefs, max_attempts=2).requests == 0  # もう作らない
    assert Ledger(out / "ledger.jsonl").get(victim)["status"] == "failed"
    assert prepare(out, briefs, max_attempts=2, retry_failed=True).requests == 1  # 明示したときだけ


def test_lost_results_stay_queued_until_requeued(tmp_path):
    briefs = sample_briefs(1)
    out = tmp_path / "out"
    prepare(out, briefs)
    worker(out, 1)
    rows = batch.results_path(out, 1).read_text().splitlines()
    batch.results_path(out, 1).write_text("\n".join(rows[:-1]) + "\n")  # GPU ジョブが途中で止まり、1 件が戻らない
    report = ingest(out, briefs, 1)
    assert len(report.accepted) == 3 and len(report.lost) == 1
    assert prepare(out, briefs).requests == 0
    again = prepare(out, briefs, requeue_queued=True)
    assert again.requests == 1 and json.loads(batch.requests_path(out, 2).read_text())["attempt"] == 1  # 試行は数えない


def test_stale_results_are_ignored_when_the_bible_changed(tmp_path):
    briefs = sample_briefs(1)
    out = tmp_path / "out"
    prepare(out, briefs)
    worker(out, 1)
    changed = [b.__class__(**{**b.to_dict(), "key": "0" * 16}) for b in briefs]  # 計画の版が変わった
    report = ingest(out, changed, 1)
    assert len(report.ignored) == 4 and not report.accepted


def test_a_gpu_job_is_limited_in_requests_and_time(tmp_path):
    briefs = sample_briefs(2)
    out = tmp_path / "out"
    prepare(out, briefs, max_requests=8)
    with pytest.raises(WorkerLimitError):
        read_requests(batch.requests_path(out, 1), max_requests=7)
    # 期限: chunk の切れ目で止まり、そこまでの結果は残る
    ticks = iter(range(0, 1000, 10))
    requests = read_requests(batch.requests_path(out, 1), max_requests=8)
    meta = run_worker(
        FakeEngine(),
        requests,
        tmp_path / "r.jsonl",
        tmp_path / "m.json",
        deadline_seconds=75,
        chunk_size=2,
        clock=lambda: next(ticks),
    )
    assert meta["stopped"] == "deadline" and 0 < meta["completed"] < 8
    assert len((tmp_path / "r.jsonl").read_text().splitlines()) == meta["completed"]


def test_deadline_is_disabled_by_default_and_runs_every_request(tmp_path):
    briefs = sample_briefs(2)
    out = tmp_path / "out"
    prepare(out, briefs, max_requests=8)
    requests = read_requests(batch.requests_path(out, 1), max_requests=8)
    ticks = iter(range(0, 100000, 1000))  # 時計が大きく進んでも止まらない
    meta = run_worker(
        FakeEngine(),
        requests,
        tmp_path / "r.jsonl",
        tmp_path / "m.json",
        deadline_seconds=0,
        chunk_size=2,
        clock=lambda: next(ticks),
    )
    assert meta["stopped"] is None and meta["completed"] == len(requests)


def test_vllm_engine_passes_the_weight_loading_options_only_when_given():
    base = dict(
        max_model_len=8192,
        gpu_memory_utilization=0.9,
        max_num_seqs=4,
        quantization=None,
        dtype="auto",
        temperature=0.7,
        top_p=0.95,
        chat_template_kwargs=None,
    )
    plain = VllmEngine("/m", **base).describe()
    assert "load_format" not in plain and "model_loader_extra_config" not in plain and "quantization" not in plain
    streamed = VllmEngine(
        "/m",
        load_format="runai_streamer",
        model_loader_extra_config={"concurrency": 16, "memory_limit": 17179869184},
        **base,
    ).describe()
    assert streamed["load_format"] == "runai_streamer" and streamed["model_loader_extra_config"]["concurrency"] == 16


def test_prepare_cuts_requests_to_the_cost_cap(tmp_path):
    briefs = sample_briefs(2)
    out = tmp_path / "out"
    # $3/時、起動 900 秒の仮定（$0.75）+ 1 件あたり 3000 トークン ÷ 300 トークン/秒 = 10 秒（$0.0083）
    report = prepare(out, briefs, usd_per_hour=3.0, max_total_cost_usd=0.76, max_requests=8)
    assert 0 < report.requests < 8 and report.cut_by_limit
    with pytest.raises(BudgetExceeded):
        prepare(tmp_path / "out2", briefs, usd_per_hour=3.0, max_total_cost_usd=0.5)  # 起動の費用だけで超える


def test_spent_cost_counts_against_the_total_cap(tmp_path):
    briefs = sample_briefs(1)
    out = tmp_path / "out"
    prepare(out, briefs)
    worker(out, 1)
    ingest(out, briefs, 1, usd_per_hour=3.0, job_seconds=3600)  # $3 使った
    for b in briefs:
        Ledger(out / "ledger.jsonl").append(b.id, status="retry", key=b.key, attempts=1)
    with pytest.raises(BudgetExceeded):
        prepare(out, briefs, usd_per_hour=3.0, max_total_cost_usd=3.5, retry_failed=True)


def test_output_dir_inside_the_app_corpus_is_refused():
    repo = Path(__file__).resolve().parents[1]
    for sub in ("storage/pdf/x", "evals/corpora/cafe", "evals/fixtures/y", "app"):
        with pytest.raises(pipeline.UnsafeOutDir):
            pipeline.check_out_dir(repo / sub)
    pipeline.check_out_dir(repo / "evals" / "synth" / "out")  # 既定の出力先は許す


def test_ledger_survives_a_torn_last_line_and_locks(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    ledger.append("a", status="accepted", n=1)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"id": "b", "sta')  # 書きかけの末尾
    reloaded = Ledger(path)
    assert reloaded.get("a")["status"] == "accepted" and reloaded.get("b") == {}
    with exclusive(tmp_path / ".lock"):
        with pytest.raises(LockedError), exclusive(tmp_path / ".lock"):
            pass
    atomic_write(tmp_path / "x.txt", b"ok")
    assert (tmp_path / "x.txt").read_bytes() == b"ok" and not (tmp_path / "x.txt.tmp").exists()


def test_budget_reserves_before_calling_and_stops_at_the_cap():
    budget = Budget(Limits(max_docs=2, max_tokens=5000, max_cost_usd=1.0, price_out_per_m=100.0))
    budget.start_doc()
    r = budget.reserve(500, 3000, 10)
    with pytest.raises(BudgetExceeded) as exc:
        budget.reserve(500, 3000, 10)  # 予約中の分を足すと 7000 > 5000
    assert exc.value.which == "max-tokens"
    budget.settle(r, 400, 1000, 5)
    budget.start_doc()
    with pytest.raises(BudgetExceeded):
        budget.start_doc()  # 3 件目


class _ScriptedBackend:
    name, model = "scripted", "scripted"

    def __init__(self, texts):
        self.texts, self.calls = list(texts), 0

    def generate(self, system, user, *, seed, max_tokens):
        self.calls += 1
        text = self.texts.pop(0) if self.texts else "# 題\n\n本文"
        if text is None:
            raise BackendError("HTTP 500", retryable=True)
        return Completion(text, 100, 200)


def run_online(out, briefs, backend, **kw):
    limits = kw.pop("limits", Limits(max_docs=100, max_tokens=10**9, max_cost_usd=100.0, price_out_per_m=10.0))
    return pipeline.run(
        pipeline.RunConfig(out_dir=out, bibles=BIBLES, briefs=briefs, backend=backend, limits=limits, **kw)
    )


def test_online_run_accepts_with_fake_backend_and_resumes(tmp_path):
    briefs = sample_briefs(1)
    out = tmp_path / "out"
    report = run_online(out, briefs, FakeBackend(), concurrency=2)
    assert len(report.accepted) == 4 and not report.failed
    again = run_online(out, briefs, FakeBackend())
    assert len(again.already_done) == 4 and not again.accepted


def test_online_run_stops_at_max_docs_and_at_the_circuit_breaker(tmp_path):
    briefs = sample_briefs(2)
    limited = run_online(
        tmp_path / "a", briefs, FakeBackend(), limits=Limits(max_docs=3, max_tokens=10**9, max_cost_usd=10.0)
    )
    assert len(limited.accepted) == 3 and limited.stopped == "max-docs" and len(limited.not_started) == 5

    bad = _ScriptedBackend([])  # いつも本文が短すぎる
    broken = run_online(tmp_path / "b", briefs, bad, max_attempts=2, max_consecutive_failures=2)
    assert broken.stopped == "circuit-breaker" and len(broken.failed) == 2 and bad.calls == 4  # 2 件 × 2 試行で止まる


def test_online_run_retries_a_retryable_backend_error_and_gives_up_on_others(tmp_path):
    briefs = sample_briefs(1)[:1]
    ok = FakeBackend().generate(
        "", f"題名: {briefs[0].title}\n文字数: 1200〜2600\n- [x] 事実です。", seed=1, max_tokens=1
    )
    flaky = _ScriptedBackend([None, ok.text])  # 1 回目は 500、2 回目は成功（本文は検証で落ちるので失敗扱いでよい）
    run_online(tmp_path / "a", briefs, flaky, max_attempts=3)
    assert flaky.calls >= 2


def test_cost_estimate_scales_from_measurement():
    rounds = [
        {"round": 1, "requests": 8, "results": 8, "accepted": 6, "generate_seconds": 80.0, "load_seconds": 300.0,
         "job_seconds": 600.0, "job_seconds_source": "given", "prompt_tokens": 8000, "completion_tokens": 24000, "cost_usd": 0.5},
    ]  # fmt: skip
    m = measure.measure(rounds, accepted=7)
    assert m.overhead_seconds_per_round == 520 and m.gen_seconds_per_request == 10 and m.first_pass_rate == 0.75
    assert m.usd_per_accepted == pytest.approx(0.5 / 7) and m.includes_container_start
    e = measure.estimate(m, 1000, usd_per_hour=3.0, task_timeout_seconds=3000, max_attempts=2)
    # 1 回目: 1000 件 × 10 秒 = 10,000 秒 → 1 タスクの使える時間 2,480 秒で 5 つに分ける。2 回目: 250 件 → 2,500 秒 → 2 つ
    assert e.jobs == 7 and e.requests == 1250 and e.expected_accepted == pytest.approx(750 + 187.5)
    assert e.seconds == pytest.approx(7 * 520 + 12_500) and e.cost_usd == pytest.approx(e.seconds / 3600 * 3.0)
    faster = measure.estimate(m, 1000, usd_per_hour=3.0, task_timeout_seconds=3000, max_attempts=2, throughput_gain=2.0)
    assert faster.seconds < e.seconds
    with pytest.raises(ValueError):
        measure.estimate(m, 10, usd_per_hour=3.0, task_timeout_seconds=500, max_attempts=2)
    assert measure.gemini_batch_cost(m, 1000, price_in_per_m=0.15, price_out_per_m=1.25) == pytest.approx(
        1000 * (1000 * 0.15 + 3000 * 1.25) / 1e6
    )
    assert "合格 PDF あたり" in measure.format_report(
        m,
        usd_per_hour=3.0,
        task_timeout_seconds=3000,
        max_attempts=2,
        docs=[80, 1000],
        gains=[1.0],
        gemini_prices=(0.15, 1.25),
    )


def test_the_worker_cli_runs_with_the_fake_engine(tmp_path):
    from evals.synth import gpu_worker

    briefs = sample_briefs(1)
    out = tmp_path / "out"
    prepare(out, briefs)
    code = gpu_worker.main(
        ["--engine", "fake", "--requests", str(batch.requests_path(out, 1)), "--out", str(batch.results_path(out, 1)),
         "--meta", str(batch.meta_path(out, 1)), "--max-requests", "4"]
    )  # fmt: skip
    assert code == 0 and len(ingest(out, briefs, 1).accepted) == 4
    too_many = gpu_worker.main(
        [
            "--engine",
            "fake",
            "--requests",
            str(batch.requests_path(out, 1)),
            "--out",
            str(tmp_path / "o"),
            "--meta",
            str(tmp_path / "m"),
            "--max-requests",
            "3",
        ]
    )
    assert too_many == 2 and not (tmp_path / "o").exists()


class _FailingEngine(FakeEngine):
    """2 回目の生成でメモリ不足になるエンジン（OOM の代わり）。"""

    calls = 0

    def generate(self, batch_):
        self.calls += 1
        if self.calls == 2:
            raise MemoryError("secret path /models/x")  # メッセージは meta に残さない
        return super().generate(batch_)


def test_worker_records_only_the_error_type_and_keeps_finished_results(tmp_path):
    briefs = sample_briefs(1)
    out = tmp_path / "out"
    prepare(out, briefs)
    requests = read_requests(batch.requests_path(out, 1), max_requests=8)
    meta = run_worker(
        _FailingEngine(), requests, tmp_path / "r.jsonl", tmp_path / "m.json", deadline_seconds=1e9, chunk_size=2
    )
    assert (meta["stopped"], meta["error"], meta["completed"]) == ("error", "MemoryError", 2)
    assert len((tmp_path / "r.jsonl").read_text().splitlines()) == 2  # 書き終えた分は残る
    assert "secret" not in (tmp_path / "m.json").read_text()


def make_trial(out: Path, briefs, label: str, concurrency: int, engine: FakeEngine | None = None, chunk_size: int = 0):
    requests = read_requests(bench.requests_file(out), max_requests=100)
    return run_worker(
        engine or FakeEngine(concurrency), requests, bench.results_file(out, label), bench.meta_file(out, label),
        deadline_seconds=1e9, chunk_size=chunk_size,
    )  # fmt: skip


def bench_ingest(out, briefs, label, **kw):
    return bench.evaluate_trial(
        out_dir=out, bibles=BIBLES, briefs=briefs, label=label, usd_per_hour=3.0, dedupe_threshold=0.5, reference_texts={},
        **({"job_seconds": None} | kw),
    )  # fmt: skip


def test_bench_compares_concurrency_on_the_same_input_and_picks_the_lowest_that_is_close_to_best(tmp_path):
    briefs = sample_briefs(4)  # 16 件
    out = tmp_path / "out"
    requests = [batch.make_request(BIBLES, b, 1, 3000) for b in briefs]
    bench.prepare_bench(out, requests)
    for label, c in (("c04", 4), ("c08", 8), ("c16", 16)):
        make_trial(out, briefs, label, c)
    # 実行の時間（起動込み）だけが違う。c16 は c08 より 2% 速い（費用の差が 5% 以内 → 同時数の小さい c08）
    bench_ingest(out, briefs, "c04", job_seconds=1200.0)
    bench_ingest(out, briefs, "c08", job_seconds=900.0)
    r16 = bench_ingest(out, briefs, "c16", job_seconds=880.0)
    assert r16["requests"] == 16 and r16["concurrency"] == 16 and r16["valid"] + r16["invalid"] == 16

    trials = bench.latest_trials(out)
    assert [t["concurrency"] for t in trials] == [4, 8, 16] and len({t["requests_sha256"] for t in trials}) == 1
    pick, _ = bench.recommend(trials)
    assert pick == "c08"
    pick, _ = bench.recommend(trials, tolerance=0.0)
    assert pick == "c16"  # 許容を 0 にすれば、最安の c16
    report = bench.format_report(trials)
    assert "推奨: **c08**" in report and "警告" not in report and "合格1件あたり" in report

    # 同じラベルを取り込み直すと、最後の記録だけが使われる
    bench_ingest(out, briefs, "c16", job_seconds=2000.0)
    assert [t["job_seconds"] for t in bench.latest_trials(out)][-1] == 2000.0


def test_bench_excludes_a_trial_that_ran_out_of_memory_and_goes_back_down(tmp_path):
    briefs = sample_briefs(4)
    out = tmp_path / "out"
    bench.prepare_bench(out, [batch.make_request(BIBLES, b, 1, 3000) for b in briefs])
    make_trial(out, briefs, "c04", 4)
    make_trial(out, briefs, "c16", 16, engine=_FailingEngine(16), chunk_size=4)  # 2 回目の chunk でメモリ不足
    bench_ingest(out, briefs, "c04", job_seconds=1200.0)
    oom = bench_ingest(out, briefs, "c16", job_seconds=600.0)  # 安く見えても、エラー・欠落があれば除く
    assert oom["error"] == "MemoryError" and oom["missing"] > 0
    pick, why = bench.recommend(bench.latest_trials(out))
    assert pick == "c04" and "除外: c16" in why


def test_bench_refuses_a_changed_input_and_warns_on_mixed_inputs(tmp_path):
    briefs = sample_briefs(1)
    out = tmp_path / "out"
    bench.prepare_bench(out, [batch.make_request(BIBLES, b, 1, 3000) for b in briefs])
    bench.prepare_bench(out, [batch.make_request(BIBLES, b, 1, 3000) for b in briefs])  # 同じ入力なら何度でも
    with pytest.raises(bench.BenchError):
        bench.prepare_bench(out, [batch.make_request(BIBLES, b, 1, 2000) for b in briefs])  # 出力の上限が違う
    make_trial(out, briefs, "a", 4)
    bench_ingest(out, briefs, "a")
    bench.prepare_bench(out, [batch.make_request(BIBLES, b, 1, 2000) for b in briefs], force=True)
    make_trial(out, briefs, "b", 8)
    bench_ingest(out, briefs, "b")
    assert "警告" in bench.format_report(bench.latest_trials(out))
