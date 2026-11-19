"""対象ごとの共通設定（bible）。文書間の矛盾を防ぐための、事実の一覧と資料の種類の表。

`bibles/<target>.yaml` を読む。生成の本文は、ここにある事実だけを根拠にする（数値の突合は validate.py）。
事実を足す・直すと `version` が変わり、依頼票の `key` も変わる（古い生成物は stale になり、勝手には作り直さない）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class BibleError(ValueError):
    """bible の形式・参照の誤り。"""


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Kind:
    """資料の種類。`variants` の数だけ資料ができる（変化ごとに 1 件）。"""

    id: str
    doc_type: str
    title: str
    tags: tuple[str, ...]
    variants: tuple[str, ...]
    min_chars: int
    max_chars: int
    outline: tuple[str, ...]
    style: str


@dataclass(frozen=True)
class Bible:
    target: str
    label: str
    premise: str
    existing_documents: int
    documents: int | None  # この対象の目標件数（既存の資料を含む）。無ければ、呼び出し側の件数（`--per-target`）
    revision: str
    facts: tuple[Fact, ...]
    kinds: tuple[Kind, ...]
    extra_numbers: tuple[str, ...]
    contacts: tuple[str, ...]
    forbidden: tuple[str, ...]
    version: str
    classify_as: str | None = (
        None  # DB の資料の対象（documents.target）。無ければ `target` と同じ。bible の識別名（PDF の置き場所・台帳）と分けて、画面の分類を粗くするための印
    )

    def facts_for(self, kind: Kind) -> list[Fact]:
        """資料の種類のタグに合う事実（`core` タグの事実は、どの種類にも入る）。"""
        wanted = set(kind.tags) | {"core"}
        return [f for f in self.facts if wanted & set(f.tags)]


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise BibleError(f"{where}: `{key}` がありません")
    return data[key]


def _strs(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise BibleError(f"{where}: 空でない文字列のリストが必要です")
    return tuple(value)


def parse_bible(data: dict[str, Any], where: str = "bible") -> Bible:
    target = str(_require(data, "target", where))
    if not SLUG.fullmatch(target):
        raise BibleError(f"{where}: target `{target}` は英小文字・数字・ハイフンだけ（40 文字まで）")
    facts: list[Fact] = []
    seen: set[str] = set()
    for i, raw in enumerate(_require(data, "facts", where)):
        fid, text = str(raw.get("id", "")), str(raw.get("text", "")).strip()
        if not fid or not text:
            raise BibleError(f"{where}: facts[{i}] に id と text が要ります")
        if fid in seen:
            raise BibleError(f"{where}: fact id `{fid}` が重複しています")
        seen.add(fid)
        facts.append(Fact(fid, text, _strs(raw.get("tags", ["core"]), f"{where}: facts[{i}].tags")))
    tags = {t for f in facts for t in f.tags}
    kinds: list[Kind] = []
    kind_ids: set[str] = set()
    for i, raw in enumerate(_require(data, "kinds", where)):
        w = f"{where}: kinds[{i}]"
        kid = str(_require(raw, "id", w))
        if not SLUG.fullmatch(kid) or kid in kind_ids:
            raise BibleError(f"{w}: id `{kid}` が不正か重複しています")
        kind_ids.add(kid)
        doc_type = str(_require(raw, "doc_type", w))
        if not SLUG.fullmatch(doc_type):
            raise BibleError(f"{w}: doc_type `{doc_type}` が不正です")
        ktags = _strs(_require(raw, "tags", w), f"{w}.tags")
        unknown = set(ktags) - tags
        if unknown:
            raise BibleError(f"{w}: どの事実にも無いタグ {sorted(unknown)}")
        low, high = _require(raw, "chars", w)
        if not (isinstance(low, int) and isinstance(high, int) and 0 < low < high):
            raise BibleError(f"{w}: chars は [最小, 最大]（整数、最小 < 最大）")
        kinds.append(
            Kind(
                id=kid,
                doc_type=doc_type,
                title=str(_require(raw, "title", w)),
                tags=ktags,
                variants=_strs(_require(raw, "variants", w), f"{w}.variants"),
                min_chars=low,
                max_chars=high,
                outline=_strs(_require(raw, "outline", w), f"{w}.outline"),
                style=str(raw.get("style", "")),
            )
        )
    classify_as = data.get("classify_as")
    if classify_as is not None and not (
        isinstance(classify_as, str) and re.fullmatch(r"[a-z0-9_-]{1,40}", classify_as)
    ):
        raise BibleError(
            f"{where}: classify_as `{classify_as}` は英小文字・数字・ハイフン・アンダースコアだけ（40 文字まで）"
        )
    # `classify_as` は分類の印だけで、本文の根拠ではない。版（version）に入れない（入れると、台帳の key が変わって生成物が stale になる）
    canonical = json.dumps({k: v for k, v in data.items() if k != "classify_as"}, ensure_ascii=False, sort_keys=True)
    return Bible(
        target=target,
        label=str(_require(data, "label", where)),
        premise=str(_require(data, "premise", where)).strip(),
        existing_documents=int(data.get("existing_documents", 0)),
        documents=int(data["documents"]) if "documents" in data else None,
        revision=str(data.get("revision", "")),
        facts=tuple(facts),
        kinds=tuple(kinds),
        extra_numbers=tuple(data.get("extra_numbers", [])),
        contacts=tuple(data.get("contacts", [])),
        forbidden=tuple(data.get("forbidden", [])),
        version=hashlib.sha256(canonical.encode()).hexdigest()[:12],
        classify_as=classify_as,
    )


def load_bible(path: Path) -> Bible:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BibleError(f"{path}: トップが辞書ではありません")
    bible = parse_bible(data, str(path))
    if bible.target != path.stem:
        raise BibleError(f"{path}: ファイル名と target `{bible.target}` が違います")
    return bible


def load_all(directory: Path) -> dict[str, Bible]:
    bibles = {}
    for path in sorted(directory.glob("*.yaml")):
        bible = load_bible(path)
        bibles[bible.target] = bible
    if not bibles:
        raise BibleError(f"{directory}: bible（*.yaml）がありません")
    return bibles
