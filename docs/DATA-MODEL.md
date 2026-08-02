# Data model

One SQLite file: `data/opsdeck.db`. 40 tables. Schema is created on startup
if missing and migrated forward by `SCHEMA_VERSION` in `db.py`, so new
tables and columns are added without wiping anything.

Conventions used throughout:
- **Dates** are ISO strings. `local_date` columns are `YYYY-MM-DD` anchored
  to `OPSDECK_TZ`; `*_at` columns are `YYYY-MM-DD HH:MM:SS`.
- **Booleans** are `INTEGER` 0/1.
- **`profile_id`** is a text FK to `profiles(id)`.
- **JSON blobs** are stored as `TEXT` where the shape should be free to grow
  without a migration (settings, theme colours, proposal actions).

---

## Entity map

```
profiles ──┬── profile_settings (1:1, JSON blob)
           ├── boards ── lists ── cards ─┬─ checklist_items
           │                             ├─ card_labels ── labels
           │                             └─ uploads
           ├── events ── event_overrides
           ├── routines ── routine_completions
           ├── docs ── doc_tags
           ├── quick_notes
           └── notifications

skill_nodes ─┬─ node_weights ── attributes
             ├─ skill_edges (from_id, to_id)
             ├─ skill_levels        ← THE LEDGER
             └─ levelup_attempts ── docs (evidence_doc)

thm_rooms ──┬── thm_completions
            └── thm_room_nodes ── skill_nodes

activity_events   ← THE JOINT LEDGER
   ├─► relationship XP  (SUM of weight)
   ├─► companion        (xp // stage threshold)
   └─► milestones       (threshold crossings)

themes, ai_proposals, settings   (standalone)
```

The two **ledgers** (`skill_levels`, `activity_events`) are the heart of the
system — see [ARCHITECTURE §2](ARCHITECTURE.md#2-derived-state-nothing-is-a-stored-counter).

---

## Core content

### profiles
| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `primary`, `partner`, `joint` |
| `type` | TEXT | `primary` \| `partner` \| `joint` |
| `display_name` | TEXT | Shown on the switcher |
| `avatar_url` | TEXT | Nullable |
| `position` | INT | Tab order |

### profile_settings
| Column | Type | Notes |
|---|---|---|
| `profile_id` | TEXT PK | FK → profiles |
| `settings` | TEXT | JSON. `theme_id`, `enabled_modules`, `week_start`, `notifications`, … |

Stored as a blob deliberately: the settings shape changes often and a
column-per-setting would mean a migration each time. `PATCH` deep-merges.

### boards → lists → cards
| Table | Key columns |
|---|---|
| `boards` | `title`, `position`, `archived`, **`profile_id`** |
| `lists` | `board_id`, `title`, `position`, `archived` |
| `cards` | `list_id`, `title`, `description`, `due_at`, `completed`, `completed_at`, `position`, `archived` |
| `checklist_items` | `card_id`, `text`, `done`, `done_at`, `position` |
| `labels` | `board_id`, `name`, `color` |
| `card_labels` | `(card_id, label_id)` join |
| `uploads` | `card_id`, `filename`, `stored_as`, `mime`, `size` |

**Only `boards` carries `profile_id`.** Lists and cards inherit scope through
their parent — a redundant column would make it possible for a card's
profile to disagree with its board's. Queries join up to `boards` to filter.

`completed_at` and `done_at` exist so XP knows *when* work happened. Both are
nulled when un-completing, so toggling can't mint XP repeatedly.

### events / event_overrides
| Column | Notes |
|---|---|
| `start_at`, `end_at` | ISO datetimes |
| `all_day` | 0/1 |
| `rrule` | RFC 5545 string, nullable |
| `remind_min` | Minutes before, nullable |
| `profile_id` | Scoping |

`event_overrides(event_id, occurrence, action, new_start_at, new_end_at,
new_title)` lets a single occurrence be skipped or moved without forking the
series. `occurrence` is the original date key.

### routines / routine_completions
`routines(name, time_group, notes, position, active, profile_id)`.
`routine_completions(routine_id, local_date, completed_at)` with a unique
constraint on `(routine_id, local_date)`.

Nothing is destroyed at midnight: a new day simply has no rows yet. Streaks
walk backwards through the dates, and today being incomplete doesn't zero a
live streak.

### docs / doc_tags
`docs(title, kind, body, folder, created_at, updated_at, profile_id)` where
`kind` is `md` or `html`. Bodies live in the DB, not the filesystem, so a
backup of the `.db` is genuinely complete.

### quick_notes
| Column | Notes |
|---|---|
| `body` | The captured text |
| `status` | `pending` \| `filed` \| `dismissed` |
| `suggestion` | JSON — the local heuristic's guess, including `confident` |
| `filed_as` | Human-readable record of where it went |
| `profile_id` | Everything it spawns inherits this |

### notifications
`(profile_id, source_type, title, body, link, seen, created_at)`.
`source_type` ∈ `mailbox`, `ping`, `wall`, `milestone`, `relationship_xp`,
`countdown`.

---

## Growth system

### attributes
`(key, name, color, position)` — 8 rows: `networking`, `linux`, `pentest`,
`defense`, `crypto`, `grc`, `code`, `web`. Deliberately few; the tree carries
the detail, these carry the *shape*.

### skill_nodes
| Column | Notes |
|---|---|
| `title`, `description`, `domain` | 12 domains |
| `x`, `y` | Free canvas coords — the tree is allowed to be disconnected |
| `level`, `max_level` | Current / ceiling (default 5) |
| `tier` | 1–5 depth; drives XP value and difficulty |
| `unlock_attr`, `unlock_value` | Gate: locked until that attribute crosses the threshold |

`level` is a **cache of the ledger**, updated only by `grant_level()`. The
ledger is authoritative.

### node_weights
`(node_id, attribute_key, weight)` — composite PK. One node can feed several
attributes at different weights, because real skills don't map cleanly onto
one stat (e.g. *Python for security* → `code 0.7` + `pentest 0.6`).

### skill_levels — the ledger
```sql
skill_levels(id, node_id, level, attempt_id, earned_at, local_date)
```
**One row per level ever gained.** Never updated, never deleted. Every
attribute value and every week's XP is a query over this table joined to
`node_weights`. This is why editing weights retroactively fixes history.

### levelup_attempts
`(node_id, target_level, difficulty, status, questions, answers,
evidence_doc, feedback, room_code, created_at, resolved_at)`.

`status` moves `awaiting_questions → awaiting_answer → grading → granted |
rejected`. `questions`/`answers` are JSON arrays. `evidence_doc` FKs to
`docs` and is required at open time.

---

## TryHackMe

- `thm_rooms(code PK, title, difficulty, tags, description, fetched_at)`
- `thm_completions(room_code UNIQUE, local_date, source)` — `source` is
  `manual` or `sync`
- `thm_room_nodes(room_code, node_id)` — which tree nodes a room can feed

A completion carries **zero XP**. It only opens the door to a verification
on each mapped node.

---

## Joint layer

### activity_events — the joint ledger
```sql
activity_events(id, profile_id, source_type, source_id, weight, created_at)
```
`source_type` ∈ `routine_completion`, `joint_card_done`, `streak_milestone`,
`ping`, `interaction`.

Three different features read this one table:

| Derived value | Query |
|---|---|
| Relationship XP | `SUM(weight)` |
| Relationship level | `floor(sqrt(xp / 50))` |
| Companion stage | `xp // 150`, capped at 6 |
| Milestone crossings | compare `SUM(weight)` / best streak against thresholds |

`relationship_xp_config(id=1, weights)` holds a JSON map of
`source_type → multiplier`, so the balance is tunable without a migration.

### Feature tables

| Table | Columns |
|---|---|
| `mailbox_messages` | `from_profile_id`, `to_profile_id` (null = both), `body`, `deliver_at`, `delivered` |
| `wall_posts` | `profile_id`, `type` (`text`/`image`/`link`), `content`, `caption` |
| `wall_reactions` | `(post_id, profile_id, emoji)` composite PK — one of each emoji per person |
| `date_ideas` | `created_by`, `title`, `description`, `tags` (JSON), `status`, `planned_date` |
| `bucket_list_items` | `title`, `category`, `status`, `completed_at` |
| `countdowns` | `label`, `target_date`, `recurring` |
| `song_of_day` | `profile_id`, `track_title`, `track_url`, `note`, `local_date` |
| `daily_prompts` | `prompt_text`, `local_date` UNIQUE |
| `prompt_answers` | `(prompt_id, profile_id)` UNIQUE, `answer`, `answered_at` |
| `milestones` | `type`, `threshold`, `label`, `celebrated` |
| `companion` | Singleton (`id=1`): `species_or_skin`, `growth_stage`, `xp`, `last_interacted_at` |

Singletons use `CHECK (id = 1)` so a second row is impossible.

---

## Standalone

- **`themes`** — `(id, name, is_custom, owner_profile_id, colors JSON)`.
  10 built-ins seeded with `is_custom=0`; those are immutable via the API.
- **`ai_proposals`** — `(kind, title, rationale, actions JSON, status)`.
  Nothing is applied until approved.
- **`settings`** — key/value; holds `schema_version` and the THM username.

---

## Migrations

`db.py` runs `init_db()` on every start:

```python
conn.executescript(SCHEMA)   # CREATE TABLE IF NOT EXISTS — new tables appear
if fresh: conn.executescript(SEED)
_migrate(conn)               # versioned ALTERs and backfills
_seed_growth(conn)           # attributes + starter tree, only if empty
_seed_profiles(conn)         # profiles/themes/singletons, all INSERT OR IGNORE
```

Every step is idempotent. Running it repeatedly is safe — that's the whole
design, since it runs on every container start.

### Version history

| Version | Change |
|---|---|
| 1 | Initial: boards, calendar, routines, docs |
| 2 | Added `cards.completed_at` and `checklist_items.done_at`; **backfilled** existing completions so old work wasn't worth zero XP |
| 3 | TryHackMe tables; `levelup_attempts.room_code` |
| 4 | `quick_notes` |
| 5 | **Profiles.** Added `profile_id` to boards/events/routines/docs/quick_notes with `DEFAULT 'primary'` and an index each; created profiles, settings, themes, notifications, and all 11 joint tables; seeded 3 profiles, 10 themes, 6 milestones, and the singletons |
| 6 | **Per-profile growth.** Added `profile_id` to `skill_nodes`, `attributes`, `levelup_attempts`, `ai_proposals`, `thm_completions`. Each profile now has its own tree, its own stat set, and its own verification queue. Seeded the partner a 20-node non-technical starter tree with six attributes of her own, plus a board and routines |

The v5 migration is additive only. Every pre-existing row backfills to
`primary`, so nothing moves or disappears — verified on the live database:
row counts for boards, events, routines, docs, cards and skill_nodes were
identical before and after.

### Adding a migration

1. Add the table/column to `SCHEMA` (as `CREATE TABLE IF NOT EXISTS`).
2. Bump `SCHEMA_VERSION`.
3. Add an `if current < N:` block in `_migrate()` for any `ALTER` or backfill
   that `CREATE TABLE IF NOT EXISTS` can't express.
4. Guard every write so a re-run is harmless (`_columns()` check, `INSERT OR
   IGNORE`, or an existence query).

**Back up first.** `docker compose down && cp data/opsdeck.db data/backup.db`,
or see [DEPLOY.md](DEPLOY.md#backups).
