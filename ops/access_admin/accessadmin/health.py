"""ヘルスチェック。Cloud Run の起動の確認・生存の確認に使う（認証なし。ただしサービス自体が非公開）。

- `/healthz`: プロセスが応答する（DB は見ない）。
- `/readyz`: 設定が正しく効いているか。search_path の先頭が saas、`chatbot.admin_users` が読める、Django の migrate が済んでいる。
  失敗した検査の名前だけを返す（例外の本文・SQL・接続先は返さない。ログには型の名前だけ）。
"""

import logging

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

logger = logging.getLogger(__name__)


@never_cache
@require_safe
def healthz(request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok", content_type="text/plain")


def _check_search_path() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        if cursor.fetchone()[0] != "saas":
            raise RuntimeError("search_path")


def _check_admin_users() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM chatbot.admin_users LIMIT 1")
        cursor.fetchall()


def _check_migrations() -> None:
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise RuntimeError("migrations")


# (名前, 検査)。順に実行し、最初の失敗で止める
CHECKS = (
    ("search_path", _check_search_path),
    ("admin_users", _check_admin_users),
    ("migrations", _check_migrations),
)


@never_cache
@require_safe
def readyz(request: HttpRequest) -> JsonResponse:
    for name, check in CHECKS:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
            logger.warning("readyz: %s の検査に失敗しました (%s)", name, type(exc).__name__)
            return JsonResponse({"status": "unavailable", "check": name}, status=503)
    return JsonResponse({"status": "ok"})
