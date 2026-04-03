-- init-memory.sql
-- Long-term memory schema for Copilot CLI / VS Code agents.
-- Run via: python -c "import sqlite3; conn=sqlite3.connect('memory.db'); conn.executescript(open('init-memory.sql').read())"
-- All IF NOT EXISTS guards make this idempotent (safe to re-run).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── schema_version ────────────────────────────────────────────────────
-- Bump version integer on every schema change; never delete rows.
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);

-- ── topics ────────────────────────────────────────────────────────────
-- Named subjects of ongoing interest. Equivalent to a session in session-store.
CREATE TABLE IF NOT EXISTS topics (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  slug             TEXT NOT NULL UNIQUE,   -- kebab-case: "gcc-product-gaps"
  title            TEXT NOT NULL,
  category         TEXT NOT NULL
                   CHECK(category IN ('research','customer','tool','project','career',
                                      'compliance','technical','personal','analytics')),
  description      TEXT,                   -- FTS-indexed; embed searchable keywords here
  status           TEXT NOT NULL DEFAULT 'active'
                   CHECK(status IN ('active','archived','resolved')),
  last_accessed_at TEXT,                   -- updated each time this topic is loaded; drives Tier-1 query
  created_at       TEXT DEFAULT (datetime('now')),
  updated_at       TEXT DEFAULT (datetime('now'))
);
CREATE TRIGGER IF NOT EXISTS topics_updated AFTER UPDATE ON topics
BEGIN
  UPDATE topics SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- ── facts ─────────────────────────────────────────────────────────────
-- Atomic curated knowledge units. The core memory cell.
CREATE TABLE IF NOT EXISTS facts (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  content          TEXT NOT NULL,
  fact_type        TEXT NOT NULL
                   CHECK(fact_type IN ('insight','decision','finding','action',
                                       'question','constraint','todo','preference')),
  topic_id         INTEGER REFERENCES topics(id) ON DELETE SET NULL,
  -- NULL topic_id = global/cross-topic fact; SET NULL preserves facts when topic deleted
  confidence       INTEGER DEFAULT 3  CHECK(confidence BETWEEN 1 AND 5),   -- 1=rumor, 5=confirmed
  importance       INTEGER NOT NULL DEFAULT 3  CHECK(importance BETWEEN 1 AND 5), -- 5=load-always
  status           TEXT NOT NULL DEFAULT 'active'
                   CHECK(status IN ('active','superseded','disproven','resolved')),
  source           TEXT,                   -- "email Oct 2025", "WorkIQ", "web search"
  session_id       TEXT,                   -- soft ref to session-store (different DB, not FK)
  last_accessed_at TEXT,                   -- updated when fact returned in query; drives Tier-2
  created_at       TEXT DEFAULT (datetime('now')),
  updated_at       TEXT DEFAULT (datetime('now')),
  UNIQUE(topic_id, content)                -- dedup: same fact won't be inserted twice per topic
);
CREATE TRIGGER IF NOT EXISTS facts_updated AFTER UPDATE ON facts
BEGIN
  UPDATE facts SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- ── entities ──────────────────────────────────────────────────────────
-- Global registry: people, products, orgs, tools, accounts — stored once, linked across topics.
CREATE TABLE IF NOT EXISTS entities (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  entity_type TEXT NOT NULL
              CHECK(entity_type IN ('person','product','org','tool','account','concept','file')),
  is_self     INTEGER NOT NULL DEFAULT 0  CHECK(is_self IN (0,1)),
  -- is_self=1 marks the user's own entity; query WHERE is_self=1 to load identity at session start
  attributes  TEXT,   -- JSON blob; use json_extract(attributes,'$.field') in queries
  notes       TEXT,   -- free-form; FTS-indexed; embed searchable keywords here
  created_at  TEXT DEFAULT (datetime('now')),
  updated_at  TEXT DEFAULT (datetime('now')),
  UNIQUE(name, entity_type)
);
CREATE TRIGGER IF NOT EXISTS entities_updated AFTER UPDATE ON entities
BEGIN
  UPDATE entities SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- ── entity_mentions ────────────────────────────────────────────────────
-- Polymorphic junction: links entities to facts, topics, or snapshots.
-- FK on entity_id is enforced; parent_id integrity is application-enforced (SQLite limitation).
CREATE TABLE IF NOT EXISTS entity_mentions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  parent_id    INTEGER NOT NULL,
  parent_type  TEXT NOT NULL  CHECK(parent_type IN ('fact','topic','snapshot')),
  relationship TEXT NOT NULL DEFAULT 'mentions'
               CHECK(relationship IN ('about','mentions','involves','blocks','references')),
  created_at   TEXT DEFAULT (datetime('now')),
  UNIQUE(entity_id, parent_id, parent_type, relationship)
);

-- ── snapshots ─────────────────────────────────────────────────────────
-- Immutable point-in-time topic summaries. Never UPDATE; always INSERT new seq_number.
CREATE TABLE IF NOT EXISTS snapshots (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id       INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  seq_number     INTEGER NOT NULL DEFAULT 1,
  -- Latest snapshot = MAX(seq_number) per topic_id; deterministic even on same-second inserts
  title          TEXT NOT NULL,
  summary        TEXT,
  findings       TEXT,
  decisions      TEXT,
  open_questions TEXT,
  next_steps     TEXT,
  source_session TEXT,   -- soft ref to session-store session_id
  created_at     TEXT DEFAULT (datetime('now'))
  -- No updated_at: snapshots are immutable by design
);

-- ── refs ──────────────────────────────────────────────────────────────
-- Soft links to external artifacts (emails, files, URLs, sessions, meetings).
CREATE TABLE IF NOT EXISTS refs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id   INTEGER NOT NULL,
  parent_type TEXT NOT NULL
              CHECK(parent_type IN ('fact','topic','snapshot','entity')),
  ref_type    TEXT NOT NULL
              CHECK(ref_type IN ('email','file','url','session','meeting',
                                 'document','csv','pr','issue','teams-chat')),
  ref_value   TEXT NOT NULL,
  label       TEXT,
  created_at  TEXT DEFAULT (datetime('now')),
  UNIQUE(parent_id, parent_type, ref_type, ref_value)
);

-- ── memory_fts ────────────────────────────────────────────────────────
-- Unified FTS5 table. content is the searchable text; row_id+table_name locate the source row.
-- CRITICAL: row_id is only unique within a table_name bucket — never join on row_id alone.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  content,
  row_id     UNINDEXED,
  table_name UNINDEXED
);

-- FTS sync triggers — facts (3)
CREATE TRIGGER IF NOT EXISTS fts_facts_insert AFTER INSERT ON facts
BEGIN
  INSERT INTO memory_fts(content, row_id, table_name) VALUES (NEW.content, NEW.id, 'facts');
END;
CREATE TRIGGER IF NOT EXISTS fts_facts_update AFTER UPDATE ON facts
BEGIN
  DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'facts';
  INSERT INTO memory_fts(content, row_id, table_name) VALUES (NEW.content, NEW.id, 'facts');
END;
CREATE TRIGGER IF NOT EXISTS fts_facts_delete AFTER DELETE ON facts
BEGIN
  DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'facts';
END;

-- FTS sync triggers — entities (3)
CREATE TRIGGER IF NOT EXISTS fts_entities_insert AFTER INSERT ON entities
BEGIN
  INSERT INTO memory_fts(content, row_id, table_name)
  VALUES (NEW.name || ' ' || COALESCE(NEW.notes, ''), NEW.id, 'entities');
END;
CREATE TRIGGER IF NOT EXISTS fts_entities_update AFTER UPDATE ON entities
BEGIN
  DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'entities';
  INSERT INTO memory_fts(content, row_id, table_name)
  VALUES (NEW.name || ' ' || COALESCE(NEW.notes, ''), NEW.id, 'entities');
END;
CREATE TRIGGER IF NOT EXISTS fts_entities_delete AFTER DELETE ON entities
BEGIN
  DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'entities';
END;

-- FTS sync triggers — topics (3)
CREATE TRIGGER IF NOT EXISTS fts_topics_insert AFTER INSERT ON topics
BEGIN
  INSERT INTO memory_fts(content, row_id, table_name)
  VALUES (NEW.title || ' ' || COALESCE(NEW.description, ''), NEW.id, 'topics');
END;
CREATE TRIGGER IF NOT EXISTS fts_topics_update AFTER UPDATE ON topics
BEGIN
  DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'topics';
  INSERT INTO memory_fts(content, row_id, table_name)
  VALUES (NEW.title || ' ' || COALESCE(NEW.description, ''), NEW.id, 'topics');
END;
CREATE TRIGGER IF NOT EXISTS fts_topics_delete AFTER DELETE ON topics
BEGIN
  DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'topics';
END;

-- FTS sync triggers — snapshots (3)
CREATE TRIGGER IF NOT EXISTS fts_snapshots_insert AFTER INSERT ON snapshots
BEGIN
  INSERT INTO memory_fts(content, row_id, table_name)
  VALUES (
    NEW.title || ' ' || COALESCE(NEW.summary,'') || ' ' ||
    COALESCE(NEW.findings,'') || ' ' || COALESCE(NEW.open_questions,''),
    NEW.id, 'snapshots'
  );
END;
CREATE TRIGGER IF NOT EXISTS fts_snapshots_update AFTER UPDATE ON snapshots
BEGIN
  DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'snapshots';
  INSERT INTO memory_fts(content, row_id, table_name)
  VALUES (
    NEW.title || ' ' || COALESCE(NEW.summary,'') || ' ' ||
    COALESCE(NEW.findings,'') || ' ' || COALESCE(NEW.open_questions,''),
    NEW.id, 'snapshots'
  );
END;
CREATE TRIGGER IF NOT EXISTS fts_snapshots_delete AFTER DELETE ON snapshots
BEGIN
  DELETE FROM memory_fts WHERE row_id = OLD.id AND table_name = 'snapshots';
END;

-- ── indexes ────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_facts_topic       ON facts(topic_id);
CREATE INDEX IF NOT EXISTS idx_facts_type_status ON facts(fact_type, status);
CREATE INDEX IF NOT EXISTS idx_facts_importance  ON facts(importance DESC, last_accessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_topics_accessed   ON topics(last_accessed_at DESC) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_mentions_entity   ON entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_mentions_parent   ON entity_mentions(parent_id, parent_type);
CREATE INDEX IF NOT EXISTS idx_topics_cat_status ON topics(category, status);
CREATE INDEX IF NOT EXISTS idx_snapshots_topic   ON snapshots(topic_id, seq_number DESC);

-- ── fts_resolved view ─────────────────────────────────────────────────
-- Resolves FTS hits back to full source rows without per-query CASE statements.
-- Usage: SELECT r.* FROM memory_fts m
--        JOIN fts_resolved r ON r.src = m.table_name AND r.id = m.row_id
--        WHERE memory_fts MATCH 'keyword';
CREATE VIEW IF NOT EXISTS fts_resolved AS
  SELECT 'facts'     AS src, id, content                                                AS text, fact_type   AS subtype FROM facts
  UNION ALL
  SELECT 'entities'  AS src, id, name || ' ' || COALESCE(notes,'')                     AS text, entity_type AS subtype FROM entities
  UNION ALL
  SELECT 'topics'    AS src, id, title || ' ' || COALESCE(description,'')              AS text, category    AS subtype FROM topics
  UNION ALL
  SELECT 'snapshots' AS src, id, COALESCE(summary,'') || ' ' || COALESCE(findings,'')  AS text, NULL        AS subtype FROM snapshots;
