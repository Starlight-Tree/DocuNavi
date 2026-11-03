"""埋め込みを Vertex AI で呼べるかの実測（実 API を呼ぶ。課金される。運用者が手元で実行する）。

既定は Vertex AI だけ（ADC 認証。API キーは要らない）。呼び出しは 3 回で、入力は架空の短い文だけ:

    1. 3 本の文を、1 回に複数の Content として渡す（取り込みと同じ形）→ 返却本数・次元・値の健全性・遅延
    2. 質問を 1 本だけ渡す（検索と同じ形）→ 遅延
    3. 2 と同じ（接続の確立後の遅延）

本番の質問の埋め込みの上限（`EMBED_TIMEOUT_SECONDS`）と比べる。1 の本数が 3 にならなければ、取り込みは 1 本ずつの呼び出しに変える必要がある。

**既存のベクトルとの互換性は、この実行では確かめない**（API キー経路のベクトルと比べるには、`--compare-api` で `GEMINI_API_KEY` を渡し、
API キー経路を 1 回余分に呼ぶ。未指定なら呼ばない）。

    python -m scripts.check_vertex_embedding --project docunavi-app
    python -m scripts.check_vertex_embedding --project docunavi-app --compare-api   # 互換性も測る（GEMINI_API_KEY が要る）

ADC は、運用者自身が `gcloud auth application-default login` などで用意する（このスクリプトは認証情報に触れず、トークンも表示しない）。
表示するのは、本数・次元・秒数・類似度だけ。ベクトルの値、プロジェクトの詳細、例外の文字列は出さない。
"""

import argparse
import math
import os
import sys
import time

from google.genai import types

from app.ingestion import embedding
from app.search.vector import EMBED_TIMEOUT_SECONDS

DOCUMENTS = (
    "title: 洗濯の手順 | text: 洗濯槽に衣類を入れて、スタートを押します。",
    "title: none | text: 営業時間は朝から夕方までです。",
    "title: 注意 | text: 水を入れすぎないでください。",
)
QUERY = "task: search result | query: 洗濯機の使い方は？"


def _contents(texts: tuple[str, ...]) -> list[types.Content]:
    return [types.Content(parts=[types.Part(text=t)]) for t in texts]


def _embed(client, texts: tuple[str, ...]) -> tuple[list[list[float]], float]:
    started = time.monotonic()
    result = client.models.embed_content(
        model=embedding.MODEL,
        contents=_contents(texts),
        config=types.EmbedContentConfig(output_dimensionality=embedding.DIMENSIONS),
    )
    elapsed = time.monotonic() - started
    return [list(e.values or []) for e in (result.embeddings or [])], elapsed


def _healthy(vector: list[float]) -> bool:
    return all(math.isfinite(v) for v in vector) and any(v != 0 for v in vector)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def _failure(step: str, exc: Exception) -> int:
    # 例外の文字列には URL・プロジェクトが入りうる。型とステータスだけを出す
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    print(f"失敗（{step}）: {type(exc).__name__}" + (f" (status={status})" if status else ""), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vertex AI の埋め込みの実測（実 API・課金あり）")
    parser.add_argument("--project", required=True, help="Vertex AI を呼ぶ GCP プロジェクト（課金が有効なもの）")
    parser.add_argument(
        "--compare-api", action="store_true", help="API キー経路も 1 回呼び、ベクトルを比べる（GEMINI_API_KEY が要る）"
    )
    args = parser.parse_args(argv)

    os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    os.environ["EMBEDDING_USE_VERTEXAI"] = "true"
    location = os.environ.get(embedding.VERTEX_LOCATION_ENV, "").strip() or embedding.DEFAULT_VERTEX_LOCATION
    print(
        f"Vertex AI: プロジェクト {args.project} / ロケーション {location} / モデル {embedding.MODEL} / 次元 {embedding.DIMENSIONS}"
    )
    try:
        vertex = embedding.make_client(None, 60_000)
    except Exception as exc:  # noqa: BLE001 - ADC が無い場合など
        return _failure("クライアントの作成（ADC）", exc)

    problems = 0
    try:
        batch, t_batch = _embed(vertex, DOCUMENTS)
        print(
            f"1. 一括（{len(DOCUMENTS)} 本を 1 回で）: 返却 {len(batch)} 本 / 次元 {[len(v) for v in batch]} / {t_batch:.2f} 秒"
        )
        first, t_first = _embed(vertex, (QUERY,))
        print(f"2. 質問 1 本: 返却 {len(first)} 本 / 次元 {[len(v) for v in first]} / {t_first:.2f} 秒")
        again, t_again = _embed(vertex, (QUERY,))
        print(f"3. 質問 1 本（2 回目）: 返却 {len(again)} 本 / {t_again:.2f} 秒")
    except Exception as exc:  # noqa: BLE001
        return _failure("呼び出し", exc)

    if len(batch) != len(DOCUMENTS):
        print("→ 一括では 1 本ずつを返さない。取り込みは 1 本ずつ呼ぶ形に変える必要がある。")
        problems += 1
    if any(len(v) != embedding.DIMENSIONS for v in batch + first + again):
        print(f"→ 次元が {embedding.DIMENSIONS} ではない。")
        problems += 1
    if not all(_healthy(v) for v in batch + first + again):
        print("→ 非数、または全て 0 のベクトルがある。")
        problems += 1
    slowest = max(t_first, t_again)
    verdict = "収まる" if slowest < EMBED_TIMEOUT_SECONDS else "収まらない"
    print(
        f"→ 質問の遅延 {slowest:.2f} 秒は、上限 {EMBED_TIMEOUT_SECONDS:.0f} 秒に{verdict}（1〜2 回の実測。分布ではない）。"
    )
    if slowest >= EMBED_TIMEOUT_SECONDS:
        problems += 1

    if args.compare_api:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            print("--compare-api には GEMINI_API_KEY が要ります（比較は行わない）。", file=sys.stderr)
            return 2
        os.environ.pop("EMBEDDING_USE_VERTEXAI", None)
        try:
            api = embedding.make_client(key, 60_000)
            api_vectors, _ = _embed(api, DOCUMENTS)
        except Exception as exc:  # noqa: BLE001
            return _failure("API キー経路", exc)
        if len(api_vectors) == len(batch):
            sims = ", ".join(f"{_cosine(a, b):.4f}" for a, b in zip(api_vectors, batch, strict=True))
            print(f"API キー経路とのコサイン類似度: {sims}（0.99 以上なら混ぜてよい。未満なら全チャンクを作り直す）")
    else:
        print("既存ベクトルとの互換性: 未確認（--compare-api を付けると測る）。")

    print("問題なし" if not problems else f"問題 {problems} 件")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
