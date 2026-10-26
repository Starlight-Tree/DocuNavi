import os

from django.core.wsgi import get_wsgi_application

# 配備（Docker）は本番の設定を使う。環境変数が足りなければ、起動時に ImproperlyConfigured で失敗する
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()
