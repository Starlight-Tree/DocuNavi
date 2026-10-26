"""利用者に見える資料グループの計算と、ロールの割り当てを変えたときの影響（標準ライブラリだけ。Django も `app/` も import しない）。

規則は `app/access.py` の `_VISIBLE_SQL` と同じ（契約テスト tests/test_access_admin_contract.py が、実際の Postgres でアプリ本体と突き合わせる）:

- 見えるグループ = 割り当てられたロールが持つグループの和集合。
- ロールが 1 つも割り当てられていない利用者は `default` ロール。ほかのロールを 1 つでも割り当てると `default` は使われない。
- 表に無いロール（グループを持たないロール）は、何も足さない。

管理画面は、この関数で「保存する前と後で、見えるグループがどう変わるか」を計算して見せる。DB に書く内容とは独立に、
書く直前にサーバー側で計算し直す（画面から送られた値は信じない）。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

DEFAULT_ROLE = "default"

# ロール名 → そのロールが持つグループ
AccessMatrix = Mapping[str, Iterable[str]]


def visible_groups(
    assigned_roles: Iterable[str], matrix: AccessMatrix, default_role: str = DEFAULT_ROLE
) -> frozenset[str]:
    """割り当てられたロールから、見えるグループを計算する。割り当てが 0 件なら default ロールとして扱う。"""
    roles = frozenset(assigned_roles)
    effective = roles or frozenset({default_role})
    groups: set[str] = set()
    for role in effective:
        groups.update(matrix.get(role, ()))
    return frozenset(groups)


@dataclass(frozen=True)
class RoleChange:
    """ロールの割り当てを `current` から `desired` に変えたときの差と、見えるグループの前後。"""

    current: frozenset[str]
    desired: frozenset[str]
    before: frozenset[str]
    after: frozenset[str]

    @property
    def add(self) -> frozenset[str]:
        return self.desired - self.current

    @property
    def remove(self) -> frozenset[str]:
        return self.current - self.desired

    @property
    def changed(self) -> bool:
        return self.current != self.desired

    @property
    def lost(self) -> frozenset[str]:
        """見えなくなるグループ。"""
        return self.before - self.after

    @property
    def gained(self) -> frozenset[str]:
        """新しく見えるグループ。"""
        return self.after - self.before

    @property
    def shrinks(self) -> bool:
        """見えるグループが減る。確認のチェックボックスが要る変更。"""
        return bool(self.lost)

    @property
    def loses_everything(self) -> bool:
        """いま見えるグループがあるのに、変更後は 1 つも見えなくなる（その利用者は「利用できる資料がありません」になる）。"""
        return bool(self.before) and not self.after

    @property
    def leaves_default_fallback(self) -> bool:
        """割り当てが 0 件（暗黙の default）から、default を含まない割り当てに変わる。`default` のグループが見えなくなる。"""
        return not self.current and bool(self.desired) and DEFAULT_ROLE not in self.desired

    @property
    def returns_to_default_fallback(self) -> bool:
        """割り当てをすべて外して、暗黙の default に戻る。"""
        return bool(self.current) and not self.desired


def plan_change(
    current: Iterable[str], desired: Iterable[str], matrix: AccessMatrix, default_role: str = DEFAULT_ROLE
) -> RoleChange:
    current_set = frozenset(current)
    desired_set = frozenset(desired)
    return RoleChange(
        current=current_set,
        desired=desired_set,
        before=visible_groups(current_set, matrix, default_role),
        after=visible_groups(desired_set, matrix, default_role),
    )


def unknown_roles(roles: Iterable[str], known_roles: Iterable[str]) -> list[str]:
    """存在しないロール名（並びを固定）。割り当てる前に、これが空であることを確かめる。"""
    known = frozenset(known_roles)
    return sorted(frozenset(roles) - known)


def unassigned_groups(known_groups: Iterable[str], matrix: AccessMatrix) -> list[str]:
    """登録されているが、どのロールにも割り当てられていないグループ（誰にも見えない。default にも入っていない）。"""
    assigned: set[str] = set()
    for groups in matrix.values():
        assigned.update(groups)
    return sorted(frozenset(known_groups) - assigned)
