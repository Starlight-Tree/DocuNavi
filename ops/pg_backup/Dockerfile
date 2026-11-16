# 本番 Postgres の定期バックアップ Job のイメージ（手順は docs/pg-backup.md）。pg_dump は 17 以上（サーバーは 17.2）。
FROM python:3.14-slim-trixie
ENV PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/* \
    && pg_dump --version | grep -Eq '\) 1[7-9]\.|\) [2-9][0-9]\.' \
    && pg_restore --version >/dev/null
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
RUN useradd --system --no-create-home runner
USER runner
ENTRYPOINT ["python", "main.py"]
