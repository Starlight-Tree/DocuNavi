"""会話の一覧と削除を、本物の Postgres で検証する（TEST_DATABASE_URL があるときだけ動く）。

準備と実行例は tests/test_history_postgres.py と同じ（テストごとに専用スキーマを作って消す）。
メモリ版の確認は tests/test_threads.py。ここは、SQL による一覧（利用者の接頭辞での絞り込み・新しい順・上限）と、
checkpointer 経由の削除が、本物の DB で成り立つことを確かめる。
"""

import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.api import threads
from app.main import app
from tests.test_auth import SUPABASE_URL, Clock, JwksServer, Signer
from tests.test_history_postgres import db  # noqa: F401 - フィクスチャ

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").strip(), reason="TEST_DATABASE_URL が未設定"
)


@pytest.fixture
def signer() -> Signer:
    return Signer()


@pytest.fixture
def required(monkeypatch, signer) -> None:
    monkeypatch.setattr(auth, "fetch_jwks_document", JwksServer(signer))
    monkeypatch.setattr(auth, "_cache", None)
    monkeypatch.setattr(auth, "_now", Clock())
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)


def _bearer(signer: Signer, sub: str) -> dict:
    return {"Authorization": f"Bearer {signer.token(sub=sub)}"}


def _chat(client, headers: dict, message: str) -> str:
    res = client.post("/chat", json={"message": message}, headers=headers)
    assert res.status_code == 200
    return res.json()["thread_id"]


def test_list_and_delete_against_postgres(db, required, signer):  # noqa: F811
    alice, bob = str(uuid.uuid4()), str(uuid.uuid4())
    a, b = _bearer(signer, alice), _bearer(signer, bob)

    with TestClient(app) as client:
        assert client.get("/health").json()["history"] == "postgres"
        first = _chat(client, a, "アリスの最初の質問です")
        time.sleep(0.01)
        second = _chat(client, a, "アリスの二つ目の質問です")
        theirs = _chat(client, b, "ボブの質問です")

        listed = client.get("/threads", headers=a).json()["threads"]
        assert [item["thread_id"] for item in listed] == [second, first]
        assert [item["title"] for item in listed] == ["アリスの二つ目の質問です", "アリスの最初の質問です"]
        assert all(item["created_at"] and item["updated_at"] for item in listed)
        assert [item["thread_id"] for item in client.get("/threads", headers=b).json()["threads"]] == [theirs]

        # 他人の会話は消せない。持ち主の会話は残る
        assert client.delete(f"/threads/{theirs}", headers=a).status_code == 404
        assert len(client.get("/threads", headers=b).json()["threads"]) == 1

        # 本人は消せる。履歴の行も消えている
        assert client.delete(f"/threads/{first}", headers=a).status_code == 204
        assert [item["thread_id"] for item in client.get("/threads", headers=a).json()["threads"]] == [second]
        assert client.delete(f"/threads/{first}", headers=a).status_code == 404


def test_list_is_capped_against_postgres(db, required, signer, monkeypatch):  # noqa: F811
    monkeypatch.setattr(threads, "MAX_THREADS", 2)
    headers = _bearer(signer, str(uuid.uuid4()))

    with TestClient(app) as client:
        for text in ("一番目", "二番目", "三番目"):
            _chat(client, headers, text)
            time.sleep(0.01)

        items = client.get("/threads", headers=headers).json()["threads"]

    assert [item["title"] for item in items] == ["三番目", "二番目"]
