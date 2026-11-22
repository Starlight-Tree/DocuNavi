"""生成した本文の受け入れ（検証・重複防止・保存・台帳）と、PDF 化・マニフェスト。CPU 側の処理で、GPU には触れない。

本文の入手には 2 つの経路があり、受け入れ（`Acceptor`）は共通:
- 一括（batch.py）: GPU ジョブが書いた結果ファイルを取り込む。**第一候補**（モデルを 1 回読み込み、複数件を生成して終了する）。
- 逐次（`run`）: `Backend`（fake・OpenAI 互換のエンドポイント）を 1 件ずつ呼ぶ。配線の確認と、常設エンドポイントを使う場合用。

- **再開:** 受け入れ済み（key と本文のハッシュが一致）の依頼票は飛ばす。同じコマンドで続きから進む。
- **重複防止:** 同じ key は作り直さない。bible・指示の版が変わった受け入れ済みは `stale` として報告し、`regenerate_stale` のときだけ作り直す。
  本文は、受け入れ済み・参照資料との類似度（Jaccard）が閾値以上なら落とす。
- 出力先は、アプリの資料・評価の資料の置き場所（storage/ evals/corpora/ ほか）の中に置けない（`check_out_dir`）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.synth.backend import Backend, BackendError, estimate_tokens
from evals.synth.bible import Bible
from evals.synth.budget import Budget, BudgetExceeded, Limits
from evals.synth.ledger import Ledger, atomic_write, exclusive
from evals.synth.plan import Brief
from evals.synth.prompt import PROMPT_VERSION, build_messages, prompt_sha256
from evals.synth.validate import Issue, find_duplicate, shingles, validate_body

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_OUT = ("storage", "evals/corpora", "evals/fixtures", "app", "frontend", "tests", "docs")
COLLECTION = "synthetic-test"  # 将来、本番へ入れるときの collection の名前（今は入れない。README の「分離」）


class UnsafeOutDir(ValueError):
    pass


def check_out_dir(out_dir: Path, repo_root: Path = REPO_ROOT) -> Path:
    resolved = out_dir.resolve()
    for name in FORBIDDEN_OUT:
        if resolved.is_relative_to((repo_root / name).resolve()):
            raise UnsafeOutDir(f"出力先を {name}/ の中に置けません（本番の検索に混ざるのを防ぐ）: {out_dir}")
    return resolved


def md_path(out_dir: Path, target: str, doc_id: str) -> Path:
    return out_dir / "md" / target / f"{doc_id}.md"


def pdf_path(out_dir: Path, target: str, doc_id: str) -> Path:
    return out_dir / "pdf" / target / f"{doc_id}.pdf"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def total_spent_usd(out_dir: Path, ledger: Ledger) -> float:
    """これまでの費用の合計（逐次の資料ごとの費用 + 一括の実行ごとの費用）。"""
    rounds = out_dir / "rounds.jsonl"
    spent = ledger.total_cost_usd()
    if rounds.exists():
        for line in rounds.read_text(encoding="utf-8").splitlines():
            try:
                spent += float(json.loads(line).get("cost_usd", 0.0))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    return spent


def reference_shingles(reference_texts: dict[str, str]) -> dict[str, set[str]]:
    return {k: shingles(v) for k, v in reference_texts.items()}


def classify(brief: Brief, ledger: Ledger, out_dir: Path, *, retry_failed: bool, regenerate_stale: bool) -> str:
    """'todo' | 'done' | 'stale' | 'failed' | 'queued'"""
    state = ledger.get(brief.id)
    status = state.get("status")
    if status == "accepted":
        if state.get("key") != brief.key:
            return "todo" if regenerate_stale else "stale"
        path = md_path(out_dir, brief.target, brief.id)
        if path.exists() and sha256_bytes(path.read_bytes()) == state.get("md_sha256"):
            return "done"
        return "todo"  # 本文が消えた・変わった
    if state.get("key") not in (None, brief.key):
        return "todo"  # 古い版の途中経過は引き継がない
    if status == "failed" and not retry_failed:
        return "failed"
    if status == "queued":
        return "queued"
    return "todo"


class Acceptor:
    """1 件の本文を検証し、受け入れるなら保存して台帳に書く。スレッドから呼んでよい。"""

    def __init__(
        self,
        out_dir: Path,
        bibles: dict[str, Bible],
        ledger: Ledger,
        seen: dict[str, set[str]],
        *,
        threshold: float,
        backend_name: str,
        model: str,
    ) -> None:
        self.out_dir = out_dir
        self.bibles = bibles
        self.ledger = ledger
        self.seen = seen
        self.threshold = threshold
        self.backend_name = backend_name
        self.model = model
        self._lock = threading.Lock()

    def forget(self, doc_id: str) -> None:
        with self._lock:
            self.seen.pop(doc_id, None)

    def judge(
        self,
        brief: Brief,
        *,
        attempt: int,
        text: str,
        finish_reason: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        system: str,
        user: str,
    ) -> list[Issue]:
        bible = self.bibles[brief.target]
        kind = next(k for k in bible.kinds if k.id == brief.kind)
        issues = validate_body(text, bible, min_chars=kind.min_chars, max_chars=kind.max_chars)
        if finish_reason == "length":
            issues.append(Issue("truncated", "出力の上限に達して打ち切られました"))
        if not issues:
            sig = shingles(text)
            with self._lock:
                dup = find_duplicate(sig, self.seen, self.threshold)
                if dup:
                    issues.append(Issue("duplicate", f"{dup[0]} と似すぎています（{dup[1]:.2f}）"))
                else:
                    self.seen[brief.id] = sig  # 受け入れの予約（並行する他の資料との重複を防ぐ）
        if issues:
            self.ledger.append(
                brief.id,
                status="retry",
                key=brief.key,
                target=brief.target,
                attempts=attempt,
                cost_usd=cost_usd,
                last_issues=sorted({i.code for i in issues}),
            )
            return issues
        body = text.strip() + "\n"
        path = md_path(self.out_dir, brief.target, brief.id)
        atomic_write(path, body.encode())
        self.ledger.append(
            brief.id,
            status="accepted",
            key=brief.key,
            target=brief.target,
            kind=brief.kind,
            doc_type=brief.doc_type,
            variant=brief.variant,
            title=brief.title,
            seed=brief.seed + attempt - 1,
            attempts=attempt,
            md=str(path.relative_to(self.out_dir)),
            md_sha256=sha256_bytes(body.encode()),
            chars=len(body),
            backend=self.backend_name,
            model=self.model,
            prompt_version=PROMPT_VERSION,
            prompt_sha256=prompt_sha256(system, user),
            bible_version=bible.version,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            last_issues=[],
            pdf=None,
            pdf_sha256=None,
            pdf_md_sha256=None,
        )
        return []


@dataclass
class RunConfig:
    out_dir: Path
    bibles: dict[str, Bible]
    briefs: list[Brief]
    backend: Backend
    limits: Limits
    max_attempts: int = 3
    max_tokens_per_doc: int = 3000
    reserve_seconds: float = 300.0
    concurrency: int = 1
    dedupe_threshold: float = 0.5
    max_consecutive_failures: int = 5
    retry_failed: bool = False
    regenerate_stale: bool = False
    reference_texts: dict[str, str] = field(default_factory=dict)


@dataclass
class RunReport:
    accepted: list[str] = field(default_factory=list)
    already_done: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    skipped_failed: list[str] = field(default_factory=list)
    skipped_queued: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    not_started: list[str] = field(default_factory=list)
    stopped: str | None = None
    tokens: int = 0
    cost_usd: float = 0.0


class _Stop:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.reason: str | None = None
        self._lock = threading.Lock()

    def set(self, reason: str) -> None:
        with self._lock:
            if self.reason is None:
                self.reason = reason
        self.event.set()


def run(config: RunConfig) -> RunReport:
    """逐次の経路。依頼票ごとに、受け入れるか `max_attempts` に達するまで Backend を呼ぶ。"""
    out_dir = check_out_dir(config.out_dir)
    report = RunReport()
    with exclusive(out_dir / ".lock"):
        ledger = Ledger(out_dir / "ledger.jsonl")
        budget = Budget(config.limits, spent_before_usd=total_spent_usd(out_dir, ledger))
        seen = reference_shingles(config.reference_texts)
        for state in ledger.state.values():
            if state.get("status") == "accepted" and state.get("md") and (out_dir / state["md"]).exists():
                seen[state["id"]] = shingles((out_dir / state["md"]).read_text(encoding="utf-8"))
        acceptor = Acceptor(
            out_dir,
            config.bibles,
            ledger,
            seen,
            threshold=config.dedupe_threshold,
            backend_name=config.backend.name,
            model=config.backend.model,
        )

        todo: list[Brief] = []
        for brief in config.briefs:
            kind = classify(
                brief, ledger, out_dir, retry_failed=config.retry_failed, regenerate_stale=config.regenerate_stale
            )
            bucket = {
                "done": report.already_done,
                "stale": report.stale,
                "failed": report.skipped_failed,
                "queued": report.skipped_queued,
            }.get(kind)
            if bucket is not None:
                bucket.append(brief.id)
            else:
                todo.append(brief)
                acceptor.forget(brief.id)  # 作り直す本文は、重複の比較から外す

        stop = _Stop()
        lock = threading.Lock()
        consecutive = [0]

        def process(brief: Brief) -> str:
            if stop.event.is_set():
                return "not_started"
            try:
                budget.start_doc()
            except BudgetExceeded as exc:
                stop.set(exc.which)
                return "not_started"
            system, user = build_messages(config.bibles[brief.target], brief)
            doc_cost = 0.0
            issues: list[Issue] = []
            for attempt in range(1, config.max_attempts + 1):
                if stop.event.is_set():
                    return "not_started"
                try:
                    reservation = budget.reserve(
                        estimate_tokens(system + user), config.max_tokens_per_doc, config.reserve_seconds
                    )
                except BudgetExceeded as exc:
                    stop.set(exc.which)
                    return "not_started"
                started = time.monotonic()
                try:
                    completion = config.backend.generate(
                        system, user, seed=brief.seed + attempt - 1, max_tokens=config.max_tokens_per_doc
                    )
                except BackendError as exc:
                    doc_cost += budget.settle(reservation, 0, 0, time.monotonic() - started)
                    issues = [Issue("backend", str(exc))]
                    ledger.append(
                        brief.id, status="retry", key=brief.key, target=brief.target, attempts=attempt,
                        cost_usd=doc_cost, last_issues=["backend"],
                    )  # fmt: skip
                    if not exc.retryable:
                        break
                    continue
                doc_cost += budget.settle(
                    reservation, completion.prompt_tokens, completion.completion_tokens, time.monotonic() - started
                )
                issues = acceptor.judge(
                    brief,
                    attempt=attempt,
                    text=completion.text,
                    finish_reason=completion.finish_reason,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    cost_usd=doc_cost,
                    system=system,
                    user=user,
                )
                if not issues:
                    with lock:
                        consecutive[0] = 0
                    return "accepted"
            ledger.append(
                brief.id,
                status="failed",
                key=brief.key,
                target=brief.target,
                attempts=config.max_attempts,
                cost_usd=doc_cost,
                last_issues=sorted({i.code for i in issues}),
            )
            acceptor.forget(brief.id)
            with lock:
                consecutive[0] += 1
                if consecutive[0] >= config.max_consecutive_failures:
                    stop.set("circuit-breaker")
            return "failed"

        with ThreadPoolExecutor(max_workers=max(config.concurrency, 1)) as pool:
            results = list(pool.map(process, todo))
        buckets = {"accepted": report.accepted, "failed": report.failed, "not_started": report.not_started}
        for brief, result in zip(todo, results, strict=True):
            buckets[result].append(brief.id)
        report.stopped = stop.reason
        report.tokens = budget.tokens
        report.cost_usd = budget.cost_usd
    return report


Renderer = Callable[[str, Path, str, str], None]  # (本文, 出力先, 資料の id, 題名)


def render_pending(out_dir: Path, renderer: Renderer) -> list[str]:
    """受け入れ済みで、PDF が無い・本文と合わない資料を PDF にする。作った資料の id を返す。"""
    out_dir = check_out_dir(out_dir)
    done: list[str] = []
    with exclusive(out_dir / ".lock"):
        ledger = Ledger(out_dir / "ledger.jsonl")
        for doc_id, state in sorted(ledger.state.items()):
            if state.get("status") != "accepted":
                continue
            text_path = out_dir / state["md"]
            if not text_path.exists():
                continue
            target = pdf_path(out_dir, state["target"], doc_id)
            md_hash = sha256_bytes(text_path.read_bytes())
            current = target.exists() and state.get("pdf_sha256") == sha256_bytes(target.read_bytes())
            if state.get("pdf_md_sha256") == md_hash and current:
                continue
            renderer(text_path.read_text(encoding="utf-8"), target, doc_id, state["title"])
            ledger.append(
                doc_id,
                pdf=str(target.relative_to(out_dir)),
                pdf_sha256=sha256_bytes(target.read_bytes()),
                pdf_md_sha256=md_hash,
            )
            done.append(doc_id)
    return done


def build_manifest(out_dir: Path, classify: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """分類マニフェスト。PDF まで済んだ資料だけを、id の順に出す（日時を入れない。同じ状態から同じ内容になる）。

    `classify` は、bible の識別名 → DB の対象（`classify_as`）。無い識別名は、そのまま対象にする。元の識別名は `bible_target` に残す。
    """
    classify = classify or {}
    ledger = Ledger(out_dir / "ledger.jsonl")
    rows = []
    for doc_id, s in sorted(ledger.state.items()):
        if s.get("status") != "accepted" or not s.get("pdf"):
            continue
        rows.append(
            {
                "id": doc_id,
                "source": Path(s["pdf"]).name,
                "path": s["pdf"],
                "target": classify.get(s["target"], s["target"]),
                "bible_target": s["target"],
                "doc_type": s["doc_type"],
                "collection": COLLECTION,
                "title": s["title"],
                "kind": s["kind"],
                "variant": s["variant"],
                "synthetic": True,
                "sha256": s["pdf_sha256"],
                "md_sha256": s["md_sha256"],
                "chars": s["chars"],
                "backend": s["backend"],
                "model": s["model"],
                "prompt_version": s["prompt_version"],
                "prompt_sha256": s["prompt_sha256"],
                "bible_version": s["bible_version"],
                "key": s["key"],
                "seed": s["seed"],
            }
        )
    return rows


def write_manifest(out_dir: Path, classify: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    out_dir = check_out_dir(out_dir)
    rows = build_manifest(out_dir, classify)
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    atomic_write(out_dir / "manifest.jsonl", data.encode())
    return rows
