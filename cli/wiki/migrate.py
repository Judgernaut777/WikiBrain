"""Forward-only schema migrations (pure code, ZERO model calls).

`schema.py` holds `CORE_DDL` (the *fresh-install* shape — always the latest) and
`SCHEMA_VERSION`. This module carries an **existing** database forward when the
version bumps. Each `MIGRATIONS[v]` is the list of statements that brings a DB up
to `user_version == v`; a statement runs only when its target > the DB's current
`user_version`, so:

- a fresh install (created by `CORE_DDL`, stamped at `SCHEMA_VERSION`) → no-op,
- the live DB (older `user_version`) → applies exactly the missing steps once,
- re-running is idempotent.

Keep `schema.SCHEMA_VERSION == latest_version()` (asserted in tests).
"""
from __future__ import annotations

import sqlite3

# target user_version -> DDL statements that bring the DB UP TO that version.
# ALTER TABLE ... ADD COLUMN is metadata-only and safe on a populated table as
# long as new columns are nullable or carry a DEFAULT.
MIGRATIONS: dict[int, list[str]] = {
    2: [  # source typing + labels (drop folder, image vision)
        "ALTER TABLE sources ADD COLUMN mime_type TEXT",
        "ALTER TABLE sources ADD COLUMN category TEXT",
        "ALTER TABLE sources ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
    ],
    3: [  # local-embedding index for semantic search ([semantic] extra)
        "CREATE TABLE embeddings ("
        " claim_id INTEGER PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,"
        " model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL,"
        " created_at TEXT NOT NULL)",
    ],
    4: [  # Phase 6: skills authored from promoted claims (see BUILD_SPEC §8)
        "CREATE TABLE skills ("
        " id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,"
        " description TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',"
        " allowed_tools TEXT, status TEXT NOT NULL DEFAULT 'draft',"
        " input_hash TEXT, installed INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL, reviewed_at TEXT)",
        "CREATE TABLE skill_claims ("
        " skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,"
        " claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,"
        " PRIMARY KEY (skill_id, claim_id))",
    ],
    5: [  # Phase 6.1: skill version history + rollback
        "ALTER TABLE skills ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
        "CREATE TABLE skill_versions ("
        " id INTEGER PRIMARY KEY,"
        " skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,"
        " version INTEGER NOT NULL, description TEXT NOT NULL, body TEXT NOT NULL,"
        " allowed_tools TEXT, input_hash TEXT, claim_ids TEXT NOT NULL DEFAULT '[]',"
        " note TEXT, created_at TEXT NOT NULL, UNIQUE(skill_id, version))",
    ],
}


def latest_version() -> int:
    """The highest version this code knows how to produce."""
    return max(MIGRATIONS) if MIGRATIONS else 1


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in ascending order. Returns the new user_version.

    Cheap and safe to call on every open: when the DB is already current it does
    a single `PRAGMA user_version` read and returns.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = False
    for target in sorted(MIGRATIONS):
        if target > current:
            for stmt in MIGRATIONS[target]:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={target}")
            current = target
            applied = True
    if applied:
        conn.commit()
    return current
