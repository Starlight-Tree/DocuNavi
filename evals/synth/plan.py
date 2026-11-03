"""依頼票（Brief）の計画。同じ bible・同じ件数なら、いつも同じ依頼票の列になる（決定的）。

重複防止は `key`（bible の版・資料の種類・変化・プロンプトの版のハッシュ）で行う。同じ key の資料は作り直さない。
各対象で、既存の資料（`existing_documents`）を引いた残りだけを作る。資料の種類を巡回して選ぶので、件数を減らしても偏らない。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from evals.synth.bible import Bible, Kind
from evals.synth.prompt import PROMPT_VERSION


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Brief:
    id: str
    target: str
    kind: str
    doc_type: str
    variant: str
    title: str
    seed: int
    key: str
    min_chars: int
    max_chars: int

    def to_dict(self) -> dict:
        return asdict(self)


def _brief_key(bible: Bible, kind_id: str, variant: str) -> str:
    raw = "\n".join([bible.version, kind_id, variant, PROMPT_VERSION])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def plan_target(bible: Bible, per_target: int) -> list[Brief]:
    needed = per_target - bible.existing_documents
    if needed < 0:
        raise PlanError(f"{bible.target}: 既存 {bible.existing_documents} 件が、目標 {per_target} 件を超えています")
    # 変化の番号を外側、資料の種類を内側にして巡回する
    slots: list[tuple[int, Kind, str]] = []
    longest = max((len(k.variants) for k in bible.kinds), default=0)
    for vi in range(longest):
        for kind in bible.kinds:
            if vi < len(kind.variants):
                slots.append((vi, kind, kind.variants[vi]))
    if needed > len(slots):
        raise PlanError(f"{bible.target}: bible の種類×変化は {len(slots)} 件で、必要な {needed} 件に足りません")
    briefs = []
    for vi, kind, variant in slots[:needed]:
        key = _brief_key(bible, kind.id, variant)
        briefs.append(
            Brief(
                id=f"{bible.target}-{kind.id}-{vi + 1:02d}",
                target=bible.target,
                kind=kind.id,
                doc_type=kind.doc_type,
                variant=variant,
                title=kind.title.format(label=bible.label, variant=variant),
                seed=int(key, 16) % (2**31),
                key=key,
                min_chars=kind.min_chars,
                max_chars=kind.max_chars,
            )
        )
    return briefs


def build_plan(bibles: dict[str, Bible], per_target: int, targets: list[str] | None = None) -> list[Brief]:
    chosen = targets or sorted(bibles)
    missing = [t for t in chosen if t not in bibles]
    if missing:
        raise PlanError(f"bible がない対象: {missing}")
    briefs: list[Brief] = []
    for target in chosen:
        briefs.extend(plan_target(bibles[target], per_target))
    ids = [b.id for b in briefs]
    if len(ids) != len(set(ids)):
        raise PlanError("依頼票の id が重複しています")
    return briefs
