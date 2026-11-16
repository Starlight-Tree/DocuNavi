-- App membership: which signed-in users may use the app at all (MEMBERSHIP_MODE, app/access.py).
-- Deliberately separate from roles: a role decides which document groups a user can see; membership decides whether the
-- user may use the chat / document APIs at all. Granting or removing one never changes the other.
-- The table is created empty. Rows are added by an operator (python -m app.access_admin member add ...), never by a
-- migration: no user id belongs in a migration file.
CREATE TABLE app_members (
  user_id    text PRIMARY KEY REFERENCES "user" (id) ON DELETE CASCADE,
  note       text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);
