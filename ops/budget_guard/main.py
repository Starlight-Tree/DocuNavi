"""予算が上限に届いたら、このプロジェクトの課金を無効化する（Cloud Run functions、Pub/Sub トリガー）。

手順は docs/deploy.md の「G. 予算の自動停止」。公式の方法（Cloud Billing の「Disable billing with notifications」）に沿う。
`DRY_RUN`（既定 true）の間は、止める判断をログに出すだけで、課金は変えない。本番で止めるには `DRY_RUN=false` にする。
課金を無効にすると、プロジェクトのサービスはすべて止まり、リソースは消えることがある。復旧は手動。
予算の通知は課金データより遅れるので、上限は厳密ではない。
"""

import base64
import json
import logging
import os
from typing import Any

try:
    import functions_framework

    _register = functions_framework.cloud_event
except ImportError:  # テスト（CI）には入れていない。本番の関数では requirements.txt で入る

    def _register(func: Any) -> Any:
        return func


log = logging.getLogger("budget-guard")


def decode(cloud_event: Any) -> dict[str, Any]:
    """Pub/Sub のメッセージ本文（base64 の JSON）を辞書にする。"""
    raw = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("budget notification is not a JSON object")
    return payload


def over_budget(payload: dict[str, Any]) -> bool:
    """累計の費用が予算額に届いたか。数値が読めないときは、止めない（誤って止めるより、ログに残す）。"""
    cost, budget = payload.get("costAmount"), payload.get("budgetAmount")
    if not isinstance(cost, int | float) or not isinstance(budget, int | float) or budget <= 0:
        log.warning("budget notification without usable amounts; not stopping")
        return False
    return cost >= budget


def _disable_billing(project_id: str) -> None:
    from google.cloud import billing_v1  # 本番の関数にだけある

    # 先に get_project_billing_info で確かめない: プロジェクトの閲覧権限（resourcemanager.projects.get）が別に要り、
    # 請求先アカウント側の権限だけの SA では 403 になる（2026-10-20）。外す操作は、すでに外れていても害がない。
    client = billing_v1.CloudBillingClient()
    client.update_project_billing_info(name=f"projects/{project_id}", project_billing_info={"billing_account_name": ""})
    log.warning("billing disabled for the project")


@_register
def stop_billing(cloud_event: Any) -> None:
    project_id = os.environ.get("PROJECT_ID", "")
    if not project_id:
        raise RuntimeError("PROJECT_ID is not set")  # 黙って何もしないより、失敗として見えるようにする
    payload = decode(cloud_event)
    if not over_budget(payload):
        log.info("within budget (threshold %s)", payload.get("alertThresholdExceeded"))
        return
    if os.environ.get("DRY_RUN", "true").lower() != "false":
        log.warning(
            "DRY_RUN: would disable billing (cost %s of budget %s)",
            payload.get("costAmount"),
            payload.get("budgetAmount"),
        )
        return
    _disable_billing(project_id)
