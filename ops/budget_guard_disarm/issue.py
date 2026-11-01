"""承認トークンを 1 つ作る（人が自分の端末で実行する）。

標準出力には、Secret Manager に入れる JSON（トークンのハッシュと期限だけ）を出す。トークン本体は標準エラーに 1 回だけ出す。
    python -m ops.budget_guard_disarm.issue --minutes 15 \
      | gcloud secrets versions add <シークレット名> --project <プロジェクト> --data-file=-
トークンは `X-Disarm-Token` ヘッダーで送る。使うと無効になり、期限（既定 15 分）を過ぎても無効になる。
"""

import argparse
import hashlib
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta

MAX_MINUTES = 60


def new_token(now: datetime, minutes: int) -> tuple[str, dict[str, str]]:
    if not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f"minutes must be between 1 and {MAX_MINUTES}")
    token = secrets.token_urlsafe(32)
    payload = {
        "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
    }
    return token, payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=int, default=15, help=f"有効な分数（1〜{MAX_MINUTES}。既定 15）")
    args = parser.parse_args()
    token, payload = new_token(datetime.now(UTC), args.minutes)
    sys.stderr.write(f"token (1 回だけ表示。X-Disarm-Token に入れる): {token}\n")
    sys.stdout.write(json.dumps(payload) + "\n")


if __name__ == "__main__":
    main()
