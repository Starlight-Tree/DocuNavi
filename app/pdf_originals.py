"""資料の元 PDF を探す（`documents` の行 → `storage/pdf/` のどの PDF か）。

DB の `documents` が持っているのは、`source`（ファイル名）と `content_hash`（取り込んだ PDF のバイト列の sha256）だけで、
どの資料群のディレクトリにあったかは持っていない。そこで次のように決める。

- `source` は、ファイル名として使えるものだけ受け付ける（区切り文字・`.`・`..` を含むものは「見つからない」）。DB の文字列を
  パスとして結合せず、`storage/pdf/<資料群>/` の直下を走査して、名前が完全に一致するファイルを候補にする
- 候補の中身を読み、sha256 が `content_hash` と一致するものだけを元 PDF とする（名前が同じでも、中身が違えば返さない）。
  これにより、DB に保存したテキストの元とは別版の PDF を見せない
- **返すバイト列は、ハッシュを確かめた、そのバイト列**（検証のあとで読み直さない）
- シンボリックリンクは使わない。ファイルがリンクなら読まず、資料群のディレクトリがリンクで置き場の外へ出るなら走査しない
  （行き先の中身が一致していても返さない）
- 大きすぎるファイルは読まない（`MAX_ORIGINAL_BYTES`）

置き場は環境変数 `PDF_STORE` で選ぶ。`local`（既定）は上のとおり `storage/pdf/`。`r2` は Cloudflare R2（`app/r2_store.py`）で、
キー `<資料群>/<source>` を同じ規則（資料群の列挙 → 名前が完全に一致 → ハッシュの確認）で探す。R2 を読めなかった（設定の不足・通信・権限）ときは
reason="unavailable"（見つからない・食い違いとは区別する。API は 503 の固定文）。ローカルへは切り替えない（食い違った版を見せないため）。

同期の関数（ファイルを読む）。API からはスレッドで呼ぶ。
"""

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# 元 PDF の置き場（リポジトリ直下の storage/pdf/）。直下のディレクトリが資料群で、その下に PDF を置く。検索には使わない（検索は DB だけを読む）
PDF_DIR = Path(__file__).resolve().parents[1] / "storage" / "pdf"

# 元 PDF として読む上限。いまの PDF は 100 KB 前後。応答はメモリに載せるので、これを超えるものは返さない
MAX_ORIGINAL_BYTES = 20 * 1024 * 1024

Reason = Literal["missing", "mismatch", "unavailable"]


@dataclass(frozen=True)
class OriginalPdf:
    """元 PDF の探索結果。ok のときだけ data（ハッシュを確かめたバイト列）がある。"""

    ok: bool
    reason: Reason | None = None
    data: bytes | None = None


def _missing() -> OriginalPdf:
    return OriginalPdf(ok=False, reason="missing")


def _usable_name(source: object) -> bool:
    """ファイル名として使える文字列か（ディレクトリの区切り・NUL・`.` / `..` を含まない）。"""
    if not isinstance(source, str) or not source or source in (".", ".."):
        return False
    return not any(char in source for char in ("/", "\\", "\x00"))


def _collection_dirs(root: Path) -> list[Path]:
    """置き場 `root` の直下の資料群のディレクトリ（名前順）。リンクの行き先が root の外なら除く。"""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    directories: list[Path] = []
    for entry in entries:
        try:
            resolved = entry.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and resolved.parent == root:
            directories.append(resolved)
    return directories


def _read_regular_file(path: Path) -> bytes | None:
    """通常のファイルだけを、リンクをたどらずに読む。読めない・大きすぎる・通常のファイルでないなら None。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:  # 無い・権限がない・リンク（O_NOFOLLOW）
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ORIGINAL_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_ORIGINAL_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= MAX_ORIGINAL_BYTES else None


def _find_in_r2(source: str, content_hash: object) -> OriginalPdf:
    from app import r2_store

    wanted = content_hash.strip().lower() if isinstance(content_hash, str) else ""
    try:
        store = r2_store.default_store()
        seen = False
        for collection in store.collections():
            data = store.get(f"{collection}/{source}", MAX_ORIGINAL_BYTES)
            if data is None:
                continue
            seen = True
            if wanted and hashlib.sha256(data).hexdigest() == wanted:
                return OriginalPdf(ok=True, data=data)
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・鍵の ID が入りうる。型の名前だけを残す
        logger.warning("R2 から元の PDF を読めませんでした (%s)", type(exc).__name__)
        return OriginalPdf(ok=False, reason="unavailable")
    if seen:
        logger.warning("元の PDF の内容が、DB に保存された資料と一致しません")
        return OriginalPdf(ok=False, reason="mismatch")
    return _missing()


def find_original(source: object, content_hash: object) -> OriginalPdf:
    """`source` と `content_hash` に合う元 PDF を探す。

    見つからなければ reason="missing"。名前の合うファイルはあるが、どれも内容が違う（または、DB にハッシュが無く
    確かめられない）なら reason="mismatch"。複数が一致したときは中身が同一なので、名前順で先のものを返す。
    """
    if not _usable_name(source):
        return _missing()
    if os.environ.get("PDF_STORE", "local").strip().lower() == "r2":
        return _find_in_r2(str(source), content_hash)
    root = Path(PDF_DIR)
    try:
        root = root.resolve(strict=True)
    except OSError:
        return _missing()
    wanted = content_hash.strip().lower() if isinstance(content_hash, str) else ""
    seen = False
    for directory in _collection_dirs(root):
        data = _read_regular_file(directory / str(source))
        if data is None:
            continue
        seen = True
        if wanted and hashlib.sha256(data).hexdigest() == wanted:
            return OriginalPdf(ok=True, data=data)
    if seen:
        logger.warning("元の PDF の内容が、DB に保存された資料と一致しません")
        return OriginalPdf(ok=False, reason="mismatch")
    return _missing()
