"""検索できる資料グループの範囲（`Scope`）。標準ライブラリだけで書く（`ops/mcp_search` も import するので、FastAPI や認証は読み込まない）。

- `Scope.ALL` は「全グループ」。**明示したときだけ**全資料を探す。検索（`app/search/vector.py`）は `scope` を必須の引数にし、
  渡し忘れ・`None`・`Scope` 以外は `TypeError`、空の範囲（`Scope.NONE`）は何も返さない（漏れる方向には倒れない）。
- 範囲は `config["configurable"]` で検索ツールまで渡す。会話の状態（チェックポイント）には保存しない。
- `shadow`: `ACCESS_MODE=shadow` のときだけ入る「enforce なら見える範囲」。制限には使わず、絞り込みになる件数をログに出すためだけに持つ。
- `complete`: 範囲が、いま存在する全グループを含むか。過去の会話のうち、出典のグループを記録していない古い検索結果を伏せるかどうかだけに使う
  （`app/access.py` の `mask_hidden_turns`）。検索・一覧・本文の絞り込み（`allows`）は変えない。
  設計は docs/configuration.md の「資料グループのアクセス制御」。
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

logger = logging.getLogger(__name__)

# 検索ツールへ範囲を渡す `config["configurable"]` のキー（LLM が渡す引数ではない）
ACCESS_SCOPE_KEY = "access_scope"

# SQL の WHERE に足す条件。`%(allowed)s` は text[]（None なら全グループ）。グループの無い資料（collection が NULL）は、
# 範囲を絞ったときには含めない（`NULL = ANY(...)` は真にならない）
ALLOWED_FILTER_SQL = "(%(allowed)s::text[] IS NULL OR d.collection = ANY(%(allowed)s::text[]))"


@dataclass(frozen=True)
class Scope:
    """`names` が None なら全グループ（`Scope.ALL`）。空の集合は何も見えない（`Scope.NONE`）。既定値は持たない（渡し忘れを許さない）。"""

    names: frozenset[str] | None
    shadow: frozenset[str] | None = None
    # いま存在する全グループ（登録済み + 資料にあるもの）を `names` が含む。会話の伏せ方だけに使い、`allows` には効かない
    complete: bool = False

    ALL: ClassVar["Scope"]
    NONE: ClassVar["Scope"]

    @classmethod
    def only(cls, names: Iterable[str]) -> "Scope":
        return cls(frozenset(names))

    @property
    def is_all(self) -> bool:
        return self.names is None

    @property
    def is_empty(self) -> bool:
        return self.names is not None and not self.names

    def allows(self, collection: str | None) -> bool:
        """このグループの資料を扱ってよいか。グループの無い資料（None）は、全グループのときだけ。"""
        return self.names is None or collection in self.names

    def sql_names(self) -> list[str] | None:
        """`ALLOWED_FILTER_SQL` の `allowed` に渡す値（None は全グループ）。並びを固定する。"""
        return None if self.names is None else sorted(self.names)

    def shadow_hidden(self, collections: Iterable[str | None]) -> tuple[int, int] | None:
        """(enforce なら絞り込まれる件数, 全件数)。shadow でなければ None。"""
        if self.shadow is None:
            return None
        items = list(collections)
        return sum(1 for c in items if c not in self.shadow), len(items)

    def log_shadow(self, path: str, collections: Iterable[str | None]) -> None:
        """shadow のとき、`ACCESS_MODE=enforce` なら何件が絞り込まれるかをログに出す（件数だけ。グループ名は出さない）。"""
        counted = self.shadow_hidden(collections)
        if counted is not None:
            logger.info(
                "アクセス制御(shadow): %s 全 %d 件のうち %d 件が enforce では絞り込まれる", path, counted[1], counted[0]
            )


def require_scope(scope: object) -> Scope:
    """`Scope` でなければ TypeError（`None` を「全部」と読み替えない）。"""
    if not isinstance(scope, Scope):
        raise TypeError("scope は Scope で渡してください（全グループは Scope.ALL を明示する）")
    return scope


Scope.ALL = Scope(None)
Scope.NONE = Scope(frozenset())
