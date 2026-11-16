# MCP のつなぎ方の検証用の最小サーバーのイメージ（docs/mcp-tool-design.md）。検証が終わったら、サービスとイメージを削除する。
FROM python:3.15-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
RUN useradd --system --no-create-home runner
USER runner
ENTRYPOINT ["python", "main.py"]
