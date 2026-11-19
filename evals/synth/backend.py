"""本文を書くバックエンド。差し替え可能にしてある（Cloud Run GPU の vLLM も、Gemini の互換エンドポイントも同じ口で呼ぶ）。

- `FakeBackend`: 外部に接続しない。プロンプトの事実から決まった本文を作る（配線とテスト用。品質の測定には使えない）。
- `OpenAICompatBackend`: `POST {base_url}/chat/completions`（OpenAI 互換。vLLM の `vllm serve` が話す形）。
  https か、ローカル（localhost・127.0.0.1。`gcloud run services proxy` の受け口）だけを許す。
  認証トークンは、環境変数の名前（`--token-env`）から読む。値は表示・ログ・例外に出さない。応答の本文はエラーに入れない。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"


class BackendError(Exception):
    """バックエンドの失敗。メッセージは固定文（応答の本文・URL・トークンを含めない）。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class Backend(Protocol):
    name: str
    model: str

    def generate(self, system: str, user: str, *, seed: int, max_tokens: int) -> Completion: ...


def estimate_tokens(text: str) -> int:
    """日本語を含む文字列のおおよそのトークン数（上限の予約用。1 文字 = 1 トークンとして多めに見る）。"""
    return len(text)


class FakeBackend:
    name = "fake"
    model = "fake-deterministic"

    def generate(self, system: str, user: str, *, seed: int, max_tokens: int) -> Completion:
        title = re.search(r"^題名: (.+)$", user, re.M)
        span = re.search(r"^文字数: (\d+)〜(\d+)$", user, re.M)
        facts = re.findall(r"^- \[[^\]]+\] (.+)$", user, re.M)
        if not (title and span and facts):
            raise BackendError("fake: プロンプトの書式を読めません")
        min_chars = int(span.group(1))
        body: list[str] = [f"# {title.group(1)}", "## 概要"]
        count, i = 0, 0
        while sum(len(p) for p in body) < int(min_chars * 1.1) + 40:  # 見出しの記号と「（N 項）」の分が、検証の文字数から引かれるので、長い資料では余裕を割合で持つ
            if i and i % 6 == 0:
                body.append(f"## 補足 {i // 6}")
            fact = facts[(seed + i) % len(facts)]
            body.append(f"{fact}（{i + 1} 項）")
            i += 1
            count += 1
            if count > 5000:
                raise BackendError("fake: 本文を組み立てられません")
        text = "\n\n".join(body)
        return Completion(text, prompt_tokens=estimate_tokens(system + user), completion_tokens=estimate_tokens(text))


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme == "https" and parsed.hostname:
        return base_url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return base_url.rstrip("/")
    raise BackendError("base-url は https か、ローカル（localhost・127.0.0.1）の http だけ使えます")


class OpenAICompatBackend:
    name = "openai"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        token_env: str | None = None,
        temperature: float = 0.7,
        timeout: float = 600.0,
    ) -> None:
        self.base_url = check_base_url(base_url)
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._token_env = token_env

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token_env:
            token = os.environ.get(self._token_env)
            if not token:
                raise BackendError(f"環境変数 {self._token_env} が空です")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def generate(self, system: str, user: str, *, seed: int, max_tokens: int) -> Completion:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "seed": seed,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise BackendError(f"HTTP {exc.code}", retryable=exc.code in (408, 429, 500, 502, 503, 504)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BackendError("接続に失敗しました", retryable=True) from None
        except json.JSONDecodeError:
            raise BackendError("応答が JSON ではありません") from None
        try:
            choice = data["choices"][0]
            usage = data.get("usage") or {}
            return Completion(
                text=choice["message"]["content"] or "",
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                finish_reason=str(choice.get("finish_reason") or "stop"),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise BackendError("応答の形が想定と違います") from None
