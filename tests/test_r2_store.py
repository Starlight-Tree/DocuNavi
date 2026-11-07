"""R2（S3 互換）の置き場（app/r2_store.py）と、それを使う元 PDF の配信・取り込み。S3 は偽のクライアントで、実 R2 にはつながない。

確かめるのは、PDF_STORE=r2 でも `storage/pdf/` と同じ規則（名前が完全一致・ハッシュが合うものだけ）で返すこと、R2 を読めないときは
「見つからない」にせず 503 の固定文にして（ローカルへ切り替えない）、鍵の値をどこにも出さないこと、`r2://` の取り込みとアップロードの計画。
"""

import hashlib
import json
import logging
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app import pdf_originals, r2_store
from app.api import documents
from app.ingestion import __main__ as cli
from tests.test_document_file import PDF_A, PDF_B, doc_row, sha
from tests.test_documents import FakePool, chunk_row, use_pool  # noqa: F401

KEY_ID = "KEY-ID-DO-NOT-LOG"
SECRET_KEY = "SECRET-ACCESS-KEY-DO-NOT-LOG"
CREDS = json.dumps({"access_key_id": KEY_ID, "secret_access_key": SECRET_KEY})
ENV = {"R2_ACCOUNT_ID": "acct", "R2_BUCKET": "docunavi-documents", "R2_CREDENTIALS_JSON": CREDS}


def client_error(code: str, status: int = 400, op: str = "GetObject") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"{KEY_ID} {SECRET_KEY}"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int = -1) -> bytes:
        return self._data if n < 0 else self._data[:n]


class FakeS3:
    """list_objects_v2 / get_object / head_object / put_object だけの S3。ページは 2 件ずつにして、続きの取得も通す。"""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.error: Exception | None = None
        self.puts: list[str] = []

    def _check(self) -> None:
        if self.error:
            raise self.error

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, ContinuationToken=None):  # noqa: N803
        self._check()
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        prefixes: list[str] = []
        contents: list[str] = []
        for k in keys:
            rest = k[len(Prefix) :]
            if Delimiter and Delimiter in rest:
                p = Prefix + rest.split(Delimiter)[0] + Delimiter
                if p not in prefixes:
                    prefixes.append(p)
            else:
                contents.append(k)
        items = [("p", p) for p in prefixes] + [("c", c) for c in contents]
        start = int(ContinuationToken or 0)
        page = items[start : start + 2]
        more = start + 2 < len(items)
        out = {
            "CommonPrefixes": [{"Prefix": v} for t, v in page if t == "p"],
            "Contents": [{"Key": v} for t, v in page if t == "c"],
            "IsTruncated": more,
        }
        if more:
            out["NextContinuationToken"] = str(start + 2)
        return out

    def get_object(self, Bucket, Key):  # noqa: N803
        self._check()
        if Key not in self.objects:
            raise client_error("NoSuchKey", 404)
        data = self.objects[Key]
        return {"Body": FakeBody(data), "ContentLength": len(data)}

    def head_object(self, Bucket, Key):  # noqa: N803
        self._check()
        if Key not in self.objects:
            raise client_error("404", 404, "HeadObject")
        return {"ETag": f'"{hashlib.md5(self.objects[Key]).hexdigest()}"'}

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
        self._check()
        assert ContentType == "application/pdf"
        self.objects[Key] = Body
        self.puts.append(Key)


@pytest.fixture
def s3() -> FakeS3:
    return FakeS3({"cafe/a.pdf": PDF_A, "cafe/b.pdf": PDF_B, "guides/a.pdf": PDF_B, "zahard/z.pdf": PDF_A})


@pytest.fixture
def r2(monkeypatch, s3) -> FakeS3:
    """PDF_STORE=r2 にして、置き場を偽の S3 に差し替える。"""
    monkeypatch.setenv("PDF_STORE", "r2")
    r2_store.default_store.cache_clear()
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(s3, "docunavi-documents"))
    return s3


# --- 設定 -------------------------------------------------------------------------------------------------


def test_config_is_read_from_the_environment_and_never_shows_the_secret() -> None:
    config = r2_store.config_from_env(ENV)
    assert config.endpoint == "https://acct.r2.cloudflarestorage.com" and config.bucket == "docunavi-documents"
    assert config.access_key_id == KEY_ID
    assert KEY_ID not in repr(config) and SECRET_KEY not in repr(config)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "missing: R2_ACCOUNT_ID, R2_BUCKET, R2_CREDENTIALS_JSON"),
        ({**ENV, "R2_BUCKET": ""}, "missing: R2_BUCKET"),
        ({**ENV, "R2_CREDENTIALS_JSON": "not json " + SECRET_KEY}, "R2_CREDENTIALS_JSON is not valid JSON"),
        ({**ENV, "R2_CREDENTIALS_JSON": json.dumps({"access_key_id": KEY_ID})}, "R2_CREDENTIALS_JSON needs"),
        ({**ENV, "R2_CREDENTIALS_JSON": "[1]"}, "R2_CREDENTIALS_JSON needs"),
    ],
)
def test_bad_config_is_reported_by_name_without_values(env, expected) -> None:
    with pytest.raises(r2_store.R2ConfigError) as exc:
        r2_store.config_from_env(env)
    assert expected in str(exc.value) and SECRET_KEY not in str(exc.value) and KEY_ID not in str(exc.value)


def test_the_client_is_pointed_at_the_account_endpoint_with_signature_v4() -> None:
    client = r2_store.make_client(r2_store.config_from_env(ENV))
    assert client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert client.meta.config.signature_version == "s3v4"


# --- R2Store -----------------------------------------------------------------------------------------------


def test_collections_are_the_top_level_prefixes_across_pages(s3) -> None:
    assert r2_store.R2Store(s3, "b").collections() == ["cafe", "guides", "zahard"]


def test_pdf_keys_are_under_the_prefix_and_only_pdfs(s3) -> None:
    s3.objects["cafe/readme.txt"] = b"x"
    s3.objects["cafe/UPPER.PDF"] = PDF_A
    assert r2_store.R2Store(s3, "b").pdf_keys("cafe/") == ["cafe/UPPER.PDF", "cafe/a.pdf", "cafe/b.pdf"]


def test_get_returns_none_for_missing_or_too_large_but_raises_on_other_errors(s3) -> None:
    store = r2_store.R2Store(s3, "b")
    assert store.get("cafe/a.pdf", 1000) == PDF_A
    assert store.get("cafe/none.pdf", 1000) is None
    assert store.get("cafe/a.pdf", len(PDF_A) - 1) is None
    s3.error = client_error("AccessDenied", 403)
    with pytest.raises(ClientError):
        store.get("cafe/a.pdf", 1000)


def test_etag_is_the_md5_of_the_object(s3) -> None:
    store = r2_store.R2Store(s3, "b")
    assert store.etag("cafe/a.pdf") == hashlib.md5(PDF_A).hexdigest()
    assert store.etag("cafe/none.pdf") is None


# --- 元 PDF の探索（PDF_STORE=r2）------------------------------------------------------------------------------


def test_finds_the_object_whose_name_and_hash_match(r2) -> None:
    result = pdf_originals.find_original("z.pdf", sha(PDF_A))
    assert result.ok and result.data == PDF_A


def test_with_the_same_name_in_two_collections_the_matching_hash_wins(r2) -> None:
    assert pdf_originals.find_original("a.pdf", sha(PDF_B)).data == PDF_B  # cafe/a.pdf は PDF_A、guides/a.pdf は PDF_B
    assert pdf_originals.find_original("a.pdf", sha(PDF_A)).data == PDF_A


def test_same_name_but_other_content_is_a_mismatch(r2, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        result = pdf_originals.find_original("z.pdf", sha(b"other"))
    assert (result.ok, result.reason) == (False, "mismatch")


def test_a_missing_hash_cannot_be_verified(r2) -> None:
    assert pdf_originals.find_original("z.pdf", None).reason == "mismatch"


def test_an_unknown_name_is_missing(r2) -> None:
    assert pdf_originals.find_original("nope.pdf", sha(PDF_A)).reason == "missing"


@pytest.mark.parametrize(
    "name", ["../x.pdf", "cafe/../a.pdf", "a//b.pdf", "/abs.pdf", "a/b/c/d.pdf", "..", "", "a\\b.pdf"]
)
def test_names_that_are_not_plain_file_names_are_missing_without_asking_r2(r2, name) -> None:
    r2.error = RuntimeError("must not be called")
    assert pdf_originals.find_original(name, sha(PDF_A)).reason == "missing"


def test_a_relative_source_is_looked_up_as_collection_slash_source(r2) -> None:
    # 合成資料の source は `<サブディレクトリ>/<ファイル>.pdf`。R2 のキーは `<資料群>/<source>`
    r2.objects["synthetic-test/sub/n.pdf"] = PDF_B
    result = pdf_originals.find_original("sub/n.pdf", sha(PDF_B))
    assert result.ok and result.data == PDF_B
    assert pdf_originals.find_original("sub/n.pdf", sha(PDF_A)).reason == "mismatch"
    assert pdf_originals.find_original("other/n.pdf", sha(PDF_B)).reason == "missing"


@pytest.mark.parametrize(
    "error", [client_error("AccessDenied", 403), client_error("InternalError", 500), TimeoutError("slow")]
)
def test_an_unreadable_store_is_unavailable_not_missing(r2, caplog, error) -> None:
    r2.error = error
    with caplog.at_level(logging.WARNING):
        result = pdf_originals.find_original("z.pdf", sha(PDF_A))
    assert (result.ok, result.reason) == (False, "unavailable")
    assert KEY_ID not in caplog.text and SECRET_KEY not in caplog.text  # 例外の本文（鍵の ID が入りうる）は出さない


def test_it_does_not_fall_back_to_local_files(r2, tmp_path, monkeypatch) -> None:
    (tmp_path / "cafe").mkdir()
    (tmp_path / "cafe" / "z.pdf").write_bytes(PDF_A)
    monkeypatch.setattr(pdf_originals, "PDF_DIR", tmp_path)
    r2.error = client_error("AccessDenied", 403)
    assert pdf_originals.find_original("z.pdf", sha(PDF_A)).reason == "unavailable"


def test_missing_configuration_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("PDF_STORE", "r2")
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    r2_store.default_store.cache_clear()
    assert pdf_originals.find_original("z.pdf", sha(PDF_A)).reason == "unavailable"


def test_the_default_store_is_still_the_local_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PDF_STORE", raising=False)
    monkeypatch.setattr(pdf_originals, "PDF_DIR", tmp_path)
    (tmp_path / "cafe").mkdir()
    (tmp_path / "cafe" / "x.pdf").write_bytes(PDF_A)
    assert pdf_originals.find_original("x.pdf", sha(PDF_A)).ok


# --- API ----------------------------------------------------------------------------------------------------


def test_file_endpoint_serves_from_r2(client, use_pool, r2) -> None:  # noqa: F811
    use_pool(FakePool(document=[doc_row(source="z.pdf", content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 200 and res.content == PDF_A
    assert res.headers["content-type"] == "application/pdf" and res.headers["cache-control"] == "no-store"


def test_file_endpoint_answers_503_with_a_fixed_message_when_r2_fails(client, use_pool, r2) -> None:  # noqa: F811
    use_pool(FakePool(document=[doc_row(source="z.pdf", content_hash=sha(PDF_A))]))
    r2.error = client_error("AccessDenied", 403)
    res = client.get("/documents/1/file")
    assert res.status_code == 503
    assert res.json() == {"detail": documents._DETAIL_ORIGINAL_UNAVAILABLE}
    assert KEY_ID not in res.text and SECRET_KEY not in res.text


def test_file_endpoint_keeps_404_and_409_for_missing_and_mismatch(client, use_pool, r2) -> None:  # noqa: F811
    use_pool(FakePool(document=[doc_row(source="nope.pdf", content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404
    use_pool(FakePool(document=[doc_row(source="z.pdf", content_hash=sha(PDF_B))]))
    assert client.get("/documents/1/file").status_code == 409


@pytest.mark.parametrize(
    ("source", "content_hash", "error"),
    [
        ("z.pdf", sha(PDF_A), None),  # 読める
        ("nope.pdf", sha(PDF_A), None),  # R2 に無い
        ("z.pdf", sha(PDF_B), None),  # 名前は同じで中身が違う
        ("z.pdf", sha(PDF_A), client_error("AccessDenied", 403)),  # R2 を読めない（鍵の失効など）
    ],
)
def test_the_document_screen_keeps_working_whatever_r2_answers(
    client,
    use_pool,
    r2,
    source,
    content_hash,
    error,  # noqa: F811
) -> None:
    """PDF_STORE=r2 に切り替えたあと、PDF が R2 に無い・読めないときも、資料一覧の取得（/chunks）は 200 で、元 PDF の有無だけが変わる。"""
    use_pool(FakePool(document=[doc_row(source=source, content_hash=content_hash)], chunks=[chunk_row(0)]))
    r2.error = error
    res = client.get("/documents/1/chunks")
    assert res.status_code == 200
    assert res.json()["original_pdf"] is (source == "z.pdf" and content_hash == sha(PDF_A) and error is None)
    assert KEY_ID not in res.text and SECRET_KEY not in res.text


# --- r2:// の取り込み -----------------------------------------------------------------------------------------


@pytest.fixture
def ingest_env(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("D1_API_URL", "")


def real_pdfs() -> dict[str, bytes]:
    root = Path(__file__).resolve().parents[1] / "storage" / "pdf" / "cafe"
    return {f"cafe/{p.name}": p.read_bytes() for p in sorted(root.glob("*.pdf"))[:1]}


def test_ingestion_reads_a_collection_from_r2_with_the_prefix_as_its_collection(
    ingest_env, monkeypatch, capsys
) -> None:
    objects = real_pdfs()
    (name,) = [k.split("/", 1)[1] for k in objects]
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(FakeS3(objects), "b"))
    assert cli.main(["--input", "r2://cafe"]) == 0
    out = capsys.readouterr().out
    assert name in out and "collection=cafe" in out and "status=would_insert" in out
    assert "確認だけ" in out


def test_ingestion_can_take_a_single_object(ingest_env, monkeypatch, capsys) -> None:
    objects = {**real_pdfs(), "cafe/other.pdf": PDF_A}
    (name,) = [k.split("/", 1)[1] for k in real_pdfs()]
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(FakeS3(objects), "b"))
    assert cli.main(["--input", f"r2://cafe/{name}"]) == 0
    out = capsys.readouterr().out
    assert name in out and "other.pdf" not in out and "文書 1 件" in out


def test_ingestion_from_r2_reports_missing_input_config_and_failures(ingest_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(FakeS3({}), "b"))
    assert cli.main(["--input", "r2://cafe"]) == 2  # PDF が無い
    assert cli.main(["--input", "r2://bad name"]) == 2  # 資料群の名前の形
    capsys.readouterr()

    def unconfigured():
        raise r2_store.R2ConfigError("missing: R2_BUCKET")

    monkeypatch.setattr(r2_store, "default_store", unconfigured)
    assert cli.main(["--input", "r2://cafe"]) == 2
    assert "R2_BUCKET" in capsys.readouterr().err

    broken = FakeS3({"cafe/a.pdf": PDF_A})
    broken.error = client_error("AccessDenied", 403)
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(broken, "b"))
    assert cli.main(["--input", "r2://cafe"]) == 1
    err = capsys.readouterr().err
    assert "ClientError" in err and KEY_ID not in err and SECRET_KEY not in err


def test_the_temporary_download_is_removed(ingest_env, monkeypatch) -> None:
    seen: list[Path] = []
    original = r2_store.download_collection

    def spy(store, collection, target, max_bytes, only=None):
        seen.append(target)
        return original(store, collection, target, max_bytes, only)

    monkeypatch.setattr(r2_store, "download_collection", spy)
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(FakeS3(real_pdfs()), "b"))
    assert cli.main(["--input", "r2://cafe"]) == 0
    assert seen and not seen[0].exists()


def test_download_refuses_keys_that_would_leave_the_target(tmp_path) -> None:
    store = r2_store.R2Store(FakeS3({"cafe/../evil.pdf": PDF_A, "cafe/ok.pdf": PDF_A}), "b")
    files = r2_store.download_collection(store, "cafe", tmp_path / "t", 1000)
    assert [f.name for f in files] == ["ok.pdf"] and not (tmp_path / "evil.pdf").exists()


# --- アップロード -----------------------------------------------------------------------------------------------


def make_local(tmp_path: Path) -> Path:
    for rel, data in {
        "cafe/a.pdf": PDF_A,
        "cafe/new.pdf": PDF_B,
        "guides/g.pdf": PDF_A,
        "top.pdf": PDF_A,
        "cafe/note.txt": b"x",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    return tmp_path


def test_upload_plan_classifies_new_same_and_changed(tmp_path) -> None:
    s3 = FakeS3({"cafe/a.pdf": PDF_A, "guides/g.pdf": PDF_B})
    plan = r2_store.upload_plan(r2_store.R2Store(s3, "b"), make_local(tmp_path))
    assert [(k, state) for k, _, state in plan] == [
        ("cafe/a.pdf", "same"),
        ("cafe/new.pdf", "new"),
        ("guides/g.pdf", "changed"),
    ]  # top.pdf と .txt は対象外


def test_upload_defaults_to_a_dry_run(tmp_path, monkeypatch, capsys) -> None:
    s3 = FakeS3({"cafe/a.pdf": PDF_A})
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(s3, "b"))
    assert r2_store.main(["upload", "--input", str(make_local(tmp_path))]) == 0
    assert s3.puts == [] and "確認だけ" in capsys.readouterr().out


def test_upload_apply_puts_only_what_differs(tmp_path, monkeypatch, capsys) -> None:
    s3 = FakeS3({"cafe/a.pdf": PDF_A, "guides/g.pdf": PDF_B})
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(s3, "b"))
    assert r2_store.main(["upload", "--input", str(make_local(tmp_path)), "--apply"]) == 0
    assert sorted(s3.puts) == ["cafe/new.pdf", "guides/g.pdf"] and s3.objects["guides/g.pdf"] == PDF_A
    assert "置く対象 2 件" in capsys.readouterr().out


def test_upload_reports_config_and_failures_without_values(tmp_path, monkeypatch, capsys) -> None:
    def unconfigured():
        raise r2_store.R2ConfigError("missing: R2_ACCOUNT_ID")

    monkeypatch.setattr(r2_store, "default_store", unconfigured)
    assert r2_store.main(["upload", "--input", str(make_local(tmp_path))]) == 2
    broken = FakeS3()
    broken.error = client_error("AccessDenied", 403)
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(broken, "b"))
    assert r2_store.main(["upload", "--input", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "ClientError" in err and KEY_ID not in err and SECRET_KEY not in err


# --- 一致確認（python -m app.r2_store verify）--------------------------------------------------------------------


def _ready_docs() -> list[dict]:
    return [
        {"id": 1, "source": "a.pdf", "content_hash": sha(PDF_A)},
        {"id": 2, "source": "b.pdf", "content_hash": sha(PDF_B)},
    ]


def test_verify_uses_the_same_rule_as_the_file_endpoint(r2) -> None:
    docs = _ready_docs() + [
        {"id": 3, "source": "a.pdf", "content_hash": sha(b"other bytes")},  # 名前は有るが中身が違う
        {"id": 4, "source": "nowhere.pdf", "content_hash": sha(PDF_A)},  # 無い
    ]
    lines, problems = r2_store.verify_originals(docs, pdf_originals.find_original_in_r2)
    assert [line.split()[0] for line in lines] == ["ok", "ok", "mismatch", "missing"]
    assert problems == 2


def test_verify_reports_an_unreadable_store_as_unavailable(r2) -> None:
    r2.error = client_error("AccessDenied", 403)
    lines, problems = r2_store.verify_originals(_ready_docs(), pdf_originals.find_original_in_r2)
    assert {line.split()[0] for line in lines} == {"unavailable"} and problems == 2


def _verify_env(monkeypatch, docs, s3: FakeS3) -> None:
    monkeypatch.setenv("D1_API_URL", "https://worker.example")
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(s3, "b"))
    monkeypatch.setattr(r2_store, "_ready_documents", lambda: docs)


def test_verify_exits_zero_only_when_every_ready_document_matches(monkeypatch, s3, capsys) -> None:
    _verify_env(monkeypatch, _ready_docs(), s3)
    assert r2_store.main(["verify"]) == 0
    assert "一致しない 0 件" in capsys.readouterr().out
    _verify_env(monkeypatch, _ready_docs() + [{"id": 9, "source": "x.pdf", "content_hash": sha(PDF_A)}], s3)
    assert r2_store.main(["verify"]) == 1


def test_verify_with_no_documents_is_not_a_success(monkeypatch, s3, capsys) -> None:
    _verify_env(monkeypatch, [], s3)
    assert r2_store.main(["verify"]) == 1
    assert "一致したとは言えません" in capsys.readouterr().err


def test_verify_reports_config_and_failures_without_values(monkeypatch, s3, capsys) -> None:
    monkeypatch.delenv("D1_API_URL", raising=False)
    assert r2_store.main(["verify"]) == 2
    monkeypatch.setenv("D1_API_URL", "https://worker.example")

    def unconfigured():
        raise r2_store.R2ConfigError("missing: R2_ACCOUNT_ID")

    monkeypatch.setattr(r2_store, "default_store", unconfigured)
    assert r2_store.main(["verify"]) == 2

    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(s3, "b"))

    def broken():
        raise RuntimeError("could not connect to postgresql://user:pw-DO-NOT-LOG@host/db")

    monkeypatch.setattr(r2_store, "_ready_documents", broken)
    assert r2_store.main(["verify"]) == 1
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err and "pw-DO-NOT-LOG" not in captured.out + captured.err


# --- 鍵の入れ替えへの追従（認証エラーのとき、ファイルを読み直して 1 回だけやり直す） ---------------------------------------

NEW_KEY_ID = "NEW-KEY-ID-DO-NOT-LOG"
NEW_SECRET = "NEW-SECRET-ACCESS-KEY-DO-NOT-LOG"


def creds_json(key_id: str, secret: str) -> str:
    return json.dumps({"access_key_id": key_id, "secret_access_key": secret})


def test_credentials_file_is_used_when_the_json_variable_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(creds_json(KEY_ID, SECRET_KEY) + "\n")
    env = {"R2_ACCOUNT_ID": "acct", "R2_BUCKET": "b", "R2_CREDENTIALS_FILE": str(path)}
    assert r2_store.config_from_env(env).access_key_id == KEY_ID
    path.write_text(creds_json(NEW_KEY_ID, NEW_SECRET))
    assert r2_store.config_from_env(env).access_key_id == NEW_KEY_ID  # 呼ぶたびに、いまのファイルを読む


def test_the_json_variable_wins_over_the_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(creds_json(NEW_KEY_ID, NEW_SECRET))
    assert r2_store.config_from_env({**ENV, "R2_CREDENTIALS_FILE": str(path)}).access_key_id == KEY_ID


def test_an_unreadable_credentials_file_is_a_config_error_without_the_path(tmp_path: Path) -> None:
    env = {"R2_ACCOUNT_ID": "acct", "R2_BUCKET": "b", "R2_CREDENTIALS_FILE": str(tmp_path / "missing.json")}
    with pytest.raises(r2_store.R2ConfigError) as error:
        r2_store.config_from_env(env)
    assert str(error.value) == "R2_CREDENTIALS_FILE cannot be read" and "missing.json" not in str(error.value)


def test_a_credentials_file_in_the_wrong_shape_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"access_key_id": KEY_ID}))
    env = {"R2_ACCOUNT_ID": "acct", "R2_BUCKET": "b", "R2_CREDENTIALS_FILE": str(path)}
    with pytest.raises(r2_store.R2ConfigError) as error:
        r2_store.config_from_env(env)
    assert KEY_ID not in str(error.value)


class Calls:
    def __init__(self) -> None:
        self.reloads = 0


def store_with_reload(old: FakeS3, fresh: tuple[FakeS3, str] | Exception, calls: Calls) -> r2_store.R2Store:
    def reload() -> tuple[FakeS3, str]:
        calls.reloads += 1
        if isinstance(fresh, Exception):
            raise fresh
        return fresh

    return r2_store.R2Store(old, "docunavi-documents", reload=reload, key_id=KEY_ID)


@pytest.mark.parametrize(
    ("code", "status"),
    [("InvalidAccessKeyId", 403), ("SignatureDoesNotMatch", 403), ("AccessDenied", 403), ("Unauthorized", 401)],
)
def test_an_auth_error_reloads_the_key_and_retries_once_with_the_new_client(code, status) -> None:
    old = FakeS3({"cafe/a.pdf": PDF_A})
    old.error = client_error(code, status)
    new = FakeS3({"cafe/a.pdf": PDF_A})
    calls = Calls()
    store = store_with_reload(old, (new, NEW_KEY_ID), calls)
    assert store.get("cafe/a.pdf", 1_000_000) == PDF_A
    assert calls.reloads == 1
    assert store.get("cafe/a.pdf", 1_000_000) == PDF_A  # 以降は新しいクライアントを使い続ける（読み直しは増えない）
    assert calls.reloads == 1


def test_every_operation_follows_a_key_rotation() -> None:
    old = FakeS3()
    old.error = client_error("InvalidAccessKeyId", 403)
    new = FakeS3({"cafe/a.pdf": PDF_A})
    store = store_with_reload(old, (new, NEW_KEY_ID), Calls())
    assert store.collections() == ["cafe"]
    old.error = client_error("InvalidAccessKeyId", 403)
    store = store_with_reload(old, (new, NEW_KEY_ID), Calls())
    assert store.pdf_keys("cafe/") == ["cafe/a.pdf"]
    old.error = client_error("InvalidAccessKeyId", 403)
    store = store_with_reload(old, (new, NEW_KEY_ID), Calls())
    assert store.etag("cafe/a.pdf") == hashlib.md5(PDF_A).hexdigest()


def test_an_unchanged_key_id_is_not_retried() -> None:
    """鍵は同じ（本当に権限が無い・鍵が失効した）。やり直さず、元のエラーを返す。"""
    old = FakeS3()
    old.error = client_error("AccessDenied", 403)
    new = FakeS3({"cafe/a.pdf": PDF_A})
    calls = Calls()
    store = store_with_reload(old, (new, KEY_ID), calls)
    with pytest.raises(ClientError):
        store.get("cafe/a.pdf", 1_000_000)
    assert calls.reloads == 1


def test_the_retry_happens_only_once() -> None:
    old = FakeS3()
    old.error = client_error("InvalidAccessKeyId", 403)
    new = FakeS3()
    new.error = client_error("InvalidAccessKeyId", 403)
    calls = Calls()
    store = store_with_reload(old, (new, NEW_KEY_ID), calls)
    with pytest.raises(ClientError):
        store.get("cafe/a.pdf", 1_000_000)
    assert calls.reloads == 1


@pytest.mark.parametrize(("code", "status"), [("NoSuchKey", 404), ("InternalError", 500), ("SlowDown", 503)])
def test_other_errors_do_not_reload_the_key(code, status) -> None:
    old = FakeS3()
    old.error = client_error(code, status)
    calls = Calls()
    store = store_with_reload(old, (FakeS3(), NEW_KEY_ID), calls)
    if code == "NoSuchKey":
        assert store.get("cafe/a.pdf", 1_000_000) is None
    else:
        with pytest.raises(ClientError):
            store.get("cafe/a.pdf", 1_000_000)
    assert calls.reloads == 0


def test_a_failed_reload_returns_the_original_error_without_its_own_details() -> None:
    old = FakeS3()
    old.error = client_error("InvalidAccessKeyId", 403)
    store = store_with_reload(old, r2_store.R2ConfigError("R2_CREDENTIALS_FILE cannot be read"), Calls())
    with pytest.raises(ClientError) as error:
        store.get("cafe/a.pdf", 1_000_000)
    assert error.value is old.error and error.value.__cause__ is None and error.value.__suppress_context__


def test_the_default_store_follows_a_rotated_credentials_file(monkeypatch, tmp_path: Path, caplog) -> None:
    """Cloud Run がボリュームで渡す Secret が新しい版に変わったら、再配備なしで新しい鍵に切り替わる（模擬）。"""
    path = tmp_path / "credentials.json"
    path.write_text(creds_json(KEY_ID, SECRET_KEY))
    old = FakeS3({"cafe/a.pdf": PDF_A})
    old.error = client_error("InvalidAccessKeyId", 403)  # 古い鍵は、入れ替えのあと失効した
    new = FakeS3({"cafe/a.pdf": PDF_A})
    clients = {KEY_ID: old, NEW_KEY_ID: new}
    monkeypatch.setattr(r2_store, "make_client", lambda config: clients[config.access_key_id])
    for name, value in {
        "R2_ACCOUNT_ID": "acct",
        "R2_BUCKET": "docunavi-documents",
        "R2_CREDENTIALS_FILE": str(path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("R2_CREDENTIALS_JSON", raising=False)
    r2_store.default_store.cache_clear()
    try:
        store = r2_store.default_store()  # 起動時は古い鍵
        path.write_text(creds_json(NEW_KEY_ID, NEW_SECRET))  # Secret の新しい版（ボリュームの中身が変わる）
        with caplog.at_level(logging.DEBUG):
            found = pdf_originals.find_original_in_r2("a.pdf", sha(PDF_A))
        assert found.ok and found.data == PDF_A
        assert store is r2_store.default_store()  # 置き場は作り直さない。鍵だけ入れ替わる
    finally:
        r2_store.default_store.cache_clear()
    for secret in (KEY_ID, SECRET_KEY, NEW_KEY_ID, NEW_SECRET):
        assert secret not in caplog.text


def test_an_environment_variable_key_is_not_reloaded(monkeypatch) -> None:
    """環境変数で渡された鍵は起動時のまま。読み直しても同じ ID なので、やり直しは起きない。"""
    old = FakeS3()
    old.error = client_error("InvalidAccessKeyId", 403)
    monkeypatch.setattr(r2_store, "make_client", lambda config: old)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("R2_CREDENTIALS_FILE", raising=False)
    r2_store.default_store.cache_clear()
    try:
        assert pdf_originals.find_original_in_r2("a.pdf", sha(PDF_A)).reason == "unavailable"
    finally:
        r2_store.default_store.cache_clear()


def test_the_issuers_secret_with_an_issued_at_field_is_accepted() -> None:
    """発行 Job は、キー ID・Secret Access Key に加えて issued_at（入れ替え時期の記録）を同じ JSON に入れる。アプリはそれを無視する。"""
    creds = json.dumps(
        {"access_key_id": KEY_ID, "secret_access_key": SECRET_KEY, "issued_at": "2026-10-21T12:00:00+00:00"}
    )
    config = r2_store.config_from_env({**ENV, "R2_CREDENTIALS_JSON": creds})
    assert (config.access_key_id, config.secret_access_key) == (KEY_ID, SECRET_KEY)
    assert "issued_at" not in repr(config)
