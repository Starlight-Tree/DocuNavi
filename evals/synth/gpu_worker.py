"""GPU ジョブの本体。依頼ファイルを読み、モデルを 1 回だけ読み込み、全部生成して、結果を書いて終了する（オフライン一括推論）。

Cloud Run Jobs の 1 つのタスクとして動かす想定（`evals/synth/gpu/README.md`）。GPU は 1 台・1 タスク。
**検証・PDF 化・重複判定はしない**（CPU 側。batch.py / bench.py）。標準ライブラリだけで書いてある
（vLLM は `--engine vllm` のとき、読み込みの時間を測る位置で import する）。コンテナには、このファイル 1 つを入れればよい。

**キューの動き:** 全依頼を 1 回で vLLM に渡す（`--chunk-size 0` が既定）。vLLM は、同時に走らせる系列を `--max-num-seqs`
（= 同時数）までに保ち、1 件が終わるたびに、待っている次の依頼を空いた枠へ入れる（連続バッチ）。同時数は、同じ入力で
4・8・16 を比べて決める（bench.py）。`--chunk-size N` を指定すると、N 件ずつ渡して、切れ目ごとに結果を書く
（期限の確認と、途中の書き出しができるが、切れ目で最後の 1 件を待つ分だけ効率が落ちる）。

制限（GPU の時間を使い切らないため。ジョブ側の `--task-timeout` `--parallelism=1` `--max-retries=0` と重ねて使う）:
- `--max-requests`: 依頼がこれを超えていたら、何も生成せずに止まる（exit 2）。
- `--deadline-seconds`: プロセス開始からの上限。既定は 0（無効。全件が終わるまで続ける）。正の値のときだけ、chunk の切れ目で確かめ、
  超えたら、そこまでの結果を残して止まる（exit 0・meta の stopped）。読み込みの時間も数える（2026-11-04 の初回試験は、読み込みだけで 2100 秒を超えて 0 件で止まった）。
- 実行中の例外（メモリ不足など）は、型の名前だけを meta に残して止まる（exit 3）。書き終えた結果は残る。同時数を下げた再実行は、新しいジョブとして出す。

使い方（ローカルの確認は `--engine fake`。GPU・vLLM は要らない）:
    python -m evals.synth.gpu_worker --engine fake --requests R --out O --meta M
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Protocol


class Engine(Protocol):
    model: str

    def describe(self) -> dict[str, Any]: ...

    def load(self) -> None: ...

    def generate(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """依頼の列 → 同じ順の結果の列（text, finish_reason, prompt_tokens, completion_tokens, 任意で latency_seconds）。"""
        ...


class FakeEngine:
    model = "fake-deterministic"

    def __init__(self, max_num_seqs: int = 1) -> None:
        self.max_num_seqs = max_num_seqs

    def describe(self) -> dict[str, Any]:
        return {"engine": "fake", "max_num_seqs": self.max_num_seqs}

    def load(self) -> None:
        from evals.synth.backend import FakeBackend

        self._backend = FakeBackend()

    def generate(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for req in batch:
            c = self._backend.generate(req["system"], req["user"], seed=req["seed"], max_tokens=req["max_tokens"])
            out.append(
                {
                    "text": c.text,
                    "finish_reason": c.finish_reason,
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                }
            )
        return out


class VllmEngine:
    """vLLM のオフライン推論（`LLM.chat`）。実機での実行は未確認（API の細部は、使う版の文書で確かめる）。"""

    def __init__(
        self,
        model: str,
        *,
        max_model_len: int,
        gpu_memory_utilization: float,
        max_num_seqs: int,
        quantization: str | None,
        dtype: str,
        temperature: float,
        top_p: float,
        chat_template_kwargs: dict[str, Any] | None,
        load_format: str | None = None,
        model_loader_extra_config: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self._opts: dict[str, Any] = {
            "model": model,
            "max_model_len": max_model_len,
            "gpu_memory_utilization": gpu_memory_utilization,
            "max_num_seqs": max_num_seqs,  # 同時に走らせる系列の数（= 同時数）
            "dtype": dtype,
            "tensor_parallel_size": 1,  # 1 台に GPU は 1 枚（Cloud Run の上限）
        }
        if quantization:
            self._opts["quantization"] = quantization
        if load_format:
            self._opts["load_format"] = load_format  # 例: runai_streamer（重みの並列読み込み）
        if model_loader_extra_config:
            self._opts["model_loader_extra_config"] = model_loader_extra_config
        self._temperature, self._top_p = temperature, top_p
        self._chat_kwargs = chat_template_kwargs

    def describe(self) -> dict[str, Any]:
        opts = {k: v for k, v in self._opts.items() if k != "model"}
        return {"engine": "vllm", "temperature": self._temperature, "top_p": self._top_p, **opts}

    def load(self) -> None:
        from vllm import LLM  # 読み込みの時間に import も含める

        self._llm = LLM(**self._opts)

    def generate(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from vllm import SamplingParams

        params = [
            SamplingParams(temperature=self._temperature, top_p=self._top_p, max_tokens=r["max_tokens"], seed=r["seed"])
            for r in batch
        ]
        messages = [[{"role": "system", "content": r["system"]}, {"role": "user", "content": r["user"]}] for r in batch]
        kwargs: dict[str, Any] = {"use_tqdm": False}
        if self._chat_kwargs:
            kwargs["chat_template_kwargs"] = self._chat_kwargs
        outputs = self._llm.chat(messages, params, **kwargs)
        results = []
        for o in outputs:
            row = {
                "text": o.outputs[0].text,
                "finish_reason": o.outputs[0].finish_reason or "stop",
                "prompt_tokens": len(o.prompt_token_ids),
                "completion_tokens": len(o.outputs[0].token_ids),
            }
            m = getattr(o, "metrics", None)  # 版によっては無い。あれば、1 件ごとの所要時間を残す
            if m is not None and getattr(m, "finished_time", None) and getattr(m, "arrival_time", None):
                row["latency_seconds"] = float(m.finished_time - m.arrival_time)
            results.append(row)
        return results


class WorkerLimitError(Exception):
    pass


def read_requests(path: Path, max_requests: int) -> list[dict[str, Any]]:
    requests = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            requests.append(json.loads(line))
    if len(requests) > max_requests:
        raise WorkerLimitError(f"依頼が {len(requests)} 件あり、上限 {max_requests} 件を超えています")
    return requests


def run_worker(
    engine: Engine,
    requests: list[dict[str, Any]],
    out_path: Path,
    meta_path: Path,
    *,
    deadline_seconds: float,
    chunk_size: int = 0,
    clock=time.monotonic,
    started: float | None = None,
) -> dict[str, Any]:
    t0 = clock() if started is None else started
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("", encoding="utf-8")
    size = chunk_size if chunk_size > 0 else max(len(requests), 1)
    meta: dict[str, Any] = {
        "model": engine.model,
        "engine": engine.describe(),
        "requests": len(requests),
        "chunk_size": size,
        "completed": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "stopped": None,
        "error": None,
        "load_seconds": None,
        "generate_seconds": 0.0,
    }
    try:
        t_load = clock()
        engine.load()
        meta["load_seconds"] = clock() - t_load
        done = 0
        for i in range(0, len(requests), size):
            if deadline_seconds > 0 and clock() - t0 > deadline_seconds:
                meta["stopped"] = "deadline"
                break
            chunk = requests[i : i + size]
            t_gen = clock()
            results = engine.generate(chunk)
            meta["generate_seconds"] += clock() - t_gen
            with open(out_path, "a", encoding="utf-8") as f:
                for req, res in zip(chunk, results, strict=True):
                    f.write(json.dumps({"id": req["id"], "attempt": req["attempt"], **res}, ensure_ascii=False) + "\n")
                    meta["prompt_tokens"] += int(res.get("prompt_tokens", 0))
                    meta["completion_tokens"] += int(res.get("completion_tokens", 0))
                f.flush()
                os.fsync(f.fileno())
            done += len(chunk)
            meta["completed"] = done
    except Exception as exc:  # メモリ不足など。型の名前だけ残し、メッセージ・パスは残さない
        meta["stopped"], meta["error"] = "error", type(exc).__name__
    meta["total_seconds"] = clock() - t0
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", choices=["fake", "vllm"], required=True)
    p.add_argument("--requests", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--meta", type=Path, required=True)
    p.add_argument("--max-requests", type=int, default=16, help="これを超える依頼は受け付けない（GPU の生成量の上限）")
    p.add_argument(
        "--deadline-seconds",
        type=float,
        default=0.0,
        help="プロセス開始からの上限。0 = 無効（既定。全件が終わるまで続ける。上限はジョブの task-timeout だけ）",
    )
    p.add_argument("--chunk-size", type=int, default=0, help="0 = 全依頼を 1 回で渡す（キュー）。N = N 件ずつ")
    p.add_argument("--model", default="", help="vllm のとき必須（ローカルのパスか、モデル名）")
    p.add_argument("--max-model-len", type=int, default=8192)
    p.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    p.add_argument(
        "--max-num-seqs", type=int, default=4, help="同時数（vLLM が同時に走らせる系列の数）。4・8・16 を比べる"
    )
    p.add_argument("--quantization", default=None)
    p.add_argument("--dtype", default="auto")
    p.add_argument("--load-format", default=None, help="vLLM の load_format。例: runai_streamer（重みの並列読み込み）")
    p.add_argument(
        "--model-loader-extra-config", default=None, help='JSON。例: \'{"concurrency":16,"memory_limit":17179869184}\''
    )
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--chat-template-kwargs", default=None, help="JSON。例: '{\"enable_thinking\": false}'")
    args = p.parse_args(argv)

    try:
        requests = read_requests(args.requests, args.max_requests)
    except WorkerLimitError as exc:
        print(f"中止: {exc}", file=sys.stderr)
        return 2
    if args.engine == "fake":
        engine: Engine = FakeEngine(args.max_num_seqs)
    else:
        if not args.model:
            print("--model が要ります", file=sys.stderr)
            return 2
        engine = VllmEngine(
            args.model,
            max_model_len=args.max_model_len,
            gpu_memory_utilization=args.gpu_memory_utilization,
            max_num_seqs=args.max_num_seqs,
            quantization=args.quantization,
            dtype=args.dtype,
            temperature=args.temperature,
            top_p=args.top_p,
            chat_template_kwargs=json.loads(args.chat_template_kwargs) if args.chat_template_kwargs else None,
            load_format=args.load_format,
            model_loader_extra_config=json.loads(args.model_loader_extra_config)
            if args.model_loader_extra_config
            else None,
        )
    meta = run_worker(
        engine,
        requests,
        args.out,
        args.meta,
        deadline_seconds=args.deadline_seconds,
        chunk_size=args.chunk_size,
        started=started,
    )
    load = f"{meta['load_seconds']:.1f}s" if meta["load_seconds"] is not None else "—"
    print(
        f"完了: {meta['completed']}/{meta['requests']} 件、読み込み {load}、生成 {meta['generate_seconds']:.1f}s、"
        f"合計 {meta['total_seconds']:.1f}s、停止 {meta['stopped']}、エラー {meta['error']}"
    )
    return 3 if meta["stopped"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
