#!/bin/sh
# ローカルの db コンテナの初期化。pg-data が空のときの初回の起動で、一度だけ実行される（postgres イメージの仕組み）。
# アプリの DB（Supabase）と同じ形の「入れ物」だけを作る。テーブル・資料・埋め込みは作らない
# （テーブルは python -m app.ingestion --apply、埋め込みは python -m app.ingestion.embed --apply。手順は docs/development.md）。
#
#   extensions スキーマ + vector 型  検索の SQL が extensions.vector と OPERATOR(extensions.<=>) を使う（app/search/vector.py）
#   chatbot_app ロール + chatbot スキーマ  本番と同じ。ロールの search_path が chatbot なので、テーブルは chatbot にできる
#                                         （docs/configuration.md の「DB のロール」）
#
# 作るのは pgvector まで。supabase/migrations/20261018080137（pgroonga）は、アプリが読まないので、ローカルには入れない。
# パスワードは compose の環境変数 APP_DB_PASSWORD（このコンテナはポートを公開せず、internal のネットワークだけにある）。
set -eu

psql -v ON_ERROR_STOP=1 -v app_pw="$APP_DB_PASSWORD" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
create schema if not exists extensions;
create extension if not exists vector with schema extensions;
create role chatbot_app login password :'app_pw';
create schema chatbot authorization chatbot_app;
alter role chatbot_app set search_path = chatbot;
grant usage on schema extensions to chatbot_app;
SQL
