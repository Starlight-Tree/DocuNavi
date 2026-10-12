FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# /code 直下に app/ と storage/ を置く（tools.py は ../../storage/chunk を参照する）
WORKDIR /code

# root 以外で実行する。UID 固定にしておくと、Cloud Run 以外でもボリュームの権限を合わせやすい
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install -r requirements.txt

# 本番相当で単体実行できるようにコードも COPY する。
# 開発時は docker-compose.yml のボリュームマウントで上書きされる。
COPY --chown=app:app app ./app
COPY --chown=app:app storage ./storage

USER app

# Cloud Run は待ち受けポートを $PORT で渡してくる。未指定（ローカル・compose）なら 8000
ENV PORT=8000
EXPOSE 8000

# exec 形式のままでは $PORT が展開されないため sh -c を挟み、exec で uvicorn を PID 1 にして
# SIGTERM（Cloud Run のスケールダウン）を直接受けられるようにする
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
