-- docunavi D1 schema (SQLite). Replaces supabase/migrations for the Cloudflare side.
-- Dropped on purpose: chunks.embedding (-> Vectorize), document_images.data (-> R2), saas.* (Django), checkpoint* (new tables below).
-- Timestamps are INTEGER unix milliseconds.

CREATE TABLE document_collections (
  name        TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 40 AND name NOT GLOB '*[^a-zA-Z0-9_-]*'),
  label       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  updated_at  INTEGER NOT NULL DEFAULT (unixepoch() * 1000)
);

CREATE TABLE document_targets (
  name        TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 40 AND name NOT GLOB '*[^a-z0-9_-]*'),
  label       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  updated_at  INTEGER NOT NULL DEFAULT (unixepoch() * 1000)
);

CREATE TABLE document_types (
  name        TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 40 AND name NOT GLOB '*[^a-z0-9_-]*'),
  label       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  updated_at  INTEGER NOT NULL DEFAULT (unixepoch() * 1000)
);

CREATE TABLE documents (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  source         TEXT NOT NULL UNIQUE,
  content_hash   TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('ready', 'failed', 'processing')),
  failure_reason TEXT,
  pages          INTEGER NOT NULL DEFAULT 0,
  title          TEXT,
  collection     TEXT CHECK (collection IS NULL OR (length(collection) BETWEEN 1 AND 40 AND collection NOT GLOB '*[^A-Za-z0-9_-]*')),
  target         TEXT REFERENCES document_targets (name) ON UPDATE RESTRICT ON DELETE RESTRICT,
  doc_type       TEXT REFERENCES document_types (name) ON UPDATE RESTRICT ON DELETE RESTRICT,
  suggested_questions TEXT,
  suggestions_hash    TEXT,
  suggestions_at      INTEGER,
  suggestions_failure TEXT,
  created_at     INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  updated_at     INTEGER NOT NULL DEFAULT (unixepoch() * 1000)
);
CREATE INDEX documents_collection_idx ON documents (collection);
CREATE INDEX documents_target_idx ON documents (target);

CREATE TABLE chunks (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT, -- also the Vectorize vector id (stringified)
  document_id          INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  chunk_index          INTEGER NOT NULL,
  page                 INTEGER NOT NULL,
  page_end             INTEGER,
  page_label           TEXT,
  page_end_label       TEXT,
  kind                 TEXT NOT NULL DEFAULT 'text',
  heading              TEXT,
  content              TEXT NOT NULL,
  content_hash         TEXT NOT NULL,
  embedding_model      TEXT,
  embedding_dimensions INTEGER,
  embedding_input_hash TEXT,
  created_at           INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  UNIQUE (document_id, chunk_index)
);

CREATE TABLE document_images (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id   INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  kind          TEXT NOT NULL DEFAULT 'hero' CHECK (kind = 'hero'),
  status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected')),
  mime_type     TEXT NOT NULL,
  r2_key        TEXT NOT NULL UNIQUE,
  byte_size     INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 5242880),
  width         INTEGER,
  height        INTEGER,
  document_hash TEXT,
  prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64 AND prompt_sha256 NOT GLOB '*[^0-9a-f]*'),
  model         TEXT NOT NULL,
  created_at    INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  approved_at   INTEGER
);
-- at most one approved hero per document
CREATE UNIQUE INDEX document_images_one_approved ON document_images (document_id, kind) WHERE status = 'approved';

CREATE TABLE roles (
  name  TEXT PRIMARY KEY,
  label TEXT NOT NULL
);

CREATE TABLE role_collection_access (
  role       TEXT NOT NULL REFERENCES roles (name) ON DELETE CASCADE,
  collection TEXT NOT NULL,
  PRIMARY KEY (role, collection)
);

-- user_id = Better Auth user.id
CREATE TABLE user_roles (
  user_id TEXT NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
  role    TEXT NOT NULL REFERENCES roles (name) ON DELETE RESTRICT,
  PRIMARY KEY (user_id, role)
);

CREATE TABLE access_audit (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  at         INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  actor      TEXT NOT NULL,
  table_name TEXT NOT NULL,
  op         TEXT NOT NULL,
  row_data   TEXT
);

-- LangGraph checkpointer (same shape as langgraph-checkpoint-sqlite; binary columns are base64 text)
CREATE TABLE checkpoints (
  thread_id            TEXT NOT NULL,
  checkpoint_ns        TEXT NOT NULL DEFAULT '',
  checkpoint_id        TEXT NOT NULL,
  parent_checkpoint_id TEXT,
  type                 TEXT,
  checkpoint           TEXT NOT NULL,
  metadata             TEXT NOT NULL DEFAULT '{}',
  created_at           INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE INDEX checkpoints_thread_idx ON checkpoints (thread_id, created_at);

CREATE TABLE checkpoint_writes (
  thread_id     TEXT NOT NULL,
  checkpoint_ns TEXT NOT NULL DEFAULT '',
  checkpoint_id TEXT NOT NULL,
  task_id       TEXT NOT NULL,
  idx           INTEGER NOT NULL,
  channel       TEXT NOT NULL,
  type          TEXT,
  value         TEXT,
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- Better Auth (core + jwt plugin). Column names are the camelCase names Better Auth uses.
CREATE TABLE "user" (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE,
  emailVerified INTEGER NOT NULL DEFAULT 0,
  image         TEXT,
  createdAt     INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  updatedAt     INTEGER NOT NULL DEFAULT (unixepoch() * 1000)
);

CREATE TABLE session (
  id        TEXT PRIMARY KEY,
  expiresAt INTEGER NOT NULL,
  token     TEXT NOT NULL UNIQUE,
  createdAt INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  updatedAt INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  ipAddress TEXT,
  userAgent TEXT,
  userId    TEXT NOT NULL REFERENCES "user" (id) ON DELETE CASCADE
);
CREATE INDEX session_userId_idx ON session (userId);

CREATE TABLE account (
  id                    TEXT PRIMARY KEY,
  accountId             TEXT NOT NULL,
  providerId            TEXT NOT NULL,
  userId                TEXT NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
  accessToken           TEXT,
  refreshToken          TEXT,
  idToken               TEXT,
  accessTokenExpiresAt  INTEGER,
  refreshTokenExpiresAt INTEGER,
  scope                 TEXT,
  password              TEXT,
  createdAt             INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  updatedAt             INTEGER NOT NULL DEFAULT (unixepoch() * 1000)
);
CREATE INDEX account_userId_idx ON account (userId);

CREATE TABLE verification (
  id         TEXT PRIMARY KEY,
  identifier TEXT NOT NULL,
  value      TEXT NOT NULL,
  expiresAt  INTEGER NOT NULL,
  createdAt  INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  updatedAt  INTEGER NOT NULL DEFAULT (unixepoch() * 1000)
);
CREATE INDEX verification_identifier_idx ON verification (identifier);

CREATE TABLE jwks (
  id         TEXT PRIMARY KEY,
  publicKey  TEXT NOT NULL,
  privateKey TEXT NOT NULL,
  alg        TEXT,
  crv        TEXT,
  createdAt  INTEGER NOT NULL DEFAULT (unixepoch() * 1000),
  expiresAt  INTEGER
);
