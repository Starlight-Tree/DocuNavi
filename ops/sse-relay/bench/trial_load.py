"""試験用の中継（Cloud Run）への同時接続試験。docs/go-architecture-plan.md の「段階 1 の試験の設計」。

    .venv/bin/python ops/sse-relay/bench/trial_load.py \
        --relay https://docunavi-sse-trial-relay-….run.app --fake https://docunavi-sse-trial-fake-….run.app \
        --stages 10,25 --hold 20

URL は引数か環境変数（TRIAL_RELAY_URL / TRIAL_FAKE_URL）で渡す。ソースには書かない。秘密情報・認証情報は使わない。

安全のための固定の制約（引数では外せない）:
  - 相手は、ホスト名に "docunavi-sse-trial-" を含む試験用サービスか、ループバック（127.0.0.1 / localhost）だけ。本番のホストは断る
  - 同時に開く本数は --max-allowed（既定 25）まで。段階の最大 + --overflow がこれを超える指定は、始める前に断る

1 つの接続は 1 本の HTTP/2 接続（ブラウザーの利用者 1 人に当たる）。疑似の上流に ?duration=<hold>&interval=<interval> を渡し、
全部が開いたあと、段階ごとに次を確かめる。
  1. 全部が 200 で最初のバイトが届く（HTTP/2。ループバックは HTTP/1.1）
  2. 上流の /stats の active が、基準値 + 本数になる（上流の数 = 開いた本数）
  3. --overflow N: 上限の外の N 本が、すぐ 503（日本語の detail・Retry-After・許可された Origin の CORS ヘッダー）になり、
     上流に届かない（上流の started が増えない）
  4. 終わり方: 既定は最後まで流して [DONE]（上流の completed が本数ぶん増える）。--cut-after 秒 を付けると、その時点でクライアントが全部切り、
     上流の client_disconnected が本数ぶん増え、active が基準に戻るまでの秒数を測る
中継自身には統計の口が無いので、「中継が持つ本数」はクライアントが開いている本数で見る（断ったときのログ stream_limit_rejected は
Cloud Run のログで見る）。メモリ・インスタンス数は、この実行の時刻範囲（最後に UTC で表示）で Cloud Monitoring を読む。
終了コード: 全部の確認が通れば 0、1 つでも外れたら 1、入力の誤りは 2。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

TRIAL_HOST_PART = "docunavi-sse-trial-"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_MAX_ALLOWED = 25
BODY = {"trial": True}  # 疑似の上流は本文を読み捨てる


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def check_url(name: str, url: str) -> str:
    u = urlparse(url)
    host = u.hostname or ""
    if u.scheme not in ("http", "https") or not host:
        die(f"{name} の URL が正しくありません")
    if host in LOOPBACK or TRIAL_HOST_PART in host:
        return url.rstrip("/")
    die(f"{name} は試験用のサービス（ホスト名に {TRIAL_HOST_PART} を含む）かループバックだけです。本番には使えません")
    raise AssertionError  # die は戻らない


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


async def one_stream(rec: dict, relay: str, origin: str, query: str, read_timeout: float) -> None:
    """1 本の接続。結果は rec に書く（outcome: completed / stream_error / ended_without_done / http_<status> / cut_by_client / timeout / error:…）。"""
    rec["outcome"] = "pending"
    t0 = time.monotonic()
    tail = b""
    try:
        async with httpx.AsyncClient(http2=True, timeout=httpx.Timeout(15.0, read=read_timeout)) as client:
            async with client.stream(
                "POST", f"{relay}/chat/stream?{query}", json=BODY, headers={"Origin": origin}
            ) as resp:
                rec["status"] = resp.status_code
                rec["proto"] = resp.http_version
                if resp.status_code != 200:
                    body = await resp.aread()
                    rec.update(
                        outcome=f"http_{resp.status_code}",
                        body=body[:300].decode("utf-8", "replace"),
                        acao=resp.headers.get("access-control-allow-origin"),
                        retry_after=resp.headers.get("retry-after"),
                    )
                    return
                rec["open"] = True
                async for chunk in resp.aiter_bytes():
                    if "ttfb" not in rec:
                        rec["ttfb"] = time.monotonic() - t0
                    tail = (tail + chunk)[-256:]
                    if b'"type":"error"' in tail:
                        rec["saw_error"] = True
                    if b"[DONE]" in tail:
                        rec["done"] = True
                if rec.get("saw_error"):
                    rec["outcome"] = "stream_error"
                else:
                    rec["outcome"] = "completed" if rec.get("done") else "ended_without_done"
    except asyncio.CancelledError:
        rec["outcome"] = "cut_by_client"  # 呼び出し側が切った。後始末（接続を閉じる）は async with が済ませている
    except httpx.TimeoutException:
        rec["outcome"] = "timeout"
    except httpx.HTTPError as e:
        rec["outcome"] = f"error:{type(e).__name__}"
    finally:
        rec["open"] = False
        rec["total"] = time.monotonic() - t0


async def fetch_stats(client: httpx.AsyncClient, fake: str) -> dict:
    r = await client.get(f"{fake}/stats")
    r.raise_for_status()
    return r.json()


async def wait_active_at_most(
    client: httpx.AsyncClient, fake: str, target: int, timeout: float
) -> tuple[float | None, dict]:
    """上流の active が target 以下になるまでの秒数（間に合わなければ None）と、そのときの統計。"""
    t0 = time.monotonic()
    while True:
        s = await fetch_stats(client, fake)
        if s["active"] <= target:
            return time.monotonic() - t0, s
        if time.monotonic() - t0 > timeout:
            return None, s
        await asyncio.sleep(0.2)


async def run_stage(args: argparse.Namespace, n: int, sc: httpx.AsyncClient) -> dict:
    base = await fetch_stats(sc, args.fake)
    query = f"duration={args.hold}&interval={args.interval}"
    read_timeout = args.hold + 30
    recs: list[dict] = [{} for _ in range(n)]
    tasks = []
    for rec in recs:
        tasks.append(asyncio.create_task(one_stream(rec, args.relay, args.origin, query, read_timeout)))
        if args.ramp > 0:
            await asyncio.sleep(args.ramp / n)

    # 全部が最初のバイトを受けるか、結果が出るまで待つ（最大 30 秒）
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not all(
        "ttfb" in r or r.get("outcome") not in (None, "pending") for r in recs
    ):
        await asyncio.sleep(0.2)
    t_open = time.monotonic()
    held = sum(1 for r in recs if r.get("open"))
    after_open = await fetch_stats(sc, args.fake)
    peak = after_open["active"]

    # 上限の外の接続（断られるはず）
    over: list[dict] = [{} for _ in range(args.overflow)]
    if over:
        await asyncio.gather(*(one_stream(o, args.relay, args.origin, "duration=1", 15.0) for o in over))
    after_over = await fetch_stats(sc, args.fake)
    peak = max(peak, after_over["active"])

    # 保持の間、上流の active を測る。--cut-after なら、その時刻に全部切る
    t_cut = None
    end_at = t_open + (args.cut_after if args.cut_after > 0 else args.hold)
    while time.monotonic() < end_at and not all(t.done() for t in tasks):
        peak = max(peak, (await fetch_stats(sc, args.fake))["active"])
        await asyncio.sleep(min(args.sample, max(0.0, end_at - time.monotonic())))
    if args.cut_after > 0:
        t_cut = time.monotonic()
        for t in tasks:
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    settle, final = await wait_active_at_most(sc, args.fake, base["active"], args.settle)
    # 切断が上流に届くまでの秒数は、切った時刻から測る（settle は待ちの長さ）。/stats の往復 1 回ぶんを含む
    propagation = (time.monotonic() - t_cut) if (t_cut is not None and settle is not None) else None

    delta = {
        k: final[k] - base[k] for k in ("started", "completed", "errored", "aborted", "client_disconnected", "rejected")
    }
    ttfbs = [r["ttfb"] for r in recs if "ttfb" in r]
    outcomes: dict[str, int] = {}
    for r in recs:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    protos = sorted({r.get("proto", "?") for r in recs})

    checks: list[tuple[str, bool, str]] = []
    checks.append(
        (
            "全部が開いた（200 と最初のバイト）",
            held == n and all(r.get("status") == 200 and "ttfb" in r for r in recs),
            f"held={held}/{n}, proto={protos}",
        )
    )
    checks.append(
        (
            "上流の active = 基準 + 本数",
            peak - base["active"] == n,
            f"peak_active={peak}, base={base['active']}, 期待={n}",
        )
    )
    if over:
        ok_over = all(
            o.get("status") == 503
            and o.get("retry_after")
            and "ただいま混み合っています" in o.get("body", "")
            and o.get("acao") == args.origin
            for o in over
        )
        checks.append(
            ("上限の外は 503（日本語の detail・Retry-After・CORS）", ok_over, f"{[o.get('status') for o in over][:5]}…")
        )
        started_during = after_over["started"] - base["started"]
        checks.append(("断った接続は上流に届かない", started_during == n, f"started の増分={started_during}, 期待={n}"))
    if args.cut_after > 0:
        checks.append(
            (
                "全部の切断が上流に届いた",
                delta["client_disconnected"] == n,
                f"client_disconnected の増分={delta['client_disconnected']}, 期待={n}, "
                + (
                    f"切った→上流の active が戻るまで {propagation * 1000:.0f}ms"
                    if propagation is not None
                    else "active は戻らなかった"
                ),
            )
        )
    else:
        checks.append(
            (
                "全部が [DONE] で終わった",
                outcomes.get("completed") == n and delta["completed"] == n,
                f"クライアント={outcomes}, 上流 completed の増分={delta['completed']}",
            )
        )
    checks.append(
        (
            "上流の active が基準に戻った",
            settle is not None,
            f"{'%.1f 秒' % settle if settle is not None else '戻らなかった（' + str(args.settle) + ' 秒）'}（終了を待ち始めてから）",
        )
    )
    unexpected = {k: v for k, v in outcomes.items() if k not in ("completed", "cut_by_client")}
    checks.append(
        (
            "予期しない結果がない",
            not unexpected and delta["errored"] == 0 and delta["aborted"] == 0,
            f"{unexpected or 'なし'}, 上流 errored={delta['errored']} aborted={delta['aborted']}",
        )
    )

    return {
        "streams": n,
        "overflow": args.overflow,
        "mode": f"cut_after={args.cut_after}s" if args.cut_after > 0 else "complete",
        "ttfb_s": {"p50": pct(ttfbs, 50), "p95": pct(ttfbs, 95), "max": max(ttfbs)} if ttfbs else None,
        "outcomes": outcomes,
        "upstream_delta": delta,
        "upstream_peak_active": peak,
        "settle_s_after_end": settle,
        "propagation_s_after_cut": propagation,
        "checks": [{"name": a, "ok": b, "detail": c} for a, b, c in checks],
        "overflow_samples": [
            {k: o.get(k) for k in ("status", "body", "acao", "retry_after", "total")} for o in over[:3]
        ],
    }


def print_stage(res: dict) -> None:
    t = res["ttfb_s"]
    print(f"\n== {res['streams']} 本（{res['mode']}、上限の外 {res['overflow']} 本） ==")
    if t:
        print(f"最初のバイトまで: p50={t['p50'] * 1000:.0f}ms p95={t['p95'] * 1000:.0f}ms max={t['max'] * 1000:.0f}ms")
    print(
        f"結果: {res['outcomes']}  上流の増分: {res['upstream_delta']}  上流の最大 active: {res['upstream_peak_active']}"
    )
    for c in res["checks"]:
        print(f"  [{'OK' if c['ok'] else 'NG'}] {c['name']} — {c['detail']}")


async def amain(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(http2=True, timeout=15.0) as sc:
        t_start = datetime.now(timezone.utc)
        try:
            s = await fetch_stats(sc, args.fake)
        except httpx.HTTPError as e:
            print(f"疑似の上流の /stats が読めません: {type(e).__name__}", file=sys.stderr)
            return 2
        print(f"疑似の上流（開始前）: {s}")
        results = []
        for n in args.stage_list:
            res = await run_stage(args, n, sc)
            print_stage(res)
            results.append(res)
            if not all(c["ok"] for c in res["checks"]):
                print("確認が外れたので、ここで止めます（次の段階には進みません）")
                break
        t_end = datetime.now(timezone.utc)
    ok = len(results) == len(args.stage_list) and all(c["ok"] for r in results for c in r["checks"])
    print(
        f"\n時刻範囲（UTC。Cloud Monitoring / Logging 用）: {t_start:%Y-%m-%dT%H:%M:%SZ} 〜 {t_end:%Y-%m-%dT%H:%M:%SZ}"
    )
    print("総合:", "OK" if ok else "NG")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                {"start": t_start.isoformat(), "end": t_end.isoformat(), "stages": results, "ok": ok},
                f,
                ensure_ascii=False,
                indent=2,
            )
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--relay", default=os.environ.get("TRIAL_RELAY_URL", ""), help="試験用の中継の URL（環境変数 TRIAL_RELAY_URL）"
    )
    ap.add_argument(
        "--fake", default=os.environ.get("TRIAL_FAKE_URL", ""), help="疑似の上流の URL（環境変数 TRIAL_FAKE_URL）"
    )
    ap.add_argument("--stages", default="10", help="同時本数の段階（カンマ区切り。例 10,25）")
    ap.add_argument("--hold", type=float, default=20.0, help="1 本が流れる秒数（疑似の上流の duration）")
    ap.add_argument("--interval", type=float, default=1.0, help="疑似の上流の text-delta の間隔（秒）")
    ap.add_argument("--ramp", type=float, default=2.0, help="1 段階の接続を開く時間を何秒に散らすか")
    ap.add_argument("--overflow", type=int, default=0, help="全部が開いたあとに足す、上限の外の本数（503 のはず）")
    ap.add_argument(
        "--cut-after", type=float, default=0.0, help="0 なら最後まで流す。正なら、全部が開いてからこの秒数で全部を切る"
    )
    ap.add_argument("--sample", type=float, default=1.0, help="上流の /stats を読む間隔（秒）")
    ap.add_argument("--settle", type=float, default=15.0, help="終了後、上流の active が基準に戻るのを待つ最大秒数")
    ap.add_argument(
        "--origin", default="https://docunavi-eosin.vercel.app", help="Origin ヘッダー（断りの CORS の確認用）"
    )
    ap.add_argument(
        "--max-allowed",
        type=int,
        default=DEFAULT_MAX_ALLOWED,
        help=f"同時に開く本数の上限（既定 {DEFAULT_MAX_ALLOWED}）",
    )
    ap.add_argument("--json", default="", help="結果の JSON の保存先")
    args = ap.parse_args()

    if not args.relay or not args.fake:
        die("--relay と --fake（または TRIAL_RELAY_URL と TRIAL_FAKE_URL）が要ります")
    args.relay = check_url("--relay", args.relay)
    args.fake = check_url("--fake", args.fake)
    try:
        args.stage_list = [int(x) for x in args.stages.split(",") if x.strip()]
    except ValueError:
        die("--stages は整数のカンマ区切りにしてください")
    if not args.stage_list or any(n < 1 for n in args.stage_list) or args.overflow < 0:
        die("--stages は 1 以上、--overflow は 0 以上にしてください")
    need = max(args.stage_list) + args.overflow
    if need > args.max_allowed:
        die(
            f"同時に開く本数が {need} 本で、--max-allowed={args.max_allowed} を超えます。承認された範囲に収めてください"
        )
    if args.hold < 5 or (args.cut_after and args.cut_after > args.hold - 2):
        die("--hold は 5 以上、--cut-after は --hold より 2 秒以上短くしてください")
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
