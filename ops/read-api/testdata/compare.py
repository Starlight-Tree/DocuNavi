"""Python の API と Go の読み取り API に同じ要求を送り、応答（状態・本文・主なヘッダー）が同じかを確かめる。

  python ops/read-api/testdata/compare.py <Python の URL> <Go の URL> [--bearer <トークン>]

同じ DB（testdata/seed.sql を入れたもの）を読ませた 2 つのサーバーが前提。標準ライブラリだけ。違いがあれば表示して、終了コード 1。
本文は、JSON として等しいか（意味）と、バイト列まで等しいか（Go は U+2028/2029 を \\u エスケープするなど、意味は同じでも綴りが違いうる）を分けて見る。
"""

import json
import sys
import urllib.error
import urllib.request

PATHS = ["/collections", "/targets", "/doc-types", "/documents", "/nope"]
# 資料 1 件（seed.sql の資料の id は 1〜10。11 は無い）と会話の一覧。{id} の検査（422）と、PDF・画像の経路を含む
DETAIL_PATHS = (
    [f"/documents/{i}/{leaf}" for i in list(range(1, 12)) for leaf in ("chunks", "file", "hero")]
    + [
        f"/documents/{i}/{leaf}"
        for i in ("abc", "0", "-5", "9223372036854775808", "1.0", "%201%20", "1_0", "1e2")
        for leaf in ("chunks", "file", "hero")
    ]
    + [
        "/threads",
        "/documents/1",
    ]  # /threads/{id}/messages と /documents/{id}/graph は、Python だけが返す（Go は 404 の別の文）
)
HEADERS = [
    "content-type",
    "cache-control",
    "www-authenticate",
    "allow",
    "content-disposition",
    "x-content-type-options",
]


def fetch(base: str, path: str, bearer: str | None, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(base + path, method=method)
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def check(py: str, go: str, path: str, method: str, bearer: str | None) -> tuple[tuple, tuple, list[str]]:
    """同じ要求を両方に送る。違い（状態・主なヘッダー・本文）の一覧を返す。空なら同じ。"""
    a, b = fetch(py, path, bearer, method), fetch(go, path, bearer, method)
    notes = []
    if a[0] != b[0]:
        notes.append(f"状態 {a[0]} != {b[0]}")
    for name in HEADERS:
        if a[1].get(name) != b[1].get(name):
            notes.append(f"{name}: {a[1].get(name)!r} != {b[1].get(name)!r}")
    try:
        same_json = json.loads(a[2]) == json.loads(b[2])
    except ValueError:
        same_json = a[2] == b[2]
    if not same_json:
        notes.append(f"本文（JSON）が違う\n    py: {a[2][:300]!r}\n    go: {b[2][:300]!r}")
    elif a[2] != b[2]:
        notes.append(f"本文の綴りだけ違う（JSON は同じ）\n    py: {a[2][:200]!r}\n    go: {b[2][:200]!r}")
    return a, b, notes


def main() -> int:
    args = [a for a in sys.argv[1:]]
    bearer = None
    if "--bearer" in args:
        i = args.index("--bearer")
        bearer = args[i + 1]
        del args[i : i + 2]
    if len(args) != 2:
        print(__doc__)
        return 2
    py, go = args
    failures = 0
    cases = [(p, "GET") for p in PATHS + DETAIL_PATHS] + [
        ("/collections", "POST"),
        ("/documents/1/chunks", "POST"),
        ("/threads", "POST"),
    ]
    for path, method in cases:
        a, b, notes = check(py, go, path, method, bearer)
        failures += bool(notes)
        print(f"{'NG' if notes else 'OK'} {method} {path} -> {a[0]} / {b[0]}  ({len(a[2])} B / {len(b[2])} B)")
        for n in notes:
            print("   ", n)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
