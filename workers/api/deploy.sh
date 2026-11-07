#!/usr/bin/env bash
# Host-side deploy for the data Worker (the dev container has no Cloudflare credentials).
# Usage: cd <repo>/workers/api && ./deploy.sh
# Secrets are read from .secrets/<NAME> (git-ignored). Missing Google values are asked for (hidden input).
set -euo pipefail
cd "$(dirname "$0")"
npm ci --no-audit --no-fund
npx wrangler whoami >/dev/null 2>&1 || npx wrangler login
npx wrangler d1 migrations apply docunavi --remote
npx wrangler deploy
mkdir -p .secrets && chmod 700 .secrets
for n in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET; do
  if [ ! -s ".secrets/$n" ]; then read -rsp "$n (hidden): " v; echo; printf '%s' "$v" > ".secrets/$n"; chmod 600 ".secrets/$n"; fi
done
for n in DATA_API_TOKEN BETTER_AUTH_SECRET GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET; do
  npx wrangler secret put "$n" < ".secrets/$n"
done
echo "deployed: docunavi-api"
