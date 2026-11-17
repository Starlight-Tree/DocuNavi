#!/usr/bin/env bash
# Host-side deploy for the data Worker (the dev container has no Cloudflare credentials).
# Usage: cd <repo>/workers/api && ./deploy.sh
# Secrets are read from .secrets/<NAME> (git-ignored). Missing Google values are asked for (hidden input).
set -euo pipefail
cd "$(dirname "$0")"
npm ci --no-audit --no-fund --ignore-scripts
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then node_modules/.bin/wrangler whoami >/dev/null 2>&1 || node_modules/.bin/wrangler login; fi
node_modules/.bin/wrangler deploy
mkdir -p .secrets && chmod 700 .secrets
for n in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET; do
  if [ ! -s ".secrets/$n" ]; then read -rsp "$n (hidden): " v; echo; printf '%s' "$v" > ".secrets/$n"; chmod 600 ".secrets/$n"; fi
done
for n in BETTER_AUTH_SECRET GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET; do
  node_modules/.bin/wrangler secret put "$n" < ".secrets/$n"
done
echo "deployed: docunavi-api"
