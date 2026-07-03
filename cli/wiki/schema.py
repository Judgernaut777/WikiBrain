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

-- Hot-path indexes: status filtering, per-source claim lookup, entity/relation
-- graph traversal.
CREATE INDEX claims_status ON claims(status);
CREATE INDEX claims_source_id ON claims(source_id);
CREATE INDEX claim_entities_entity_id ON claim_entities(entity_id);
CREATE INDEX relations_dst ON relations(dst);

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
#
# The skills tables (Phase 6) make Claude skills a *third* projection out of the
# DB (after wiki pages): a skill's body is authored from PROMOTED claims only and
# projected to .claude/skills/<name>/SKILL.md by `wiki skill render`. They mirror
# the pages model — `body` is free prose like pages.synthesis, `input_hash` is the
# drift basis like pages.synthesis_input_hash. See SCHEMA.md and BUILD_SPEC.md §12.
EXT_DDL = """
CREATE TABLE gather_events (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,          -- YYYY-MM-DD local, the "night" bucket
  kind TEXT NOT NULL,         -- query | fetch
  qid INTEGER,                -- research_queue id this action served (nullable)
  created_at TEXT NOT NULL
);
CREATE INDEX gather_events_day ON gather_events(day, kind, qid);

-- Optional local-embedding index for semantic search (the [semantic] extra).
-- vec is packed float32 (little-endian), length dim*4. Affects ranking only,
-- never the byte-deterministic render layer.
CREATE TABLE embeddings (
  claim_id INTEGER PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vec BLOB NOT NULL,
  created_at TEXT NOT NULL
);

-- Phase 6: Claude skills authored from promoted claims (see BUILD_SPEC §8).
-- `body` is the only free-prose field (the SKILL.md content, like pages.synthesis).
-- status: draft -> approved -> archived. Only `approved` skills render to disk;
-- `draft` skills live in the DB but never touch .claude/skills (the gate, mirroring
-- "unattended work produces only pending items, never edited pages").
CREATE TABLE skills (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,           -- kebab-case slug = directory name
  description TEXT NOT NULL DEFAULT '',-- one-line skill activation description
  body TEXT NOT NULL DEFAULT '',       -- SKILL.md body prose (authored in-session)
  allowed_tools TEXT,                  -- optional JSON array; NULL = inherit all
  status TEXT NOT NULL DEFAULT 'draft',-- draft | approved | archived
  input_hash TEXT,                     -- sha256 of promoted source claims at approval
  installed INTEGER NOT NULL DEFAULT 0,-- 1 = copied to ~/.claude/skills (opt-in)
  version INTEGER NOT NULL DEFAULT 0,  -- current approved version (0 = never approved)
  created_at TEXT NOT NULL,
  reviewed_at TEXT
);

-- Provenance + drift basis: the PROMOTED claims a skill was derived from.
CREATE TABLE skill_claims (
  skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  PRIMARY KEY (skill_id, claim_id)
);

-- Append-only version history (Phase 6.1): one row per approve/revert, so a bad
-- change can always be rolled back. `claim_ids` is the JSON snapshot of the linked
-- claim set at that version. The DB body is the truth; git versions the rendered
-- files as a secondary backstop.
CREATE TABLE skill_versions (
  id INTEGER PRIMARY KEY,
  skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,            -- 1-based, per skill
  description TEXT NOT NULL,
  body TEXT NOT NULL,
  allowed_tools TEXT,
  input_hash TEXT,
  claim_ids TEXT NOT NULL DEFAULT '[]',
  note TEXT,                           -- 'approved' | 'reverted to vN' | ...
  created_at TEXT NOT NULL,
  UNIQUE(skill_id, version)
);
"""

ALL_DDL = CORE_DDL + EXT_DDL

# User-version stamped on the DB. Keep in sync with migrate.latest_version()
# (the migration runner carries existing DBs forward to this version).
SCHEMA_VERSION = 6
