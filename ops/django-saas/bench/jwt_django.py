"""Django 側（saas/jwt_auth.py）の JWT 検証の所要時間と、拒否の理由。.venv-django で動かす。

使い方: python bench/jwt_django.py <fixtures のディレクトリ> [回数]
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
fixtures = Path(sys.argv[1])
count = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
os.environ["SAAS_JWKS_FILE"] = str(fixtures / "jwks.json")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from _stats import summarize  # noqa: E402
from saas.jwt_auth import InvalidToken, verify_token  # noqa: E402

tokens = json.loads((fixtures / "tokens.json").read_text(encoding="utf-8"))
valid = tokens["valid"]
assert verify_token(valid) == tokens["sub"]

reasons = {}
for name, token in tokens.items():
    if name in ("sub", "valid"):
        continue
    try:
        verify_token(token)
        reasons[name] = "ACCEPTED"
    except InvalidToken as exc:
        reasons[name] = exc.reason

for _ in range(500):
    verify_token(valid)
samples = []
for _ in range(count):
    t0 = time.perf_counter_ns()
    verify_token(valid)
    samples.append(time.perf_counter_ns() - t0)
rejected = []
for _ in range(2000):
    t0 = time.perf_counter_ns()
    try:
        verify_token(tokens["expired"])
    except InvalidToken:
        pass
    rejected.append(time.perf_counter_ns() - t0)

print(
    json.dumps(
        {
            "impl": "django saas/jwt_auth.py",
            "valid": summarize(samples),
            "expired": summarize(rejected),
            "reasons": reasons,
        },
        indent=1,
    )
)
