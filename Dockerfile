# ---- 実行用イメージ（FastAPI + LangGraph。API だけ。画面は Vercel の frontend-next が配信する）-----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# /code 直下に app/ と storage/ を置く（tools.py は ../../storage/pdf を参照する）
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

# exec 形式のままでは $PORT が展開されないため sh -c を挟み、exec で hypercorn を PID 1 にして
# SIGTERM（Cloud Run のスケールダウン）を直接受けられるようにする。
# hypercorn は同じポートで HTTP/1.1 と h2c（HTTP/2 の平文）の両方を受ける。Cloud Run の --use-http2 を付けなければ、従来どおり HTTP/1.1 で届く
CMD ["sh", "-c", "exec hypercorn app.main:app --bind 0.0.0.0:${PORT}"]
