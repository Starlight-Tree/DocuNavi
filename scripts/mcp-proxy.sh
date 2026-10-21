#!/usr/bin/env bash
# docunavi-mcp へのローカルプロキシ（127.0.0.1:3001）を、ログイン済みのユーザー自身の権限で起動する。
# 日常の ID（閲覧専用のなりすまし）には run.invoker が無いので、なりすましを外して動かす。トークンは書かない。
# 端末を開いたままにする。約 45 分で 401 になるので、止めて起動し直す（docs/mcp-tool-design.md）。
set -euo pipefail

exec env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud run services proxy docunavi-mcp \
  --region asia-northeast1 --project docunavi-app --port 3001
