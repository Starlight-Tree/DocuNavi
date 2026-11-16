# R2 の配線（Cloud Run サービスの更新・切り替え・切り戻し）の Job のイメージ（手順は docs/r2-rollout.md）。
# モードは Job の引数で固定する（ENTRYPOINT は引数なしだと終了コード 2 で止まる）。
FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
RUN useradd --system --no-create-home runner
USER runner
ENTRYPOINT ["python", "main.py"]
