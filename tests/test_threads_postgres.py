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

        # 発言は本人にだけ読める
        read = client.get(f"/threads/{second}/messages", headers=a)
        assert read.status_code == 200
        assert [m["role"] for m in read.json()["messages"]] == ["user", "assistant"]
        assert read.json()["messages"][0]["text"] == "アリスの二つ目の質問です"
        assert client.get(f"/threads/{theirs}/messages", headers=a).status_code == 404

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


def test_the_scope_of_a_conversation_survives_the_postgres_checkpointer(db, required, signer):  # noqa: F811
    # 検索対象（collection）は会話の状態としてチェックポイントに残る。続きの会話の復元・409・履歴の取得が、本物の DB でも成り立つ
    a = _bearer(signer, str(uuid.uuid4()))

    with TestClient(app) as client:
        first = client.post("/chat", json={"message": "最初の質問です", "collection": "zahard"}, headers=a)
        assert first.status_code == 200 and first.json()["collection"] == "zahard"
        thread = first.json()["thread_id"]

        # 省略なら復元される。違う値は 409（両方の検索対象を載せた固定文）
        assert client.post("/chat", json={"message": "続き", "thread_id": thread}, headers=a).json()["collection"] == (
            "zahard"
        )
        conflict = client.post("/chat", json={"message": "別", "thread_id": thread, "collection": "cafe"}, headers=a)
        assert conflict.status_code == 409
        assert "「zahard」" in conflict.json()["detail"] and "「cafe」" in conflict.json()["detail"]
        streamed = client.post(
            "/chat/stream", json={"message": "別", "thread_id": thread, "collection": "cafe"}, headers=a
        )
        assert streamed.status_code == 409

        # 既存の会話を開いたとき（履歴の取得）にも分かる。検索対象の無い会話は null
        assert client.get(f"/threads/{thread}/messages", headers=a).json()["collection"] == "zahard"
        plain = client.post("/chat", json={"message": "検索対象なし"}, headers=a).json()["thread_id"]
        assert client.get(f"/threads/{plain}/messages", headers=a).json()["collection"] is None
