# ロール別の資料グループ制御の管理画面（Django Admin。ロードマップ ⑧ のフェーズ 4a）。非公開の Cloud Run `docunavi-admin` 用。
# ビルドの文脈は ops/access_admin（アプリ本体の app/ は使わない）。Cloud Run が $PORT を渡す。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /code

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && chown app:app /code

COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY --chown=app:app . .

USER app

# 静的ファイルの集約は、本番の設定を読むため、ビルド専用のダミー値を渡す（この RUN の間だけの環境変数で、イメージには残らない）
RUN ADMIN_SECRET_KEY=build-only-build-only-build-only-0000 \
    ADMIN_ALLOWED_HOSTS=localhost \
    ADMIN_DATABASE_URL=postgresql://build:build@localhost:5432/postgres \
    python manage.py collectstatic --noinput

ENV PORT=8080
EXPOSE 8080

# 起動のたびに設定を検査する（足りない環境変数・形式の誤りで、ここで失敗する）。migrate は起動時に走らせない（別のステージで運用者が実行する）。
# Cloud Run の起動プローブは /healthz（DB を見ない）、設定が効いているかの確認は /readyz。
CMD ["sh", "-c", "python manage.py check && exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 30 --access-logfile -"]
