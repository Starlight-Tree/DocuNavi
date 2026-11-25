"""資料の PDF を探す（`documents` の行 → `storage/pdf/` のどの PDF か）。

DB の `documents` が持っているのは、`source`（ファイル名）と `content_hash`（取り込んだ PDF のバイト列の sha256）だけで、
どの資料群のディレクトリにあったかは持っていない。そこで次のように決める。

- `source` は、置き場の中の安全な相対パス（`a.pdf` か `dir/a.pdf`。部品は 3 個まで）だけ受け付ける。絶対パス・空の部品・`.` / `..`・
  バックスラッシュ・NUL・制御文字を含むものは「見つからない」。DB の値をそのまま結合せず、`storage/pdf/<資料群>/` の直下を走査して、
  `<資料群>/<source>` が完全に一致するファイルを候補にする（途中のディレクトリがリンクなら読まない）
- 候補の中身を読み、sha256 が `content_hash` と一致するものだけを PDF とする（名前が同じでも、中身が違えば返さない）。
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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# PDF の置き場（リポジトリ直下の storage/pdf/）。直下のディレクトリが資料群で、その下に PDF を置く。検索には使わない（検索は DB だけを読む）
PDF_DIR = Path(__file__).resolve().parents[1] / "storage" / "pdf"

# PDF として読む上限。いまの PDF は 100 KB 前後。応答はメモリに載せるので、これを超えるものは返さない
MAX_ORIGINAL_BYTES = 20 * 1024 * 1024

Reason = Literal["missing", "mismatch", "unavailable"]


@dataclass(frozen=True)
class OriginalPdf:
    """PDF の探索結果。ok のときだけ data（ハッシュを確かめたバイト列）がある。"""

    ok: bool
    reason: Reason | None = None
    data: bytes | None = None


def _missing() -> OriginalPdf:
    return OriginalPdf(ok=False, reason="missing")


# `source` に許す区切りの数（`<ディレクトリ>/<ファイル>.pdf` のような相対パス）。深い入れ子は取り込みが作らないので受け付けない
MAX_SOURCE_SEGMENTS = 3
MAX_SOURCE_CHARS = 255


def _source_parts(source: object) -> tuple[str, ...] | None:
    """`source` を、置き場の中の安全な相対パスの部品に分ける。使えない名前なら None。

    許すのは `a.pdf` と `dir/a.pdf` の形だけ（部品は `MAX_SOURCE_SEGMENTS` 個まで）。絶対パス・空の部品（`//`、先頭・末尾の `/`）・
    `.` / `..`・バックスラッシュ・NUL・制御文字を含むものは、探す前に「見つからない」にする。
    """
    if not isinstance(source, str) or not source or len(source) > MAX_SOURCE_CHARS:
        return None
    parts = tuple(source.split("/"))
    if len(parts) > MAX_SOURCE_SEGMENTS:
        return None
    for part in parts:
        if not part or part in (".", "..") or "\\" in part or any(ord(char) < 32 or ord(char) == 127 for char in part):
            return None
    return parts


def _usable_name(source: object) -> bool:
    """`source` が、置き場の中の安全な相対パスとして使えるか。"""
    return _source_parts(source) is not None


def _file_in(directory: Path, parts: tuple[str, ...]) -> bytes | None:
    """`directory` の下の `parts`（相対パス）の通常のファイルを読む。途中のディレクトリがリンクなら読まない。"""
    current = directory
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return None
    return _read_regular_file(current / parts[-1])


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
        logger.warning("R2 から PDF を読めませんでした (%s)", type(exc).__name__)
        return OriginalPdf(ok=False, reason="unavailable")
    if seen:
        logger.warning("PDF の内容が、DB に保存された資料と一致しません")
        return OriginalPdf(ok=False, reason="mismatch")
    return _missing()


def find_original_in_r2(source: object, content_hash: object) -> OriginalPdf:
    """`PDF_STORE` に関わらず R2 から探す（`python -m app.r2_store verify` が、本番と同じ規則で一致を確かめるために使う）。"""
    if not _usable_name(source):
        return _missing()
    return _find_in_r2(str(source), content_hash)


def find_original(source: object, content_hash: object) -> OriginalPdf:
    """`source` と `content_hash` に合う PDF を探す。

    見つからなければ reason="missing"。名前の合うファイルはあるが、どれも内容が違う（または、DB にハッシュが無く
    確かめられない）なら reason="mismatch"。複数が一致したときは中身が同一なので、名前順で先のものを返す。
    """
    parts = _source_parts(source)
    if parts is None:
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
        data = _file_in(directory, parts)
        if data is None:
            continue
        seen = True
        if wanted and hashlib.sha256(data).hexdigest() == wanted:
            return OriginalPdf(ok=True, data=data)
    if seen:
        logger.warning("PDF の内容が、DB に保存された資料と一致しません")
        return OriginalPdf(ok=False, reason="mismatch")
    return _missing()


# --- `/documents/{id}/chunks` の `original_pdf`（PDF を返せるかだけ）の照合結果の控え --------------------------------------------
# `PDF_STORE=r2` では、`find_original` が毎回、R2 の資料群を列挙し、PDF を取り、sha256 を照合する（往復と転送と計算）。`/chunks` は、
# その結果の「返せるか」だけを使う。PDF は内容のハッシュで決まる（キーが (source, content_hash)）ので、同じ組の結果は、プロセス内で一定時間使い回してよい。
# - 控えるのは `PDF_STORE=r2` のときだけ（ローカルは、ファイルを読むだけで速く、テストでディレクトリを差し替えるため）
# - 返せた（ok）は `CHECK_CACHE_OK_SECONDS`、見つからない・食い違いは `CHECK_CACHE_NEGATIVE_SECONDS`。**R2 を読めなかった（unavailable）は控えない**
# - 限界: R2 の PDF が、DB のハッシュを変えずに差し替わったとき、最大この秒数だけ古い答え（「返せる」）が出る。`/file` は毎回確かめるので、
#   その場合も、内容が合わない PDF は返さない（409）
# - Cloud Run のインスタンスごとに別（プロセス内だけ）。再起動すると空になる
CHECK_CACHE_OK_SECONDS = 600.0
CHECK_CACHE_NEGATIVE_SECONDS = 60.0
_CHECK_CACHE_MAX_ENTRIES = 1024
# (source, 正規化したハッシュ) → (期限の monotonic 秒, 返せるか)
_check_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_check_lock = threading.Lock()
_now = time.monotonic  # テストが差し替える


def clear_original_cache() -> None:
    """照合結果の控えを空にする（テスト用）。"""
    with _check_lock:
        _check_cache.clear()


def original_available(source: object, content_hash: object) -> bool:
    """`find_original(source, content_hash).ok` と同じ答え。`/chunks` の `original_pdf` だけが使う（PDF のバイト列は要らない）。

    `PDF_STORE=r2` のときだけ、結果をプロセス内に控える（上の説明）。それ以外は `find_original` をそのまま呼ぶ。
    """
    if os.environ.get("PDF_STORE", "local").strip().lower() != "r2" or not isinstance(source, str):
        return find_original(source, content_hash).ok
    wanted = content_hash.strip().lower() if isinstance(content_hash, str) else ""
    key = (source, wanted)
    now = _now()
    with _check_lock:
        hit = _check_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    found = find_original(source, content_hash)
    if found.reason != "unavailable":  # 読めなかった結果は控えない（R2 が戻ったら、すぐ正しい答えにする）
        ttl = CHECK_CACHE_OK_SECONDS if found.ok else CHECK_CACHE_NEGATIVE_SECONDS
        with _check_lock:
            if len(_check_cache) >= _CHECK_CACHE_MAX_ENTRIES:
                for stale in [k for k, v in _check_cache.items() if v[0] <= now]:
                    del _check_cache[stale]
                if len(_check_cache) >= _CHECK_CACHE_MAX_ENTRIES:
                    _check_cache.pop(next(iter(_check_cache)))
            _check_cache[key] = (now + ttl, found.ok)
    return found.ok
