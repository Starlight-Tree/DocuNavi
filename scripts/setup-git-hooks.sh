#!/bin/sh
# このリポジトリの Git フック（.githooks/）を有効にする。clone し直したら 1 回実行する。
# core.hooksPath を設定すると .git/hooks/ は見られなくなる（pre-commit も .githooks/ に移してある）。
set -eu
cd "$(git rev-parse --show-toplevel)"
chmod +x .githooks/pre-commit .githooks/commit-msg
git config core.hooksPath .githooks
echo "core.hooksPath = $(git config --get core.hooksPath)"
