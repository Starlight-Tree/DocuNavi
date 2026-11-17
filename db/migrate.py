"""Minimal SQL migration runner for Postgres (Prisma Postgres by default; Cloud SQL and AlloyDB as explicit targets).

  python db/migrate.py            # plan: list applied / pending (read-only session, writes nothing)
  python db/migrate.py --apply    # apply pending files, one transaction per file

Targets (MIGRATE_TARGET):
  prisma (default)  reads DIRECT_URL. Only the direct host db.prisma.io:5432 is accepted (DDL must not go through the
                    PgBouncer pooled host). Behaviour and query count are unchanged from before: no extra checks, because
                    every query on Prisma Postgres counts against the plan's operation quota.
  cloudsql          reads MIGRATE_DATABASE_URL (never DIRECT_URL). Needs an explicit allowlist, all required:
                      MIGRATE_ALLOWED_HOST / MIGRATE_ALLOWED_DB / MIGRATE_ALLOWED_USER
                    and a TLS URL: sslmode=verify-ca (or verify-full) + sslrootcert=<existing file>. Rejected before connecting:
                    other hosts, libpq overrides in the URL (host=, hostaddr=, ...), PG* environment overrides, *.prisma.io.
                    Checked after connecting: the real host/db/user, TLS in use (server side), the user is not a superuser or a
                    member of cloudsqlsuperuser (so create the migration user with SQL, not `gcloud sql users create`), and the
                    `vector` extension already exists (create it once as postgres; this user cannot).
                    Optional MIGRATE_APP_ROLE: after --apply, grant that role DML on the tables (and default privileges).
  alloydb           same as cloudsql (same variables, same checks). The superuser role it refuses is alloydbsuperuser; the
                    server CA comes from the AlloyDB API (generateClientCertificate -> caCert), and TLS is verify-ca (the
                    host is a private PSC IP, so the host name is not verified).

Secrets: the URL and its password are never printed. Failures print a fixed message, the exception class and the SQLSTATE only.
Files: db/migrations/NNNN_name.sql, applied in name order.
History table schema_migrations(version, checksum, applied_at): an applied file whose checksum changed aborts the run
(edit a new file instead). A session advisory lock prevents two runners at once. Needs psycopg 3.
"""

import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg import sql

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 7260801  # arbitrary constant for pg_advisory_lock
HISTORY_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
)

# Both managed-Postgres targets (cloudsql, alloydb) share the same checks.
SQL_TARGETS = ("cloudsql", "alloydb")
# cloudsql/alloydb target: the only URL query parameters accepted. Anything else (host, hostaddr, service, options, ...) could
# redirect the connection past the allowlist.
CLOUDSQL_ALLOWED_QUERY = {"sslmode", "sslrootcert"}
CLOUDSQL_TLS_MODES = {"verify-ca", "verify-full"}
# libpq reads these when the URL leaves a field out; the cloudsql target requires everything explicit.
FORBIDDEN_PG_ENV = (
    "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGSERVICE", "PGSERVICEFILE", "PGSSLMODE",
    "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY", "PGOPTIONS", "PGPASSFILE",
)  # fmt: skip


def fail(message: str) -> None:
    """Exit with a fixed message. Never pass a URL, password or exception text here."""
    sys.exit(message)


def validate_cloudsql_url(url: str, env: dict[str, str], target: str = "cloudsql") -> None:
    """Checks that need no connection. Calls fail() with a fixed message (no URL content) on any violation."""
    allowed = {k: env.get(k, "") for k in ("MIGRATE_ALLOWED_HOST", "MIGRATE_ALLOWED_DB", "MIGRATE_ALLOWED_USER")}
    missing = [k for k, v in allowed.items() if not v]
    if missing:
        fail(f"{target} target: set " + ", ".join(missing) + " (explicit allowlist)")
    leaked = [k for k in FORBIDDEN_PG_ENV if env.get(k)]
    if leaked:
        fail(f"{target} target: unset these PG* environment variables: " + ", ".join(leaked))
    try:
        u = urlsplit(url)
        port = u.port
        query = parse_qs(u.query, keep_blank_values=True)
    except ValueError:
        fail(f"{target} target: MIGRATE_DATABASE_URL is not a valid URL")
    if u.scheme not in ("postgresql", "postgres"):
        fail(f"{target} target: MIGRATE_DATABASE_URL must be a postgresql:// URL")
    host = u.hostname or ""
    if "," in u.netloc.rpartition("@")[2] or not host:
        fail(f"{target} target: exactly one explicit host is required")
    if host.endswith("prisma.io") or allowed["MIGRATE_ALLOWED_HOST"].endswith("prisma.io"):
        fail(f"{target} target: a prisma.io host is never accepted here (use MIGRATE_TARGET=prisma)")
    if host != allowed["MIGRATE_ALLOWED_HOST"]:
        fail(f"{target} target: host is not MIGRATE_ALLOWED_HOST")
    if port != 5432:
        fail(f"{target} target: port must be explicit 5432")
    if urlsplit(url).path.lstrip("/") != allowed["MIGRATE_ALLOWED_DB"]:
        fail(f"{target} target: database is not MIGRATE_ALLOWED_DB")
    if u.username != allowed["MIGRATE_ALLOWED_USER"]:
        fail(f"{target} target: user is not MIGRATE_ALLOWED_USER")
    if not u.password:
        fail(f"{target} target: the URL must carry a password (it is never printed)")
    extra = sorted(set(query) - CLOUDSQL_ALLOWED_QUERY)
    if extra:
        fail(f"{target} target: URL query parameters not allowed: " + ", ".join(extra))
    mode = (query.get("sslmode") or [""])[0]
    if mode not in CLOUDSQL_TLS_MODES:
        fail(f"{target} target: sslmode must be verify-ca or verify-full")
    root = (query.get("sslrootcert") or [""])[0]
    if not root or not Path(root).is_file():
        fail(f"{target} target: sslrootcert must point at an existing CA file")


def check_cloudsql_session(conn: psycopg.Connection, env: dict[str, str], target: str = "cloudsql") -> None:
    """Checks on the live session (read-only queries). Calls fail() on any violation."""
    info = conn.info
    if (info.host, info.port, info.dbname, info.user) != (
        env["MIGRATE_ALLOWED_HOST"], 5432, env["MIGRATE_ALLOWED_DB"], env["MIGRATE_ALLOWED_USER"]
    ):  # fmt: skip
        fail(f"{target} target: the connected host/port/db/user differ from the allowlist")
    if not info.ssl_in_use:
        fail(f"{target} target: TLS is not in use")
    row = conn.execute(
        "select current_database(), current_user, session_user,"
        " coalesce((select ssl from pg_stat_ssl where pid = pg_backend_pid()), false),"
        " (select r.rolsuper or r.rolcreaterole or r.rolcreatedb or r.rolreplication or r.rolbypassrls"
        "    from pg_roles r where r.rolname = current_user),"
        " coalesce((select bool_or(pg_has_role(current_user, r.oid, 'member')) from pg_roles r"
        "    where r.rolname in ('cloudsqlsuperuser', 'alloydbsuperuser')), false),"
        " exists (select 1 from pg_extension where extname = 'vector')"
    ).fetchone()
    db, cur, sess, ssl, privileged, superuser_member, has_vector = row
    if db != env["MIGRATE_ALLOWED_DB"] or cur != env["MIGRATE_ALLOWED_USER"] or sess != env["MIGRATE_ALLOWED_USER"]:
        fail(f"{target} target: current_database/current_user/session_user differ from the allowlist")
    if not ssl:
        fail(f"{target} target: the server reports this session is not encrypted")
    if privileged or superuser_member:
        fail(
            f"{target} target: the migration user must not be superuser/createrole/createdb/replication/bypassrls "
            "or a member of cloudsqlsuperuser/alloydbsuperuser (create it without superuser rights; on Cloud SQL use SQL CREATE ROLE "
            "or --database-roles, not a plain gcloud sql users create)"
        )
    if not has_vector:
        fail(f"{target} target: extension vector is missing (create it once as postgres; this user cannot)")


def connect(read_only: bool) -> psycopg.Connection:
    target = os.environ.get("MIGRATE_TARGET", "prisma")
    env = dict(os.environ)
    if target == "prisma":
        url = env.get("DIRECT_URL")
        if not url:
            fail("DIRECT_URL is not set (run via: infisical run ... -- python db/migrate.py)")
        u = urlsplit(url)
        if u.hostname != "db.prisma.io" or u.port != 5432:
            fail("DIRECT_URL must point at the direct host db.prisma.io:5432 (not the pooled host)")
    elif target in SQL_TARGETS:
        url = env.get("MIGRATE_DATABASE_URL")
        if not url:
            fail(f"{target} target: MIGRATE_DATABASE_URL is not set")
        validate_cloudsql_url(url, env, target)
    else:
        fail("MIGRATE_TARGET must be prisma (default), cloudsql or alloydb")
    try:
        conn = psycopg.connect(url, connect_timeout=15, autocommit=True)
    except psycopg.Error as e:  # libpq text can echo connection details; print only the class and SQLSTATE
        fail(f"connection failed ({type(e).__name__}, sqlstate={e.sqlstate})")
    if target in SQL_TARGETS:
        check_cloudsql_session(conn, env, target)
    if read_only:  # server-side guard for plan mode (conn.read_only is not applied in autocommit mode)
        conn.execute("set default_transaction_read_only = on")
    return conn


def grant_app_role(conn: psycopg.Connection, role: str) -> None:
    """DML for the app role on what the migration user created, plus default privileges for later tables.
    Idempotent. Runs as the migration user (the owner), so it needs no extra membership."""
    r = sql.Identifier(role)
    for stmt in (
        "GRANT USAGE ON SCHEMA public TO {r}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {r}",
    ):
        conn.execute(sql.SQL(stmt).format(r=r))
    # the history table is the migration user's own bookkeeping
    conn.execute(sql.SQL("REVOKE ALL ON TABLE schema_migrations FROM {r}").format(r=r))


def files() -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        text = p.read_text(encoding="utf-8")
        out.append((p.stem, hashlib.sha256(text.encode()).hexdigest(), text))
    return out


def history(conn: psycopg.Connection) -> dict[str, str] | None:
    """None when schema_migrations does not exist yet."""
    if conn.execute("select to_regclass('public.schema_migrations')").fetchone()[0] is None:
        return None
    return dict(conn.execute("select version, checksum from schema_migrations").fetchall())


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    conn = connect(read_only=not apply)
    conn.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
    done = history(conn)
    state = done or {}
    for version, checksum, _ in files():
        if version in state and state[version] != checksum:
            sys.exit(f"ABORT: {version} was applied with a different checksum (do not edit applied files)")
    pending = [f for f in files() if f[0] not in state]
    print("applied:", ", ".join(state) or "(none)")
    print("pending:", ", ".join(f[0] for f in pending) or "(none)")
    if done is None:
        n = conn.execute(
            "select count(*) from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE'"
        ).fetchone()[0]
        if n:
            sys.exit(
                f"ABORT: no history table but public already has {n} tables (not an empty DB; decide how to adopt it first)"
            )
    if not apply:
        print("plan only (nothing written). Re-run with --apply to apply.")
        return
    conn.execute(HISTORY_DDL)
    conn.autocommit = False
    for version, checksum, text in pending:
        with conn.transaction():
            conn.execute(text)
            conn.execute("insert into schema_migrations (version, checksum) values (%s, %s)", (version, checksum))
        print("applied", version)
    conn.autocommit = True
    app_role = os.environ.get("MIGRATE_APP_ROLE", "") if os.environ.get("MIGRATE_TARGET") in SQL_TARGETS else ""
    if app_role:
        with conn.transaction():
            grant_app_role(conn, app_role)
        print("granted DML to the app role")
    print("DONE")


if __name__ == "__main__":
    main()
