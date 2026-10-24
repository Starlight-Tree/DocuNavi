"""今の app/auth.py の verify_token の所要時間と、拒否の理由。.venv（アプリ本体の環境）で動かす。

JWKS の取得は行わない。同じ JWKS のファイルから鍵を読み込んだ JwksCache を、取得済みの状態（TTL 内）にして使う。
app の verify_token は async なので、1 つのイベントループの中で await する（Django 側は同期）。

使い方: python bench/jwt_app.py <fixtures のディレクトリ> [回数]
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

repo = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(repo))
sys.path.insert(0, str(Path(__file__).resolve().parent))
fixtures = Path(sys.argv[1])
count = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
os.environ["SUPABASE_URL"] = "http://127.0.0.1:54321"  # issuer が Django 側の設定と同じになる

from _stats import summarize  # noqa: E402

from app import auth  # noqa: E402

tokens = json.loads((fixtures / "tokens.json").read_text(encoding="utf-8"))
document = json.loads((fixtures / "jwks.json").read_text(encoding="utf-8"))
cache = auth.JwksCache(auth.jwks_url())
cache._keys = auth._parse_keys(document)
cache._fetched_at = auth._now()
auth._cache = cache


async def main() -> None:
    valid = tokens["valid"]
    assert await auth.verify_token(valid) == tokens["sub"]
    reasons = {}
    for name, token in tokens.items():
        if name in ("sub", "valid"):
            continue
        try:
            await auth.verify_token(token)
            reasons[name] = "ACCEPTED"
        except auth.InvalidToken as exc:
            reasons[name] = str(exc)
    for _ in range(500):
        await auth.verify_token(valid)
    samples = []
    for _ in range(count):
        t0 = time.perf_counter_ns()
        await auth.verify_token(valid)
        samples.append(time.perf_counter_ns() - t0)
    rejected = []
    for _ in range(2000):
        t0 = time.perf_counter_ns()
        try:
            await auth.verify_token(tokens["expired"])
        except auth.InvalidToken:
            pass
        rejected.append(time.perf_counter_ns() - t0)
    print(
        json.dumps(
            {"impl": "app/auth.py", "valid": summarize(samples), "expired": summarize(rejected), "reasons": reasons},
            indent=1,
        )
    )


asyncio.run(main())
