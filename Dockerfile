FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# /code 直下に app/ と storage/ を置く（tools.py は ../../storage/chunk を参照する）
WORKDIR /code

COPY requirements.txt .
RUN pip install -r requirements.txt

# 本番相当で単体実行できるようにコードも COPY する。
# 開発時は docker-compose.yml のボリュームマウントで上書きされる。
COPY app ./app
COPY storage ./storage

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
