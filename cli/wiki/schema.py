"""Canonical DDL for the wiki-brain database.

The core tables mirror BUILD_SPEC.md §3.1 verbatim in intent. The FTS5 virtual
tables use the standard external-content pattern with sync triggers. A small
number of *extension* tables (clearly separated below) support budget bookkeeping
for Phase 4; these are documented in SCHEMA.md.
"""

# --- Core schema (BUILD_SPEC.md §3.1) ---------------------------------------
CORE_DDL = """
CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  hash TEXT UNIQUE NOT NULL,
  path TEXT NOT NULL,
  title TEXT, url TEXT,
  origin TEXT NOT NULL,
  fetched_at TEXT, ingested_at TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  mime_type TEXT,             -- content type (drop folder / extractors); nullable
  category TEXT,              -- session-assigned label for routing (e.g. images)
  tags TEXT NOT NULL DEFAULT '[]'  -- JSON array of session-assigned tags
);

CREATE TABLE claims (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  location TEXT,
  confidence REAL NOT NULL,
  origin TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  superseded_by INTEGER REFERENCES claims(id),
  created_at TEXT NOT NULL, reviewed_at TEXT
);

CREATE TABLE summaries (
  id INTEGER PRIMARY KEY,
  source_id INTEGER UNIQUE NOT NULL REFERENCES sources(id),
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE entities (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE relations (
  id INTEGER PRIMARY KEY,
  src INTEGER NOT NULL REFERENCES entities(id),
  rel TEXT NOT NULL,
  dst INTEGER NOT NULL REFERENCES entities(id),
  claim_id INTEGER REFERENCES claims(id),
  UNIQUE(src, rel, dst, claim_id)
);

CREATE TABLE claim_entities (
  claim_id INTEGER NOT NULL REFERENCES claims(id),
  entity_id INTEGER NOT NULL REFERENCES entities(id),
  PRIMARY KEY (claim_id, entity_id)
);

CREATE TABLE contradictions (
  id INTEGER PRIMARY KEY,
  claim_a INTEGER NOT NULL REFERENCES claims(id),
  claim_b INTEGER NOT NULL REFERENCES claims(id),
  status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT,
  proposal TEXT
);

CREATE TABLE research_queue (
  id INTEGER PRIMARY KEY,
  question TEXT NOT NULL,
  priority REAL NOT NULL DEFAULT 0.5,
  origin TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL, attempts INTEGER DEFAULT 0
);

CREATE TABLE escalations (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE pages (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,
  entity_id INTEGER REFERENCES entities(id),
  dirty INTEGER NOT NULL DEFAULT 1,
  synthesis TEXT NOT NULL DEFAULT '',
  synthesis_input_hash TEXT
);

CREATE VIRTUAL TABLE claims_fts USING fts5(text, content=claims, content_rowid=id);
CREATE VIRTUAL TABLE summaries_fts USING fts5(text, content=summaries, content_rowid=id);

CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
  INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER claims_au AFTER UPDATE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER summaries_ai AFTER INSERT ON summaries BEGIN
  INSERT INTO summaries_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER summaries_ad AFTER DELETE ON summaries BEGIN
  INSERT INTO summaries_fts(summaries_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER summaries_au AFTER UPDATE ON summaries BEGIN
  INSERT INTO summaries_fts(summaries_fts, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO summaries_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

# --- Extension schema (not in §3.1; see SCHEMA.md) --------------------------
# gather_events records budgeted Phase-4 actions so the CLI can enforce the
# per-question / per-night budgets across separate process invocations.
EXT_DDL = """
CREATE TABLE gather_events (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,          -- YYYY-MM-DD local, the "night" bucket
  kind TEXT NOT NULL,         -- query | fetch
  qid INTEGER,                -- research_queue id this action served (nullable)
  created_at TEXT NOT NULL
);
CREATE INDEX gather_events_day ON gather_events(day, kind, qid);
"""

ALL_DDL = CORE_DDL + EXT_DDL

# User-version stamped on the DB. Keep in sync with migrate.latest_version()
# (the migration runner carries existing DBs forward to this version).
SCHEMA_VERSION = 2
