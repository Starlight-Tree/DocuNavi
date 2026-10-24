"""実際の HTTP での 1 リクエストの遅延。Django の開発サーバー（runserver）を起動して測る。.venv-django で動かす。

開発サーバーは本番用ではない（1 リクエストごとに接続を閉じ、DB の接続も毎回開く = CONN_MAX_AGE=0）。
クライアントも 1 リクエストごとに接続を開く。つまり「最も素朴な構成の、接続を含む遅延」で、本番の目安ではない。
測定の前にシードを入れ、終わりに消す。起動したサーバーは PID を控えて SIGTERM で止める。

使い方: python bench/http_latency.py <fixtures のディレクトリ> [回数]
"""

import http.client
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
fixtures = Path(sys.argv[1])
count = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
os.environ["SAAS_JWKS_FILE"] = str(fixtures / "jwks.json")
os.environ["DJANGO_DEBUG"] = "0"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from _stats import summarize  # noqa: E402
from saas.models import Membership, Organization, Plan  # noqa: E402

PORT = 8300
tokens = json.loads((fixtures / "tokens.json").read_text(encoding="utf-8"))
plan = Plan.objects.create(code=f"bench-{uuid.uuid4().hex[:8]}", name="bench", max_members=10)
org = Organization.objects.create(name="bench", slug=f"bench-{uuid.uuid4().hex[:8]}", plan=plan)
Membership.objects.create(organization=org, external_user_id=tokens["sub"], role="owner")

server = subprocess.Popen(
    [sys.executable, "manage.py", "runserver", f"127.0.0.1:{PORT}", "--noreload"],
    cwd=ROOT,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)


def get(path: str, token: str | None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


try:
    for _ in range(100):  # 起動待ち
        try:
            get("/api/me/orgs/", None)
            break
        except OSError:
            time.sleep(0.1)
    result = {"server_pid": server.pid}
    for name, path, token, expect in (
        ("me_orgs_200", "/api/me/orgs/", tokens["valid"], 200),
        ("org_detail_200", f"/api/orgs/{org.id}/", tokens["valid"], 200),
        ("no_token_401", "/api/me/orgs/", None, 401),
    ):
        for _ in range(100):
            assert get(path, token) == expect
        samples = []
        for _ in range(count):
            t0 = time.perf_counter_ns()
            status = get(path, token)
            samples.append(time.perf_counter_ns() - t0)
            assert status == expect
        result[name] = summarize(samples)
finally:
    server.send_signal(signal.SIGTERM)
    server.wait(timeout=15)
    org.delete()
    plan.delete()
print(json.dumps(result, indent=1))
