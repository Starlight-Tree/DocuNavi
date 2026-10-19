# 開発用の短縮コマンド。Postgres のテストに使う一時的な Postgres 17 の起動・停止と、その一括実行。
# 手順は docs/development.md の「テスト」節。初回は apt-get download でパッケージを取得する（ネットワークを使う）。
PYTEST ?= $(if $(wildcard .venv/bin/pytest),.venv/bin/pytest,pytest)

.PHONY: test-db-up test-db-down test-db

# 起動して、接続 URL を表示する。pytest には TEST_DATABASE_URL=<その URL> を付けて渡す
test-db-up:
	@scripts/test-postgres-up.sh

test-db-down:
	@scripts/test-postgres-down.sh

# 起動 → pytest 全体（Postgres のテストも動く）→ 停止。テストが失敗しても、必ず停止する
test-db:
	@url=$$(scripts/test-postgres-up.sh) || exit 1; \
	TEST_DATABASE_URL="$$url" $(PYTEST) -q $(PYTEST_ARGS); status=$$?; \
	scripts/test-postgres-down.sh; exit $$status
