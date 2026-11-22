---
paths:
  - "ops/django-saas/**"
---

# Django の試作（ops/django-saas/）を触るとき

詳細は [ops/django-saas/CLAUDE.md](../../ops/django-saas/CLAUDE.md)。製品には入っていない（運用者向けの管理画面 `ops/access_admin/` は 2026-11-08 に廃止した）。

- 規則（`permissions.py` `member_admin.py`）は HTTP に依存させない。`views.py` と `web_views.py` は、規則を呼んで結果を返すだけにする
- 例外の文字列を応答に含めない。失敗は固定の文（`MemberError` など）で返す。入っていない組織は存在を知らせず 404
- 権限は、サーバー側で毎回確かめる。画面でボタンを隠すだけにしない
- モデルを変えない（変えるなら事前に承認。`migrations/` を勝手に作らない）。触れたら `SAAS_DB_USER=postgres` で `manage.py makemigrations --check` が空であることを確かめる
- `requirements.txt` を変えない。`bench/` からアプリを import しない
- 本番・Cloud Run・実 API・外部の認証サーバーに触れない。`SECRET_KEY` や `SAAS_DEMO_LOGIN` の既定を公開向けにしない
- 触れたら、一時 Postgres で `manage.py test`（88 件）と `manage.py check` を実行する（手順は ops/django-saas/CLAUDE.md の「テスト」）。製品の `pytest` には含まれない
