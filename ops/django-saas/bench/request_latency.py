"""1 リクエストの遅延（Django のテスト用クライアントでプロセス内に呼ぶ。HTTP サーバーは通らない）。.venv-django で動かす。

`sub` から Membership を引く経路（/api/me/orgs/、/api/orgs/<id>/）と、DB に行かない 401 を測る。
DB は一時 Postgres の saas_dev（saas_app で接続）。測定の前にシードを入れ、終わりに消す。
注意: テスト用クライアントは request_finished の close_old_connections を外すので、DB の接続は全リクエストで使い回される
（CONN_MAX_AGE の違いは、この測定には出ない）。接続を毎回開く場合は http_latency.py で測る。

使い方: python bench/request_latency.py <fixtures のディレクトリ> [回数]
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
fixtures = Path(sys.argv[1])
count = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
os.environ["SAAS_JWKS_FILE"] = str(fixtures / "jwks.json")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_DEBUG", "0")

import django  # noqa: E402

django.setup()

import uuid  # noqa: E402

from _stats import summarize  # noqa: E402
from django.test import Client  # noqa: E402
from saas.models import Membership, Organization, Plan  # noqa: E402

tokens = json.loads((fixtures / "tokens.json").read_text(encoding="utf-8"))
sub = tokens["sub"]
plan = Plan.objects.create(code=f"bench-{uuid.uuid4().hex[:8]}", name="bench", max_members=10)
org = Organization.objects.create(name="bench", slug=f"bench-{uuid.uuid4().hex[:8]}", plan=plan)
Membership.objects.create(organization=org, external_user_id=sub, role="owner")
for _ in range(4):
    Membership.objects.create(organization=org, external_user_id=uuid.uuid4(), role="member")

client = Client()
auth = {"HTTP_AUTHORIZATION": f"Bearer {tokens['valid']}"}


def measure(path: str, headers: dict, expect: int) -> dict:
    for _ in range(200):
        assert client.get(path, **headers).status_code == expect
    samples = []
    for _ in range(count):
        t0 = time.perf_counter_ns()
        response = client.get(path, **headers)
        samples.append(time.perf_counter_ns() - t0)
        assert response.status_code == expect
    return summarize(samples)


try:
    result = {
        "me_orgs_200": measure("/api/me/orgs/", auth, 200),
        "org_detail_200": measure(f"/api/orgs/{org.id}/", auth, 200),
        "no_token_401": measure("/api/me/orgs/", {}, 401),
    }
finally:
    org.delete()
    plan.delete()
print(json.dumps(result, indent=1))
