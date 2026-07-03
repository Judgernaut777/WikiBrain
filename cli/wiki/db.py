"""DB connection, init, dump, and the Repo context object.

The Repo bundles config + an open sqlite connection and provides the
mutation-finalize step (refresh db/dump.sql + append to log.md) that every
mutating command must run (BUILD_SPEC.md §2, §3.2).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Config
from .schema import ALL_DDL, SCHEMA_VERSION
from .migrate import migrate
from . import util


class Repo:
    def __init__(self, config: Config, conn: sqlite3.Connection):
        self.cfg = config
        self.conn = conn

    # --- lifecycle -----------------------------------------------------------
    @classmethod
    def open(cls, start: Path | None = None, *, must_exist: bool = True) -> "Repo":
        cfg = Config.load(start)
        db_path = cfg.db_path
        if must_exist and not db_path.exists():
            raise SystemExit(
                f"error: no database at {db_path}. Run `wiki init` first."
            )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        # The librarian runs as a separate process against the same WAL DB;
        # wait for a writer instead of failing fast with "database is locked".
        conn.execute("PRAGMA busy_timeout=10000;")
        # Carry an existing DB forward if SCHEMA_VERSION has bumped since it was
        # created. No-op (a single PRAGMA read) once the DB is current.
        migrate(conn)
        return cls(cfg, conn)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # --- paths ---------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.cfg.root

    def rel(self, p: Path) -> str:
        """Repo-relative POSIX path string for storage in the DB."""
        return p.resolve().relative_to(self.root).as_posix()

    # --- query convenience ---------------------------------------------------
    def q(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchone()

    def ex(self, sql: str, params=()):
        return self.conn.execute(sql, params)

    # --- mutation finalize ---------------------------------------------------
    def finalize(self, op: str, summary: str):
        """Commit, refresh dump.sql, append to log.md. Call after a mutation."""
        self.conn.commit()
        self.dump()
        self.log(op, summary)

    def dump(self):
        out = self.root / "db" / "dump.sql"
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for line in self.conn.iterdump():
            # embeddings are regenerable (`wiki embed --all`) and, packed as hex
            # float32 BLOBs, would otherwise bloat this git-committed file once
            # the [semantic] extra is in use. Keep the CREATE TABLE (schema stays
            # round-trippable) but drop the row data.
            if line.startswith('INSERT INTO "embeddings"'):
                continue
            lines.append(line)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def log(self, op: str, summary: str):
        logp = self.root / "log.md"
        header = f"## [{util.now_local_compact()}] {op} | {summary}\n"
        with open(logp, "a", encoding="utf-8") as fh:
            fh.write(header)


def init_db(start: Path | None = None) -> Repo:
    """Create the DB (apply DDL) and return an open Repo."""
    cfg = Config.load(start)
    db_path = cfg.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(ALL_DDL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION};")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.commit()
    return Repo(cfg, conn)
