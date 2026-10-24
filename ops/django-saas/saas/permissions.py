"""役割ごとの権限。数字が大きいほど強い。"""

from .models import Role

_RANK = {Role.VIEWER: 1, Role.MEMBER: 2, Role.OWNER: 3}


def allows(role: str, minimum: str) -> bool:
    return _RANK.get(role, 0) >= _RANK[minimum]
