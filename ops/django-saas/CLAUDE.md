# ops/django-saas/（Django の学習用の試作）

全体の方針・禁止事項は、ルートの [CLAUDE.md](../../CLAUDE.md)。再現の手順と構成の詳細は [README.md](README.md)。測定の結果は `docs/target-architecture.md` の「Django の測定」。

**製品には入っていない。** 組織・メンバー・役割・プランを Django で作ると、どれだけの手間かを測る試作。本番・Cloud Run・実 API・外部の認証サーバーには触れない。

## 取り違えない

運用者向けのロール割り当て画面 `ops/access_admin/`（別物）は、2026-11-08 に廃止して削除した。
- 試作の `Role`（owner / admin / member / viewer）と、`app/access.py` のロール（資料グループの束）は別物。

## 構成（アプリは `saas` ひとつ）

| 層 | ファイル | 責務 |
|---|---|---|
| モデル | `saas/models.py` | `Plan` `Organization` `Membership` `Subscription` `Usage`。利用者の ID は JWT の `sub`（`external_user_id`）。Django のユーザー表は使わない |
| 規則 | `saas/permissions.py` `saas/member_admin.py` | 役割の強さ（`rank` `allows`）と、メンバー管理の規則。**HTTP に依存しない**。失敗は `MemberError`（ステータスと固定の文） |
| 表示用 | `saas/presenters.py` | 使用量の状態と契約状態の文言。DB を書かない |
| API | `saas/views.py` `saas/urls.py`（`/api/`） | Bearer の JWT で認証。JSON。例外の文字列は返さない |
| 画面 | `saas/web_views.py` `saas/web_urls.py` `saas/templates/saas/` | HTML。規則は `member_admin` を呼ぶだけで、持たない |
| 認証 | `saas/jwt_auth.py`（API）、`saas/session_auth.py`（画面） | 画面の「いま誰か」は `current_sub()` だけ。実認証にするときはこの関数を差し替える |
| 管理画面 | `saas/admin.py` | Django Admin（運用者向け） |
| 投入 | `saas/management/commands/seed_demo.py` | 架空データ（繰り返し可） |
| 設定 | `config/settings.py` | `saas` スキーマだけを使う（`search_path=saas`）。秘密を持たない |
| 測定 | `bench/` | 境界の検査と遅延の測定。アプリのコードから import しない |
| テスト | `saas/tests/` | `helpers.py` が共通の部品。API は `test_api.py`、画面は `test_web.py`、規則は `test_members.py` |

## 守ること

- **動作を変える変更の前に確認する。** 試作でも、権限の規則（`member_admin.py` の表）と「別の組織は 404」「API の追加は owner だけ」は仕様。
- **migration は作らない・流さない。** モデルを変えると `makemigrations` が要る。変えるときは事前に承認を得る。変更しても `manage.py makemigrations --check` が空であることを確かめる。
- **本番に出さない。** `SECRET_KEY` は固定の開発値、`SAAS_DEMO_LOGIN` は認証なしのログイン。公開環境では使えない。
- **依存は変えない。** `requirements.txt` は製品の依存とは別。
- 権限は、画面でボタンを隠すだけにせず、サーバー側で毎回確かめる。

## テスト

製品の `pytest` には含まれない（`.venv-django` で別に動かす）。一時 Postgres 17 が要る。

```bash
scripts/test-postgres-up.sh && .venv-django/bin/python ops/django-saas/bench/bootstrap_db.py   # 初回だけ
cd ops/django-saas && SAAS_DB_USER=postgres ../../.venv-django/bin/python manage.py test       # 88 件
../../.venv-django/bin/python manage.py check
SAAS_DB_USER=postgres ../../.venv-django/bin/python manage.py makemigrations --check   # saas_app では django_migrations の権限が無く失敗する
cd ../.. && scripts/test-postgres-down.sh
```

他のセッションが 55432 番を使っているときは、起動が失敗する（止めない）。2026-11-01 に上の手順で、88 件・`check`・`makemigrations --check`（No changes detected）が通ることを確かめた。
