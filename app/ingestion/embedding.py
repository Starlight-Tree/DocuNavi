"""チャンクの埋め込み（ベクトル）を作って、chunks.embedding に保存する。

モデルは gemini-embedding-2、768 次元。**保存する資料側と、後で検索の質問から作る側で、モデルと次元を必ず揃える**
（このファイルの `MODEL` / `DIMENSIONS` / `document_text` / `query_text` が唯一の定義）。このモデルは `task_type` を
持たないので、用途は本文の前置きで伝える（公式の形: 資料は `title: {見出し} | text: {本文}`、質問は
`task: search result | query: {質問}`）。距離はコサイン（pgvector の `<=>`）。専用ロールの search_path に
extensions スキーマは無いので、検索のSQLでは `OPERATOR(extensions.<=>)` とスキーマ付きで書く。

**複数の本文を 1 回で送るとき、文字列のリストを渡してはいけない。** このモデルは文字列のリストを 1 つのベクトルに
まとめてしまう（公式の説明）。1 本ずつ `Content` に包んで渡すと、本文ごとのベクトルが返る。件数と次元は毎回検査する。

**変更の検出は、実際に送る入力全体（前置き・見出し・本文）のハッシュ（`input_hash`）で行う。** 本文が同じでも、見出しや
前置きの形が変わったら、作り直しの対象になる。保存するときも、行の本文と見出しが埋め込みを作ったときのままかを確かめる。

呼び出しの上限（`ApiBudget`）は、**送る直前**に数える。再試行は自前でもライブラリでもしない（試行は 1 回）。
失敗したら止まり、そこまでの保存は残る。次の実行は、まだ埋め込みの無い行から続ける。
"""

import hashlib
import json
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.d1 import MAX_BATCH

MODEL = "gemini-embedding-2"
DIMENSIONS = 768
BATCH_SIZE = 32  # 1 回に送る本文の数。上限は公式に書かれていないので、控えめにする（272 件で 9 回）
DEFAULT_MAX_CHUNKS = 300
DEFAULT_MAX_CALLS = 10
MAX_INPUT_CHARS = (
    4000  # 1 件の入力の文字数の上限。超えるチャンクがあれば、1 回も送らずに止める（モデルの上限は 8,192 トークン）
)
# 標準料金（1M トークンあたりの米ドル、2026-10-18 に公式の料金ページで確認。無料枠の対象でもある）。**概算用**で、強制はしない
PRICE_PER_MILLION_TOKENS = 0.20


# 埋め込みを Vertex AI（ADC 認証。API キーは使わない）で呼ぶかどうか。LLM の GOOGLE_GENAI_USE_VERTEXAI とは別の変数にする
# （LLM だけを切り替えずに済むように。真でも偽でも、モデルと次元は同じで、保存済みのベクトルはそのまま使う）
VERTEX_ENV = "EMBEDDING_USE_VERTEXAI"
VERTEX_LOCATION_ENV = "EMBEDDING_VERTEX_LOCATION"
# gemini-embedding-2 の提供先は global・us・eu（公式のモデルページ。リージョン指定は不可）
DEFAULT_VERTEX_LOCATION = "global"
_TRUTHY = {"1", "true", "yes", "on"}


def use_vertex() -> bool:
    return os.environ.get(VERTEX_ENV, "").strip().lower() in _TRUTHY


def batch_size() -> int:
    """1 回の呼び出しで送る本文の数。Vertex AI の gemini-embedding-2 は SDK が 1 回 1 本に限る（複数だと ValueError）。"""
    return 1 if use_vertex() else BATCH_SIZE


def make_client(api_key: str | None, timeout_ms: int) -> Any:
    """埋め込み用の SDK クライアント（取り込みと質問で共通）。再試行しない（attempts=1）。timeout はミリ秒。

    `use_vertex()` が真なら Vertex AI（`GOOGLE_CLOUD_PROJECT` があれば使い、無ければ ADC から解決。ロケーションは
    `EMBEDDING_VERTEX_LOCATION`、既定は global）。偽なら API キー。どちらも `vertexai` を明示して、
    `GOOGLE_GENAI_USE_VERTEXAI`（LLM 用）が埋め込みの経路を勝手に変えないようにする。
    """
    from google import genai
    from google.genai import types

    http_options = types.HttpOptions(timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=1))
    if use_vertex():
        return genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip() or None,
            location=os.environ.get(VERTEX_LOCATION_ENV, "").strip() or DEFAULT_VERTEX_LOCATION,
            http_options=http_options,
        )
    return genai.Client(vertexai=False, api_key=api_key, http_options=http_options)


class EmbeddingError(RuntimeError):
    """埋め込みの取得に失敗した、または返りが想定と違う（件数・次元・中身）。"""


class BudgetExceeded(EmbeddingError):
    """呼び出しの上限に達した（上限を超える呼び出しは、送る前に止める）。"""


def document_text(heading: str | None, content: str) -> str:
    return f"title: {heading or 'none'} | text: {content}"


def input_hash(heading: str | None, content: str) -> str:
    """API に送る入力全体（前置きを含む）のハッシュ。作り直しの判定と、保存時の競合の検査に使う。"""
    return hashlib.sha256(document_text(heading, content).encode("utf-8")).hexdigest()


def query_text(question: str) -> str:
    return f"task: search result | query: {question}"


@dataclass
class ApiBudget:
    max_calls: int
    calls: int = 0

    def spend(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"呼び出しの上限 {self.max_calls} 回に達しました")
        self.calls += 1


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """texts の順に、1 本ずつのベクトルを返す。"""


def check_vectors(vectors: Sequence[Sequence[float]], expected: int) -> list[list[float]]:
    """件数・次元・中身を検査する。まとめられて 1 本だけ返った場合なども、ここで止める。"""
    if len(vectors) != expected:
        raise EmbeddingError(f"ベクトルの数が合いません（送信 {expected}、返信 {len(vectors)}）")
    checked: list[list[float]] = []
    for vector in vectors:
        values = [float(v) for v in vector]
        if len(values) != DIMENSIONS:
            raise EmbeddingError(f"次元が合いません（期待 {DIMENSIONS}、返信 {len(values)}）")
        if not all(math.isfinite(v) for v in values) or not any(values):
            raise EmbeddingError("ベクトルの中身が不正です（非数、または全て 0）")
        checked.append(values)
    return checked


class GeminiEmbedder:
    def __init__(self, api_key: str | None, budget: ApiBudget) -> None:
        from google.genai import types

        self._types = types
        self._budget = budget
        # api_key は Vertex AI のとき使わない（None でよい）
        self._client = make_client(api_key, 60_000)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        types = self._types
        self._budget.spend()
        contents = [types.Content(parts=[types.Part(text=text)]) for text in texts]
        try:
            result = self._client.models.embed_content(
                model=MODEL,
                contents=contents,
                config=types.EmbedContentConfig(output_dimensionality=DIMENSIONS),
            )
        except Exception as exc:  # noqa: BLE001 - 例外の文字列には本文・URL が入りうる。型とステータスだけを残す
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            raise EmbeddingError(f"{type(exc).__name__}" + (f" (status={status})" if status else "")) from None
        return check_vectors([e.values or [] for e in (result.embeddings or [])], len(texts))


@dataclass(frozen=True)
class PendingChunk:
    id: int
    content_hash: str
    heading: str | None
    content: str

    @property
    def input_hash(self) -> str:
        return input_hash(self.heading, self.content)


class EmbeddingStore(Protocol):
    def pending_embeddings(self, limit: int) -> list[PendingChunk]:
        """埋め込みが無い（または別のモデル・次元の）チャンク。id の順。"""

    def count_pending(self) -> int: ...

    def save_embeddings(self, rows: Sequence[tuple[PendingChunk, list[float]]]) -> int:
        """1 回の取引で保存する。本文のハッシュが変わっていた行は飛ばす。保存した行数を返す。"""


@dataclass
class EmbedReport:
    pending_total: int  # 保存先で、まだ埋め込みの無いチャンクの数
    selected: int  # 今回の対象（max_chunks で切った数）
    characters: int  # 送る本文（前置きを含む）の文字数
    estimated_cost_usd: float  # 概算。文字数 × 1 文字 1 トークンとみた推計で、保証された上限ではない（強制するのは件数・回数・入力の大きさ）
    calls_needed: int
    applied: bool
    saved: int = 0
    calls: int = 0
    error: str | None = None


def embed_pending(
    store: EmbeddingStore, embedder: Embedder, budget: ApiBudget, *, max_chunks: int, apply: bool
) -> EmbedReport:
    pending = store.pending_embeddings(max_chunks)
    texts = [document_text(c.heading, c.content) for c in pending]
    characters = sum(len(t) for t in texts)
    report = EmbedReport(
        pending_total=store.count_pending(),
        selected=len(pending),
        characters=characters,
        estimated_cost_usd=characters * PRICE_PER_MILLION_TOKENS / 1_000_000,
        calls_needed=math.ceil(len(pending) / batch_size()),
        applied=apply,
    )
    if not apply or not pending:
        return report
    too_long = [c.id for c, t in zip(pending, texts, strict=True) if len(t) > MAX_INPUT_CHARS]
    if too_long:
        report.error = (
            f"入力が {MAX_INPUT_CHARS} 文字を超えるチャンクがあります（id: {too_long[:5]}）。送らずに止めました"
        )
        return report
    if report.calls_needed > budget.max_calls:
        report.error = (
            f"必要な呼び出し {report.calls_needed} 回が上限 {budget.max_calls} 回を超えます（送らずに止めました）"
        )
        return report
    step = batch_size()
    for start in range(0, len(pending), step):
        batch = pending[start : start + step]
        try:
            vectors = embedder.embed(texts[start : start + step])
            report.saved += store.save_embeddings(list(zip(batch, vectors, strict=True)))
        except EmbeddingError as exc:
            report.error = str(exc)
            break
        finally:
            report.calls = budget.calls
    return report


class D1EmbeddingStore:
    """chunks の埋め込みの記録（モデル・次元・入力ハッシュ）を D1 に、ベクトルそのものを Vectorize に書く。

    作り直しの判定は、送る入力全体のハッシュ（Python 側の `input_hash`）で行うので、全行を読んで比べる（数百〜数千行の想定）。
    Vectorize のメタデータ（collection / target / document_id）は、ベクトルを書くときの資料の値。資料の分類を後から変えたときは、
    その資料のチャンクの `embedding_model` を NULL に戻して再実行する（Vectorize はメタデータだけの更新ができない）。
    """

    def __init__(self, client: Any) -> None:
        self._db = client
        self._pending: list[PendingChunk] | None = (
            None  # 1 回の実行で全チャンクを 2 回読まないための控え（保存で捨てる）
        )

    def _all_pending(self) -> list[PendingChunk]:
        if self._pending is not None:
            return self._pending
        rows = self._db.execute(
            "SELECT id, content_hash, heading, content, embedding_model IS NOT NULL AS has_vector, embedding_model, "
            "embedding_dimensions, embedding_input_hash FROM chunks ORDER BY id"
        )
        pending: list[PendingChunk] = []
        for row in rows:
            chunk = PendingChunk(row["id"], row["content_hash"], row["heading"], row["content"])
            current = (
                bool(row["has_vector"])
                and row["embedding_model"] == MODEL
                and row["embedding_dimensions"] == DIMENSIONS
                and row["embedding_input_hash"] == chunk.input_hash
            )
            if not current:
                pending.append(chunk)
        self._pending = pending
        return pending

    def pending_embeddings(self, limit: int) -> list[PendingChunk]:
        return self._all_pending()[:limit]

    def count_pending(self) -> int:
        return len(self._all_pending())

    def save_embeddings(self, rows: Sequence[tuple[PendingChunk, list[float]]]) -> int:
        self._pending = None
        # 本文のハッシュと見出しが、埋め込みを作ったときのままの行だけを更新する（取り込みで入れ替わった行に、古い入力のベクトルを付けない）
        saved = 0
        for i in range(0, len(rows), MAX_BATCH):
            part = rows[i : i + MAX_BATCH]
            self._db.batch(
                [
                    (
                        "UPDATE chunks SET embedding_model = ?, embedding_dimensions = ?, embedding_input_hash = ? "
                        "WHERE id = ? AND content_hash = ? AND heading IS NOT DISTINCT FROM ?",
                        (MODEL, DIMENSIONS, chunk.input_hash, chunk.id, chunk.content_hash, chunk.heading),
                    )
                    for chunk, _ in part
                ]
            )
            ids = json.dumps([chunk.id for chunk, _ in part])
            meta = {
                row["id"]: row
                for row in self._db.execute(
                    "SELECT c.id AS id, c.embedding_input_hash AS input_hash, d.id AS document_id, d.collection AS collection, "
                    "d.target AS target FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE c.id IN (SELECT value FROM json_each(?))",
                    (ids,),
                )
            }
            vectors = []
            for chunk, vector in part:
                row = meta.get(chunk.id)
                if row is None or row["input_hash"] != chunk.input_hash:
                    continue  # 取り込みで入れ替わった行
                metadata: dict[str, Any] = {"document_id": row["document_id"]}
                if row["collection"] is not None:
                    metadata["collection"] = row["collection"]
                if row["target"] is not None:
                    metadata["target"] = row["target"]
                vectors.append({"id": str(chunk.id), "values": [float(v) for v in vector], "metadata": metadata})
            if vectors:
                self._db.vectors_upsert(vectors)
                saved += len(vectors)
        return saved
