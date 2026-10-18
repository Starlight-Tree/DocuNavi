# ---- Stage 1: フロントエンド（Vite + React）のビルド ------------------------------------
# frontend/package.json の engines（Node 22.12 以上）を満たすバージョン。手元の検証は Node 24
FROM node:24-slim AS frontend-build

WORKDIR /frontend

# 依存の層は package*.json が変わらない限りキャッシュされる
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# ビルドに必要なものだけを COPY する（手元の node_modules / dist をイメージに持ち込まない）。
# frontend/ に public/ などビルド入力が増えたら、ここに足す
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src

# tsc --noEmit && vite build（成果物は /frontend/dist）
RUN npm run build


# ---- Stage 2: 実行用イメージ（FastAPI + LangGraph + ビルド済みフロントエンド）-----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# /code 直下に app/ と storage/ を置く（tools.py は ../../storage/pdf を参照する）。
# app/frontend.py は /code/frontend/dist（このイメージでは Stage 1 の成果物）を配信する
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
COPY --from=frontend-build --chown=app:app /frontend/dist ./frontend/dist

USER app

# Cloud Run は待ち受けポートを $PORT で渡してくる。未指定（ローカル・compose）なら 8000
ENV PORT=8000
EXPOSE 8000

# exec 形式のままでは $PORT が展開されないため sh -c を挟み、exec で uvicorn を PID 1 にして
# SIGTERM（Cloud Run のスケールダウン）を直接受けられるようにする
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
