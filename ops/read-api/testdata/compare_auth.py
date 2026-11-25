"""認証・利用資格・アクセス制御つきで、Python の API と Go の読み取り API の応答を比べる（ローカル専用）。

  READ_API_CMP_DATABASE_URL=postgresql://…/readapi_cmp  python ops/read-api/testdata/compare_auth.py <Go のバイナリ>

自分で JWKS の小さなサーバー（127.0.0.1:19001）を立て、ES256 のトークンを作り、設定の組み合わせごとに、
Python（uvicorn、18000 番）と Go（18080 番）を起動して、トークンごとに同じ要求を送る。終わったら、起動した 2 つを PID で止める。
DB は `setup_db.py` で作り、`seed.sql` を入れたもの。トークンと URL は表示しない。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare import check  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
ISSUER = "http://127.0.0.1:19001"
PY, GO = "http://127.0.0.1:18000", "http://127.0.0.1:18080"
PATHS = ["/collections", "/targets", "/doc-types", "/documents", "/threads"] + [
    f"/documents/{i}/{leaf}"
    for i in ("1", "2", "3", "5", "6", "8", "10", "11", "abc")
    for leaf in ("chunks", "file", "hero")
]

KEY = ec.generate_private_key(ec.SECP256R1())
JWK = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "cmp-1", "alg": "ES256", "use": "sig"})


class Jwks(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"keys": [JWK]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


def token(sub: str, **override: object) -> str:
    now = int(time.time())
    claims = {"sub": sub, "iss": ISSUER, "aud": "docunavi", "exp": now + 600, "iat": now}
    claims.update(override)
    return jwt.encode(claims, KEY, algorithm="ES256", headers={"kid": "cmp-1"})


def tokens() -> dict[str, str | None]:
    return {
        "なし": None,
        "壊れた": "abc.def.ghi",
        "期限切れ": token("user_staff0001", exp=int(time.time()) - 600),
        "aud 違い": token("user_staff0001", aud="other"),
        "sub が短い": token("short"),
        "職員（資格あり・staff）": token("user_staff0001"),
        "一般（資格なし・ロールなし）": token("user_plain0001"),
        "空ロール（資格あり）": token("user_nogroup01"),
        "未登録の利用者": token("user_unknown0001"),
    }


SCENARIOS = {
    "資格 enforce + 権限 enforce": {"MEMBERSHIP_MODE": "enforce", "ACCESS_MODE": "enforce"},
    "資格 off + 権限 enforce": {"MEMBERSHIP_MODE": "off", "ACCESS_MODE": "enforce"},
    "資格 shadow + 権限 shadow": {"MEMBERSHIP_MODE": "shadow", "ACCESS_MODE": "shadow"},
    "資格 enforce + 権限 off": {"MEMBERSHIP_MODE": "enforce", "ACCESS_MODE": "off"},
}


def prepare_pdfs(db: str) -> None:
    """/file と /chunks の original_pdf を、実在する PDF（storage/pdf/）で確かめられるように、資料の行を書き換える（使い捨ての DB 専用）。

    id 2 = cafe/cafe_store_guide.pdf（ハッシュが合う → 200）、id 3 = guides/tsubasa_dorm_rules.pdf だがハッシュ違い（409）、
    id 6 = guides/aoba_aw820_manual.pdf（ハッシュが合う）。それ以外は置き場に無い（404）。
    """
    import hashlib

    import psycopg

    def digest(path: str) -> str:
        return hashlib.sha256((ROOT / "storage" / "pdf" / path).read_bytes()).hexdigest()

    rows = [
        ("cafe_b.pdf", "cafe_store_guide.pdf", digest("cafe/cafe_store_guide.pdf")),
        ("cafe_c.pdf", "tsubasa_dorm_rules.pdf", "0" * 64),
        ("guide_b.pdf", "aoba_aw820_manual.pdf", digest("guides/aoba_aw820_manual.pdf")),
    ]
    with psycopg.connect(db, autocommit=True, connect_timeout=5) as conn:
        for old, source, content_hash in rows:
            conn.execute(
                "UPDATE documents SET source = %s, content_hash = %s WHERE source = %s", (source, content_hash, old)
            )


def prepare_threads(db: str) -> None:
    """/threads を比べられるようにする（使い捨ての DB 専用）。

    seed.sql のチェックポイントは中身が空の blob なので、Python が復号できる本物の blob（発言なし）に置き換える。
    さらに、題名（metadata の title）が無い古い会話を 1 件足し、`python -m app.history_admin --backfill-titles --apply`（本番の再開後に流す補完）を
    実 Postgres（jsonb）に対して流す。流した後は、Python（本体を復号して題名を作る）と Go（metadata の title だけを読む）の一覧が一致する。
    """
    import psycopg
    from langchain_core.messages import HumanMessage

    sys.path.insert(0, str(ROOT))
    from app.pg_saver import PgSaver

    saver = PgSaver(None)

    def blob(messages: list[HumanMessage], checkpoint_id: str) -> tuple[str, bytes]:
        return saver.serde.dumps_typed(
            {"v": 4, "id": checkpoint_id, "ts": "2026-11-20T00:00:00+00:00", "channel_values": {"messages": messages}}
        )

    with psycopg.connect(db, autocommit=True, connect_timeout=5) as conn:
        empty_type, empty = blob([], "x")
        conn.execute("UPDATE checkpoints SET type = %s, checkpoint = %s", (empty_type, empty))
        legacy_type, legacy = blob([HumanMessage(content="  古い会話の\n最初の質問  ")], "1ef-0099")
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata, created_at) "
            "VALUES ('user_staff0001:t-legacy', '', '1ef-0099', %s, %s, '{}', '2026-11-18 03:00:00.123456+00') ON CONFLICT DO NOTHING",
            (legacy_type, legacy),
        )
    env = {**os.environ, "DB_BACKEND": "pg", "DATABASE_URL": db}
    done = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "app.history_admin", "--backfill-titles", "--apply"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    print(done.stdout.strip() or done.stderr.strip())
    if done.returncode:
        raise SystemExit("題名の補完に失敗しました")


def wait(url: str) -> None:
    for _ in range(120):
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise SystemExit(f"{url} が起動しません")


def main() -> int:
    go_bin = sys.argv[1]
    db = os.environ["READ_API_CMP_DATABASE_URL"]
    prepare_pdfs(db)
    prepare_threads(db)
    server = HTTPServer(("127.0.0.1", 19001), Jwks)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    failures = 0
    base = {
        **os.environ,
        "DATABASE_URL": db,
        "AUTH_MODE": "required",
        "AUTH_BASE_URL": ISSUER,
        "AUTH_AUDIENCE": "docunavi",
        "ACCESS_CACHE_TTL_SECONDS": "0",
        "PDF_STORE": "local",
        "PDF_DIR": str(ROOT / "storage" / "pdf"),
    }
    for name, extra in SCENARIOS.items():
        env = {**base, **extra}
        py = subprocess.Popen(
            [
                str(ROOT / ".venv/bin/python"),
                "-m",
                "uvicorn",
                "app.main:app",
                "--port",
                "18000",
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env={**env, "DB_BACKEND": "pg", "GEMINI_API_KEY": ""},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        go = subprocess.Popen(
            [go_bin], env={**env, "PORT": "18080"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            wait(PY + "/health")
            wait(GO + "/health")
            print(f"=== {name}")
            for label, tok in tokens().items():
                row = []
                for path in PATHS:
                    a, b, notes = check(PY, GO, path, "GET", tok)
                    row.append(f"{a[0]}/{b[0]}" + ("" if not notes else " NG"))
                    if notes:
                        failures += 1
                        print(f"  NG [{label}] {path}")
                        for n in notes:
                            print("     ", n)
                print(f"  {label}: " + "  ".join(row))
        finally:
            for proc in (py, go):
                proc.terminate()
            for proc in (py, go):
                proc.wait(timeout=15)
    server.shutdown()
    print("差分なし" if not failures else f"差分 {failures} 件")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
