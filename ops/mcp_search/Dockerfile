# 資料検索の MCP サーバーのイメージ（docs/mcp-tool-design.md）。アプリの app/ を使うので、ビルドの文脈はリポジトリの根
# （ops/mcp_search/cloudbuild.yaml が `--file` で指す）。アプリの依存に `mcp` は足さず、このイメージにだけ入れる。
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /code

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

# アプリの固定済みの依存に mcp を足す。1 回の pip install で解決し、アプリの版を動かさない
COPY requirements.txt ./requirements.txt
COPY ops/mcp_search/requirements.txt ./mcp-requirements.txt
RUN pip install --only-binary :all: -r requirements.txt -r mcp-requirements.txt

COPY --chown=app:app app ./app
COPY --chmod=0755 ops/mcp_search/main.py ./ops/mcp_search/main.py

USER app

# Cloud Run が $PORT を渡す。main.py が読む
ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["python", "-m", "ops.mcp_search.main"]
