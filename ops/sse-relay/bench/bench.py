"""SSE 中継の測定（開発コンテナの 127.0.0.1 だけ。外部通信なし・課金なし）。

    cd ops/sse-relay && PATH=$PATH:/usr/local/go/bin go build -o /tmp/sse-relay . && \
      ../../.venv/bin/python bench/bench.py --relay-bin /tmp/sse-relay

構成（ブラウザー側のクライアントは常に HTTP/1.1。ブラウザー → 中継は本番と違うが、比べたいのは中継の有無）:
  direct-uvicorn      client -> uvicorn（FastAPI 直接）
  relay-uvicorn       client -> Go 中継 -> uvicorn（HTTP/1.1）
  direct-hypercorn    client -> hypercorn（h1）
  relay-hypercorn     client -> Go 中継 -> hypercorn（h2c）
測ること: (1) 同時接続 (2) 切断が上流に届くまで (3) 最初のバイトまでと、イベントの遅延。
起動したサーバーは PID を控えて、終わりに止める。
"""

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM_PORT = 8110
RELAY_PORT = 8210
EVENT = re.compile(rb"data: (\{[^}]*\})\n\n")


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def tree_pids(root: int) -> list[int]:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                stat = Path(f"/proc/{d}/stat").read_text()
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
                kids.setdefault(ppid, []).append(int(d))
            except (OSError, ValueError, IndexError):
                pass
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        out.append(pid)
        todo.extend(kids.get(pid, []))
    return out


def proc_usage(root: int) -> dict:
    """プロセス（と子）の最大 RSS（MB）と、これまでの CPU 秒。"""
    hwm_kb, cpu_ticks = 0, 0
    for pid in tree_pids(root):
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    hwm_kb += int(line.split()[1])
            f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            cpu_ticks += int(f[11]) + int(f[12])
        except (OSError, ValueError, IndexError):
            pass
    return {"rss_mb": round(hwm_kb / 1024, 1), "cpu_s": cpu_ticks / os.sysconf("SC_CLK_TCK")}


def wait_http(url: str, timeout: float = 15.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"起動しない: {url}")


def get_json(url: str) -> dict:
    return json.loads(urllib.request.urlopen(url, timeout=5).read())


class Rig:
    """上流（と、あれば中継）を起動して、終わりに PID を指定して止める。"""

    def __init__(self, name: str, relay_bin: str):
        self.name, self.relay_bin, self.procs = name, relay_bin, []
        self.upstream_pid = self.relay_pid = None
        self.client_port = UPSTREAM_PORT

    def __enter__(self):
        py = sys.executable
        env = {**os.environ, "PYTHONPATH": str(HERE)}
        kind = "uvicorn" if self.name.endswith("uvicorn") else "hypercorn"
        if kind == "uvicorn":
            cmd = [py, "-m", "uvicorn", "sse_upstream:app", "--port", str(UPSTREAM_PORT), "--log-level", "warning"]
        else:
            cmd = [
                py,
                "-m",
                "hypercorn",
                "sse_upstream:app",
                "--bind",
                f"127.0.0.1:{UPSTREAM_PORT}",
                "--log-level",
                "warning",
            ]
        up = subprocess.Popen(cmd, cwd=HERE, env=env)
        self.procs.append(up)
        self.upstream_pid = up.pid
        wait_http(f"http://127.0.0.1:{UPSTREAM_PORT}/stats")
        if self.name.startswith("relay"):
            args = [
                self.relay_bin,
                "-listen",
                f"127.0.0.1:{RELAY_PORT}",
                "-upstream",
                f"http://127.0.0.1:{UPSTREAM_PORT}",
            ]
            if kind == "hypercorn":
                args.append("-upstream-h2c")
            rl = subprocess.Popen(args, stderr=subprocess.DEVNULL)
            self.procs.append(rl)
            self.relay_pid = rl.pid
            wait_http(f"http://127.0.0.1:{RELAY_PORT}/stats")
            self.client_port = RELAY_PORT
        return self

    def __exit__(self, *exc):
        for p in self.procs:
            p.send_signal(signal.SIGTERM)
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def stats(self, reset: bool = False) -> dict:
        return get_json(f"http://127.0.0.1:{UPSTREAM_PORT}/stats" + ("?reset=1" if reset else ""))


async def one_stream(port: int, sid: str, hold: float, interval: float, cut_after: float | None, out: dict):
    """1 本の SSE を読む。TTFB・イベントの遅延・エラーを out に足す。cut_after があれば、その秒数で接続を乱暴に切る。"""
    t0 = time.time()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 30)
        writer.write(
            f"GET /stream?sid={sid}&hold={hold}&interval={interval} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        buf, first, events = b"", None, 0
        deadline = None if cut_after is None else t0 + cut_after
        while True:
            timeout = None if deadline is None else max(0.0, deadline - time.time())
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout)
            except asyncio.TimeoutError:
                out["cut"][sid] = time.time()
                writer.transport.abort()  # RST を送って乱暴に切る（タブを閉じる相当）
                return
            if not chunk:
                break
            now = time.time()
            if first is None:
                first = now - t0
                out["ttfb"].append(first)
            buf += chunk
            for m in EVENT.finditer(buf):
                out["delay"].append(now - json.loads(m.group(1))["t"])
                events += 1
            last = buf.rfind(b"\n\n")
            buf = buf[last + 2 :] if last >= 0 else buf
        out["completed"] += 1
        writer.close()
    except Exception as exc:
        out["errors"].append(type(exc).__name__)


def new_out() -> dict:
    return {"ttfb": [], "delay": [], "errors": [], "completed": 0, "cut": {}}


def ms(x: float) -> float:
    return round(x * 1000, 1)


async def run_load(rig: Rig, n: int, hold: float, interval: float) -> dict:
    rig.stats(reset=True)
    out = new_out()
    await asyncio.gather(*[one_stream(rig.client_port, f"l{i}", hold, interval, None, out) for i in range(n)])
    return {
        "n": n,
        "completed": out["completed"],
        "errors": len(out["errors"]),
        "error_kinds": sorted(set(out["errors"])),
        "ttfb_ms_p50": ms(pct(out["ttfb"], 50)),
        "ttfb_ms_p99": ms(pct(out["ttfb"], 99)),
        "delay_ms_p50": ms(pct(out["delay"], 50)),
        "delay_ms_p99": ms(pct(out["delay"], 99)),
        "delay_ms_max": ms(max(out["delay"], default=float("nan"))),
    }


async def run_disconnect(rig: Rig, n: int) -> dict:
    rig.stats(reset=True)
    out = new_out()
    await asyncio.gather(*[one_stream(rig.client_port, f"d{i}", 30, 0.5, 2.0, out) for i in range(n)])
    await asyncio.sleep(3)
    cancelled = rig.stats()["cancelled"]
    lags = [cancelled[s] - t for s, t in out["cut"].items() if s in cancelled]
    return {
        "cut": len(out["cut"]),
        "reached_upstream": len(lags),
        "lag_ms_p50": ms(pct(lags, 50)),
        "lag_ms_max": ms(max(lags, default=float("nan"))),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay-bin", required=True)
    ap.add_argument("--configs", default="direct-uvicorn,relay-uvicorn,direct-hypercorn,relay-hypercorn")
    ap.add_argument("--loads", default="100,500,1000")
    ap.add_argument("--hold", type=float, default=20.0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    results = {}
    for name in args.configs.split(","):
        res = {"latency": None, "load": [], "disconnect": None}
        with Rig(name, args.relay_bin) as rig:
            # (3) 遅延: 低負荷（20 本）。イベントは 0.2 秒ごと
            res["latency"] = asyncio.run(run_load(rig, 20, 8.0, 0.2))
            # (2) 切断
            res["disconnect"] = asyncio.run(run_disconnect(rig, 20))
            # (1) 同時接続
            for n in map(int, args.loads.split(",")):
                before = {k: proc_usage(p) for k, p in (("up", rig.upstream_pid), ("relay", rig.relay_pid)) if p}
                r = asyncio.run(run_load(rig, n, args.hold, args.interval))
                after = {k: proc_usage(p) for k, p in (("up", rig.upstream_pid), ("relay", rig.relay_pid)) if p}
                r["upstream_rss_mb_peak"] = after["up"]["rss_mb"]
                r["upstream_cpu_s"] = round(after["up"]["cpu_s"] - before["up"]["cpu_s"], 2)
                if "relay" in after:
                    r["relay_rss_mb_peak"] = after["relay"]["rss_mb"]
                    r["relay_cpu_s"] = round(after["relay"]["cpu_s"] - before["relay"]["cpu_s"], 2)
                res["load"].append(r)
        results[name] = res
        print(name, json.dumps(res, ensure_ascii=False), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
