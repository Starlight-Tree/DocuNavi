"""ヒーロー画像の取り込み・承認（app/hero_images）のテスト。実 DB・実 API は使わない（接続は偽物）。

確かめるのは、画像を中身で判定すること（拡張子を信じない）、plan.json と出力の名前の突き合わせ、
1 件でも問題があれば何も入れないこと、draft で入ること、承認で同じ資料の古い approved が rejected になること。
"""

import hashlib
import json
import struct
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app import hero_images as hero
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


class Cursor:
    def __init__(self, rows: list[tuple], description: list[str] | None = None) -> None:
        self._rows = rows
        self.description = [type("C", (), {"name": n})() for n in description or []]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class Conn:
    """execute() の SQL と引数を記録し、SQL ごとに用意した行を返す偽物。"""

    def __init__(self, answers: dict[str, list[tuple]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, tuple]] = []
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def execute(self, sql: str, params: tuple = ()) -> Cursor:
        self.calls.append((sql, params))
        return Cursor(self.answers.get(sql, []))


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


def test_import_inserts_a_draft_with_the_real_mime_type(tmp_path):
    plan = hero.load_plan(make_plan(tmp_path))
    place(tmp_path, plan, 3, JPEG)
    conn = Conn({hero.SELECT_DOCUMENT_SQL: [("doc-hash",)], hero.INSERT_SQL: [(41,)]})
    result = hero.import_images(conn, plan, tmp_path / "out")
    assert result == [hero.Imported(41, 3, len(JPEG))]
    sql, params = conn.calls[-1]
    assert sql == hero.INSERT_SQL and "'draft'" in sql
    assert params == (
        3,
        "hero",
        "image/jpeg",
        JPEG,
        1024,
        576,
        "doc-hash",
        plan.items[0].prompt_sha256,
        "the-approved-model",
    )


def test_import_writes_nothing_if_any_image_is_missing_or_invalid(tmp_path):
    items = [{"document_id": "3", "prompt": "a"}, {"document_id": "4", "prompt": "b"}]
    plan = hero.load_plan(make_plan(tmp_path, items))
    place(tmp_path, plan, 3, PNG)
    conn = Conn()
    with pytest.raises(HeroImageError, match="資料 4 の画像がありません"):
        hero.import_images(conn, plan, tmp_path / "out")
    place(tmp_path, plan, 4, b"<html>")
    with pytest.raises(HeroImageError, match="PNG / JPEG / WebP"):
        hero.import_images(conn, plan, tmp_path / "out")
    assert conn.calls == [] and conn.transactions == 0


def test_import_rejects_a_document_that_is_not_in_the_database(tmp_path):
    plan = hero.load_plan(make_plan(tmp_path))
    place(tmp_path, plan, 3, PNG)
    with pytest.raises(HeroImageError, match="DB にありません"):
        hero.import_images(Conn({hero.SELECT_DOCUMENT_SQL: []}), plan, tmp_path / "out")


def test_import_rejects_an_oversized_image(tmp_path):
    plan = hero.load_plan(make_plan(tmp_path))
    place(tmp_path, plan, 3, PNG + b"\x00" * hero.MAX_IMAGE_BYTES)
    with pytest.raises(HeroImageError, match="大きさ"):
        hero.import_images(Conn(), plan, tmp_path / "out")


# --- 承認・却下 -----------------------------------------------------------------------------------------------


def test_approving_demotes_the_old_approved_image_of_the_same_document_first():
    conn = Conn({hero.SELECT_IMAGE_SQL: [(12, 3, "hero", "draft")]})
    message = hero.set_status(conn, 12, "approved")
    assert [sql for sql, _ in conn.calls] == [hero.SELECT_IMAGE_SQL, hero.DEMOTE_SQL, hero.APPROVE_SQL]
    assert conn.calls[1][1] == (3, "hero", 12)
    assert "approved にしました" in message and conn.transactions == 1


def test_approving_an_already_approved_image_changes_nothing():
    conn = Conn({hero.SELECT_IMAGE_SQL: [(12, 3, "hero", "approved")]})
    assert "すでに approved" in hero.set_status(conn, 12, "approved")
    assert [sql for sql, _ in conn.calls] == [hero.SELECT_IMAGE_SQL]


def test_rejecting_and_unknown_ids():
    conn = Conn({hero.SELECT_IMAGE_SQL: [(12, 3, "hero", "approved")]})
    hero.set_status(conn, 12, "rejected")
    assert conn.calls[-1][0] == hero.REJECT_SQL
    with pytest.raises(HeroImageError, match="ありません"):
        hero.set_status(Conn(), 99, "approved")
    with pytest.raises(HeroImageError):
        hero.set_status(Conn(), 1, "draft")


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


def test_cli_prefers_the_write_url_over_the_app_url(monkeypatch):
    from app.hero_images import __main__ as cli

    seen: list[str] = []

    def fake_connect(url, **_kwargs):
        seen.append(url)
        raise psycopg.OperationalError("x")

    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app/db")
    monkeypatch.setenv("HERO_IMAGES_DATABASE_URL", "postgresql://writer/db")
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    assert cli.main(["list"]) == 1
    assert seen == ["postgresql://writer/db"]

    monkeypatch.delenv("HERO_IMAGES_DATABASE_URL")
    seen.clear()
    assert cli.main(["list"]) == 1
    assert seen == ["postgresql://app/db"]


def test_cli_without_any_url_exits_with_usage_error(monkeypatch):
    from app.hero_images import __main__ as cli

    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HERO_IMAGES_DATABASE_URL", raising=False)
    assert cli.main(["list"]) == 2
