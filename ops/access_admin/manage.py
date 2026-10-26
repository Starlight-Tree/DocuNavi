#!/usr/bin/env python
"""Django の管理コマンド。設定の既定は config.settings.test（ローカルの一時 Postgres）。本番は DJANGO_SETTINGS_MODULE=config.settings.prod を渡す。"""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
