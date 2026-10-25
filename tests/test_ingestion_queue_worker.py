"""R2 の通知の取り込み（app/ingestion/queue_worker.py）。Queue・R2・DB・API は偽物で、実際には何も呼ばない。"""

import io
import json
import urllib.error

import pytest

from app.ingestion import queue_worker as qw
from app.pdf_originals import MAX_ORIGINAL_BYTES

BUCKET = "docunavi-documents"


def body(key="cafe/menu.pdf", *, action="PutObject", bucket=BUCKET, size=1000, as_string=True):
    message = {
        "account": "x",
        "bucket": bucket,
        "eventTime": "2026-10-24T20:12:23.200Z",
        "action": action,
        "object": {"key": key, "size": size, "eTag": "abc"},
    }
    return json.dumps(message) if as_string else message


def msg(n, **kwargs):
    return qw.QueueMessage(id=f"m{n}", lease_id=f"l{n}", body=body(**kwargs))


class Commands:
    """取り込み・埋め込み・存在確認の偽物。呼ばれた順と引数を記録する。"""

    def __init__(self, ingest_codes=None, embed_code=0, missing=()):
        self.ingest_codes = ingest_codes or {}
        self.embed_code = embed_code
        self.missing = set(missing)
        self.log = []

    def exists(self, key):
        self.log.append(("exists", key))
        return key not in self.missing

    def ingest(self, argv):
        self.log.append(("ingest", list(argv)))
        value = self.ingest_codes.get(argv[1].removeprefix("r2://"), 0)
        if isinstance(value, Exception):
            raise value
        return value

    def embed(self, argv):
        self.log.append(("embed", list(argv)))
        if isinstance(self.embed_code, Exception):
            raise self.embed_code
        return self.embed_code

    def kinds(self):
        return [kind for kind, _ in self.log]


def batch(messages, commands):
    return qw.process_batch(
        messages,
        bucket=BUCKET,
        exists=commands.exists,
        ingest_main=commands.ingest,
        embed_main=commands.embed,
        max_chunks=50,
        max_calls=2,
    )


# --- 通知の検査 -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["PutObject", "CopyObject", "CompleteMultipartUpload"])
def test_classify_accepts_create_actions(action):
    assert qw.classify(body(action=action), BUCKET) == qw.Target("cafe", "menu.pdf")


def test_classify_accepts_a_dict_body_and_japanese_and_uppercase_names():
    assert qw.classify(body(as_string=False), BUCKET) == qw.Target("cafe", "menu.pdf")
    assert qw.classify(body("zahard/ザーハード1.pdf"), BUCKET) == qw.Target("zahard", "ザーハード1.pdf")
    assert qw.classify(body("cafe/MENU.PDF"), BUCKET) == qw.Target("cafe", "MENU.PDF")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"bucket": "other"}, "bucket"),
        ({"action": "DeleteObject"}, "action"),
        ({"action": "LifecycleDeletion"}, "action"),
        ({"key": "menu.pdf"}, "key"),  # 資料群が無い
        ({"key": "cafe/sub/menu.pdf"}, "key"),  # 余分な階層
        ({"key": "cafe/../menu.pdf"}, "key"),
        ({"key": "../menu.pdf"}, "key"),
        ({"key": "/menu.pdf"}, "key"),
        ({"key": "cafe/"}, "key"),
        ({"key": "cafe/.."}, "key"),
        ({"key": "ca fe/menu.pdf"}, "key"),  # 資料群の形が合わない
        ({"key": "x" * 41 + "/menu.pdf"}, "key"),
        ({"key": "cafe/menu.txt"}, "key"),
        ({"key": "cafe/men\x00u.pdf"}, "key"),
        ({"size": MAX_ORIGINAL_BYTES + 1}, "size"),
    ],
)
def test_classify_drops_unsafe_or_unsupported_messages(kwargs, reason):
    assert qw.classify(body(**kwargs), BUCKET) == qw.Drop(reason)


@pytest.mark.parametrize(
    "raw", [None, 5, "not json", "[]", '"text"', "{}", json.dumps({"bucket": BUCKET, "action": "PutObject"})]
)
def test_classify_drops_malformed_bodies(raw):
    assert isinstance(qw.classify(raw, BUCKET), qw.Drop)


def test_classify_accepts_a_missing_size():
    message = {"bucket": BUCKET, "action": "PutObject", "object": {"key": "cafe/menu.pdf"}}
    assert qw.classify(message, BUCKET) == qw.Target("cafe", "menu.pdf")


# --- 1 回分の処理 ----------------------------------------------------------------------------------------------


def test_success_ingests_each_pdf_then_embeds_once_and_acks_all():
    commands = Commands()
    acks, retries, outcome = batch([msg(1, key="cafe/a.pdf"), msg(2, key="guides/b.pdf")], commands)
    assert acks == ["l1", "l2"]
    assert retries == []
    assert commands.kinds() == ["exists", "ingest", "exists", "ingest", "embed"]  # 埋め込みは取り込みのあとに 1 回
    assert commands.log[1][1] == ["--input", "r2://cafe/a.pdf", "--apply"]
    assert commands.log[-1][1] == ["--apply", "--max-chunks", "50", "--max-calls", "2"]
    assert (outcome.ingested, outcome.acked, outcome.exit_code()) == (2, 2, 0)


def test_duplicate_notifications_for_one_pdf_ingest_it_once_and_ack_every_copy():
    commands = Commands()
    acks, retries, _ = batch([msg(1), msg(2), msg(3)], commands)
    assert commands.kinds().count("ingest") == 1
    assert sorted(acks) == ["l1", "l2", "l3"]
    assert retries == []


def test_dropped_messages_are_acked_without_touching_r2_or_ingest():
    commands = Commands()
    acks, retries, outcome = batch([msg(1, action="DeleteObject"), msg(2, key="cafe/x/y.pdf")], commands)
    assert acks == ["l1", "l2"]
    assert commands.log == []  # 存在確認も取り込みも埋め込みも呼ばない
    assert outcome.drops == {"action": 1, "key": 1}
    assert outcome.exit_code() == 0


def test_missing_object_is_acked_and_not_ingested():
    commands = Commands(missing=["cafe/gone.pdf"])
    acks, retries, outcome = batch([msg(1, key="cafe/gone.pdf")], commands)
    assert acks == ["l1"]
    assert "ingest" not in commands.kinds()
    assert "embed" not in commands.kinds()
    assert outcome.drops == {"missing": 1}


def test_broken_pdf_is_acked_because_retrying_cannot_help():
    commands = Commands({"cafe/bad.pdf": 3})
    acks, retries, outcome = batch([msg(1, key="cafe/bad.pdf")], commands)
    assert acks == ["l1"]
    assert commands.kinds()[-1] == "embed"
    assert outcome.exit_code() == 0


def test_transient_ingest_failure_retries_only_that_pdf():
    commands = Commands({"cafe/a.pdf": 1})
    acks, retries, outcome = batch([msg(1, key="cafe/a.pdf"), msg(2, key="guides/b.pdf")], commands)
    assert retries == ["l1"]
    assert acks == ["l2"]
    assert commands.kinds()[-1] == "embed"  # 取り込めた分は、先へ進める
    assert outcome.exit_code() == 1


def test_exception_in_ingest_or_exists_is_a_retry_and_never_leaks_its_text(caplog):
    secret = "postgresql://user:pw@host/db"
    commands = Commands({"cafe/a.pdf": RuntimeError(secret)})
    with caplog.at_level("ERROR"):
        acks, retries, outcome = batch([msg(1, key="cafe/a.pdf")], commands)
    assert (acks, retries) == ([], ["l1"])
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
    assert outcome.exit_code() == 1


def test_config_error_leaves_the_message_unacked_and_unretried():
    commands = Commands({"cafe/a.pdf": 2})
    acks, retries, outcome = batch([msg(1, key="cafe/a.pdf")], commands)
    assert (acks, retries) == ([], [])
    assert outcome.left == 1
    assert outcome.exit_code() == 2


def test_embed_failure_retries_every_ingested_pdf():
    commands = Commands(embed_code=1)
    acks, retries, outcome = batch([msg(1, key="cafe/a.pdf"), msg(2, key="guides/b.pdf")], commands)
    assert acks == []
    assert retries == ["l1", "l2"]
    assert outcome.exit_code() == 1


def test_embed_exception_is_a_retry_and_embed_config_error_is_left():
    acks, retries, _ = batch([msg(1)], Commands(embed_code=RuntimeError("boom")))
    assert (acks, retries) == ([], ["l1"])
    acks, retries, outcome = batch([msg(1)], Commands(embed_code=2))
    assert (acks, retries, outcome.exit_code()) == ([], [], 2)


def test_dropped_messages_are_acked_even_when_embedding_fails():
    commands = Commands(embed_code=1)
    acks, retries, _ = batch([msg(1, action="DeleteObject"), msg(2)], commands)
    assert acks == ["l1"]
    assert retries == ["l2"]


# --- くり返し --------------------------------------------------------------------------------------------------


class FakeQueue:
    def __init__(self, batches):
        self.batches = list(batches)
        self.pulls = []
        self.acked = []

    def pull(self, batch_size, visibility_timeout_seconds):
        self.pulls.append((batch_size, visibility_timeout_seconds))
        return self.batches.pop(0) if self.batches else []

    def ack(self, acks, retries):
        self.acked.append((list(acks), list(retries)))


def worker(queue, commands, **kwargs):
    return qw.run_worker(
        queue,
        bucket=BUCKET,
        exists=commands.exists,
        ingest_main=commands.ingest,
        embed_main=commands.embed,
        **kwargs,
    )


def test_worker_pulls_until_the_queue_is_empty(capsys):
    queue = FakeQueue([[msg(1)], [msg(2, key="guides/b.pdf")]])
    assert worker(queue, Commands()) == 0
    assert len(queue.pulls) == 3  # 空の pull で終わる
    assert queue.acked == [(["l1"], []), (["l2"], [])]
    assert "取り込み 2 件" in capsys.readouterr().out


def test_worker_passes_batch_size_and_a_visibility_timeout_longer_than_the_work():
    queue = FakeQueue([])
    worker(queue, Commands())
    batch_size, timeout = queue.pulls[0]
    assert batch_size == qw.DEFAULT_BATCH_SIZE
    assert timeout >= 5 * 60


def test_worker_stops_at_max_batches():
    queue = FakeQueue([[msg(n, key=f"cafe/f{n}.pdf")] for n in range(10)])
    assert worker(queue, Commands(), max_batches=3) == 0
    assert len(queue.pulls) == 3
    assert len(queue.batches) == 7  # 残りは次の起動（または毎時の突き合わせ）


def test_worker_stops_after_a_config_error():
    queue = FakeQueue([[msg(1, key="cafe/a.pdf")], [msg(2, key="cafe/b.pdf")]])
    assert worker(queue, Commands({"cafe/a.pdf": 2})) == 2
    assert len(queue.pulls) == 1


def test_worker_exit_code_is_one_for_transient_failures_and_continues():
    queue = FakeQueue([[msg(1, key="cafe/a.pdf")], [msg(2, key="cafe/b.pdf")]])
    assert worker(queue, Commands({"cafe/a.pdf": 1})) == 1
    assert queue.acked == [([], ["l1"]), (["l2"], [])]


def http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "x", {}, io.BytesIO(b"secret-body"))


@pytest.mark.parametrize(("code", "expected"), [(401, 2), (403, 2), (429, 1), (500, 1)])
def test_worker_maps_pull_http_errors(code, expected, capsys):
    class Failing:
        def pull(self, *_):
            raise http_error(code)

    assert worker(Failing(), Commands()) == expected
    err = capsys.readouterr().err
    assert f"HTTP {code}" in err
    assert "secret-body" not in err


def test_worker_pull_and_ack_exceptions_print_only_the_class_name(capsys):
    class Boom:
        def pull(self, *_):
            raise RuntimeError("token=abc")

    assert worker(Boom(), Commands()) == 1
    assert "token=abc" not in capsys.readouterr().err

    class AckFails(FakeQueue):
        def ack(self, acks, retries):
            raise RuntimeError("token=abc")

    assert worker(AckFails([[msg(1)]]), Commands()) == 1
    assert "token=abc" not in capsys.readouterr().err


# --- HTTP クライアント ----------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


class FakeUrlopen:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.payload)


def test_client_pull_sends_the_documented_request_and_parses_messages():
    payload = {
        "success": True,
        "result": {
            "messages": [
                {"id": "a", "lease_id": "L1", "body": body(), "attempts": 2},
                {"id": "b", "body": "no lease"},  # lease_id が無い要素は読み飛ばす
                "garbage",
            ]
        },
    }
    urlopen = FakeUrlopen(payload)
    client = qw.QueueClient("acct", "queue-1", "tok", urlopen=urlopen)
    messages = client.pull(5, 900)
    request, timeout = urlopen.requests[0]
    assert request.full_url == "https://api.cloudflare.com/client/v4/accounts/acct/queues/queue-1/messages/pull"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"batch_size": 5, "visibility_timeout_ms": 900_000}
    assert request.get_header("Authorization") == "Bearer tok"
    assert timeout > 0
    assert messages == [qw.QueueMessage(id="a", lease_id="L1", body=body(), attempts=2)]


def test_client_ack_sends_acks_and_delayed_retries_and_skips_an_empty_call():
    urlopen = FakeUrlopen({"success": True, "result": {}})
    client = qw.QueueClient("acct", "queue-1", "tok", urlopen=urlopen)
    client.ack([], [])
    assert urlopen.requests == []
    client.ack(["L1"], ["L2"])
    request, _ = urlopen.requests[0]
    assert request.full_url.endswith("/messages/ack")
    assert json.loads(request.data) == {
        "acks": [{"lease_id": "L1"}],
        "retries": [{"lease_id": "L2", "delay_seconds": qw.RETRY_DELAY_SECONDS}],
    }


def test_client_treats_an_unsuccessful_response_as_an_error_without_its_text():
    client = qw.QueueClient("acct", "q", "tok", urlopen=FakeUrlopen({"success": False, "errors": [{"message": "tok"}]}))
    with pytest.raises(qw.QueueError) as info:
        client.pull(1, 60)
    assert "tok" not in str(info.value)


def test_client_quotes_ids_so_they_cannot_change_the_path():
    urlopen = FakeUrlopen({"success": True, "result": {"messages": []}})
    qw.QueueClient("a/../b", "q?x", "tok", urlopen=urlopen).pull(1, 60)
    url = urlopen.requests[0][0].full_url
    assert "/accounts/a%2F..%2Fb/queues/q%3Fx/messages/pull" in url


# --- コマンド -------------------------------------------------------------------------------------------------

R2_ENV = {
    "R2_ACCOUNT_ID": "acct",
    "R2_BUCKET": BUCKET,
    "R2_CREDENTIALS_JSON": json.dumps({"access_key_id": "k", "secret_access_key": "s"}),
}
FULL_ENV = {**R2_ENV, "INGEST_QUEUE_ID": "q1", "INGEST_QUEUE_TOKEN": "tok"}


def test_main_without_apply_checks_config_and_never_builds_a_client(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Queue に触れてはいけない")

    monkeypatch.setattr(qw, "QueueClient", forbidden)
    assert qw.main([], env=FULL_ENV) == 0
    assert "何も pull していません" in capsys.readouterr().out


def test_main_reports_missing_settings_by_name_only(capsys):
    assert qw.main(["--apply"], env=R2_ENV) == 2
    err = capsys.readouterr().err
    assert "INGEST_QUEUE_ID" in err
    assert "tok" not in err
    assert qw.main(["--apply"], env={}) == 2
    assert "R2_ACCOUNT_ID" in capsys.readouterr().err


def test_main_rejects_non_positive_limits(capsys):
    assert qw.main(["--batch-size", "0"], env=FULL_ENV) == 2
    assert qw.main(["--visibility-timeout", "0"], env=FULL_ENV) == 2


def test_main_apply_wires_the_client_and_a_read_only_existence_check(monkeypatch):
    from app import r2_store

    class Store:
        def __init__(self):
            self.calls = []

        def etag(self, key):
            self.calls.append(key)
            return None if key.endswith("gone.pdf") else "etag"

        def __getattr__(self, name):  # 書き込み系（put など）が呼ばれたら失敗させる
            raise AssertionError(f"R2 に書いてはいけない: {name}")

    store = Store()
    monkeypatch.setattr(r2_store, "default_store", lambda: store)
    captured = {}

    def fake_run_worker(client, **kwargs):
        captured["client"] = client
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(qw, "run_worker", fake_run_worker)
    assert qw.main(["--apply", "--batch-size", "3"], env=FULL_ENV) == 0
    assert isinstance(captured["client"], qw.QueueClient)
    assert captured["bucket"] == BUCKET
    assert captured["batch_size"] == 3
    assert captured["exists"]("cafe/a.pdf") is True
    assert captured["exists"]("cafe/gone.pdf") is False
    assert store.calls == ["cafe/a.pdf", "cafe/gone.pdf"]


def test_worker_source_has_no_r2_write_calls():
    # R2 に書かない（通知の元になる書き込みを起こさない）ことを、コードの上でも固定する
    import inspect

    source = inspect.getsource(qw)
    for forbidden in (".put(", "put_object", "delete_object", "copy_object", ".upload"):
        assert forbidden not in source
