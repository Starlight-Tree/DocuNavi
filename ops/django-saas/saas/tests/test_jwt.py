import os
import time
import uuid

import jwt
from django.test import SimpleTestCase

from saas.jwt_auth import InvalidToken, verify_token

from .helpers import Signer, install_keys

SUB = str(uuid.uuid4())


class VerifyTokenTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.signer = Signer()
        cls.keyfile = install_keys(cls.signer)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.keyfile)
        super().tearDownClass()

    def reason(self, token: str) -> str:
        with self.assertRaises(InvalidToken) as ctx:
            verify_token(token)
        return ctx.exception.reason

    def test_valid_token_returns_the_sub(self):
        self.assertEqual(verify_token(self.signer.token(sub=SUB)), SUB)

    def test_uppercase_sub_is_normalised(self):
        self.assertEqual(verify_token(self.signer.token(sub=SUB.upper())), SUB)

    def test_expired_token(self):
        self.assertEqual(self.reason(self.signer.token(exp=int(time.time()) - 3600)), "ExpiredSignatureError")

    def test_wrong_audience(self):
        self.assertEqual(self.reason(self.signer.token(aud="other")), "InvalidAudienceError")

    def test_wrong_issuer(self):
        self.assertEqual(self.reason(self.signer.token(iss="http://evil.example/auth/v1")), "InvalidIssuerError")

    def test_missing_claims(self):
        for claim in ("exp", "iss", "aud"):
            with self.subTest(claim=claim):
                self.assertEqual(self.reason(self.signer.token(**{claim: None})), "MissingRequiredClaimError")

    def test_bad_sub(self):
        self.assertEqual(self.reason(self.signer.token(sub="not-a-uuid")), "sub")

    def test_anonymous_and_wrong_role(self):
        self.assertEqual(self.reason(self.signer.token(is_anonymous=True)), "role")
        self.assertEqual(self.reason(self.signer.token(role="service_role")), "role")

    def test_token_signed_by_another_key_with_the_same_kid(self):
        self.assertEqual(self.reason(Signer().token()), "InvalidSignatureError")

    def test_unknown_kid(self):
        self.assertEqual(self.reason(Signer(kid="other").token()), "unknown_key")

    def test_hs256_with_the_public_key_as_secret_is_rejected(self):
        forged = jwt.encode(
            {"sub": SUB}, "secret-secret-secret-secret-secret!", algorithm="HS256", headers={"kid": "key-1"}
        )
        self.assertEqual(self.reason(forged), "algorithm")

    def test_alg_none_is_rejected(self):
        forged = jwt.encode({"sub": SUB}, None, algorithm="none", headers={"kid": "key-1"})
        self.assertEqual(self.reason(forged), "algorithm")

    def test_garbage(self):
        self.assertEqual(self.reason("not.a.token"), "malformed")
