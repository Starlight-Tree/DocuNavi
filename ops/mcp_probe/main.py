"""Cloud Run での MCP のつなぎ方を確かめるための、固定の返答だけの最小サーバー（docs/mcp-tool-design.md の「先にやる小さな検証」）。

資料・DB・秘密情報には触れない。ツールは `ping` だけで、常に "pong" を返す。検証が終わったら、サービスごと削除する。
"""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("docunavi-mcp-probe", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), stateless_http=True)


@mcp.tool()
def ping() -> str:
    """接続の確認用。常に "pong" を返す。"""
    return "pong"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
