"""測定用の疑似 JWT と、その公開鍵の JWKS を書き出す（鍵は使い捨て。Supabase の実鍵ではない）。

使い方: python bench/make_fixtures.py <出力ディレクトリ>   → jwks.json, tokens.json
Django 側（.venv-django）と app 側（.venv）の両方が同じファイルを読み、同じ条件で検証する。
"""

import json
import sys
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

ISSUER = "http://127.0.0.1:54321/auth/v1"
KID = "key-1"


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    jwk = {
        **jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True),
        "kid": KID,
        "alg": "ES256",
        "use": "sig",
    }
    (out / "jwks.json").write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")

    sub = str(uuid.uuid4())

    def make(signing=key, kid=KID, alg="ES256", **overrides) -> str:
        claims = {
            "iss": ISSUER,
            "aud": "authenticated",
            "sub": sub,
            "role": "authenticated",
            "exp": int(time.time()) + 86400,
        }
        claims.update(overrides)
        return jwt.encode(claims, signing, algorithm=alg, headers={"kid": kid})

    tokens = {
        "sub": sub,
        "valid": make(),
        "expired": make(exp=int(time.time()) - 3600),
        "wrong_aud": make(aud="other"),
        "wrong_iss": make(iss="http://evil.example/auth/v1"),
        "bad_signature": make(signing=other),
        "unknown_kid": make(kid="other"),
        "bad_sub": make(sub="not-a-uuid"),
        "anonymous": make(is_anonymous=True),
        "alg_none": jwt.encode({"sub": sub}, None, algorithm="none", headers={"kid": KID}),
    }
    (out / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    print(f"wrote {out}/jwks.json and tokens.json ({len(tokens) - 1} tokens)")


if __name__ == "__main__":
    main()
