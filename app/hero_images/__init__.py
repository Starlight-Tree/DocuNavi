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

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # migration の document_images_size と同じ
KIND = "hero"
_DIGITS = re.compile(r"[0-9]+")

SELECT_DOCUMENT_SQL = "SELECT content_hash FROM chatbot.documents WHERE id = %s"
INSERT_SQL = (
    "INSERT INTO chatbot.document_images (document_id, kind, status, mime_type, data, width, height, document_hash, prompt_sha256, model) "
    "VALUES (%s, %s, 'draft', %s, %s, %s, %s, %s, %s, %s) RETURNING id"
)
SELECT_IMAGE_SQL = "SELECT id, document_id, kind, status FROM chatbot.document_images WHERE id = %s FOR UPDATE"
DEMOTE_SQL = (
    "UPDATE chatbot.document_images SET status = 'rejected', approved_at = NULL "
    "WHERE document_id = %s AND kind = %s AND status = 'approved' AND id <> %s"
)
APPROVE_SQL = "UPDATE chatbot.document_images SET status = 'approved', approved_at = now() WHERE id = %s"
REJECT_SQL = "UPDATE chatbot.document_images SET status = 'rejected', approved_at = NULL WHERE id = %s"
LIST_SQL = (
    "SELECT id, document_id, kind, status, mime_type, octet_length(data) AS bytes, width, height, created_at "
    "FROM chatbot.document_images ORDER BY document_id, id"
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


def import_images(conn: Any, plan: Plan, directory: Path) -> list[Imported]:
    """plan の各項目の画像（`<directory>/<資料ID>/<ハッシュ先頭 16 文字>.png`）を draft で入れる。1 件でも問題があれば、何も入れない。"""
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
    results: list[Imported] = []
    with conn.transaction():
        for item, data, info in staged:
            row = conn.execute(SELECT_DOCUMENT_SQL, (item.document_id,)).fetchone()
            if row is None:
                raise HeroImageError(f"資料 {item.document_id} が DB にありません。")
            inserted = conn.execute(
                INSERT_SQL,
                (
                    item.document_id,
                    KIND,
                    info.mime_type,
                    data,
                    info.width,
                    info.height,
                    row[0],
                    item.prompt_sha256,
                    plan.model,
                ),
            ).fetchone()
            results.append(Imported(int(inserted[0]), item.document_id, len(data)))
    return results


def set_status(conn: Any, image_id: int, status: str) -> str:
    """draft / rejected を approved に、または approved / draft を rejected にする。承認では、同じ資料の古い approved を rejected にする。"""
    if status not in ("approved", "rejected"):
        raise HeroImageError("状態は approved か rejected です。")
    with conn.transaction():
        row = conn.execute(SELECT_IMAGE_SQL, (image_id,)).fetchone()
        if row is None:
            raise HeroImageError(f"画像 {image_id} がありません。")
        _, document_id, kind, current = row
        if current == status:
            return f"画像 {image_id}（資料 {document_id}）は、すでに {status} です。"
        if status == "approved":
            conn.execute(DEMOTE_SQL, (document_id, kind, image_id))
            conn.execute(APPROVE_SQL, (image_id,))
        else:
            conn.execute(REJECT_SQL, (image_id,))
    return f"画像 {image_id}（資料 {document_id}）を {status} にしました。"


def list_images(conn: Any) -> list[Mapping[str, Any]]:
    cursor = conn.execute(LIST_SQL)
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


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
