"""台帳（再開・重複防止の状態）。追記だけの JSONL。1 行 = 1 件の出来事で、同じ id の行を順に重ねたものが現在の状態。

- 追記ごとに flush と fsync をする（途中で止まっても、書き終えた行は残る。壊れた末尾の行は読み飛ばす）。
- ロックファイルで、同じ出力先を 2 つのプロセスが同時に使うのを防ぐ（fcntl.flock）。
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class LockedError(RuntimeError):
    pass


@contextmanager
def exclusive(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockedError(f"別のプロセスがこの出力先を使っています: {lock_path}") from None
        yield
    finally:
        handle.close()


def atomic_write(path: Path, data: bytes) -> None:
    """同じディレクトリーの一時ファイルへ書いてから置き換える（読む側が途中の内容を見ない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.state: dict[str, dict[str, Any]] = {}
        self.events = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                doc_id = event["id"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue  # 壊れた行（書きかけの末尾など）は無かったものとして扱う
            self.state.setdefault(doc_id, {}).update(event)
            self.events += 1

    def append(self, doc_id: str, **fields: Any) -> None:
        event = {"id": doc_id, **fields}
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self.state.setdefault(doc_id, {}).update(event)
            self.events += 1

    def get(self, doc_id: str) -> dict[str, Any]:
        return self.state.get(doc_id, {})

    def total_cost_usd(self) -> float:
        return sum(float(s.get("cost_usd", 0.0)) for s in self.state.values())
