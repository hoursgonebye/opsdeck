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

SCHEMA_VERSION = 6

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


DEFAULT_SETTINGS = {
    "theme_id": "midnight",
    "accent_override": None,
    "color_mode": "auto",
    "week_start": "monday",
    "timezone": "America/New_York",
    "enabled_modules": ["today", "boards", "calendar", "routines", "docs",
                        "tree", "thm", "growth", "chat"],
    "notifications": {"routine_reminders": True, "reminder_time": "08:00",
                      "joint_activity": True},
}

# The partner profile is the same app minus the cybersecurity progression -
# proof that enabled_modules alone reskins a tab, no code fork required.
PARTNER_MODULES = ["today", "boards", "calendar", "routines", "docs"]
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

    conn.commit()
    conn.close()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]
