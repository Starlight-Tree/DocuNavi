import json
import tempfile
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings

from saas import jwt_auth

ISSUER = settings.SAAS_JWT_ISSUER


class Signer:
    """テスト用の署名鍵（EC P-256）と、その公開鍵の JWKS。"""

    def __init__(self, kid: str = "key-1") -> None:
        self.kid = kid
        self.private_key = ec.generate_private_key(ec.SECP256R1())

    def jwk(self) -> dict:
        data = jwt.algorithms.ECAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        return {**data, "kid": self.kid, "alg": "ES256", "use": "sig"}

    def token(self, sub: str | None = None, **overrides) -> str:
        claims: dict = {
            "iss": ISSUER,
            "aud": "authenticated",
            "sub": sub or str(uuid.uuid4()),
            "role": "authenticated",
            "exp": int(time.time()) + 3600,
        }
        claims.update(overrides)
        claims = {key: value for key, value in claims.items() if value is not None}
        return jwt.encode(claims, self.private_key, algorithm="ES256", headers={"kid": self.kid})


def install_keys(*signers: Signer) -> str:
    """署名鍵の JWKS を一時ファイルに書き、検証側の鍵の置き場をそれに向ける。ファイルのパスを返す。"""
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"keys": [s.jwk() for s in signers]}, handle)
    handle.close()
    settings.SAAS_JWKS_FILE = handle.name
    jwt_auth.reset_keys()
    return handle.name
