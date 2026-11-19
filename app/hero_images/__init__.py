"""資料のヒーロー画像の取り込みと承認（運用者用。アプリ本体の API からは書き込まない）。

生成は、星庭ジョブ（`jobs/image-gen/`）が plan.json を読んで GCS に PNG を書く。ここは、その PNG を `document_images` に
**下書き（draft）** として入れ、人が見てから承認（approved）にするところまで。承認した画像だけが `GET /documents/{id}/hero` で返る。

- 画像のバイト列を、中身で判定する（PNG / JPEG / WebP）。ファイル名の拡張子は信じない（モデルは PNG と JPEG のどちらでも返しうる）
- 送ったプロンプトは、本文ではなく SHA-256 だけを保存する。画像は、plan.json の項目と、ジョブの出力の名前（`<資料ID>/<plan のハッシュ先頭 16 文字>.png`）で突き合わせる
- 承認し直すときは、同じ資料の古い approved を rejected にする（資料ごと・kind ごとに approved は 1 枚）
- 表示するのは、画像の ID・資料 ID・状態・バイト数だけ（プロンプトも本文も出さない）
"""

import hashlib
import json
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # db/migrations/0001_schema.sql の document_images.byte_size と同じ
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
KIND = "hero"
_DIGITS = re.compile(r"[0-9]+")

SELECT_DOCUMENT_SQL = "SELECT content_hash FROM documents WHERE id = ?"
INSERT_SQL = (
    "INSERT INTO document_images (document_id, kind, status, mime_type, r2_key, byte_size, width, height, document_hash, prompt_sha256, model) "
    "VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id"
)
SELECT_IMAGE_SQL = "SELECT id, document_id, kind, status FROM document_images WHERE id = ?"
DEMOTE_SQL = (
    "UPDATE document_images SET status = 'rejected', approved_at = NULL "
    "WHERE document_id = ? AND kind = ? AND status = 'approved' AND id <> ?"
)
APPROVE_SQL = "UPDATE document_images SET status = 'approved', approved_at = unixepoch() * 1000 WHERE id = ?"
REJECT_SQL = "UPDATE document_images SET status = 'rejected', approved_at = NULL WHERE id = ?"
LIST_SQL = (
    "SELECT id, document_id, kind, status, mime_type, byte_size AS bytes, width, height, created_at "
    "FROM document_images ORDER BY document_id, id"
)


class HeroImageError(Exception):
    """運用者に見せてよい固定の文（パス・SQL・接続先は入れない）。"""


@dataclass(frozen=True)
class PlanItem:
    document_id: int
    prompt_sha256: str


@dataclass(frozen=True)
class Plan:
    sha256: str
    model: str
    items: tuple[PlanItem, ...]

    @property
    def short_hash(self) -> str:
        return self.sha256[:16]


@dataclass(frozen=True)
class ImageInfo:
    mime_type: str
    width: int | None
    height: int | None


def sniff_image(data: bytes) -> ImageInfo | None:
    """PNG / JPEG / WebP のどれかを、先頭のバイトで判定する（寸法が読めるのは PNG と JPEG）。それ以外は None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", data[16:24])
        return ImageInfo("image/png", width or None, height or None)
    if data.startswith(b"\xff\xd8"):
        return ImageInfo("image/jpeg", *_jpeg_size(data))
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ImageInfo("image/webp", None, None)
    return None


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None, None
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width or None, height or None
        i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return None, None


def load_plan(path: Path) -> Plan:
    """plan.json を読む。ジョブが使ったのと同じバイト列のハッシュで、画像の出力名と突き合わせる。"""
    try:
        raw = path.read_bytes()
        body = json.loads(raw)
    except (OSError, ValueError):
        raise HeroImageError("plan.json を読めません。") from None
    if not isinstance(body, dict):
        raise HeroImageError("plan.json の形が違います。")
    model = body.get("model")
    items = body.get("items")
    if not isinstance(model, str) or not model.strip():
        raise HeroImageError("plan.json に model がありません（生成に使ったモデルの名前が要ります）。")
    if not isinstance(items, list) or not items:
        raise HeroImageError("plan.json に items がありません。")
    parsed: list[PlanItem] = []
    for item in items:
        document_id = item.get("document_id") if isinstance(item, dict) else None
        prompt = item.get("prompt") if isinstance(item, dict) else None
        if (
            not isinstance(document_id, str)
            or not _DIGITS.fullmatch(document_id)
            or not isinstance(prompt, str)
            or not prompt
        ):
            raise HeroImageError("plan.json の items の形が違います。")
        parsed.append(PlanItem(int(document_id), hashlib.sha256(prompt.encode("utf-8")).hexdigest()))
    return Plan(hashlib.sha256(raw).hexdigest(), model.strip(), tuple(parsed))


@dataclass(frozen=True)
class Imported:
    image_id: int
    document_id: int
    bytes: int


def import_images(db: Any, store: Any, plan: Plan, directory: Path) -> list[Imported]:
    """plan の各項目の画像（`<directory>/<資料ID>/<ハッシュ先頭 16 文字>.png`）を R2 に置き、draft で入れる。1 件でも問題があれば、何も入れない。"""
    staged: list[tuple[PlanItem, bytes, ImageInfo]] = []
    for item in plan.items:
        path = directory / str(item.document_id) / f"{plan.short_hash}.png"
        try:
            data = path.read_bytes()
        except OSError:
            raise HeroImageError(
                f"資料 {item.document_id} の画像がありません（期待する名前: {item.document_id}/{plan.short_hash}.png）。"
            ) from None
        if not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise HeroImageError(f"資料 {item.document_id} の画像の大きさが範囲外です。")
        info = sniff_image(data)
        if info is None:
            raise HeroImageError(f"資料 {item.document_id} の画像は、PNG / JPEG / WebP ではありません。")
        staged.append((item, data, info))
    hashes: dict[int, str] = {}
    for item, _, _ in staged:
        rows = db.execute(SELECT_DOCUMENT_SQL, (item.document_id,))
        if not rows:
            raise HeroImageError(f"資料 {item.document_id} が DB にありません。")
        hashes[item.document_id] = rows[0]["content_hash"]
    results: list[Imported] = []
    for item, data, info in staged:
        key = f"hero/{item.document_id}/{plan.short_hash}-{item.prompt_sha256[:12]}.{_EXTENSIONS[info.mime_type]}"
        store.put(key, data, content_type=info.mime_type)
        inserted = db.execute(
            INSERT_SQL,
            (
                item.document_id,
                KIND,
                info.mime_type,
                key,
                len(data),
                info.width,
                info.height,
                hashes[item.document_id],
                item.prompt_sha256,
                plan.model,
            ),
        )
        results.append(Imported(int(inserted[0]["id"]), item.document_id, len(data)))
    return results


def set_status(db: Any, image_id: int, status: str) -> str:
    """draft / rejected を approved に、または approved / draft を rejected にする。承認では、同じ資料の古い approved を rejected にする。"""
    if status not in ("approved", "rejected"):
        raise HeroImageError("状態は approved か rejected です。")
    rows = db.execute(SELECT_IMAGE_SQL, (image_id,))
    if not rows:
        raise HeroImageError(f"画像 {image_id} がありません。")
    document_id, kind, current = rows[0]["document_id"], rows[0]["kind"], rows[0]["status"]
    if current == status:
        return f"画像 {image_id}（資料 {document_id}）は、すでに {status} です。"
    if status == "approved":
        db.batch([(DEMOTE_SQL, (document_id, kind, image_id)), (APPROVE_SQL, (image_id,))])
    else:
        db.execute(REJECT_SQL, (image_id,))
    return f"画像 {image_id}（資料 {document_id}）を {status} にしました。"


def list_images(db: Any) -> list[Mapping[str, Any]]:
    return db.execute(LIST_SQL)


def format_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "画像はまだありません。"
    lines = ["id  資料  kind  状態      形式        バイト  寸法"]
    for row in rows:
        size = f"{row['width']}x{row['height']}" if row.get("width") and row.get("height") else "-"
        lines.append(
            f"{row['id']:<3} {row['document_id']:<5} {row['kind']:<5} {row['status']:<9} {row['mime_type']:<11} {row['bytes']:<7} {size}"
        )
    return "\n".join(lines)
