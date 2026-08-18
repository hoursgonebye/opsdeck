"""
SQLite storage for Ops Deck.

One file at data/opsdeck.db. The schema is created on startup if missing
and migrated forward by SCHEMA_VERSION, so new tables and columns can be
added without wiping data.

Design note on the growth system: XP totals and attribute values are never
stored as running counters. Every level-up writes one row to skill_levels
with a timestamp, and any week's XP or attribute shape is a query over that
ledger plus the existing routine/card completion tables. One source of
truth, no drift, and history stays correct even if weights change later.
"""
import json
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "opsdeck.db"
UPLOAD_DIR = DATA_DIR / "uploads"

SCHEMA_VERSION = 16

# Tables that gain a profile_id in v5. Pre-v5 rows all belong to 'primary'.
PROFILE_SCOPED_TABLES = ("boards", "events", "routines", "docs", "quick_notes")

# v6 extends scoping to the growth system and the mentor, so each profile
# has its own skill tree, its own attributes, and its own verification
# queue. Child tables (node_weights, skill_edges, skill_levels) inherit
# scope through skill_nodes rather than carrying a redundant column.
GROWTH_SCOPED_TABLES = ("skill_nodes", "attributes", "levelup_attempts",
                        "ai_proposals", "thm_completions")

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  position   INTEGER NOT NULL DEFAULT 0,
  archived   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lists (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  title    TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lists_board ON lists(board_id);

CREATE TABLE IF NOT EXISTS cards (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  list_id      INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
  title        TEXT NOT NULL,
  description  TEXT NOT NULL DEFAULT '',
  due_at       TEXT,
  completed    INTEGER NOT NULL DEFAULT 0,
  position     INTEGER NOT NULL DEFAULT 0,
  archived     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cards_list ON cards(list_id);
CREATE INDEX IF NOT EXISTS idx_cards_due  ON cards(due_at);

CREATE TABLE IF NOT EXISTS labels (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  name     TEXT NOT NULL,
  color    TEXT NOT NULL DEFAULT 'gray'
);

CREATE TABLE IF NOT EXISTS card_labels (
  card_id  INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
  PRIMARY KEY (card_id, label_id)
);

CREATE TABLE IF NOT EXISTS checklist_items (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id  INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  text     TEXT NOT NULL,
  done     INTEGER NOT NULL DEFAULT 0,
  position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_checklist_card ON checklist_items(card_id);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  location    TEXT NOT NULL DEFAULT '',
  start_at    TEXT NOT NULL,
  end_at      TEXT,
  all_day     INTEGER NOT NULL DEFAULT 0,
  rrule       TEXT,
  color       TEXT NOT NULL DEFAULT 'blue',
  remind_min  INTEGER,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);

CREATE TABLE IF NOT EXISTS event_overrides (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  occurrence   TEXT NOT NULL,
  action       TEXT NOT NULL DEFAULT 'skip',
  new_start_at TEXT,
  new_end_at   TEXT,
  new_title    TEXT,
  UNIQUE (event_id, occurrence)
);

CREATE TABLE IF NOT EXISTS routines (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  time_group TEXT NOT NULL DEFAULT 'anytime',
  notes      TEXT NOT NULL DEFAULT '',
  position   INTEGER NOT NULL DEFAULT 0,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS routine_completions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  routine_id   INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
  local_date   TEXT NOT NULL,
  completed_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (routine_id, local_date)
);
CREATE INDEX IF NOT EXISTS idx_completions_date ON routine_completions(local_date);

CREATE TABLE IF NOT EXISTS docs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'md',
  body       TEXT NOT NULL DEFAULT '',
  folder     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_docs_folder ON docs(folder);

CREATE TABLE IF NOT EXISTS doc_tags (
  doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  tag    TEXT NOT NULL,
  PRIMARY KEY (doc_id, tag)
);

CREATE TABLE IF NOT EXISTS uploads (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id    INTEGER REFERENCES cards(id) ON DELETE CASCADE,
  filename   TEXT NOT NULL,
  stored_as  TEXT NOT NULL,
  mime       TEXT NOT NULL DEFAULT '',
  size       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ===================== growth system (schema v2) =====================

-- Broad RPG-style stats. A node can feed several of these at once.
CREATE TABLE IF NOT EXISTS attributes (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  key      TEXT NOT NULL UNIQUE,
  name     TEXT NOT NULL,
  color    TEXT NOT NULL DEFAULT 'teal',
  position INTEGER NOT NULL DEFAULT 0
);

-- Skill tree nodes. x/y are free-form canvas coordinates - the tree is
-- deliberately allowed to be disconnected, so nothing forces every branch
-- into one graph.
CREATE TABLE IF NOT EXISTS skill_nodes (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  description  TEXT NOT NULL DEFAULT '',
  domain       TEXT NOT NULL DEFAULT 'general',
  x            REAL NOT NULL DEFAULT 0,
  y            REAL NOT NULL DEFAULT 0,
  level        INTEGER NOT NULL DEFAULT 0,
  max_level    INTEGER NOT NULL DEFAULT 5,
  tier         INTEGER NOT NULL DEFAULT 1,
  unlock_attr  TEXT,
  unlock_value REAL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_nodes_domain ON skill_nodes(domain);

CREATE TABLE IF NOT EXISTS skill_edges (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  from_id INTEGER NOT NULL REFERENCES skill_nodes(id) ON DELETE CASCADE,
  to_id   INTEGER NOT NULL REFERENCES skill_nodes(id) ON DELETE CASCADE,
  UNIQUE (from_id, to_id)
);

-- Many-to-one with weights: one node can push several attributes by
-- different amounts, because real skills don't map cleanly to one stat.
CREATE TABLE IF NOT EXISTS node_weights (
  node_id       INTEGER NOT NULL REFERENCES skill_nodes(id) ON DELETE CASCADE,
  attribute_key TEXT NOT NULL,
  weight        REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (node_id, attribute_key)
);

-- The ledger. One row per level gained, ever. Attribute values and weekly
-- XP are both computed from this, so nothing needs recomputing or repairing.
CREATE TABLE IF NOT EXISTS skill_levels (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id    INTEGER NOT NULL REFERENCES skill_nodes(id) ON DELETE CASCADE,
  level      INTEGER NOT NULL,
  attempt_id INTEGER,
  earned_at  TEXT NOT NULL DEFAULT (datetime('now')),
  local_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_levels_date ON skill_levels(local_date);

-- A level-up request in flight. The user cannot self-grant a level; this
-- row is the handshake between them and the mentor.
CREATE TABLE IF NOT EXISTS levelup_attempts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id      INTEGER NOT NULL REFERENCES skill_nodes(id) ON DELETE CASCADE,
  target_level INTEGER NOT NULL,
  difficulty   INTEGER NOT NULL DEFAULT 1,
  status       TEXT NOT NULL DEFAULT 'awaiting_questions',
  questions    TEXT NOT NULL DEFAULT '[]',
  answers      TEXT NOT NULL DEFAULT '[]',
  evidence_doc INTEGER REFERENCES docs(id) ON DELETE SET NULL,
  feedback     TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON levelup_attempts(status);

-- ==================== TryHackMe integration (schema v3) ====================

-- Room metadata. Populated best-effort from TryHackMe's unofficial public
-- endpoints, or minimally by hand when logging a completion. The code is
-- the room's URL slug (e.g. 'vulnversity').
CREATE TABLE IF NOT EXISTS thm_rooms (
  code        TEXT PRIMARY KEY,
  title       TEXT NOT NULL DEFAULT '',
  difficulty  TEXT NOT NULL DEFAULT '',
  tags        TEXT NOT NULL DEFAULT '[]',
  description TEXT NOT NULL DEFAULT '',
  fetched_at  TEXT
);

-- Which skill-tree nodes a room maps to. A completion of the room offers a
-- verification attempt on each mapped node - it never grants anything.
CREATE TABLE IF NOT EXISTS thm_room_nodes (
  room_code TEXT NOT NULL REFERENCES thm_rooms(code) ON DELETE CASCADE,
  node_id   INTEGER NOT NULL REFERENCES skill_nodes(id) ON DELETE CASCADE,
  PRIMARY KEY (room_code, node_id)
);

-- One row per room ever completed. Source is 'manual' (self-reported) or
-- 'sync' (pulled from the public profile). Completions carry zero XP by
-- design: the level-up they unlock is the thing that pays, and only after
-- the mentor signs off.
CREATE TABLE IF NOT EXISTS thm_completions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  room_code  TEXT NOT NULL UNIQUE REFERENCES thm_rooms(code) ON DELETE CASCADE,
  local_date TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Quick capture from the Today page. The point is that writing something
-- down never blocks on deciding where it goes: the note lands here first
-- and is filed afterwards, by heuristic, by an agent, or by hand.
CREATE TABLE IF NOT EXISTS quick_notes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  body        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending | filed | dismissed
  suggestion  TEXT NOT NULL DEFAULT '{}',        -- JSON: local heuristic guess
  filed_as    TEXT NOT NULL DEFAULT '',          -- human note of where it went
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_qnotes_status ON quick_notes(status);

-- Anything the AI wants to change on its own initiative lands here first.
-- Nothing is applied until the user approves it.
CREATE TABLE IF NOT EXISTS ai_proposals (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,
  title       TEXT NOT NULL,
  rationale   TEXT NOT NULL DEFAULT '',
  actions     TEXT NOT NULL DEFAULT '[]',
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON ai_proposals(status);

-- ==================== profiles / themes (schema v5) ====================
--
-- The app began single-user. v5 turns "the user" into one of several
-- profiles: primary (the original owner - all pre-v5 data backfills to
-- them), partner, and joint (a pseudo-user that owns shared content). Every
-- content table gains a profile_id, and the active profile is chosen by the
-- X-Profile-Id request header. See ARCHITECTURE.md for why this is a header
-- rather than a path segment.

CREATE TABLE IF NOT EXISTS profiles (
  id           TEXT PRIMARY KEY,               -- 'primary' | 'partner' | 'joint' | uuid
  type         TEXT NOT NULL,                  -- primary | partner | joint
  display_name TEXT NOT NULL,
  avatar_url   TEXT,
  position     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One settings blob per profile, stored as JSON so the shape can grow
-- without a migration each time. enabled_modules is the mechanism that lets
-- a profile drop a whole feature (e.g. the partner omitting the skill tree)
-- with no code change - the nav renders only what's listed.
CREATE TABLE IF NOT EXISTS profile_settings (
  profile_id TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
  settings   TEXT NOT NULL DEFAULT '{}'
);

-- Built-in themes are seeded with is_custom=0 and owner_profile_id=NULL.
-- Custom themes belong to the profile that created them.
CREATE TABLE IF NOT EXISTS themes (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  is_custom        INTEGER NOT NULL DEFAULT 0,
  owner_profile_id TEXT REFERENCES profiles(id) ON DELETE CASCADE,
  colors           TEXT NOT NULL,              -- JSON: {bg,surface,primary,accent,text,text_muted,...}
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ==================== joint features (schema v5) ====================

-- Append-only activity log. Relationship XP, companion growth and milestone
-- checks are all derived from this - the same "nothing is a stored counter"
-- pattern the personal XP system already uses.
CREATE TABLE IF NOT EXISTS activity_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  source_type TEXT NOT NULL,                   -- routine_completion | joint_card_done | streak_milestone | ping | interaction
  source_id   TEXT,
  weight      REAL NOT NULL DEFAULT 1.0,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_events(created_at);

CREATE TABLE IF NOT EXISTS relationship_xp_config (
  id      INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton
  weights TEXT NOT NULL DEFAULT '{}'           -- JSON: source_type -> multiplier
);

CREATE TABLE IF NOT EXISTS mailbox_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  from_profile_id TEXT NOT NULL,
  to_profile_id   TEXT,                        -- NULL = visible to both
  body            TEXT NOT NULL,
  deliver_at      TEXT NOT NULL,
  delivered       INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mailbox_delivered ON mailbox_messages(delivered, deliver_at);

CREATE TABLE IF NOT EXISTS wall_posts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  type       TEXT NOT NULL DEFAULT 'text',     -- image | text | link
  content    TEXT NOT NULL,
  caption    TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wall_created ON wall_posts(created_at);

CREATE TABLE IF NOT EXISTS wall_reactions (
  post_id    INTEGER NOT NULL REFERENCES wall_posts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  emoji      TEXT NOT NULL,
  PRIMARY KEY (post_id, profile_id, emoji)
);

CREATE TABLE IF NOT EXISTS date_ideas (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  created_by   TEXT NOT NULL,
  title        TEXT NOT NULL,
  description  TEXT,
  tags         TEXT NOT NULL DEFAULT '{}',     -- JSON: {cost,setting,duration}
  status       TEXT NOT NULL DEFAULT 'idea',   -- idea | planned | done
  planned_date TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS companion (
  id                 INTEGER PRIMARY KEY CHECK (id = 1),   -- singleton
  species_or_skin    TEXT NOT NULL DEFAULT 'sprout',
  growth_stage       INTEGER NOT NULL DEFAULT 0,
  xp                 REAL NOT NULL DEFAULT 0,
  last_interacted_at TEXT
);

CREATE TABLE IF NOT EXISTS countdowns (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  label       TEXT NOT NULL,
  target_date TEXT NOT NULL,
  recurring   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS song_of_day (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  track_title TEXT NOT NULL,
  track_url   TEXT,
  note        TEXT,
  local_date  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_song_date ON song_of_day(local_date);

CREATE TABLE IF NOT EXISTS daily_prompts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt_text TEXT NOT NULL,
  local_date  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS prompt_answers (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt_id   INTEGER NOT NULL REFERENCES daily_prompts(id) ON DELETE CASCADE,
  profile_id  TEXT NOT NULL,
  answer      TEXT NOT NULL,
  answered_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (prompt_id, profile_id)
);

CREATE TABLE IF NOT EXISTS milestones (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  type       TEXT NOT NULL,                    -- streak | relationship_xp | custom
  threshold  REAL NOT NULL,
  label      TEXT NOT NULL,
  celebrated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bucket_list_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  category     TEXT,
  status       TEXT NOT NULL DEFAULT 'someday', -- someday | planned | done
  completed_at TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- In-app notifications. The browser already polls reminders; this table
-- gives mailbox deliveries, pings, wall posts and milestones a durable home
-- and a per-profile inbox.
CREATE TABLE IF NOT EXISTS notifications (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  source_type TEXT NOT NULL,                   -- mailbox | ping | wall | relationship_xp | milestone | countdown
  title       TEXT NOT NULL,
  body        TEXT NOT NULL DEFAULT '',
  link        TEXT,
  seen        INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notif_profile ON notifications(profile_id, seen);

-- ==================== calendar feeds (schema v8) ====================
--
-- Subscribed read-only .ics feeds: a work roster, a class timetable. Their
-- events land in the normal events table tagged with feed_id, so they show
-- up everywhere events already do - Today, the month grid, the merged Us
-- view - without any of those needing to know feeds exist.
CREATE TABLE IF NOT EXISTS calendar_feeds (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id     TEXT NOT NULL,
  name           TEXT NOT NULL,
  url            TEXT NOT NULL,
  color          TEXT NOT NULL DEFAULT 'blue',
  enabled        INTEGER NOT NULL DEFAULT 1,
  last_synced_at TEXT,
  last_status    TEXT NOT NULL DEFAULT '',
  last_count     INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feeds_profile ON calendar_feeds(profile_id);

-- ==================== web push (schema v11) ====================
--
-- One row per subscribed browser/device per profile. The endpoint URL is
-- the identity (push services rotate them on re-subscribe); rows whose
-- endpoint answers 404/410 are pruned automatically on send.
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id TEXT NOT NULL,
  endpoint   TEXT NOT NULL UNIQUE,
  p256dh     TEXT NOT NULL,
  auth       TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_push_profile ON push_subscriptions(profile_id);

-- ==================== finance (schema v9) ====================
--
-- A personal ledger. Same discipline as the growth system: transactions are
-- the ledger, and every spending total or budget figure is a query over
-- them - no stored balance or running total anywhere. Amounts are integer
-- cents; floats never touch money in this schema.
--
-- Scoping follows the boards model: fin_accounts, fin_categories and
-- fin_income_sources carry profile_id; fin_transactions inherit scope
-- through their account, so a transaction's profile can never disagree
-- with its account's.
CREATE TABLE IF NOT EXISTS fin_accounts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL DEFAULT 'checking',    -- checking|credit|cash|other
  institution TEXT,
  is_active   INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fin_accounts_profile ON fin_accounts(profile_id);

-- is_transfer marks the seeded Transfers category: moving money between
-- your own accounts is not spending, and every aggregation excludes these
-- rows. A flag rather than a name match, so renaming the category cannot
-- silently turn card payments back into "spending".
CREATE TABLE IF NOT EXISTS fin_categories (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  name        TEXT NOT NULL,
  parent_id   INTEGER REFERENCES fin_categories(id),   -- one level max, enforced in code
  is_income   INTEGER NOT NULL DEFAULT 0,
  is_transfer INTEGER NOT NULL DEFAULT 0,
  color       TEXT,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  UNIQUE (profile_id, name)
);
CREATE INDEX IF NOT EXISTS idx_fin_categories_profile ON fin_categories(profile_id);

-- merchant_raw is never mutated after entry; merchant_normalized (lowered,
-- punctuation stripped, whitespace collapsed) is what dedupe keys and -
-- later - category rules match against. category_id NULL means
-- uncategorized; there is deliberately no "Uncategorized" category row,
-- because two representations of the same state would drift.
CREATE TABLE IF NOT EXISTS fin_transactions (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id          INTEGER NOT NULL REFERENCES fin_accounts(id),
  posted_date         TEXT NOT NULL,                 -- YYYY-MM-DD, drives all budget math
  amount_cents        INTEGER NOT NULL,              -- integers only, never floats
  direction           TEXT NOT NULL,                 -- debit|credit
  merchant_raw        TEXT NOT NULL,
  merchant_normalized TEXT NOT NULL,
  category_id         INTEGER REFERENCES fin_categories(id),
  category_source     TEXT NOT NULL DEFAULT 'manual',  -- manual|rule|ai|import
  is_pending          INTEGER NOT NULL DEFAULT 0,
  notes               TEXT,
  source              TEXT NOT NULL DEFAULT 'manual',  -- manual|csv
  dedupe_key          TEXT NOT NULL UNIQUE,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fin_tx_date ON fin_transactions(posted_date);
CREATE INDEX IF NOT EXISTS idx_fin_tx_account ON fin_transactions(account_id, posted_date);
CREATE INDEX IF NOT EXISTS idx_fin_tx_category ON fin_transactions(category_id, posted_date);

-- Category rules (schema v10): deterministic first-match-wins
-- categorization evaluated by priority on entry and import. A rule NEVER
-- overwrites category_source='manual' - user decisions are final. Rules
-- the AI proposes arrive with origin='ai_suggested' and is_active=0; they
-- do nothing until a person switches them on.
CREATE TABLE IF NOT EXISTS fin_category_rules (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id      TEXT NOT NULL,
  match_type      TEXT NOT NULL DEFAULT 'contains',  -- contains|starts_with|exact|regex
  pattern         TEXT NOT NULL,                     -- matched against merchant_normalized
  category_id     INTEGER NOT NULL REFERENCES fin_categories(id),
  account_id      INTEGER REFERENCES fin_accounts(id),  -- NULL = all accounts
  priority        INTEGER NOT NULL DEFAULT 100,      -- lower evaluates first
  is_active       INTEGER NOT NULL DEFAULT 1,
  origin          TEXT NOT NULL DEFAULT 'user',      -- user|ai_suggested
  hit_count       INTEGER NOT NULL DEFAULT 0,
  last_matched_at TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fin_rules_profile ON fin_category_rules(profile_id, is_active, priority);

-- Envelope budgets (schema v10), one row per category per month. Scope
-- inherits through the category. period_type only ever holds 'monthly'
-- today; the column exists so a different period length is a value, not a
-- migration.
CREATE TABLE IF NOT EXISTS fin_budgets (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id  INTEGER NOT NULL REFERENCES fin_categories(id),
  period_start TEXT NOT NULL,                        -- YYYY-MM-01
  period_type  TEXT NOT NULL DEFAULT 'monthly',
  limit_cents  INTEGER NOT NULL,
  rollover     INTEGER NOT NULL DEFAULT 0,
  UNIQUE (category_id, period_start)
);

-- Stored AI narrative reviews (schema v10), so re-reading one is a SELECT,
-- not another API call.
CREATE TABLE IF NOT EXISTS fin_ai_reviews (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id   TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'monthly',      -- weekly|monthly|custom
  period_start TEXT NOT NULL,
  period_end   TEXT NOT NULL,
  body         TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fin_reviews_profile ON fin_ai_reviews(profile_id, period_start);

-- Expectation only. Actual income is a fin_transactions row with
-- direction='credit' and an income category; nothing ever treats an
-- expected amount as received.
CREATE TABLE IF NOT EXISTS fin_income_sources (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id            TEXT NOT NULL,
  name                  TEXT NOT NULL,
  expected_amount_cents INTEGER,                     -- nullable: hours vary
  cadence               TEXT NOT NULL DEFAULT 'irregular',
                        -- weekly|biweekly|semimonthly|monthly|irregular
  account_id            INTEGER REFERENCES fin_accounts(id),
  is_active             INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_fin_income_profile ON fin_income_sources(profile_id);

-- ==================== health (schema v7) ====================
--
-- Deliberately provider-agnostic. Google's Health API is the intended
-- source, but the same table takes a push from Tasker, Home Assistant, an
-- iOS Shortcut or a curl one-liner - the ingest endpoint does not care.
--
-- One row per (profile, metric, day, source). The unique constraint makes
-- re-syncing a date range idempotent: a second pull of the same day
-- overwrites rather than duplicating, which matters because step counts
-- keep changing until the day is over.
CREATE TABLE IF NOT EXISTS health_metrics (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  metric      TEXT NOT NULL,        -- steps | sleep_minutes | active_minutes | exercise_minutes | weight_kg | resting_hr | distance_km | calories
  value       REAL NOT NULL,
  unit        TEXT NOT NULL DEFAULT '',
  local_date  TEXT NOT NULL,        -- YYYY-MM-DD in OPSDECK_TZ
  source      TEXT NOT NULL DEFAULT 'manual',
  recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (profile_id, metric, local_date, source)
);
CREATE INDEX IF NOT EXISTS idx_health_lookup ON health_metrics(profile_id, metric, local_date);

-- Refresh tokens for connected providers. Access tokens are short-lived
-- (Google's are one hour) so the refresh token is the thing worth storing;
-- the access token is cached alongside it only to avoid refreshing on every
-- single call.
CREATE TABLE IF NOT EXISTS oauth_tokens (
  provider      TEXT NOT NULL,
  profile_id    TEXT NOT NULL,
  access_token  TEXT NOT NULL DEFAULT '',
  refresh_token TEXT NOT NULL DEFAULT '',
  expires_at    TEXT,
  scope         TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (provider, profile_id)
);

-- ==================== academics (schema v12) ====================
--
-- The transcript, and every GPA derived from it. Same discipline as the
-- ledgers: no GPA is ever stored. A term GPA, a cumulative GPA and a
-- forecast are all one query over acad_courses joined to the grade scale,
-- so correcting a single grade - or the scale itself - re-derives every
-- number in the section with no backfill.

CREATE TABLE IF NOT EXISTS acad_terms (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  name        TEXT NOT NULL,                 -- 'Fall 2025'
  season      TEXT NOT NULL DEFAULT 'fall',  -- winter|spring|summer|fall
  year        INTEGER NOT NULL,
  status      TEXT NOT NULL DEFAULT 'completed',  -- completed|in_progress|planned
  institution TEXT NOT NULL DEFAULT '',
  notes       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (profile_id, name)
);
CREATE INDEX IF NOT EXISTS idx_acad_terms_profile ON acad_terms(profile_id, year, season);

-- Scope inherits through the term, exactly like transactions inherit through
-- accounts - a course row carries no profile_id of its own.
--
-- credits is REAL rather than integer-hundredths (the money rule) because a
-- credit hour is only ever a whole number or a half, both of which binary
-- floating point represents exactly. GPA is irreducibly fractional anyway.
CREATE TABLE IF NOT EXISTS acad_courses (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  term_id          INTEGER NOT NULL REFERENCES acad_terms(id) ON DELETE CASCADE,
  code             TEXT NOT NULL DEFAULT '',    -- 'CIS 110'
  title            TEXT NOT NULL DEFAULT '',
  credits          REAL NOT NULL DEFAULT 3,
  grade            TEXT NOT NULL DEFAULT '',    -- '' until the term closes
  projected_grade  TEXT NOT NULL DEFAULT '',    -- what he expects; drives the forecast
  tags             TEXT NOT NULL DEFAULT '[]',  -- JSON array, e.g. ["ub-core"]
  exclude_from_gpa INTEGER NOT NULL DEFAULT 0,  -- manual override
  position         INTEGER NOT NULL DEFAULT 0,
  notes            TEXT NOT NULL DEFAULT '',
  -- Which earlier attempt this course is a retake of (schema v13). Under a
  -- grade-replacement policy the original stops counting once the retake is
  -- graded - and not one moment before, which is why this is a link rather
  -- than the exclude_from_gpa flag. Ticking that flag today would show a GPA
  -- the registrar has not yet agreed to. ON DELETE SET NULL so deleting an
  -- old attempt quietly un-links rather than cascading the retake away.
  replaces_course_id INTEGER REFERENCES acad_courses(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_acad_courses_term ON acad_courses(term_id);

-- Per-profile because scales genuinely differ between institutions, and
-- getting one wrong silently poisons every GPA in the app. `verified` marks
-- the entries proven against a real transcript; the rest are assumptions the
-- UI flags until the user confirms them against the catalog.
--
-- points NULL means the grade carries no quality points at all (W, I, P) -
-- distinct from 0.0, which is a real F.
CREATE TABLE IF NOT EXISTS acad_grade_scale (
  profile_id   TEXT NOT NULL,
  grade        TEXT NOT NULL,
  points       REAL,
  counts_gpa   INTEGER NOT NULL DEFAULT 1,   -- enters the GPA denominator
  earns_credit INTEGER NOT NULL DEFAULT 1,   -- counts toward credits earned
  sort_order   INTEGER NOT NULL DEFAULT 0,
  verified     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (profile_id, grade)
);

-- A GPA floor that matters (SFS 3.4, UB CSE 2.8). scope_tag NULL means the
-- cumulative GPA; a tag scopes the goal to courses carrying it, which is how
-- "core GPA over the four UB transfer courses" is expressed without a
-- second, parallel notion of a transcript.
CREATE TABLE IF NOT EXISTS acad_goals (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  name        TEXT NOT NULL,
  target_gpa  REAL NOT NULL,
  scope_tag   TEXT,
  note        TEXT NOT NULL DEFAULT '',
  position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_acad_goals_profile ON acad_goals(profile_id);

-- ==================== homelab (schema v16) ====================
--
-- An inventory that is meant to be *argued with*, not just listed: every
-- device carries what it is for, and recommendations hang off devices (or off
-- the lab as a whole) with a severity and a cost so the next thing to do is
-- obvious rather than buried in a wiki.
--
-- Live state is deliberately not stored. Reachability is probed on read, the
-- same discipline as every balance and GPA in this app - a cached "online"
-- flag is wrong the moment something is unplugged.

CREATE TABLE IF NOT EXISTS lab_devices (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id   TEXT NOT NULL,
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'other',
                 -- server|guest|laptop|sbc|workstation|printer|network|iot|phone|other
  status       TEXT NOT NULL DEFAULT 'active',  -- active|building|planned|retired
  purpose      TEXT NOT NULL DEFAULT '',
  specs        TEXT NOT NULL DEFAULT '',        -- free text, one fact per line
  hostname     TEXT NOT NULL DEFAULT '',
  lan_ip       TEXT NOT NULL DEFAULT '',
  tailscale_ip TEXT NOT NULL DEFAULT '',
  mac          TEXT NOT NULL DEFAULT '',
  -- What to knock on to decide "up". A TCP connect, never ICMP: the app image
  -- has no ping binary and raw sockets in a container are a fight not worth
  -- having. Port 0 means "nothing to probe" - an unmanaged switch is not
  -- broken just because it never answers.
  probe_host   TEXT NOT NULL DEFAULT '',
  probe_port   INTEGER NOT NULL DEFAULT 0,
  notes        TEXT NOT NULL DEFAULT '',
  position     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lab_devices_profile ON lab_devices(profile_id, position);

-- device_id NULL means the recommendation is about the lab rather than one
-- box - segmentation, backups, a managed switch.
CREATE TABLE IF NOT EXISTS lab_upgrades (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  TEXT NOT NULL,
  device_id   INTEGER REFERENCES lab_devices(id) ON DELETE CASCADE,
  title       TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '',
  category    TEXT NOT NULL DEFAULT 'performance',
                -- security|reliability|performance|capacity|cost|capability
  severity    TEXT NOT NULL DEFAULT 'medium',   -- high|medium|low
  cost        TEXT NOT NULL DEFAULT '',         -- free text: "$30", "free", "$400+"
  status      TEXT NOT NULL DEFAULT 'idea',     -- idea|planned|doing|done|declined
  position    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lab_upgrades_profile ON lab_upgrades(profile_id, position);
CREATE INDEX IF NOT EXISTS idx_lab_upgrades_device ON lab_upgrades(device_id);
"""

SEED = """
INSERT INTO boards (id, title, position) VALUES (1, 'Main', 0);
INSERT INTO lists (board_id, title, position) VALUES
  (1, 'To do', 0), (1, 'In progress', 1), (1, 'Done', 2);
INSERT INTO labels (board_id, name, color) VALUES
  (1, 'School', 'blue'), (1, 'Homelab', 'teal'), (1, 'Urgent', 'red');
INSERT INTO routines (name, time_group, position) VALUES
  ('Review today''s board', 'morning', 0),
  ('Check calendar for tomorrow', 'evening', 0);
"""

# Six broad stats. Deliberately few - the tree carries the detail, these
# carry the shape.
SEED_ATTRIBUTES = [
    ("networking", "Networking", "blue", 0),
    ("linux", "Linux", "amber", 1),
    ("pentest", "Pentest", "red", 2),
    ("defense", "Defense", "teal", 3),
    ("crypto", "Cryptography", "purple", 4),
    ("grc", "GRC", "green", 5),
]

# A starting tree, hand-placed into six domain clusters so it reads as a map
# rather than a graph dump. Everything here is editable - this is a starting
# point, not a fixed taxonomy.
#  (title, domain, x, y, tier, [(attr, weight)], unlock_attr, unlock_value)
SEED_NODES = [
    ("TCP/IP fundamentals",     "networking", 400, 300, 1, [("networking", 1.0)], None, None),
    ("Subnetting & VLANs",      "networking", 250, 220, 2, [("networking", 1.0)], None, None),
    ("Packet analysis",         "networking", 330, 150, 2, [("networking", 0.7), ("defense", 0.5)], None, None),
    ("Routing protocols",       "networking", 150, 150, 3, [("networking", 1.0)], None, None),
    ("DNS internals",           "networking", 250, 380, 2, [("networking", 0.8), ("defense", 0.3)], None, None),
    ("Network forensics",       "networking", 200,  70, 4, [("networking", 0.6), ("defense", 1.0)], "networking", 6),

    ("Linux CLI",               "linux",      600, 300, 1, [("linux", 1.0)], None, None),
    ("Permissions & users",     "linux",      720, 230, 2, [("linux", 0.9), ("defense", 0.3)], None, None),
    ("Bash scripting",          "linux",      700, 390, 2, [("linux", 1.0)], None, None),
    ("systemd & services",      "linux",      840, 300, 3, [("linux", 1.0)], None, None),
    ("Containers & namespaces", "linux",      850, 420, 3, [("linux", 0.8), ("defense", 0.3)], None, None),
    ("Kernel hardening",        "linux",      960, 240, 4, [("linux", 1.0), ("defense", 0.7)], "linux", 8),

    ("Recon & enumeration",     "pentest",    460, 560, 1, [("pentest", 1.0), ("networking", 0.3)], None, None),
    ("Web app testing",         "pentest",    340, 640, 2, [("pentest", 1.0), ("networking", 0.2)], None, None),
    ("Privilege escalation",    "pentest",    570, 650, 2, [("pentest", 1.0), ("linux", 0.6)], None, None),
    ("Active Directory",        "pentest",    460, 740, 3, [("pentest", 1.0), ("networking", 0.4)], None, None),
    ("Exploit development",     "pentest",    650, 780, 4, [("pentest", 1.0)], "pentest", 8),

    ("Log analysis",            "defense",    900, 620, 1, [("defense", 1.0), ("linux", 0.3)], None, None),
    ("SIEM & detection",        "defense",    790, 700, 2, [("defense", 1.0)], None, None),
    ("IDS/IPS (Suricata)",      "defense",   1010, 700, 2, [("defense", 1.0), ("networking", 0.5)], None, None),
    ("Incident response",       "defense",    900, 800, 3, [("defense", 1.0), ("grc", 0.4)], None, None),
    ("Threat hunting",          "defense",   1020, 860, 4, [("defense", 1.0)], "defense", 8),

    ("Crypto primitives",       "crypto",     130, 560, 1, [("crypto", 1.0)], None, None),
    ("TLS & PKI",               "crypto",      90, 680, 2, [("crypto", 1.0), ("networking", 0.4)], None, None),
    ("Hashing & passwords",     "crypto",     230, 660, 2, [("crypto", 0.9), ("pentest", 0.3)], None, None),
    ("Applied cryptanalysis",   "crypto",     140, 800, 4, [("crypto", 1.0)], "crypto", 6),

    ("Security frameworks",     "grc",       1180, 330, 1, [("grc", 1.0)], None, None),
    ("Risk assessment",         "grc",       1280, 430, 2, [("grc", 1.0)], None, None),
    ("Compliance (SOC2/ISO)",   "grc",       1150, 470, 2, [("grc", 1.0)], None, None),
    ("Security policy design",  "grc",       1270, 560, 3, [("grc", 1.0)], "grc", 5),
]

SEED_EDGES = [
    ("TCP/IP fundamentals", "Subnetting & VLANs"),
    ("TCP/IP fundamentals", "Packet analysis"),
    ("TCP/IP fundamentals", "DNS internals"),
    ("Subnetting & VLANs", "Routing protocols"),
    ("Packet analysis", "Network forensics"),
    ("Linux CLI", "Permissions & users"),
    ("Linux CLI", "Bash scripting"),
    ("Permissions & users", "systemd & services"),
    ("Bash scripting", "Containers & namespaces"),
    ("systemd & services", "Kernel hardening"),
    ("Recon & enumeration", "Web app testing"),
    ("Recon & enumeration", "Privilege escalation"),
    ("Privilege escalation", "Active Directory"),
    ("Active Directory", "Exploit development"),
    ("Log analysis", "SIEM & detection"),
    ("Log analysis", "IDS/IPS (Suricata)"),
    ("SIEM & detection", "Incident response"),
    ("IDS/IPS (Suricata)", "Threat hunting"),
    ("Crypto primitives", "TLS & PKI"),
    ("Crypto primitives", "Hashing & passwords"),
    ("TLS & PKI", "Applied cryptanalysis"),
    ("Security frameworks", "Risk assessment"),
    ("Security frameworks", "Compliance (SOC2/ISO)"),
    ("Risk assessment", "Security policy design"),
]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn):
    """Idempotent forward migrations. Safe to run on every startup."""
    row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
    current = int(row["value"]) if row else 0

    if current < 2:
        # v1 cards/checklists had no completion timestamps; XP needs them to
        # attribute work to a week. Backfill existing completions rather than
        # letting old work silently count as zero.
        if "completed_at" not in _columns(conn, "cards"):
            conn.execute("ALTER TABLE cards ADD COLUMN completed_at TEXT")
            conn.execute("UPDATE cards SET completed_at = created_at WHERE completed = 1")
        if "done_at" not in _columns(conn, "checklist_items"):
            conn.execute("ALTER TABLE checklist_items ADD COLUMN done_at TEXT")
            conn.execute("UPDATE checklist_items SET done_at = datetime('now') WHERE done = 1")

    if current < 3:
        # v3: attempts can be triggered by a TryHackMe completion, and the
        # notes doc is attached at request time (it was optional-at-answer
        # in v2; existing rows keep whatever they had).
        if "room_code" not in _columns(conn, "levelup_attempts"):
            conn.execute("ALTER TABLE levelup_attempts ADD COLUMN room_code TEXT")

    # v4 adds quick_notes, which SCHEMA already creates via CREATE TABLE IF
    # NOT EXISTS - nothing to backfill, the bump just records the version.

    if current < 5:
        # v5: profiles. Add profile_id to every content table and backfill
        # existing rows to 'primary' so nothing already built moves or
        # disappears. New profile-aware tables are created by SCHEMA above.
        for table in PROFILE_SCOPED_TABLES:
            if "profile_id" not in _columns(conn, table):
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN profile_id TEXT "
                    f"NOT NULL DEFAULT 'primary'"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_profile "
                    f"ON {table}(profile_id)"
                )
        _seed_profiles(conn)

    if current < 6:
        # v6: the skill tree, attributes, and the mentor queue become
        # per-profile. Everything that exists today was built by the
        # original owner, so it backfills to 'primary' - the partner starts
        # with her own empty tree rather than inheriting a cybersecurity map
        # she never asked for.
        for table in GROWTH_SCOPED_TABLES:
            if "profile_id" not in _columns(conn, table):
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN profile_id TEXT "
                    f"NOT NULL DEFAULT 'primary'"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_profile "
                    f"ON {table}(profile_id)"
                )
        # attributes.key was globally unique by convention; with per-profile
        # attributes two people can both have a 'health' stat.
        conn.execute("DROP INDEX IF EXISTS idx_attributes_key")

    if current < 8:
        # v8: subscribed calendar feeds. Events gain a feed_id (NULL for
        # anything the user made themselves) and external_uid, a content
        # hash used to identify a feed event across re-syncs.
        if "feed_id" not in _columns(conn, "events"):
            conn.execute("ALTER TABLE events ADD COLUMN feed_id INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_feed ON events(feed_id)")
        if "external_uid" not in _columns(conn, "events"):
            conn.execute("ALTER TABLE events ADD COLUMN external_uid TEXT")

    if current < 7:
        # v7 adds health_metrics and oauth_tokens, both created by SCHEMA
        # above. No health data exists to migrate, but every profile already
        # has a settings row written before the Health module existed - so
        # changing DEFAULT_SETTINGS alone would leave the tab invisible on
        # every existing profile. Append it to their enabled_modules.
        for row in conn.execute("SELECT profile_id, settings FROM profile_settings").fetchall():
            try:
                cfg = json.loads(row["settings"] or "{}")
            except (ValueError, TypeError):
                continue
            mods = cfg.get("enabled_modules")
            if isinstance(mods, list) and "health" not in mods:
                mods.append("health")
                cfg["enabled_modules"] = mods
                conn.execute(
                    "UPDATE profile_settings SET settings=? WHERE profile_id=?",
                    (json.dumps(cfg), row["profile_id"]),
                )

    # v11 adds push_subscriptions, created by SCHEMA above - nothing to
    # backfill, the bump just records the version.

    if current < 10:
        # v10: rules, budgets and reviews are new tables (SCHEMA creates
        # them). fin_accounts gains a balance anchor: a known-true balance
        # on a date, from which the current balance is *derived* by summing
        # the ledger after it - the closest thing to a stored balance this
        # app will ever have, and it is still not a running counter.
        if "balance_anchor_cents" not in _columns(conn, "fin_accounts"):
            conn.execute("ALTER TABLE fin_accounts ADD COLUMN balance_anchor_cents INTEGER")
            conn.execute("ALTER TABLE fin_accounts ADD COLUMN balance_anchor_date TEXT")

    if current < 9:
        # v9: the finance ledger. Tables are created by SCHEMA above and
        # categories are seeded per-profile in init_db; the migration itself
        # only surfaces the tab, same as v7 did for health - every profile's
        # settings row predates the module, so DEFAULT_SETTINGS alone would
        # leave it invisible.
        for row in conn.execute("SELECT profile_id, settings FROM profile_settings").fetchall():
            try:
                cfg = json.loads(row["settings"] or "{}")
            except (ValueError, TypeError):
                continue
            mods = cfg.get("enabled_modules")
            if isinstance(mods, list) and "finance" not in mods:
                mods.append("finance")
                cfg["enabled_modules"] = mods
                conn.execute(
                    "UPDATE profile_settings SET settings=? WHERE profile_id=?",
                    (json.dumps(cfg), row["profile_id"]),
                )

    if current < 12:
        # v12: the transcript. Tables come from SCHEMA above and the grade
        # scale is seeded per-profile in init_db; the migration only surfaces
        # the tab, the same thing v7 and v9 did - every existing profile's
        # settings row predates the module, so DEFAULT_SETTINGS alone would
        # leave Academics invisible on a database that already exists.
        #
        # Unlike v7/v9 this skips joint profiles on purpose: "Us" is a
        # household pseudo-profile and a household does not have a transcript.
        for row in conn.execute(
            "SELECT ps.profile_id, ps.settings FROM profile_settings ps "
            "JOIN profiles p ON p.id = ps.profile_id WHERE p.type != 'joint'"
        ).fetchall():
            try:
                cfg = json.loads(row["settings"] or "{}")
            except (ValueError, TypeError):
                continue
            mods = cfg.get("enabled_modules")
            if isinstance(mods, list) and "academics" not in mods:
                mods.append("academics")
                cfg["enabled_modules"] = mods
                conn.execute(
                    "UPDATE profile_settings SET settings=? WHERE profile_id=?",
                    (json.dumps(cfg), row["profile_id"]),
                )

    if current < 13:
        # v13: repeated courses. A retake links to the attempt it replaces so
        # the original can be retired automatically the moment the new grade
        # posts. Existing rows have no repeats to backfill.
        if "replaces_course_id" not in _columns(conn, "acad_courses"):
            conn.execute("ALTER TABLE acad_courses ADD COLUMN replaces_course_id INTEGER")

    if current < 14:
        # v14: the printer tab. No tables - the printer is configured by env
        # and holds no state here - so this only surfaces the nav item, and
        # only for the owner: it is his machine, and the partner profile can
        # switch it on from Settings if she ever wants it.
        row = conn.execute(
            "SELECT settings FROM profile_settings WHERE profile_id='primary'"
        ).fetchone()
        if row:
            try:
                cfg = json.loads(row["settings"] or "{}")
            except (ValueError, TypeError):
                cfg = None
            if cfg is not None:
                mods = cfg.get("enabled_modules")
                if isinstance(mods, list) and "printer" not in mods:
                    mods.append("printer")
                    cfg["enabled_modules"] = mods
                    conn.execute(
                        "UPDATE profile_settings SET settings=? WHERE profile_id='primary'",
                        (json.dumps(cfg),),
                    )

    if current < 15:
        # v15: the Govee tab. Like the printer it owns no tables - the API key
        # and chosen device live as rows in `settings` - so this only surfaces
        # the nav item, and only for the owner.
        row = conn.execute(
            "SELECT settings FROM profile_settings WHERE profile_id='primary'"
        ).fetchone()
        if row:
            try:
                cfg = json.loads(row["settings"] or "{}")
            except (ValueError, TypeError):
                cfg = None
            if cfg is not None:
                mods = cfg.get("enabled_modules")
                if isinstance(mods, list) and "govee" not in mods:
                    mods.append("govee")
                    cfg["enabled_modules"] = mods
                    conn.execute(
                        "UPDATE profile_settings SET settings=? WHERE profile_id='primary'",
                        (json.dumps(cfg),),
                    )

    if current < 16:
        # v16: the homelab inventory. Tables come from SCHEMA above and the
        # seed runs in init_db; this only surfaces the tab, owner-only.
        row = conn.execute(
            "SELECT settings FROM profile_settings WHERE profile_id='primary'"
        ).fetchone()
        if row:
            try:
                cfg = json.loads(row["settings"] or "{}")
            except (ValueError, TypeError):
                cfg = None
            if cfg is not None:
                mods = cfg.get("enabled_modules")
                if isinstance(mods, list) and "homelab" not in mods:
                    mods.append("homelab")
                    cfg["enabled_modules"] = mods
                    conn.execute(
                        "UPDATE profile_settings SET settings=? WHERE profile_id='primary'",
                        (json.dumps(cfg),),
                    )

    conn.execute(
        "INSERT INTO settings (key,value) VALUES ('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


def _seed_growth(conn, profile_id="primary", attributes=None, nodes=None, edges=None):
    """
    Seed one profile's attributes and starter tree, only if that profile has
    none. Per-profile since v6: each person's tree is their own, so this can
    be called again for a new profile without touching an existing one.
    """
    attributes = attributes if attributes is not None else SEED_ATTRIBUTES
    nodes = nodes if nodes is not None else SEED_NODES
    edges = edges if edges is not None else SEED_EDGES

    has_attrs = conn.execute(
        "SELECT COUNT(*) FROM attributes WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]
    if has_attrs == 0:
        conn.executemany(
            "INSERT INTO attributes (key,name,color,position,profile_id) VALUES (?,?,?,?,?)",
            [(k, n, c, p, profile_id) for k, n, c, p in attributes],
        )

    has_nodes = conn.execute(
        "SELECT COUNT(*) FROM skill_nodes WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]
    if has_nodes == 0:
        ids = {}
        for title, domain, x, y, tier, weights, ua, uv in nodes:
            cur = conn.execute(
                """INSERT INTO skill_nodes (title,domain,x,y,tier,unlock_attr,unlock_value,profile_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (title, domain, x, y, tier, ua, uv, profile_id),
            )
            ids[title] = cur.lastrowid
            for key, w in weights:
                conn.execute(
                    "INSERT INTO node_weights (node_id,attribute_key,weight) VALUES (?,?,?)",
                    (cur.lastrowid, key, w),
                )
        for a, b in edges:
            if a in ids and b in ids:
                conn.execute(
                    "INSERT OR IGNORE INTO skill_edges (from_id,to_id) VALUES (?,?)",
                    (ids[a], ids[b]),
                )


# ---------------------------------------------------------------- partner
# The partner profile gets its own attributes and its own starter tree. The
# primary seed is a cybersecurity map, which would be meaningless to someone
# not doing that - so this is a deliberately broad, non-technical starting
# point. Everything here is editable: rename a domain, delete a node, or
# clear it out and build from scratch.

PARTNER_ATTRIBUTES = [
    ("wellbeing", "Wellbeing", "green", 0),
    ("craft", "Craft", "purple", 1),
    ("mind", "Mind", "blue", 2),
    ("home", "Home", "amber", 3),
    ("people", "People", "pink", 4),
    ("work", "Work", "teal", 5),
]

#  (title, domain, x, y, tier, [(attr, weight)], unlock_attr, unlock_value)
PARTNER_NODES = [
    ("Movement",           "wellbeing", 400, 300, 1, [("wellbeing", 1.0)], None, None),
    ("Sleep routine",      "wellbeing", 250, 220, 2, [("wellbeing", 1.0)], None, None),
    ("Cooking",            "wellbeing", 320, 430, 2, [("wellbeing", 0.7), ("home", 0.5)], None, None),
    ("Strength training",  "wellbeing", 180, 360, 3, [("wellbeing", 1.0)], None, None),

    ("Creative practice",  "craft",     760, 300, 1, [("craft", 1.0)], None, None),
    ("Drawing",            "craft",     900, 230, 2, [("craft", 1.0)], None, None),
    ("Photography",        "craft",     900, 380, 2, [("craft", 1.0)], None, None),
    ("Writing",            "craft",     640, 220, 2, [("craft", 0.8), ("mind", 0.4)], None, None),

    ("Reading habit",      "mind",      400, 640, 1, [("mind", 1.0)], None, None),
    ("Learning a language","mind",      250, 700, 2, [("mind", 1.0), ("people", 0.3)], None, None),
    ("Focus & attention",  "mind",      540, 700, 2, [("mind", 1.0)], None, None),

    ("Home systems",       "home",      760, 640, 1, [("home", 1.0)], None, None),
    ("Budgeting",          "home",      900, 700, 2, [("home", 0.8), ("work", 0.4)], None, None),
    ("Plants & garden",    "home",      640, 720, 2, [("home", 0.7), ("wellbeing", 0.3)], None, None),

    ("Relationships",      "people",   1120, 300, 1, [("people", 1.0)], None, None),
    ("Hosting",            "people",   1240, 380, 2, [("people", 0.8), ("home", 0.4)], None, None),
    ("Hard conversations", "people",   1240, 220, 3, [("people", 1.0)], None, None),

    ("Career skills",      "work",     1120, 640, 1, [("work", 1.0)], None, None),
    ("Public speaking",    "work",     1240, 700, 3, [("work", 0.8), ("people", 0.5)], None, None),
    ("Organisation",       "work",     1000, 720, 2, [("work", 0.7), ("home", 0.4)], None, None),
]

PARTNER_EDGES = [
    ("Movement", "Sleep routine"), ("Movement", "Cooking"),
    ("Sleep routine", "Strength training"),
    ("Creative practice", "Drawing"), ("Creative practice", "Photography"),
    ("Creative practice", "Writing"),
    ("Reading habit", "Learning a language"), ("Reading habit", "Focus & attention"),
    ("Home systems", "Budgeting"), ("Home systems", "Plants & garden"),
    ("Relationships", "Hosting"), ("Relationships", "Hard conversations"),
    ("Career skills", "Public speaking"), ("Career skills", "Organisation"),
]

# A starting board and a couple of routines, so her Today page has shape on
# day one instead of being three empty panels.
PARTNER_BOARD = ("My Board", ["To do", "In progress", "Done"])
PARTNER_ROUTINES = [
    ("Morning stretch", "morning", 0),
    ("Drink water", "morning", 1),
    ("Read before bed", "evening", 0),
]


# Every profile gets the same categories to start; they are per-profile
# rows, so renaming or adding ones later never leaks across profiles.
# (name, is_income, is_transfer, color, sort_order)
SEED_FIN_CATEGORIES = [
    ("Groceries",       0, 0, "green",  0),
    ("Dining",          0, 0, "amber",  1),
    ("Gas/Transport",   0, 0, "blue",   2),
    ("Rent/Housing",    0, 0, "purple", 3),
    ("Utilities",       0, 0, "teal",   4),
    ("Phone/Internet",  0, 0, "teal",   5),
    ("Subscriptions",   0, 0, "pink",   6),
    ("Education",       0, 0, "blue",   7),
    ("Tools/Hardware",  0, 0, "gray",   8),
    ("Health",          0, 0, "red",    9),
    ("Personal",        0, 0, "pink",   10),
    ("Entertainment",   0, 0, "amber",  11),
    ("Transfers",       0, 1, "gray",   12),
    ("Paycheck",        1, 0, "green",  13),
    ("Other Income",    1, 0, "green",  14),
]


def _seed_finance(conn, profile_id):
    """Seed one profile's categories, only if it has none yet."""
    n = conn.execute("SELECT COUNT(*) FROM fin_categories WHERE profile_id=?",
                     (profile_id,)).fetchone()[0]
    if n:
        return
    for name, is_income, is_transfer, color, pos in SEED_FIN_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO fin_categories "
            "(profile_id,name,is_income,is_transfer,color,sort_order) "
            "VALUES (?,?,?,?,?,?)",
            (profile_id, name, is_income, is_transfer, color, pos),
        )


def _seed_academics(conn, profile_id, with_goals=False):
    """
    Seed one profile's grade scale, and for the owner its GPA goals.

    The scale is seeded as a block-if-empty rather than per-row, so a user who
    deliberately deletes a grade they don't have (say AU) doesn't get it
    handed back on the next restart.

    Goals are the owner's only: SEED_ACAD_GOALS encodes one specific person's
    transfer plan, and handing "a scholarship programme 3.4" to the partner profile
    would be the same category error as seeding her a pentest skill tree.
    """
    n = conn.execute("SELECT COUNT(*) FROM acad_grade_scale WHERE profile_id=?",
                     (profile_id,)).fetchone()[0]
    if not n:
        for grade, points, counts, earns, pos, verified in SEED_GRADE_SCALE:
            conn.execute(
                "INSERT OR IGNORE INTO acad_grade_scale "
                "(profile_id,grade,points,counts_gpa,earns_credit,sort_order,verified) "
                "VALUES (?,?,?,?,?,?,?)",
                (profile_id, grade, points, counts, earns, pos, verified),
            )

    if not with_goals:
        return
    n = conn.execute("SELECT COUNT(*) FROM acad_goals WHERE profile_id=?",
                     (profile_id,)).fetchone()[0]
    if not n:
        for pos, (name, target, tag, note) in enumerate(SEED_ACAD_GOALS):
            conn.execute(
                "INSERT INTO acad_goals (profile_id,name,target_gpa,scope_tag,note,position) "
                "VALUES (?,?,?,?,?,?)",
                (profile_id, name, target, tag, note, pos),
            )


# The grade scale seeded for a new profile.
#
# The letter grades below are WCC's published quality-point table, confirmed
# against the college's own Academic Standing page and cross-checked against a
# real transcript (B on 3 credits = 9.000 points, B+ on 3 = 10.500, A on 4 =
# 12.000, D on 3 = 3.000, W contributing nothing at all). It is a uniform
# half-step scale: no minus grades, and notably no D+ - so none are seeded,
# because a phantom grade in the dropdown is a silent mis-entry waiting to
# happen. A wrong scale is the one input that can corrupt every number in the
# section while still looking plausible.
#
# The non-letter codes are left unverified: they are the usual registrar set
# rather than anything the transcript or the catalog page proved.
#
# points=None means "carries no quality points at all", which is a different
# thing from 0.0: an F is a real zero that drags the average down, a W is not
# in the average.
#   grade, points, counts_gpa, earns_credit, sort, verified
SEED_GRADE_SCALE = [
    ("A",  4.0, 1, 1,  0, 1),
    ("B+", 3.5, 1, 1,  1, 1),
    ("B",  3.0, 1, 1,  2, 1),
    ("C+", 2.5, 1, 1,  3, 1),
    ("C",  2.0, 1, 1,  4, 1),
    ("D",  1.0, 1, 1,  5, 1),
    ("F",  0.0, 1, 0,  6, 1),
    ("W",  None, 0, 0,  7, 1),   # withdrawal - attempted, not earned, not averaged
    ("I",  None, 0, 0,  8, 0),   # incomplete
    ("P",  None, 0, 1,  9, 0),   # pass (credit, no points)
    ("NP", None, 0, 0, 10, 0),   # no pass
    ("AU", None, 0, 0, 11, 0),   # audit
    ("TR", None, 0, 1, 12, 0),   # transfer credit
]

# The floors that actually gate the plan. Seeded so the section is useful the
# moment it opens rather than after a setup chore.
SEED_ACAD_GOALS = [
    ("a scholarship programme", 3.4, None,
     "Scholarship-for-Service minimum. The scarce resource - UB's transfer bar "
     "is far lower than this one."),
    ("UB CSE transfer", 2.8, None,
     "Threshold, not a ranked competition - clearing it is what matters."),
    ("UB core courses", 2.5, "ub-core",
     "Calculus 1, CSE 115, CSE 116, CSE 191. Fast-Track needs 3.0 over any two "
     "of them instead."),
]

DEFAULT_SETTINGS = {
    "theme_id": "midnight",
    "accent_override": None,
    "color_mode": "auto",
    "week_start": "monday",
    "timezone": "America/New_York",
    "enabled_modules": ["today", "boards", "calendar", "routines", "docs",
                        "tree", "thm", "growth", "chat", "health", "finance",
                        "academics", "printer", "govee", "homelab"],
    "notifications": {"routine_reminders": True, "reminder_time": "08:00",
                      "joint_activity": True},
}

# The partner profile is the same app minus the cybersecurity progression -
# proof that enabled_modules alone reskins a tab, no code fork required.
PARTNER_MODULES = ["today", "boards", "calendar", "routines", "docs", "health",
                   "finance", "academics"]
JOINT_MODULES = ["joint", "calendar", "boards", "routines", "docs"]

SEED_PROFILES = [
    ("primary", "primary", "You", 0, DEFAULT_SETTINGS["enabled_modules"]),
    ("partner", "partner", "Her", 1, PARTNER_MODULES),
    ("joint", "joint", "Us", 2, JOINT_MODULES),
]

# id, name, colors. Two dark, two light, one high-contrast, a few "fun".
SEED_THEMES = [
    ("midnight", "Midnight", {"bg": "#10141b", "surface": "#171d26", "surface_alt": "#1e2530",
        "border": "#2a3341", "primary": "#5b9bd5", "accent": "#e8a33d",
        "text": "#e7eaee", "text_muted": "#8994a3"}),
    ("slate", "Slate", {"bg": "#0d1117", "surface": "#161b22", "surface_alt": "#1c232c",
        "border": "#2d333b", "primary": "#4f8cff", "accent": "#c792ea",
        "text": "#e6edf3", "text_muted": "#8b949e"}),
    ("parchment", "Parchment", {"bg": "#f4f1ea", "surface": "#fbf9f4", "surface_alt": "#ece7db",
        "border": "#d9d2c3", "primary": "#9a6a3f", "accent": "#c2410c",
        "text": "#2b2620", "text_muted": "#7a7264"}),
    ("daylight", "Daylight", {"bg": "#ffffff", "surface": "#f6f8fa", "surface_alt": "#eef1f4",
        "border": "#d0d7de", "primary": "#2563eb", "accent": "#db2777",
        "text": "#1f2328", "text_muted": "#656d76"}),
    ("contrast", "High Contrast", {"bg": "#000000", "surface": "#0a0a0a", "surface_alt": "#141414",
        "border": "#3a3a3a", "primary": "#00e0ff", "accent": "#ffd400",
        "text": "#ffffff", "text_muted": "#b0b0b0"}),
    ("rose", "Rose Quartz", {"bg": "#1a1016", "surface": "#241620", "surface_alt": "#2f1d2a",
        "border": "#43293a", "primary": "#f472b6", "accent": "#fbbf24",
        "text": "#f6e9f0", "text_muted": "#b98fa8"}),
    ("forest", "Forest", {"bg": "#0f1512", "surface": "#161f1a", "surface_alt": "#1d2a23",
        "border": "#2a3a30", "primary": "#4ade80", "accent": "#facc15",
        "text": "#e7f0ea", "text_muted": "#8ba396"}),
    ("mono", "Monochrome", {"bg": "#121212", "surface": "#1c1c1c", "surface_alt": "#262626",
        "border": "#383838", "primary": "#e0e0e0", "accent": "#a0a0a0",
        "text": "#f0f0f0", "text_muted": "#909090"}),
    ("grape", "Grape Soda", {"bg": "#15101f", "surface": "#1f1730", "surface_alt": "#2a1f40",
        "border": "#3b2c56", "primary": "#a78bfa", "accent": "#f472b6",
        "text": "#ede9f5", "text_muted": "#9d8bc0"}),
    ("ocean", "Ocean", {"bg": "#0a1620", "surface": "#0f2030", "surface_alt": "#152b40",
        "border": "#1f3a52", "primary": "#38bdf8", "accent": "#2dd4bf",
        "text": "#e2f0f7", "text_muted": "#7fa6bd"}),
]

# A small starter bank for the rotating daily question. The API cycles
# through these by date so there's always one without needing an LLM.
SEED_DAILY_PROMPTS = [
    "What's something small I did recently that made you happy?",
    "If we could teleport anywhere for dinner tonight, where?",
    "What's a tiny goal you want to hit this week?",
    "What song are you secretly obsessed with right now?",
    "What's a memory of us you think about often?",
    "What would your perfect lazy Sunday look like?",
    "What's something you want to learn or try together?",
    "What made you laugh most this week?",
]

SEED_MILESTONES = [
    ("relationship_xp", 100, "First 100 together"),
    ("relationship_xp", 500, "Hitting your stride"),
    ("relationship_xp", 1000, "Four digits"),
    ("streak", 7, "One week streak"),
    ("streak", 30, "One month streak"),
    ("streak", 100, "Hundred day streak"),
]


def _seed_profiles(conn):
    """Idempotently seed profiles, their settings, themes, and joint singletons."""
    for pid, ptype, name, pos, modules in SEED_PROFILES:
        conn.execute(
            "INSERT OR IGNORE INTO profiles (id,type,display_name,position) VALUES (?,?,?,?)",
            (pid, ptype, name, pos),
        )
        if not conn.execute("SELECT 1 FROM profile_settings WHERE profile_id=?", (pid,)).fetchone():
            s = dict(DEFAULT_SETTINGS)
            s["enabled_modules"] = list(modules)
            if ptype == "partner":
                s["theme_id"] = "rose"
            elif ptype == "joint":
                s["theme_id"] = "grape"
            conn.execute(
                "INSERT INTO profile_settings (profile_id,settings) VALUES (?,?)",
                (pid, json.dumps(s)),
            )

    for tid, name, colors in SEED_THEMES:
        conn.execute(
            "INSERT OR IGNORE INTO themes (id,name,is_custom,owner_profile_id,colors) "
            "VALUES (?,?,0,NULL,?)",
            (tid, name, json.dumps(colors)),
        )

    conn.execute("INSERT OR IGNORE INTO companion (id) VALUES (1)")
    conn.execute("INSERT OR IGNORE INTO relationship_xp_config (id,weights) VALUES (1,?)",
                 (json.dumps({"routine_completion": 1.0, "joint_card_done": 5.0,
                              "streak_milestone": 20.0, "ping": 0.5, "interaction": 1.0}),))

    for mtype, threshold, label in SEED_MILESTONES:
        if not conn.execute(
            "SELECT 1 FROM milestones WHERE type=? AND threshold=?", (mtype, threshold)
        ).fetchone():
            conn.execute(
                "INSERT INTO milestones (type,threshold,label) VALUES (?,?,?)",
                (mtype, threshold, label),
            )


def _seed_partner_content(conn):
    """
    Give the partner profile something to look at: a board, a few routines,
    and her own starter tree. Only runs when she has none - once she edits
    or deletes any of it, this never fires again.
    """
    has_board = conn.execute(
        "SELECT COUNT(*) FROM boards WHERE profile_id='partner'"
    ).fetchone()[0]
    if has_board == 0:
        title, lists = PARTNER_BOARD
        cur = conn.execute(
            "INSERT INTO boards (title,position,profile_id) VALUES (?,0,'partner')", (title,)
        )
        for i, name in enumerate(lists):
            conn.execute(
                "INSERT INTO lists (board_id,title,position) VALUES (?,?,?)",
                (cur.lastrowid, name, i),
            )

    has_routines = conn.execute(
        "SELECT COUNT(*) FROM routines WHERE profile_id='partner'"
    ).fetchone()[0]
    if has_routines == 0:
        conn.executemany(
            "INSERT INTO routines (name,time_group,position,profile_id) VALUES (?,?,?,'partner')",
            PARTNER_ROUTINES,
        )



# The homelab inventory, seeded from the enumeration in the private
# `homelab-footprint` repo plus a live pass over the host and the LAN. Facts
# here were observed, not assumed - where something is unverified it says so
# in the text rather than being presented as spec.
#   name, kind, status, purpose, specs, hostname, lan_ip, ts_ip, mac,
#   probe_host, probe_port, notes
SEED_LAB_DEVICES = [
    ("Proxmox host (mini PC)", "server", "active",
     "The whole lab. Single-node Proxmox VE hypervisor running every service "
     "as an unprivileged LXC container.",
     "Lenovo mini PC 10T8SNBU00\n"
     "Intel i5-8500T - 6C/6T, 35W, 2.1GHz base / 3.5GHz turbo\n"
     "32GB DDR4 - 16GB SK Hynix (3200) + 16GB Samsung (2133)\n"
     "Samsung PM981 1TB NVMe - 4% wear, 82.9TB written, 29,898 power-on hours\n"
     "Proxmox VE 9.2.2 / Debian 13 trixie, UEFI + Secure Boot\n"
     "Intel I219-V onboard NIC (nic0 -> vmbr0)",
     "proxmox", "10.0.0.69", "100.64.0.10", "aa:bb:cc:00:00:01",
     "10.0.0.69", 8006,
     "35W part in a 1L chassis: it thermally throttles under sustained all-core "
     "load long before 3.5GHz. Both DIMMs run at 2133 because the faster stick "
     "clocks down to the slower one."),

    ("CT 100 - pihole", "guest", "active",
     "Network-wide DNS sinkhole. Ad and tracker blocking for every device on "
     "the LAN.",
     "Unprivileged LXC, Debian 12\n1 core / 512MB RAM / 8GB disk\n"
     "pihole-FTL, tailscaled\nListens 53 tcp+udp, 80, 443, 22",
     "pihole", "10.0.0.97", "100.64.0.12", "aa:bb:cc:00:00:02",
     "10.0.0.97", 80,
     "512MB is comfortable now but leaves little room if FTL query retention "
     "is ever extended."),

    ("CT 101 - opsdeck", "guest", "active",
     "Docker host for Ops Deck - this dashboard - and the mentor terminal "
     "sidecar.",
     "Unprivileged LXC, Debian 12, nesting=1\n1 core / 1GB RAM / 12GB disk\n"
     "Docker: opsdeck (5000), opsdeck-terminal (7681)\n"
     "Both bound to 127.0.0.1, published only via Tailscale Serve",
     "opsdeck", "10.0.0.213", "100.64.0.11", "aa:bb:cc:00:00:03",
     "10.0.0.213", 22,
     "The loopback-plus-Tailscale-Serve pattern is genuinely good design: "
     "nothing is exposed to the LAN and access inherits the tailnet identity "
     "model. Docker-in-unprivileged-LXC stacks two isolation layers that were "
     "not designed together, which is the tradeoff that makes it acceptable."),

    ("CT 102 - wazuh", "guest", "active",
     "Wazuh SIEM/XDR. Central log collection and alerting, with agents on the "
     "hypervisor and every guest.",
     "Unprivileged LXC, Debian 12\n4 cores / 8GB RAM / 60GB disk\n"
     "Wazuh 4.14.7 - dashboard on https://10.0.0.98\n"
     "Deployed 2026-08-05",
     "wazuh", "10.0.0.98", "", "aa:bb:cc:00:00:04",
     "10.0.0.98", 443,
     "Closed the largest gap in the baseline: before this there was no "
     "centralised logging, so the window between compromise and discovery was "
     "unbounded on a network that exposes a browser-accessible shell."),

    ("HP laptop 15 (workstation)", "laptop", "active",
     "Daily driver. Where Claude Code runs, where the Kali VM lives, and the "
     "machine everything else is administered from.",
     "HP laptop 15-fb3xxx\nRyzen 7 7445HS - 6C/12T Zen 4\n"
     "RTX 4050 Laptop 6GB + Radeon 740M iGPU\n"
     "2x8GB DDR5-5600 (both SODIMM slots full)\nWD_BLACK SN850X 2TB\n"
     "Realtek 8852BE Wi-Fi\nWindows 11 Home, VirtualBox + Tailscale",
     "workstation", "10.0.0.136", "100.64.0.14", "",
     "10.0.0.136", 0,
     "CPU and GPU are BGA-soldered - RAM and the M.2 are the only upgrade "
     "paths, and the board caps at 32GB @ DDR5-5600. Decided 2026-08-17 not to "
     "buy RAM during the DRAM shortage; tuning recovered idle free RAM from "
     "0.75GB to ~8GB instead."),

    ("Cyberdeck (Pi 5)", "sbc", "building",
     "Portable wireless/RF testing deck. Wi-Fi monitor mode, sub-GHz, SDR and "
     "GPS in one handheld unit.",
     "Raspberry Pi 5 8GB (rev 1.1, replacement board)\n"
     "X1200 battery/UPS HAT - I2C 0x36\n"
     "BBQ20KBD keyboard - I2C 0x1F, out-of-tree driver\n"
     "Adafruit Ultimate GPS - /dev/ttyAMA0 @ 9600\n"
     "CC1101 sub-GHz - SPI0 CE0\n"
     "Waveshare 4.3in DSI LCD 800x480, capacitive touch\n"
     "ASUS MT7612U USB Wi-Fi - wlan1, monitor mode verified\n"
     "Nooelec NESDR SMArt v5 - R820T, verified end to end",
     "cyberdeck", "", "100.64.0.13", "",
     "100.64.0.13", 22,
     "Offline on the tailnet for 9 days as of the last check - consistent with "
     "the rebuild. Two previous boards died to energized conductors meeting "
     "grounded USB-C shielding; there is deliberately no fusing, so continuity "
     "and visual inspection before power is the standing mitigation."),

    ("X870E build (unnamed)", "workstation", "building",
     "Undecided. Far and away the most capable silicon in the house and "
     "currently doing nothing - see the recommendations below.",
     "MSI MAG X870E Tomahawk (AM5)\n"
     "Ryzen 9 7900X - 12C/24T Zen 4, 170W TDP\n"
     "Corsair RM1000 - 1000W 80+ \n"
     "1x16GB DDR5 - single channel\n"
     "GTX 560 Ti - 2011 Fermi, placeholder\n"
     "No storage yet",
     "", "", "", "", "", 0,
     "The 7900X has twice the cores and roughly four times the multicore "
     "throughput of the i5-8500T currently running the entire lab, and it is "
     "not plugged into anything."),

    ("Elegoo Neptune 4 Plus", "printer", "active",
     "3D printer. Klipper firmware with the Fluidd web UI and a Moonraker API.",
     "Elegoo Neptune 4 Plus\nKlipper v0.10.0-530-g3387a9c2 on an MKS board\n"
     "Fluidd UI behind nginx on :80\nMoonraker API proxied on the same port\n"
     "mjpg-streamer webcam on :8080, 640x480",
     "", "10.0.0.131", "", "aa:bb:cc:00:00:05",
     "10.0.0.131", 80,
     "Already wired into this dashboard: see the Printer tab. Fluidd has no "
     "authentication of its own, so anything that can reach :80 can heat the "
     "nozzle or move the axes."),

    ("TP-Link 5-port switch", "network", "active",
     "Unmanaged gigabit switch. Port fan-out only - no VLANs, no mirroring, no "
     "management plane.",
     "TP-Link 5-port unmanaged gigabit\nNo IP, no configuration interface",
     "", "", "", "", "", 0,
     "Nothing to probe, which is not the same as being down. Being unmanaged "
     "is the single thing blocking both VLAN segmentation and any form of "
     "network IDS - there is no port to mirror traffic from."),

    ("Gateway / ISP router", "network", "active",
     "Edge router, DHCP server and the only thing between the LAN and the "
     "internet.",
     "10.0.0.1 - consumer gateway\nHands out all DHCP leases on the /24",
     "", "10.0.0.1", "", "aa:bb:cc:00:00:06",
     "10.0.0.1", 80,
     "All guests take DHCP leases from here rather than static assignments, so "
     "a lease change can move a service out from under anything referencing it "
     "by IP."),
]

# Recommendations. device_index of None means the item is about the lab as a
# whole. Severities are rated for this environment - a single-node homelab on
# a trusted residential LAN with remote access already behind Tailscale.
#   device_index, title, detail, category, severity, cost, status
SEED_LAB_UPGRADES = [
    (None, "No backup jobs exist - single non-redundant NVMe",
     "/etc/pve/jobs.cfg still does not exist. All three containers, the "
     "hypervisor root and /boot/efi sit on one Samsung PM981 with no mirror "
     "and no second copy anywhere. Drive failure is total loss. A vzdump job "
     "for all three CTs to `local` is the ten-minute version and worth doing "
     "today; it lands on the same disk, so follow it with replication "
     "off-box. Rated high because it is the failure most likely to actually "
     "happen and the least recoverable.",
     "reliability", "high", "free (job) / $60+ (external disk)", "idea"),

    (None, "Proxmox firewall is disabled datacenter-wide",
     "pve-firewall reports disabled/running. Every guest NIC carries "
     "firewall=1 and Proxmox has built the fwbr bridge chain for each - so the "
     "config looks like it enforces per-guest policy and enforces nothing. "
     "Write host rules FIRST, including an explicit allow for 22 and 8006 from "
     "the tailnet (100.64.0.0/10) and the LAN, before enabling. Turning it on "
     "with a default DROP and no rules locks you out of your own hypervisor.",
     "security", "high", "free", "idea"),

    (None, "SSH permits root login with a password, on the LAN",
     "sshd -T still reports permitrootlogin yes and passwordauthentication "
     "yes, answering on 0.0.0.0. Any device on the /24 - including IoT, the "
     "printer, or anything that compromises the gateway - can attempt "
     "unlimited password guesses against uid 0. Key auth already works. Set "
     "PasswordAuthentication no and PermitRootLogin prohibit-password, and "
     "verify a key session survives before closing the one you are in.",
     "security", "high", "free", "idea"),

    (None, "Managed switch - the purchase that unblocks two other things",
     "The current switch is unmanaged, so there is no port mirroring and no "
     "VLAN support. That single fact blocks both network IDS and any "
     "segmentation. A TP-Link TL-SG108E or similar 8-port smart switch is "
     "roughly $30 and provides 802.1Q VLANs plus port mirroring. This is the "
     "cheapest item on this list with the largest unlock.",
     "capability", "medium", "~$30", "idea"),

    (None, "Network IDS - realistic only after the switch",
     "Wazuh already gives host-based detection: file integrity monitoring, log "
     "analysis and rootcheck on every machine. What is missing is network "
     "visibility. Suricata or Zeek needs to see traffic, which on a flat "
     "network with an unmanaged switch it cannot - a host only sees its own "
     "frames. Sequence: managed switch, mirror the uplink port, run Suricata "
     "in a new LXC, ship its alerts into Wazuh. Doing it before the switch "
     "means watching one host talk to itself.",
     "security", "medium", "free after the switch", "idea"),

    (None, "Segment IoT off the trusted LAN",
     "The printer and the Govee bulb sit on the same flat /24 as the "
     "hypervisor and the SIEM. Fluidd has no auth at all, so anything that "
     "reaches it can drive the printer. Once a VLAN-capable switch is in, put "
     "the printer, the bulb and anything else that phones home on their own "
     "VLAN with no route to the management segment.",
     "security", "medium", "free after the switch", "idea"),

    (None, "123 pending package updates on the hypervisor",
     "Was 115 at the August baseline, now 123. Nothing here is urgent on its "
     "own; the number only grows, and a large backlog turns a routine security "
     "patch into a risky bulk upgrade. Snapshot or back up first, then apt "
     "full-upgrade.",
     "security", "medium", "free", "idea"),

    (None, "Management interfaces answer on the LAN, not just the tailnet",
     "pveproxy (8006) and spiceproxy (3128) listen on all interfaces, as does "
     "sshd. Every human path into the box already goes over Tailscale, so LAN "
     "exposure is surface with no corresponding use. Bind them to the tailnet "
     "interface, or cover them with the firewall rules above.",
     "security", "medium", "free", "idea"),

    (None, "Single root@pam account, no 2FA",
     "One administrative identity, no second factor, and it is the same "
     "account used for day-to-day work. Proxmox supports TOTP natively. A "
     "second named admin account plus TOTP on both costs nothing.",
     "security", "medium", "free", "idea"),

    (None, "rpcbind listening on 0.0.0.0:111",
     "Nothing on this box uses NFS. rpcbind is a historically noisy service to "
     "leave reachable and serves no purpose here - apt purge rpcbind, or mask "
     "it if something pulls it in as a dependency.",
     "security", "low", "free", "idea"),

    (6, "Decide what the 7900X is for - this gates every other purchase",
     "A 12C/24T Zen 4 chip on an X870E board with a 1000W PSU is sitting idle "
     "while a 6C/6T 35W laptop chip runs the entire lab. The decision matters "
     "financially, not just architecturally: as a headless Proxmox host it "
     "needs NO graphics card at all - the GTX 560 Ti is perfectly adequate for "
     "POST and a console you will never look at - so the single most expensive "
     "line item disappears. As a gaming or AI workstation, the GPU becomes the "
     "dominant cost and the 560 Ti is unusable (Fermi: no modern CUDA, no AV1 "
     "or HEVC encode).",
     "capability", "high", "free (a decision)", "idea"),

    (6, "If it becomes the host: migrate Proxmox, demote the mini PC",
     "This solves several open findings at once and is the cheapest path to "
     "all of them. The 7900X takes over as hypervisor; the mini PC - which "
     "already has 32GB and a healthy 1TB NVMe - becomes the backup target, "
     "which closes the no-backups finding without buying storage. You would "
     "need: a boot drive for the new box, and ideally a second one to mirror. "
     "No GPU, no new RAM strictly required to start. It also ends the 35W "
     "thermal ceiling and the 2133MT/s DIMM mismatch in one move.",
     "reliability", "high", "storage only", "idea"),

    (6, "Second RAM stick - single channel is halving your bandwidth",
     "One 16GB DDR5 stick runs single-channel. A matched second stick roughly "
     "doubles memory bandwidth, which a 12-core chip feels more than most. "
     "This is the cheapest real performance gain available - but note DDR5 is "
     "badly inflated in the current shortage (32GB kits were $389-500 in "
     "August), so buy a single matching 16GB stick rather than a new kit.",
     "performance", "medium", "1x16GB DDR5", "idea"),

    (6, "Storage - nothing to boot from yet",
     "The build has no drive. If it becomes the hypervisor, two modest NVMe "
     "drives in a ZFS mirror beats one large one: it removes the single-disk "
     "failure mode that is currently the lab's biggest risk. If it becomes a "
     "workstation, one drive is fine.",
     "capacity", "high", "2x NVMe", "idea"),

    (0, "Mismatched DIMMs cost you memory bandwidth for free",
     "The SK Hynix stick is rated 3200 MT/s but both run at 2133, clocked down "
     "to the slower Samsung module. Replacing the Samsung stick with a 3200 "
     "part recovers the difference at no other cost. Low priority while DDR4 "
     "prices are also inflated, and moot if the lab migrates to the 7900X.",
     "performance", "low", "1x16GB DDR4-3200", "idea"),

    (0, "No ECC memory",
     "Error correction type is None. Acceptable for a homelab and not worth "
     "chasing on this platform - but it is the single biggest gap between this "
     "box and something you would trust with irreplaceable data. Worth knowing "
     "when deciding where backups ultimately live.",
     "reliability", "low", "platform change", "idea"),

    (5, "Cyberdeck has been offline 9 days - finish or park it deliberately",
     "Two boards have already died to shorts. There is no fusing anywhere in "
     "the build by deliberate choice, so continuity and visual inspection "
     "before applying power is the only thing standing between the third board "
     "and the first two. If the rebuild is stalled, park it explicitly rather "
     "than leaving it half-wired.",
     "reliability", "medium", "free", "idea"),

    (7, "Printer has no authentication and sits on the trusted LAN",
     "Fluidd and Moonraker expose full machine control with no login. Anything "
     "on the /24 can heat the nozzle, move the axes or start a job. The "
     "tailnet HTTPS front added for the Printer tab does not change this - the "
     "LAN path is still wide open. Covered by the IoT segmentation item above.",
     "security", "medium", "free after the switch", "idea"),

    (1, "Pi-hole has 512MB and no headroom for longer retention",
     "Fine at the current query volume. If you ever extend FTL retention to "
     "get useful historical DNS data - which is genuinely valuable next to a "
     "SIEM - it will need more memory first.",
     "capacity", "low", "free (config)", "idea"),

    (None, "Guests use DHCP, not static assignments",
     "All three containers take leases from the gateway. Anything that "
     "references them by IP - and several things do - breaks quietly if a "
     "lease moves. Either reserve the addresses on the gateway or set static "
     "IPs on the containers.",
     "reliability", "low", "free", "idea"),
]


def _seed_homelab(conn, profile_id="primary"):
    """Seed the inventory once. No-ops as soon as the profile has devices."""
    n = conn.execute("SELECT COUNT(*) FROM lab_devices WHERE profile_id=?",
                     (profile_id,)).fetchone()[0]
    if n:
        return
    ids = []
    for pos, row in enumerate(SEED_LAB_DEVICES):
        (name, kind, status, purpose, specs, hostname, lan, ts, mac,
         phost, pport, notes) = row
        cur = conn.execute(
            "INSERT INTO lab_devices (profile_id,name,kind,status,purpose,specs,"
            "hostname,lan_ip,tailscale_ip,mac,probe_host,probe_port,notes,position) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (profile_id, name, kind, status, purpose, specs, hostname, lan, ts,
             mac, phost, pport, notes, pos))
        ids.append(cur.lastrowid)

    for pos, (idx, title, detail, cat, sev, cost, st) in enumerate(SEED_LAB_UPGRADES):
        conn.execute(
            "INSERT INTO lab_upgrades (profile_id,device_id,title,detail,category,"
            "severity,cost,status,position) VALUES (?,?,?,?,?,?,?,?,?)",
            (profile_id, ids[idx] if idx is not None else None,
             title, detail, cat, sev, cost, st, pos))


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fresh = not DB_PATH.exists()

    conn = connect()
    conn.executescript(SCHEMA)

    if fresh:
        conn.executescript(SEED)

    _migrate(conn)
    _seed_profiles(conn)   # safe on every start; all inserts are OR IGNORE

    # Each profile owns its own attributes and tree (v6). Both calls no-op
    # once that profile has content.
    _seed_growth(conn, "primary")
    _seed_growth(conn, "partner", PARTNER_ATTRIBUTES, PARTNER_NODES, PARTNER_EDGES)
    _seed_partner_content(conn)

    # Finance categories and the grade scale are per-profile like the trees;
    # both no-op once that profile has content.
    for row in conn.execute("SELECT id, type FROM profiles").fetchall():
        _seed_finance(conn, row["id"])
        if row["type"] != "joint":
            _seed_academics(conn, row["id"], with_goals=row["type"] == "primary")
        if row["type"] == "primary":
            _seed_homelab(conn, row["id"])

    conn.commit()
    conn.close()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]
