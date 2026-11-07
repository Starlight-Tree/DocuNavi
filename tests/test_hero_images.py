"""ヒーロー画像の取り込み・承認（app/hero_images）のテスト。実 DB・実 API・実 R2 は使わない（偽の Worker = 本物の SQLite、偽の R2）。

確かめるのは、画像を中身で判定すること（拡張子を信じない）、plan.json と出力の名前の突き合わせ、
1 件でも問題があれば何も入れないこと、draft で入ること、承認で同じ資料の古い approved が rejected になること。
"""

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from app import hero_images as hero
from app.d1 import SyncD1
from app.hero_images import HeroImageError

PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1024, 576) + b"\x08\x06\x00\x00\x00"
JPEG = b"\xff\xd8\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 576, 1024) + b"\x03" + b"\x00" * 9
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "


def make_plan(tmp_path: Path, items: list[dict[str, Any]] | None = None, **extra: Any) -> Path:
    body = {
        "model": "the-approved-model",
        "items": items if items is not None else [{"document_id": "3", "prompt": "p3", "aspect_ratio": "16:9"}],
        **extra,
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


class FakeStore:
    """R2Store の偽物（put したものを覚える）。"""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> None:
        self.objects[key] = (data, content_type)


def db_for(worker) -> SyncD1:
    return SyncD1("https://worker.test", transport=worker.transport())


def add_document(worker, document_id: int = 3, content_hash: str = "doc-hash") -> None:
    worker.run(
        "INSERT INTO documents (id, source, content_hash, status) VALUES (?, ?, ?, 'ready')",
        (document_id, f"d{document_id}.pdf", content_hash),
    )


# --- 画像の判定 -------------------------------------------------------------------------------------------


def test_sniff_reads_the_type_and_size_from_the_bytes():
    assert hero.sniff_image(PNG) == hero.ImageInfo("image/png", 1024, 576)
    assert hero.sniff_image(JPEG) == hero.ImageInfo("image/jpeg", 1024, 576)
    assert hero.sniff_image(WEBP) == hero.ImageInfo("image/webp", None, None)


@pytest.mark.parametrize("data", [b"", b"<svg></svg>", b"GIF89a", b"%PDF-1.4", b"\x89PNG\r\n\x1a\n"])
def test_sniff_rejects_everything_else(data):
    assert hero.sniff_image(data) is None


# --- plan.json ----------------------------------------------------------------------------------------------


def test_load_plan_hashes_the_exact_bytes_and_the_prompts(tmp_path):
    path = make_plan(tmp_path)
    plan = hero.load_plan(path)
    assert plan.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert plan.short_hash == plan.sha256[:16]
    assert plan.model == "the-approved-model"
    assert plan.items == (hero.PlanItem(3, hashlib.sha256(b"p3").hexdigest()),)


@pytest.mark.parametrize(
    "items, extra",
    [
        ([], {}),
        ([{"document_id": "x1", "prompt": "p"}], {}),
        ([{"document_id": 3, "prompt": "p"}], {}),
        ([{"document_id": "3", "prompt": ""}], {}),
        ([{"document_id": "3", "prompt": "p"}], {"model": " "}),
    ],
)
def test_load_plan_rejects_bad_plans(tmp_path, items, extra):
    path = make_plan(tmp_path, items, **extra)
    with pytest.raises(HeroImageError):
        hero.load_plan(path)


def test_load_plan_needs_a_model_name(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"items": [{"document_id": "3", "prompt": "p"}]}))
    with pytest.raises(HeroImageError, match="model"):
        hero.load_plan(path)


def test_load_plan_errors_do_not_leak_paths(tmp_path):
    with pytest.raises(HeroImageError) as exc:
        hero.load_plan(tmp_path / "missing.json")
    assert str(tmp_path) not in str(exc.value)


# --- 取り込み -------------------------------------------------------------------------------------------------


def place(tmp_path: Path, plan: hero.Plan, document_id: int, data: bytes) -> None:
    folder = tmp_path / "out" / str(document_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{plan.short_hash}.png").write_bytes(data)  # ジョブは常に .png の名前で書く（中身は JPEG のこともある）


def test_import_puts_the_image_in_r2_and_inserts_a_draft_with_the_real_mime_type(tmp_path, d1_worker):
    plan = hero.load_plan(make_plan(tmp_path))
    place(tmp_path, plan, 3, JPEG)
    add_document(d1_worker)
    store = FakeStore()
    result = hero.import_images(db_for(d1_worker), store, plan, tmp_path / "out")
    assert len(result) == 1 and result[0].document_id == 3 and result[0].bytes == len(JPEG)
    (row,) = d1_worker.rows("SELECT * FROM document_images")
    assert row["status"] == "draft" and row["mime_type"] == "image/jpeg" and row["kind"] == "hero"
    assert (row["width"], row["height"], row["byte_size"]) == (1024, 576, len(JPEG))
    assert row["document_hash"] == "doc-hash" and row["prompt_sha256"] == plan.items[0].prompt_sha256
    assert row["model"] == "the-approved-model" and row["approved_at"] is None
    assert row["r2_key"].startswith("hero/3/") and row["r2_key"].endswith(".jpg")
    assert store.objects == {row["r2_key"]: (JPEG, "image/jpeg")}  # バイト列は DB ではなく R2 に入る


def test_import_writes_nothing_if_any_image_is_missing_or_invalid(tmp_path, d1_worker):
    items = [{"document_id": "3", "prompt": "a"}, {"document_id": "4", "prompt": "b"}]
    plan = hero.load_plan(make_plan(tmp_path, items))
    place(tmp_path, plan, 3, PNG)
    add_document(d1_worker)
    store = FakeStore()
    with pytest.raises(HeroImageError, match="資料 4 の画像がありません"):
        hero.import_images(db_for(d1_worker), store, plan, tmp_path / "out")
    place(tmp_path, plan, 4, b"<html>")
    with pytest.raises(HeroImageError, match="PNG / JPEG / WebP"):
        hero.import_images(db_for(d1_worker), store, plan, tmp_path / "out")
    assert store.objects == {} and d1_worker.rows("SELECT * FROM document_images") == []


def test_import_rejects_a_document_that_is_not_in_the_database(tmp_path, d1_worker):
    plan = hero.load_plan(make_plan(tmp_path))
    place(tmp_path, plan, 3, PNG)
    store = FakeStore()
    with pytest.raises(HeroImageError, match="DB にありません"):
        hero.import_images(db_for(d1_worker), store, plan, tmp_path / "out")
    assert store.objects == {}  # DB の確認が先。R2 には何も置かない


def test_import_rejects_an_oversized_image(tmp_path, d1_worker):
    plan = hero.load_plan(make_plan(tmp_path))
    place(tmp_path, plan, 3, PNG + b"\x00" * hero.MAX_IMAGE_BYTES)
    with pytest.raises(HeroImageError, match="大きさ"):
        hero.import_images(db_for(d1_worker), FakeStore(), plan, tmp_path / "out")


# --- 承認・却下 -----------------------------------------------------------------------------------------------


def add_image(worker, image_id: int, document_id: int, status: str) -> None:
    worker.run(
        "INSERT INTO document_images (id, document_id, kind, status, mime_type, r2_key, byte_size, prompt_sha256, model) "
        "VALUES (?, ?, 'hero', ?, 'image/png', ?, 10, ?, 'm')",
        (image_id, document_id, status, f"hero/{document_id}/{image_id}.png", "a" * 64),
    )


def statuses(worker) -> dict[int, str]:
    return {r["id"]: r["status"] for r in worker.rows("SELECT id, status FROM document_images")}


def test_approving_demotes_the_old_approved_image_of_the_same_document_first(d1_worker):
    add_document(d1_worker)
    add_image(d1_worker, 11, 3, "approved")
    add_image(d1_worker, 12, 3, "draft")
    message = hero.set_status(db_for(d1_worker), 12, "approved")
    assert statuses(d1_worker) == {11: "rejected", 12: "approved"}  # 資料ごとに approved は 1 枚（部分一意索引）
    assert "approved にしました" in message
    assert d1_worker.rows("SELECT approved_at FROM document_images WHERE id = 12")[0]["approved_at"] is not None


def test_approving_an_already_approved_image_changes_nothing(d1_worker):
    add_document(d1_worker)
    add_image(d1_worker, 12, 3, "approved")
    assert "すでに approved" in hero.set_status(db_for(d1_worker), 12, "approved")


def test_rejecting_and_unknown_ids(d1_worker):
    add_document(d1_worker)
    add_image(d1_worker, 12, 3, "approved")
    hero.set_status(db_for(d1_worker), 12, "rejected")
    assert statuses(d1_worker) == {12: "rejected"}
    with pytest.raises(HeroImageError, match="ありません"):
        hero.set_status(db_for(d1_worker), 99, "approved")
    with pytest.raises(HeroImageError):
        hero.set_status(db_for(d1_worker), 1, "draft")


def test_list_images_reports_the_byte_size(d1_worker):
    add_document(d1_worker)
    add_image(d1_worker, 12, 3, "draft")
    (row,) = hero.list_images(db_for(d1_worker))
    assert row["bytes"] == 10 and row["status"] == "draft"


def test_format_rows():
    assert hero.format_rows([]) == "画像はまだありません。"
    row = {
        "id": 1,
        "document_id": 3,
        "kind": "hero",
        "status": "draft",
        "mime_type": "image/png",
        "bytes": 10,
        "width": 4,
        "height": 3,
    }
    text = hero.format_rows([row])
    assert "draft" in text and "4x3" in text


def test_cli_reports_a_database_failure_without_the_url_or_token(monkeypatch, capsys):
    from app.d1 import D1Error
    from app.hero_images import __main__ as cli

    class Boom:
        def __init__(self, url):
            pass

        def __enter__(self):
            raise D1Error("D1 API の呼び出しに失敗しました (ConnectError)")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("D1_API_URL", "https://worker.secret-host.example")
    monkeypatch.setenv("D1_API_TOKEN", "tok-SECRET")
    monkeypatch.setattr(cli, "SyncD1", Boom)
    assert cli.main(["list"]) == 1
    err = capsys.readouterr().err
    assert "DB の操作に失敗しました" in err and "secret-host" not in err and "tok-SECRET" not in err


def test_cli_without_any_url_exits_with_usage_error(monkeypatch):
    from app.hero_images import __main__ as cli

    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("D1_API_URL", "")
    assert cli.main(["list"]) == 2
