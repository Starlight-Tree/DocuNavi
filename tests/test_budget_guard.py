"""予算の自動停止（ops/budget_guard/main.py）。課金は変えない（Cloud Billing の API は呼ばない）。"""

import base64
import json
from types import SimpleNamespace

import pytest

from ops.budget_guard import main as guard


def _event(payload: dict) -> SimpleNamespace:
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return SimpleNamespace(data={"message": {"data": data}})


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(guard, "_disable_billing", calls.append)
    monkeypatch.setenv("PROJECT_ID", "p")
    return calls


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"costAmount": 4.99, "budgetAmount": 5}, False),
        ({"costAmount": 5, "budgetAmount": 5}, True),
        ({"costAmount": 7.2, "budgetAmount": 5}, True),
        ({"costAmount": 0, "budgetAmount": 5}, False),
        ({"budgetAmount": 5}, False),  # 数値が読めないときは止めない
        ({"costAmount": "9", "budgetAmount": 5}, False),
        ({"costAmount": 9, "budgetAmount": 0}, False),
    ],
)
def test_over_budget(payload: dict, expected: bool) -> None:
    assert guard.over_budget(payload) is expected


def test_dry_run_is_the_default_and_does_not_disable(disabled: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRY_RUN", raising=False)
    guard.stop_billing(_event({"costAmount": 6, "budgetAmount": 5}))
    assert disabled == []


def test_live_mode_disables_only_when_over_budget(disabled: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    guard.stop_billing(_event({"costAmount": 4, "budgetAmount": 5}))
    assert disabled == []
    guard.stop_billing(_event({"costAmount": 5.01, "budgetAmount": 5}))
    assert disabled == ["p"]


def test_missing_project_id_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROJECT_ID", raising=False)
    with pytest.raises(RuntimeError):
        guard.stop_billing(_event({"costAmount": 6, "budgetAmount": 5}))
