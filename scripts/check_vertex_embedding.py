"""埋め込みを Vertex AI で呼べるかの実測（実 API を呼ぶ。課金される。運用者が手元で実行する）。

    環境変数の EMBEDDING_USE_VERTEXAI には依存せず、API キー経路（GEMINI_API_KEY）と Vertex AI（ADC）の両方で、同じ 3 本の短い文を埋め込み、次を表示する:
    - Vertex AI が、複数の Content を 1 回で渡したときに 1 本ずつのベクトルを返すか（返らなければ、取り込みは 1 本ずつの呼び出しに変える必要がある）
    - 次元（768）
    - 同じ文の API キー経路と Vertex AI のベクトルのコサイン類似度（ほぼ 1 なら、保存済みのベクトルと質問のベクトルを混ぜてよい。
      低いなら、混ぜると検索が壊れるので、全チャンクを Vertex AI で作り直す必要がある）

呼び出しは最大 2 回（API キー 1・Vertex AI で一括 1）。入力は架空の短い文だけ。キー・トークン・ベクトルの値は表示しない。

    GEMINI_API_KEY=… GOOGLE_CLOUD_PROJECT=<課金が有効なプロジェクト> python -m scripts.check_vertex_embedding

ADC は、運用者自身が `gcloud auth application-default login` などで用意する（このスクリプトは認証情報に触れない）。
"""

import math
import os
import sys

from google.genai import types

from app.ingestion import embedding

TEXTS = (
    "title: 洗濯の手順 | text: 洗濯槽に衣類を入れて、スタートを押します。",
    "title: none | text: 営業時間は朝から夕方までです。",
    "task: search result | query: 洗濯機の使い方は？",
)


def _contents() -> list[types.Content]:
    return [types.Content(parts=[types.Part(text=t)]) for t in TEXTS]


def _embed(client, contents) -> list[list[float]]:
    result = client.models.embed_content(
        model=embedding.MODEL,
        contents=contents,
        config=types.EmbedContentConfig(output_dimensionality=embedding.DIMENSIONS),
    )
    return [list(e.values or []) for e in (result.embeddings or [])]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        print("GEMINI_API_KEY と GOOGLE_CLOUD_PROJECT が要ります。", file=sys.stderr)
        return 2
    os.environ.pop("EMBEDDING_USE_VERTEXAI", None)
    api = embedding.make_client(key, 60_000)
    os.environ["EMBEDDING_USE_VERTEXAI"] = "true"
    vertex = embedding.make_client(None, 60_000)

    try:
        api_vectors = _embed(api, _contents())
        print(f"API キー: {len(api_vectors)} 本 / 次元 {[len(v) for v in api_vectors]}")
        batch = _embed(vertex, _contents())
        print(f"Vertex AI（一括 1 回）: {len(batch)} 本 / 次元 {[len(v) for v in batch]}")
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には URL・プロジェクトが入りうる。型とステータスだけ
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        print(f"失敗: {type(exc).__name__} (status={status})", file=sys.stderr)
        return 1

    if len(batch) != len(TEXTS):
        print("→ Vertex AI は一括では 1 本ずつを返さない。取り込みは 1 本ずつ呼ぶ形にする必要がある。")
        return 1
    if any(len(v) != embedding.DIMENSIONS for v in batch + api_vectors):
        print("→ 次元が 768 ではない。")
        return 1
    sims = [_cosine(a, b) for a, b in zip(api_vectors, batch, strict=True)]
    print("API キーと Vertex AI のコサイン類似度:", ", ".join(f"{s:.4f}" for s in sims))
    print("→ 0.99 以上なら混ぜてよい。それ未満なら、全チャンクを Vertex AI で作り直す。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
