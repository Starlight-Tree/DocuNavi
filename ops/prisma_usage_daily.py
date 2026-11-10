#!/usr/bin/env python3
"""Prisma Postgres の Operations を取る（読み取り専用）。

Prisma の Service Token は持たない。Agent Vault の Proxy が、リクエストに認証を付ける。
実行は `infisical agent-vault run` の配下だけ（HTTPS_PROXY と CA が環境に入る）:

  infisical agent-vault run --proxy host.docker.internal:17323 -- \
    python3 ops/prisma_usage_daily.py --start 2026-11-06 [--probe]

扱い方（2026-11-11 時点の暫定。Usage API が要求した期間どおりに返さない疑いがあるため）:
- 正とするのは、範囲全体を 1 回で要求した値だけ。返ってきた period が要求と一致するかも示す。
- 日別・時間別の集計は、通常実行では出さない（参考値としても出さない）。`--probe` は返却 period の確認用だけ。
- Console の値との整合（同じ月・同じ DB・同じ指標か）が確認できるまで、無料枠の判断は確定しない。
- Authorization ヘッダーは付けない。トークンや Proxy の環境変数の値は表示しない。
- 日の境目は UTC（--tz-offset 9 で JST）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

DEFAULT_DB_ID = "db_gn96qorjqcfu12zt2mm3gczo"
BASE = "https://api.prisma.io"
TOLERANCE = timedelta(seconds=1)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch(db_id: str, start: datetime | None, end: datetime | None) -> dict:
    params = {}
    if start is not None:
        params["startDate"] = iso(start)
    if end is not None:
        params["endDate"] = iso(end)
    qs = urllib.parse.urlencode(params)
    url = f"{BASE}/v1/databases/{db_id}/usage" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "docunavi-ops/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        print(f"[エラー] GET {url}\n  status={e.code}\n  body={body}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError) as e:
        print(f"[エラー] GET {url}\n  {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def ops(resp: dict) -> float:
    return float(resp["metrics"]["operations"]["used"])


def matches(req_start: datetime | None, req_end: datetime | None, resp: dict) -> str:
    """要求した期間と、返ってきた period の一致。日付なしの要求は、比べる相手がない。"""
    if req_start is None and req_end is None:
        return "—（要求に期間なし）"
    p = resp["period"]
    rs, re_ = parse(p["start"]), parse(p["end"])
    ok_s = req_start is None or abs(rs - req_start) <= TOLERANCE
    ok_e = req_end is None or abs(re_ - req_end) <= TOLERANCE
    if ok_s and ok_e:
        return "一致"
    # 返った期間が要求より広いか、ずれているか
    wider = (req_start is not None and rs < req_start - TOLERANCE) or (
        req_end is not None and re_ > req_end + TOLERANCE
    )
    return "不一致（広い）" if wider else "不一致"


def line(label: str, req_start, req_end, resp: dict) -> str:
    rq = f"{iso(req_start) if req_start else '-'} … {iso(req_end) if req_end else '-'}"
    p = resp["period"]
    return (
        f"{label:<22}要求 {rq}\n"
        f"{'':<22}返却 {p['start']} … {p['end']}\n"
        f"{'':<22}Operations {ops(resp):>10,.0f}   期間: {matches(req_start, req_end, resp)}"
    )


def guard() -> None:
    # Agent Vault の配下でなければ動かさない（トークンなしで api.prisma.io に出るのを防ぐ）
    if not os.environ.get("INFISICAL_AGENT_VAULT_SESSION_TOKEN") or not (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    ):
        print("Agent Vault の配下で実行してください（infisical agent-vault run … -- python3 …）。", file=sys.stderr)
        sys.exit(2)


def console_check(db_id: str, resp: dict) -> None:
    p = resp["period"]
    print("\n[Console との照合] API の値と Console の月間値は、次が同じなら比べてよい。まだ確認できていない。")
    print(f"  - DB      : API は {db_id}（Console で同じ DB を見ているか）")
    print(
        f"  - 期間    : API が返した period は {p['start']} … {p['end']}（Console の「今月」と同じ範囲か。課金月の起点も確かめる）"
    )
    print(
        f"  - 指標    : API は metrics.operations（単位 {resp['metrics']['operations']['unit']}）。Console の「Operations」と同じ定義か"
    )
    print("  - 時点    : API の generatedAt =", resp["generatedAt"], "（Console の表示時刻と、集計の遅れ）")
    print("  → 無料枠 200K の判断は、ここが確認できるまで確定しない。")


def probe(db_id: str, start: datetime, end: datetime, day_for_hours: datetime) -> None:
    """各呼び出しの、要求期間・返却 period・Operations・一致の有無を出す。"""
    mid = start + (end - start) / 2
    cases = [
        ("日付なし", None, None),
        ("範囲全体", start, end),
        ("前半", start, mid),
        ("後半", mid, end),
        ("開始日 24h", start, start + timedelta(days=1)),
        ("開始日 前半 12h", start, start + timedelta(hours=12)),
        ("開始日 後半 12h", start + timedelta(hours=12), start + timedelta(days=1)),
        ("指定日 24h", day_for_hours, day_for_hours + timedelta(days=1)),
    ]
    cases += [
        (f"指定日 {h:02d}時台", day_for_hours + timedelta(hours=h), day_for_hours + timedelta(hours=h + 1))
        for h in range(24)
    ]
    first = None
    mism = 0
    hourly = 0.0
    for label, s, e in cases:
        r = fetch(db_id, s, e)
        first = first or r
        print(line(label, s, e, r))
        if matches(s, e, r).startswith("不一致"):
            mism += 1
        if "時台" in label:
            hourly += ops(r)
    print(f"\n期間が不一致の呼び出し: {mism} / {len(cases)}")
    print(f"1 時間 24 個の合計（参考。24h の値と合うかを見るだけ）: {hourly:,.0f}")
    console_check(db_id, first)


def watch(db_id: str, start: datetime, interval: int, count: int) -> None:
    """累積値（開始日から現在まで）を一定間隔で取り、差を出す。日別の集計は使わない。
    差 = その間に増えた Operations。アプリを触らない時間帯の増加が、裏で動く処理の量になる。"""
    import time

    prev = None
    print(f"{'時刻(UTC)':<22}{'累積':>10}{'増分':>9}{'1時間換算':>11}  返却 period の終わり / 生成時刻")
    for i in range(count):
        t = datetime.now(timezone.utc)
        r = fetch(db_id, start, t)
        n = ops(r)
        d = "" if prev is None else f"{n - prev[0]:+,.0f}"
        rate = "" if prev is None else f"{(n - prev[0]) / max((t - prev[1]).total_seconds(), 1) * 3600:,.0f}"
        print(f"{iso(t):<22}{n:>10,.0f}{d:>9}{rate:>11}  {r['period']['end']} / {r['generatedAt']}", flush=True)
        prev = (n, t)
        if i + 1 < count:
            time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="開始日 YYYY-MM-DD（含む）")
    ap.add_argument("--end", help="終了日 YYYY-MM-DD（含む）。省略で今日")
    ap.add_argument("--database-id", default=DEFAULT_DB_ID)
    ap.add_argument("--probe", action="store_true", help="診断: 各呼び出しの要求期間・返却 period・一致を出す")
    ap.add_argument("--watch", type=int, metavar="秒", help="累積値を、この間隔（秒）で取って差を出す（--count 回）")
    ap.add_argument("--count", type=int, default=6, help="--watch の回数（既定 6）")
    ap.add_argument("--probe-day", help="--probe で 1 時間ずつ見る日（YYYY-MM-DD）。既定は開始日の 2 日後")
    ap.add_argument("--tz-offset", type=float, default=0.0, help="日の境目の UTC からのずれ（時間）。既定 0 = UTC")
    args = ap.parse_args()
    guard()

    tz = timezone(timedelta(hours=args.tz_offset))
    now = datetime.now(timezone.utc)
    d0 = date.fromisoformat(args.start)
    d1 = date.fromisoformat(args.end) if args.end else now.astimezone(tz).date()
    if d1 < d0:
        sys.exit("--end は --start 以降にしてください")
    start = datetime(d0.year, d0.month, d0.day, tzinfo=tz)
    end = min(datetime(d1.year, d1.month, d1.day, tzinfo=tz) + timedelta(days=1), now)

    if args.watch:
        watch(args.database_id, start, args.watch, args.count)
        return

    if args.probe:
        pd = date.fromisoformat(args.probe_day) if args.probe_day else d0 + timedelta(days=2)
        probe(args.database_id, start, end, datetime(pd.year, pd.month, pd.day, tzinfo=tz))
        return

    # 正とする値: 範囲全体を 1 回で
    whole = fetch(args.database_id, start, end)
    print(f"データベース {args.database_id}  日の境目: UTC{args.tz_offset:+g}")
    print("\n■ 範囲全体（1 回の呼び出し。これだけを正とする）")
    print(line("範囲全体", start, end, whole))
    st = whole["metrics"]["storage"]
    print(f"ストレージ {st['used']} {st['unit']}  / 生成時刻 {whole['generatedAt']}")

    console_check(args.database_id, whole)


if __name__ == "__main__":
    main()
