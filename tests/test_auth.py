"""ログイン（AUTH_MODE=required）の JWT 検証と、会話と利用者の紐づけ。

実際の Supabase は呼ばない。テストごとに作った鍵で JWT を署名し、JWKS の取得（auth.fetch_jwks_document）を差し替える。
確かめるのは、受け付けないトークン、alg の混同、鍵の取り直しの間隔、起動時の検査、利用者ごとの会話の分離。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from app import auth
from app.api import routes
from app.main import app

SUPABASE_URL = "https://project-ref.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
KID = "key-1"
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
QUESTION = "ザーハードの大気について教えて"


class Signer:
    """テスト用の署名鍵（EC P-256）と、その公開鍵の JWKS。"""

    def __init__(self, kid: str = KID) -> None:
        self.kid = kid
        self.private_key = ec.generate_private_key(ec.SECP256R1())

    def jwk(self) -> dict:
        data = jwt.algorithms.ECAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        return {**data, "kid": self.kid, "alg": "ES256", "use": "sig"}

    def token(self, sub: str | None = USER_A, **overrides) -> str:
        claims: dict = {
            "iss": ISSUER,
            "aud": "authenticated",
            "sub": sub,
            "role": "authenticated",
            "exp": int(time.time()) + 3600,
        }
        claims.update(overrides)
        claims = {key: value for key, value in claims.items() if value is not None}
        return jwt.encode(claims, self.private_key, algorithm="ES256", headers={"kid": self.kid})


class JwksServer:
    """auth.fetch_jwks_document の代わり。取得の回数と、返す鍵・失敗を変えられる。"""

    def __init__(self, *signers: Signer) -> None:
        self.keys = [signer.jwk() for signer in signers]
        self.calls = 0
        self.error: Exception | None = None

    def __call__(self, url: str) -> dict:
        self.calls += 1
        assert url == f"{ISSUER}/.well-known/jwks.json"
        if self.error is not None:
            raise self.error
        return {"keys": self.keys}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def signer() -> Signer:
    return Signer()


@pytest.fixture
def jwks(monkeypatch, signer) -> JwksServer:
    server = JwksServer(signer)
    monkeypatch.setattr(auth, "fetch_jwks_document", server)
    monkeypatch.setattr(auth, "_cache", None)
    return server


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(auth, "_now", clock)
    return clock


@pytest.fixture
def required(monkeypatch, jwks, clock) -> None:
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)


def _post(client: TestClient, token: str | None, thread_id: str | None = None, path: str = "/chat", **body):
    payload: dict = {"message": QUESTION, **body}
    if thread_id is not None:
        payload["thread_id"] = thread_id
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.post(path, json=payload, headers=headers)


def _human_texts(key: str) -> list[str]:
    state = routes.get_graph(True).get_state({"configurable": {"thread_id": key}}).values
    return [str(m.content) for m in state.get("messages", []) if isinstance(m, HumanMessage)]


# --- AUTH_MODE=off（既定） ---------------------------------------------------


def test_off_is_the_default_and_needs_no_token(client, monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert _post(client, None).status_code == 200


def test_off_ignores_any_authorization_header(client, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "off")
    assert _post(client, "not-a-token").status_code == 200


def test_off_keeps_the_plain_thread_id_as_the_history_key(client, monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    thread_id = str(uuid.uuid4())
    assert _post(client, None, thread_id).status_code == 200
    assert _human_texts(thread_id) == [QUESTION]


# --- 受け付けるトークン ------------------------------------------------------


def test_valid_token_is_accepted_on_chat_and_stream(client, required, signer):
    token = signer.token()
    assert _post(client, token).status_code == 200
    res = _post(client, token, path="/chat/stream")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")


def test_history_key_is_prefixed_with_the_sub_and_the_client_sees_the_plain_thread_id(client, required, signer):
    thread_id = str(uuid.uuid4())
    res = _post(client, signer.token(sub=USER_A), thread_id)
    assert res.json()["thread_id"] == thread_id
    assert _human_texts(f"{USER_A}:{thread_id}") == [QUESTION]
    assert _human_texts(thread_id) == []  # 接頭辞なしのキーには何も書かない


def test_stream_reports_the_plain_thread_id(client, required, signer):
    thread_id = str(uuid.uuid4())
    res = _post(client, signer.token(), thread_id, path="/chat/stream")
    assert f'"thread_id": "{thread_id}"' in res.text or f'"thread_id":"{thread_id}"' in res.text
    assert _human_texts(f"{USER_A}:{thread_id}") == [QUESTION]


def test_uppercase_sub_is_normalised_to_one_key(client, required, signer):
    thread_id = str(uuid.uuid4())
    _post(client, signer.token(sub=USER_A.upper()), thread_id)
    _post(client, signer.token(sub=USER_A), thread_id)
    assert _human_texts(f"{USER_A}:{thread_id}") == [QUESTION, QUESTION]


def test_client_supplied_user_ids_are_never_used(client, required, signer):
    thread_id = str(uuid.uuid4())
    res = client.post(
        "/chat",
        json={"message": QUESTION, "thread_id": thread_id, "user_id": USER_B, "sub": USER_B},
        headers={"Authorization": f"Bearer {signer.token(sub=USER_A)}", "X-User-Id": USER_B},
    )
    assert res.status_code == 200
    assert _human_texts(f"{USER_A}:{thread_id}") == [QUESTION]
    assert _human_texts(f"{USER_B}:{thread_id}") == []


def test_a_thread_id_cannot_carry_another_users_prefix(client, required, signer):
    res = _post(client, signer.token(sub=USER_B), f"{USER_A}:abc")
    assert res.status_code == 422  # ':' は thread_id に使えない（接頭辞を偽造できない）


# --- 会話の分離 --------------------------------------------------------------


def test_two_users_with_the_same_thread_id_get_separate_histories(client, required, signer):
    thread_id = str(uuid.uuid4())
    _post(client, signer.token(sub=USER_A), thread_id, message="Aの質問です")
    _post(client, signer.token(sub=USER_B), thread_id, message="Bの質問です")
    assert _human_texts(f"{USER_A}:{thread_id}") == ["Aの質問です"]
    assert _human_texts(f"{USER_B}:{thread_id}") == ["Bの質問です"]


def test_two_users_are_separate_through_ainvoke(required, signer):
    """routes と同じ graph.ainvoke で、同じ thread_id を別の利用者が使っても履歴が混ざらない。"""
    thread_id = str(uuid.uuid4())
    graph = routes.get_graph(True)

    async def run(user: str, text: str) -> None:
        config = {"configurable": {"thread_id": auth.conversation_key(user, thread_id)}}
        await graph.ainvoke({"messages": [HumanMessage(content=text)], "context": ""}, config)

    async def both() -> None:
        await run(USER_A, "Aの質問です")
        await run(USER_B, "Bの質問です")
        await run(USER_A, "Aの続きです")

    asyncio.run(both())
    assert _human_texts(f"{USER_A}:{thread_id}") == ["Aの質問です", "Aの続きです"]
    assert _human_texts(f"{USER_B}:{thread_id}") == ["Bの質問です"]


def test_conversation_key():
    assert auth.conversation_key(None, "abc") == "abc"
    assert auth.conversation_key(USER_A, "abc") == f"{USER_A}:abc"


# --- 受け付けないトークン ----------------------------------------------------


def _assert_unauthorized(res) -> None:
    assert res.status_code == 401
    assert res.json() == {"detail": auth._DETAIL_UNAUTHORIZED}
    assert res.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
def test_missing_token_is_401(client, required, path):
    _assert_unauthorized(_post(client, None, path=path))


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Basic abc", "abc", "Token abc"])
def test_malformed_authorization_header_is_401(client, required, header):
    res = client.post("/chat", json={"message": QUESTION}, headers={"Authorization": header})
    _assert_unauthorized(res)


def test_garbage_token_is_401(client, required):
    _assert_unauthorized(_post(client, "not.a.jwt"))


def test_expired_token_is_401(client, required, signer):
    _assert_unauthorized(_post(client, signer.token(exp=1)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://other.supabase.co/auth/v1"},
        {"aud": "other"},
        {"aud": None},
        {"iss": None},
        {"exp": None},
        {"sub": None},
        {"sub": "not-a-uuid"},
        {"sub": ""},
        {"sub": 123},
        {"role": "anon"},
        {"role": None},
        {"is_anonymous": True},
    ],
    ids=lambda o: json.dumps(o),
)
def test_bad_claims_are_401(client, required, signer, overrides):
    _assert_unauthorized(_post(client, signer.token(**overrides)))


def test_token_signed_by_another_key_with_the_same_kid_is_401(client, required):
    _assert_unauthorized(_post(client, Signer(kid=KID).token()))


def test_unknown_kid_is_401(client, required):
    _assert_unauthorized(_post(client, Signer(kid="other").token()))


def test_token_without_kid_is_401(client, required, signer):
    token = jwt.encode(
        {"iss": ISSUER, "aud": "authenticated", "sub": USER_A, "exp": 4_000_000_000},
        signer.private_key,
        algorithm="ES256",
    )
    _assert_unauthorized(_post(client, token))


def test_alg_none_is_401(client, required):
    token = jwt.encode(
        {"iss": ISSUER, "aud": "authenticated", "sub": USER_A, "role": "authenticated", "exp": 4_000_000_000},
        None,
        algorithm="none",
        headers={"kid": KID},
    )
    _assert_unauthorized(_post(client, token))


def test_hs256_signed_with_the_public_key_is_401(client, required, signer):
    """公開鍵を共有秘密として HS256 で署名する攻撃（alg の混同）。"""
    public_pem = signer.private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    payload = {"iss": ISSUER, "aud": "authenticated", "sub": USER_A, "role": "authenticated", "exp": 4_000_000_000}

    # PyJWT 自身は PEM の公開鍵での HS256 署名を拒むので、署名は手で組み立てる
    def b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    body = b64(json.dumps(payload).encode())
    signature = b64(hmac.new(public_pem, head + b"." + body, hashlib.sha256).digest())
    _assert_unauthorized(_post(client, (head + b"." + body + b"." + signature).decode()))


def test_rejection_does_not_leak_the_token_or_internals(client, required, signer, caplog):
    token = signer.token(exp=1)
    with caplog.at_level("DEBUG"):
        res = _post(client, token)
    assert token not in res.text
    assert "ExpiredSignature" not in res.text and "Traceback" not in res.text
    assert token not in caplog.text and token.split(".")[1] not in caplog.text


# --- 鍵の取得と取り直し ------------------------------------------------------


def test_keys_are_fetched_once_and_reused(client, required, signer, jwks):
    for _ in range(3):
        assert _post(client, signer.token()).status_code == 200
    assert jwks.calls == 1


def test_random_kids_do_not_amplify_requests_to_the_jwks_endpoint(client, required, signer, jwks):
    assert _post(client, signer.token()).status_code == 200
    for i in range(5):
        _assert_unauthorized(_post(client, Signer(kid=f"random-{i}").token()))
    assert jwks.calls == 1  # 直近に取得済みなので、未知の kid では取り直さない


def test_a_rotated_key_is_picked_up_after_the_minimum_interval(client, required, signer, jwks, clock):
    assert _post(client, signer.token()).status_code == 200
    new = Signer(kid="key-2")
    jwks.keys = [signer.jwk(), new.jwk()]
    _assert_unauthorized(_post(client, new.token()))  # 取得から間もない: まだ知らない
    clock.now += auth._MIN_REFRESH_INTERVAL_SECONDS + 1
    assert _post(client, new.token()).status_code == 200
    assert jwks.calls == 2


def test_keys_are_refetched_after_the_ttl(client, required, signer, jwks, clock):
    assert _post(client, signer.token()).status_code == 200
    clock.now += auth._KEYS_TTL_SECONDS + 1
    assert _post(client, signer.token()).status_code == 200
    assert jwks.calls == 2


def test_cached_keys_keep_working_when_a_refetch_fails(client, required, signer, jwks, clock):
    assert _post(client, signer.token()).status_code == 200
    clock.now += auth._KEYS_TTL_SECONDS + 1
    jwks.error = OSError("down")
    assert _post(client, signer.token()).status_code == 200


def test_no_keys_and_no_jwks_is_503_with_a_fixed_message(client, required, signer, jwks):
    jwks.error = OSError("secret-host.internal refused")
    res = _post(client, signer.token())
    assert res.status_code == 503
    assert res.json() == {"detail": auth._DETAIL_UNAVAILABLE}
    assert "secret-host" not in res.text


def test_only_ec_and_rsa_keys_with_a_kid_are_used():
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True) | {"kid": "rsa-1"}
    ec_jwk = Signer("ec-1").jwk()
    document = {
        "keys": [
            rsa_jwk,
            ec_jwk,
            {"kty": "oct", "kid": "legacy", "k": "c2VjcmV0"},
            {k: v for k, v in Signer("no-kid").jwk().items() if k != "kid"},
            "junk",
        ]
    }
    assert sorted(auth._parse_keys(document)) == ["ec-1", "rsa-1"]


# --- 起動時の検査 ------------------------------------------------------------


def test_startup_off_does_not_touch_the_network(monkeypatch, jwks):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    asyncio.run(auth.verify_auth_config())
    assert jwks.calls == 0


def test_startup_required_fetches_the_keys(required, jwks):
    asyncio.run(auth.verify_auth_config())
    assert jwks.calls == 1


def test_startup_required_without_supabase_url_fails(monkeypatch, jwks, clock):
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(auth.AuthConfigError, match="SUPABASE_URL"):
        asyncio.run(auth.verify_auth_config())


@pytest.mark.parametrize(
    "url",
    [
        "http://project.supabase.co",
        "ftp://x.example",
        "https://x.supabase.co/auth/v1",
        "https://x.supabase.co?a=1",
        "x.supabase.co",
    ],
)
def test_startup_rejects_bad_supabase_urls(monkeypatch, jwks, clock, url):
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("SUPABASE_URL", url)
    with pytest.raises(auth.AuthConfigError):
        asyncio.run(auth.verify_auth_config())


def test_local_supabase_over_http_is_allowed(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:54321/")
    assert auth.supabase_url() == "http://127.0.0.1:54321"


def test_startup_fails_when_jwks_has_no_public_keys_and_does_not_fall_back(required, jwks):
    jwks.keys = [{"kty": "oct", "kid": "legacy", "k": "c2VjcmV0"}]  # 共有秘密（HS256）の旧方式
    with pytest.raises(auth.AuthConfigError, match="共有秘密による検証には切り替えません"):
        asyncio.run(auth.verify_auth_config())


def test_startup_fails_when_jwks_cannot_be_fetched(required, jwks):
    jwks.error = OSError("secret-host.internal refused")
    with pytest.raises(auth.AuthConfigError) as info:
        asyncio.run(auth.verify_auth_config())
    assert "secret-host" not in str(info.value)


def test_unknown_auth_mode_fails_instead_of_turning_auth_off(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "requried")
    with pytest.raises(auth.AuthConfigError):
        asyncio.run(auth.verify_auth_config())


def test_app_startup_fails_loudly_when_required_cannot_be_satisfied(monkeypatch, jwks):
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
    jwks.keys = []
    with pytest.raises(auth.AuthConfigError):
        with TestClient(app):
            pass


def test_app_starts_when_required_is_satisfied(required, signer):
    with TestClient(app) as started:
        assert _post(started, signer.token()).status_code == 200
        assert started.get("/health").status_code == 200  # /health は認証なし
