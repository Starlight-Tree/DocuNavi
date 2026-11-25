"""Python の API と Go の読み取り API の応答時間を、同じ DB・同じ要求で並べて測る（ローカル専用）。

  READ_API_CMP_DATABASE_URL=postgresql://…/readapi_cmp  python ops/read-api/testdata/bench.py <Go のバイナリ>

条件: 認証 off、ACCESS_MODE は off と enforce（TTL 60 秒の控えあり）、両方とも接続プール上限 2。要求は keep-alive の接続で送る。
経路ごとに、同時 1（直列 N 回）と同時 20（スレッド 20 本で N 回）の p50 / p90 / p99（ミリ秒）と、起動から最初の 200 までの時間（起動 5 回の中央値）を出す。
測る側（このスクリプト）が Python なので、同時 20 の数字は測る側の上限に当たりうる。絶対値ではなく、同じ条件の比較として読む。
本番（Cloud Run → Prisma Postgres）の数字ではない。起動した 2 つは、PID で止める。
"""

import http.client
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PATHS = [
    "/health",
    "/collections",
    "/targets",
    "/doc-types",
    "/documents",
    "/documents/1/chunks",
]  # 1 = seed.sql の cafe_a.pdf（本文 3 チャンク）
WARMUP, SERIAL, CONCURRENT, THREADS = 100, 1000, 4000, 20
PORTS = {"python": 18000, "go": 18080}


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def hit(conn: http.client.HTTPConnection, path: str) -> float:
    start = time.perf_counter()
    conn.request("GET", path)
    response = conn.getresponse()
    response.read()
    elapsed = (time.perf_counter() - start) * 1000
    if response.status != 200:
        raise SystemExit(f"{path}: {response.status}")
    return elapsed


def serial(port: int, path: str, count: int) -> list[float]:
    conn = http.client.HTTPConnection("127.0.0.1", port)
    return [hit(conn, path) for _ in range(count)]


def concurrent(port: int, path: str, count: int) -> tuple[list[float], float]:
    results: list[float] = []
    lock = threading.Lock()
    per_thread = count // THREADS

    def worker() -> None:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        local = [hit(conn, path) for _ in range(per_thread)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, len(results) / (time.perf_counter() - start)


def spawn(kind: str, env: dict[str, str], go_bin: str) -> subprocess.Popen:
    port = str(PORTS[kind])
    if kind == "python":
        return subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "app.main:app", "--port", port, "--log-level", "warning"],
            cwd=ROOT,
            env={**env, "DB_BACKEND": "pg", "GEMINI_API_KEY": ""},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return subprocess.Popen([go_bin], env={**env, "PORT": port}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def first_ok(port: int, path: str) -> None:
    while True:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", path)
            if conn.getresponse().status == 200:
                return
        except OSError:
            time.sleep(0.005)


def main() -> int:
    go_bin = sys.argv[1]
    base = {
        **os.environ,
        "DATABASE_URL": os.environ["READ_API_CMP_DATABASE_URL"],
        "AUTH_MODE": "off",
        "DATABASE_POOL_MAX_SIZE": "2",
    }
    print("起動から最初の /collections の 200 まで（5 回の中央値、秒）")
    for kind in PORTS:
        times = []
        for _ in range(5):
            env = {**base, "ACCESS_MODE": "off"}
            start = time.perf_counter()
            proc = spawn(kind, env, go_bin)
            try:
                first_ok(PORTS[kind], "/collections")
                times.append(time.perf_counter() - start)
            finally:
                proc.terminate()
                proc.wait(timeout=15)
        print(f"  {kind:6s} {statistics.median(times):7.3f}  (最小 {min(times):.3f} / 最大 {max(times):.3f})")
    for access in ("off", "enforce"):
        print(f"\n=== ACCESS_MODE={access}（ms。左が同時 1、右が同時 {THREADS}）")
        print(
            f"  {'経路':13s} {'実装':7s} | {'p50':>7s} {'p90':>7s} {'p99':>7s} | {'p50':>7s} {'p90':>7s} {'p99':>7s} {'req/s':>8s}"
        )
        procs = {kind: spawn(kind, {**base, "ACCESS_MODE": access}, go_bin) for kind in PORTS}
        try:
            for kind in PORTS:
                first_ok(PORTS[kind], "/health")
            for path in PATHS:
                for kind in PORTS:
                    port = PORTS[kind]
                    serial(port, path, WARMUP)
                    s = serial(port, path, SERIAL)
                    c, rps = concurrent(port, path, CONCURRENT)
                    print(
                        f"  {path:13s} {kind:7s} | {percentile(s, 0.5):7.2f} {percentile(s, 0.9):7.2f} {percentile(s, 0.99):7.2f} |"
                        f" {percentile(c, 0.5):7.2f} {percentile(c, 0.9):7.2f} {percentile(c, 0.99):7.2f} {rps:8.0f}"
                    )
        finally:
            for proc in procs.values():
                proc.terminate()
            for proc in procs.values():
                proc.wait(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
