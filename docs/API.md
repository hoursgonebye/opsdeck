# API reference

Everything the browser UI does goes through these endpoints (~170 of them)
— there is no private API. If you can do it by clicking, you can do it from
a script.

---

## Authentication

Every request needs the token:

```bash
curl -H "X-API-Token: $OPSDECK_TOKEN" https://opsdeck.example.ts.net/api/boards
```

`?token=...` also works, for `<img>`/`<a>` style links (attachments use it),
but prefer the header.

Errors come back as `{"error": "..."}` with a 4xx/5xx status.

| Status | Meaning |
|---|---|
| `401` | Bad or missing token |
| `500` | `OPSDECK_TOKEN` is not configured server-side — the API refuses rather than running open |

## Profile scoping

Content endpoints are scoped by the active profile:

```bash
curl -H "X-API-Token: $TOKEN" -H "X-Profile-Id: partner" .../api/boards
```

- Valid values come from `GET /api/profiles` (`primary`, `partner`, `joint`).
- **Omitted or unknown → `primary`.** Pre-v5 scripts keep working unchanged.
- Scoped resources: boards (and their lists/cards/labels), events, routines,
  docs, quick notes, notifications, health metrics, `/today`, `/search`,
  `/reminders/upcoming`, and — since v6 — **the skill tree, attributes, XP,
  level-up attempts, proposals and TryHackMe completions**. Each profile has
  its own tree and its own mentor queue.
- **Not** scoped: everything under `/api/joint`, which is household-wide by
  definition and ignores the header.

---

## Quick reference

### Profiles, settings, themes

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/profiles` | All profiles: `id`, `type`, `display_name`, `avatar_url`, `position` |
| `PATCH` | `/api/profiles/{id}` | `display_name`, `avatar_url` |
| `GET` | `/api/profiles/{id}/settings` | Settings object (see below) |
| `PATCH` | `/api/profiles/{id}/settings` | **Deep-merges** — send only what changes |
| `GET` | `/api/themes` | All themes, built-in and custom |
| `GET` | `/api/themes/{id}` | One theme |
| `POST` | `/api/themes` | Create custom: `{name, colors}` |
| `PATCH`/`DELETE` | `/api/themes/{id}` | Custom only — built-ins return `403` |

Settings object:

```json
{
  "theme_id": "midnight",
  "accent_override": null,
  "color_mode": "auto",
  "week_start": "monday",
  "timezone": "America/New_York",
  "enabled_modules": ["today","boards","calendar","routines","docs","tree","thm","growth","chat","health"],
  "notifications": {"routine_reminders": true, "reminder_time": "08:00", "joint_activity": true}
}
```

`enabled_modules` drives the sidebar. Valid keys: `today`, `boards`,
`calendar`, `routines`, `docs`, `tree`, `thm`, `growth`, `chat`, `health`,
`joint`.

Theme `colors`: `bg`, `surface`, `surface_alt`, `border`, `primary`,
`accent`, `text`, `text_muted`.

### Boards

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/boards` | Nested: boards → lists → cards → labels/checklist/attachments. `?archived=1` includes archived |
| `POST` | `/api/boards` | `title`, optional `lists: [...]` (defaults To do / In progress / Done) |
| `PATCH`/`DELETE` | `/api/boards/{id}` | `title`, `position`, `archived` |
| `POST` | `/api/lists` | `board_id`, `title` |
| `PATCH`/`DELETE` | `/api/lists/{id}` | `title`, `position`, `archived` |
| `POST` | `/api/cards` | `list_id`, `title`, `description`, `due_at`, `label_ids`, `checklist` |
| `PATCH`/`DELETE` | `/api/cards/{id}` | Any field, plus `label_ids` |
| `POST` | `/api/cards/reorder` | `{list_id, card_ids: [...]}` — sets order, moves between lists |
| `POST` | `/api/cards/{id}/checklist` | `text` |
| `PATCH`/`DELETE` | `/api/checklist/{id}` | `text`, `done`, `position` |
| `POST` | `/api/labels` | `board_id`, `name`, `color` |
| `DELETE` | `/api/labels/{id}` | |
| `POST` | `/api/cards/{id}/attachments` | multipart `file` |
| `GET`/`DELETE` | `/api/attachments/{id}` | Download / remove |

Setting `completed: 1` stamps `completed_at` (XP needs to know *when*).
Clearing it nulls the stamp, so un/re-completing can't mint XP repeatedly.
Completing a card on the **joint** board also logs an `activity_event`.

### Calendar

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/events` | Raw series rows |
| `GET` | `/api/events?start=&end=` | **Expanded occurrences** in the window |
| `POST` | `/api/events` | `title`, `start_at`, `end_at`, `rrule`, `all_day`, `color`, `remind_min`, `location` |
| `PATCH`/`DELETE` | `/api/events/{id}` | Deleting removes the whole series |
| `POST` | `/api/events/{id}/occurrences/{date}` | `{action:"skip"}` or `{action:"move", new_start_at, new_title}` |
| `DELETE` | `/api/events/{id}/occurrences/{date}` | Undo an override |

`rrule` is standard RFC 5545, expanded server-side by `python-dateutil`:

```
RRULE:FREQ=DAILY
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;COUNT=6      every other Tuesday, 6 times
RRULE:FREQ=MONTHLY;BYDAY=2TU                       second Tuesday monthly
```

### Calendar feeds

Read-only `.ics` subscriptions — a work roster, a class timetable. Imported
events live in the normal `events` table tagged with `feed_id`, so they
appear on Today, the month grid and the merged Us view without those needing
to know feeds exist.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/calendar/feeds` | Subscriptions with `event_count`, `last_synced_at`, `next_sync_at` and `auto_sync_minutes`. **Never returns the URL** — only `url_host` |
| `POST` | `/api/calendar/feeds` | `{name, url, color}`. Syncs immediately; a feed that can't be read is rejected `400` and not saved |
| `POST` | `/api/calendar/feeds/{id}/sync` | Re-sync one |
| `POST` | `/api/calendar/feeds/sync-all` | Re-sync every enabled feed |
| `PATCH`/`DELETE` | `/api/calendar/feeds/{id}` | `name`, `color`, `enabled`. Deleting removes its imported events too |

**Feeds refresh themselves.** A background sweeper in the app re-syncs any
enabled feed once it is `OPSDECK_FEED_SYNC_MINUTES` old (default 60), across
*every* profile — `sync-all` is scoped to the active profile, so it is not
what runs on the timer. The endpoints above stay as the manual override, and
each row reports `next_sync_at` so the UI can say when the next one is due.
Set the variable to `0` to switch the sweeper off.

`last_synced_at` and `next_sync_at` are **UTC** (SQLite's `datetime('now')`),
unlike the local wall-clock times used elsewhere in the API. Convert before
displaying.

A sync that fails still stamps `last_synced_at`, with the reason in
`last_status`. That is deliberate: it backs a dead feed off to one attempt
per interval instead of one per tick.

**Feed URLs are bearer credentials.** Anyone holding one can read the
calendar, so the URL is stored server-side and never sent back to the
browser.

**Identity is a content hash, not the UID.** Feeds exist that regenerate
every `UID` on every request — the Kronos roster this was built against
embeds the request timestamp — so deduplicating on UID would produce a full
set of duplicates on every sync. The hash covers start, end and title, which
are stable by construction.

**A sync replaces rather than merges** the feed's events across the span the
feed covers. Shifts get cancelled and moved, not just added, so a cancelled
one has to be able to disappear.

Times are converted to `OPSDECK_TZ` on import: `DTSTART:20260803T200000Z`
becomes `2026-08-03T16:00:00` in `America/New_York`. Date-only values
(`20260814`) import as all-day events.

### Routines

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/routines?date=` | With `done_today` and `streak` |
| `POST` | `/api/routines` | `name`, `time_group` (`morning`/`afternoon`/`evening`/`anytime`), `notes` |
| `PATCH`/`DELETE` | `/api/routines/{id}` | `name`, `time_group`, `notes`, `active` |
| `POST` | `/api/routines/{id}/toggle` | `{date}` — flips completion, returns new streak, logs an activity event |
| `GET` | `/api/routines/history?days=30` | Per-day completion counts |

### Docs

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/docs` | Add `?body=1` to include content |
| `GET` | `/api/docs/{id}` | One doc with body and tags |
| `POST` | `/api/docs` | `title`, `kind` (`md`/`html`), `body`, `folder`, `tags` |
| `PATCH`/`DELETE` | `/api/docs/{id}` | |
| `POST` | `/api/docs/upload` | multipart `file` (+ `title`, `folder`) |

### Quick capture

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/notes/quick` | `{body, file_now?}` — stores instantly, attaches a heuristic suggestion |
| `GET` | `/api/notes/quick?status=` | `pending` \| `filed` \| `dismissed` \| `all` |
| `POST` | `/api/notes/quick/{id}/file` | Apply a plan; empty body accepts the stored suggestion |
| `DELETE` | `/api/notes/quick/{id}` | Dismiss |

`file_now: true` files immediately **only when the guess is confident**. If
the board match scored zero the note stays `pending` — see
[ARCHITECTURE §7](ARCHITECTURE.md#7-quick-capture-and-honest-uncertainty).

Filing plan: `{kind, title, due, list_id, folder, time_group}` where `kind`
is `card` | `event` | `doc` | `routine`.

### Views, search, notifications

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/today` | Events, routines, cards due + overdue for the day |
| `GET` | `/api/search?q=` | Cards, docs, events. `&scope=mine\|joint\|all` |
| `GET` | `/api/reminders/upcoming?minutes=` | Reminders due to fire soon |
| `GET` | `/api/notifications` | `?unseen=1` to filter |
| `POST` | `/api/notifications/{id}/seen` | |
| `POST` | `/api/notifications/seen-all` | |
| `GET` | `/api/context` | Everything at once, for agents |

### Growth, tree, verification

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/xp?weeks=` | Weekly XP with per-source breakdown + trailing average |
| `GET` | `/api/attributes?weeks=` | Current values + history |
| `POST` | `/api/attributes` | `key`, `name`, `color` |
| `PATCH`/`DELETE` | `/api/attributes/{id}` | |
| `GET` | `/api/tree` | Nodes, edges, weights, lock state, attributes |
| `POST` | `/api/tree/nodes` | `title`, `domain`, `x`, `y`, `tier`, `weights`, `parents` |
| `PATCH`/`DELETE` | `/api/tree/nodes/{id}` | Any field, plus `weights` |
| `POST` | `/api/tree/edges` | `from_id`, `to_id` |
| `DELETE` | `/api/tree/edges/{id}` | |
| `GET` | `/api/tree/nodes/{id}/levelup/preview` | The bar *before* attempting: difficulty, notes floor, rejection count |
| `POST` | `/api/tree/nodes/{id}/levelup` | **Opens an attempt** — requires `evidence_doc`, never grants |
| `GET` | `/api/attempts?status=` | Attempts needing mentor work |
| `GET` | `/api/attempts/{id}` | One attempt with full context |
| `POST` | `/api/attempts/{id}/questions` | Mentor supplies `{questions: [...]}` |
| `POST` | `/api/attempts/{id}/answer` | User submits `{answers: [...]}` |
| `POST` | `/api/attempts/{id}/verdict` | `{granted, feedback, suggested_nodes}` |
| `DELETE` | `/api/attempts/{id}` | Cancel — the "not ready" path, not counted as failure |

`weights` is `[{"attribute_key": "pentest", "weight": 1.0}, ...]`.
`parents` is a list of node ids; edges are created automatically.

There is **no endpoint that sets a level directly.** By design.

### TryHackMe

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/thm` | Username, completions with node mappings, stored recommendation |
| `PATCH` | `/api/thm/settings` | `username` |
| `POST` | `/api/thm/sync` | Best-effort public-profile pull |
| `POST` | `/api/thm/completions` | `room_code`, `title`, `date` |
| `DELETE` | `/api/thm/completions/{id}` | |
| `POST`/`DELETE` | `/api/thm/rooms/{code}/nodes` | `node_id` — map/unmap |
| `GET`/`POST` | `/api/thm/recommend` | GET returns context for agents; POST refreshes or stores one |

### Mentor

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/mentor/status` | Direct mode on/off, pending counts |
| `GET`/`POST` | `/api/mentor/briefing` | The daily digest (profile-scoped). GET returns the latest; POST regenerates now. Written nightly at `OPSDECK_BRIEFING_TIME` into Docs → Briefings — **deterministic, no model calls**, so the chat mentor starts each day informed for free |
| `POST` | `/api/mentor/chat` | `{message, session}` → proxied to the sidecar |
| `GET` | `/api/mentor/chat/health` | `{available, logged_in}` |
| `GET` | `/api/proposals?status=` | Changes awaiting approval |
| `POST` | `/api/proposals` | `kind`, `title`, `rationale`, `actions` |
| `POST` | `/api/proposals/{id}/approve` | Applies the action list |
| `POST` | `/api/proposals/{id}/reject` | |

Proposal actions (a deliberately small vocabulary, not arbitrary SQL):

*Non-destructive:* `move_card`, `set_due`, `update_card`, `create_card`,
`create_node`, `update_node`, `create_edge`, `delete_edge`, `create_routine`.

*Destructive:* `delete_card`, `delete_list`, `delete_board`, `delete_node`,
`delete_routine`, `delete_doc`, `delete_event`, `delete_label`,
`delete_checklist_item`, `delete_attribute`.

Deletes are **permanent and cascade** — removing a board takes its lists and
cards, removing a skill node takes its level history. They still route
through the proposal queue, so nothing runs until you've read the summary
and approved it. An agent asked *directly* to delete something can also just
call the relevant `DELETE` endpoint; the proposal queue is for changes the
agent initiated, not for changes you asked for.

---

## Joint endpoints

Everything under `/api/joint` is household-wide and ignores `X-Profile-Id`
for scoping (though some accept a `profile_id` in the body to record *who*
did something).

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/joint/home` | One call for the Us landing view |
| `GET` | `/api/joint/calendar?start=&end=` | Merged feed, each occurrence tagged `owner_profile_id` |
| `GET` | `/api/joint/relationship-xp` | `xp`, `level`, progress into level, per-source breakdown, 30-day history |
| `GET`/`PATCH` | `/api/joint/relationship-xp/config` | Per-source weight multipliers |
| `GET`/`POST` | `/api/joint/mailbox` | `?status=pending\|delivered`. POST: `{from_profile_id, to_profile_id, body, deliver_at}` |
| `GET`/`POST` | `/api/joint/wall` | `?before=` paginates. POST: `{profile_id, type, content, caption}` |
| `POST` | `/api/joint/wall/{id}/react` | `{profile_id, emoji}` — toggles |
| `GET`/`POST` | `/api/joint/date-ideas` | `?status=&tag=`. POST: `{title, description, tags}` |
| `POST` | `/api/joint/date-ideas/random?tag=` | Draw an unplanned idea |
| `PATCH`/`DELETE` | `/api/joint/date-ideas/{id}` | |
| `GET` | `/api/joint/companion` | Stage, xp, derived `mood`, next threshold |
| `POST` | `/api/joint/companion/interact` | Pet/water — `429` while on cooldown (60 min) |
| `GET`/`POST` | `/api/joint/countdowns` | Each carries `days_until` |
| `GET` | `/api/joint/countdowns/next` | Nearest upcoming; rolls recurring dates forward |
| `PATCH`/`DELETE` | `/api/joint/countdowns/{id}` | |
| `GET`/`POST` | `/api/joint/song-of-day` | `?range=30`. POST: `{profile_id, track_title, track_url, note}` |
| `GET` | `/api/joint/daily-prompt/today` | **Answers hidden until both have answered** |
| `POST` | `/api/joint/daily-prompt/{id}/answer` | `{profile_id, answer}` — upserts |
| `GET` | `/api/joint/daily-prompt/history` | Accumulated Q&A log |
| `POST` | `/api/joint/ping` | `{to_profile_id, kind}` — `thinking_of_you`, `miss_you`, `proud_of_you`, `you_got_this` |
| `GET` | `/api/joint/flashback` | Content from 1/3/6/12 months ago on today's date |
| `GET` | `/api/joint/milestones/upcoming` | With `current` and `remaining` |
| `GET` | `/api/joint/milestones/recent` | Already celebrated |
| `GET`/`POST` | `/api/joint/bucket-list` | |
| `PATCH`/`DELETE` | `/api/joint/bucket-list/{id}` | Setting `status: "done"` auto-posts a celebration to the wall |

---

## Worked examples

Create a card with a checklist:

```bash
curl -X POST https://opsdeck.example.ts.net/api/cards \
  -H "X-API-Token: $OPSDECK_TOKEN" \
  -H "X-Profile-Id: primary" \
  -H "Content-Type: application/json" \
  -d '{
    "list_id": 8,
    "title": "Finish HIST 112 essay",
    "due_at": "2026-08-03T23:59",
    "checklist": ["Outline", "Draft", "Cite sources"]
  }'
```

The full level-up handshake:

```bash
# 1. See the bar before committing
curl -H "X-API-Token: $TOKEN" .../api/tree/nodes/7/levelup/preview

# 2. Open an attempt (requires a notes doc that clears the floor)
curl -X POST .../api/tree/nodes/7/levelup \
  -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"evidence_doc": 4}'
# -> {"id": 12, "status": "awaiting_questions", "difficulty": 3}
# -> or 400 {"notes_gate": true} if the doc is missing/too short

# 3. Mentor asks
curl -X POST .../api/attempts/12/questions \
  -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"questions": ["You wrote that you used gobuster...", "..."]}'

# 4. You answer
curl -X POST .../api/attempts/12/answer \
  -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"answers": ["...", "..."]}'

# 5. Mentor judges
curl -X POST .../api/attempts/12/verdict \
  -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"granted": true, "feedback": "...", "suggested_nodes": []}'
```

File a proposal instead of editing directly:

```bash
curl -X POST .../api/proposals \
  -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "kind": "board_reorg",
    "title": "Tidy up stale cards",
    "rationale": "Three cards are two weeks past due and untouched.",
    "actions": [
      {"op": "set_due", "card_id": 12, "due_at": "2026-08-15"},
      {"op": "move_card", "card_id": 12, "list_id": 2, "position": 0}
    ]
  }'
```

---

## Agent setup

Drop this in a `CLAUDE.md` where you run Claude Code:

```markdown
## Ops Deck — mentor role

Ops Deck at $OPSDECK_URL, token in $OPSDECK_TOKEN. Start with
GET /api/context for the full picture in one call.

You are a strict examiner, not an assistant. Default to skepticism: the
burden of proof is on the user, and "no, not yet" is a legitimate final
verdict.

- GET /api/attempts?status=awaiting_questions
- Read the context block: node tier, target level, and current attribute
  values tell you how hard to push (difficulty is precomputed 1-5).
- Read the attached notes doc (evidence_doc -> GET /api/docs/{id}). Base at
  least one question on what they wrote.
- POST questions only someone who has actually done the work can answer.
- On GET /api/attempts?status=grading, judge notes and answers together.
  Vague answers, recited terminology, or pasted walkthroughs: reject, and
  state exactly what was missing.
- File board/tree changes as proposals; never write directly.
- Unfiled quick notes wait at GET /api/notes/quick?status=pending — place
  them properly rather than accepting a weak guess.
```

## Health

Provider-agnostic. The Google connector is one caller; a Tasker task, a Home
Assistant automation, an iOS Shortcut or curl can POST the same shape.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health?days=30&metric=` | Summary + series + provider state |
| GET | `/api/health/summary?days=7` | Today per metric, with trailing average |
| GET | `/api/health/stats?days=30&metric=` | avg/median/min/max/total, best & worst day, trend, coverage. Omit `metric` for all |
| GET | `/api/health/detail?metric=&days=` | Stats + series + day-of-week shape + source breakdown |
| GET | `/api/health/raw?metric=&source=&start=&end=&limit=` | Individual stored rows |
| POST | `/api/health` | Ingest one reading or a batch |
| GET | `/api/health/connect` | Returns the Google consent URL |
| GET | `/api/health/callback` | OAuth redirect target (no token header — browser navigation) |
| POST | `/api/health/sync` | Pull the last N days from the provider |
| POST | `/api/health/disconnect` | Drop stored tokens; readings are kept |

Ingest accepts either shape:

```bash
curl -X POST "$OPSDECK_URL/api/health"   -H "X-API-Token: $OPSDECK_TOKEN" -H 'Content-Type: application/json'   -d '{"metric":"steps","value":8432}'

curl -X POST "$OPSDECK_URL/api/health"   -H "X-API-Token: $OPSDECK_TOKEN" -H 'Content-Type: application/json'   -d '{"entries":[{"metric":"sleep_minutes","value":437,"date":"2026-08-04"},
                  {"metric":"weight_kg","value":78.4}]}'
```

`date` defaults to today, `source` to `manual`. Metrics: `steps`,
`distance_km`, `active_minutes`, `exercise_minutes`, `sleep_minutes`,
`calories`, `resting_hr`, `weight_kg`.

Writes are **upserts** on `(profile, metric, date, source)` — re-syncing a
date range overwrites rather than duplicating, which is required because a
day's step count keeps changing until the day is over. A batch with some
bad rows returns **207** with a per-row error list.

**On the provider:** Google Fit's REST API is retired and Health Connect is
on-device only with no cloud API, so neither can be polled from a server.
The legacy Fitbit Web API can, but sunsets 2026-09-30. This targets the
Google Health API (`health.googleapis.com/v4`), which is where that data
moves. Access tokens last one hour, so the refresh token is what's stored.

**Reading the stats.** `coverage_pct` is the share of the window that
actually has readings — an average over 4 of 30 days is not a trend, and the
number is there so a caller can tell the difference. `trend_pct` compares
the second half of the window against the first, which answers "is this
going up" better than a single average does.

`GET /api/context` carries a `health` block (7-day summary plus 30-day
stats), so an agent gets health state without extra round trips.

## Finance

A personal ledger (Phase 1: accounts, transactions, categories, income
sources, CSV import). Profile-scoped like boards: accounts, categories and
income sources carry the profile; transactions inherit scope through their
account. Amounts are **integer cents** everywhere — `amount_cents` in
responses; on write you may send either `amount_cents` or a decimal string
`amount` ("12.50"), which the server converts exactly or rejects.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/finance/accounts` | With `tx_count`. No DELETE — retire with `is_active: 0`; the ledger is the record |
| `POST` | `/api/finance/accounts` | `name`, `type` (`checking`/`credit`/`cash`/`other`), `institution` |
| `PATCH` | `/api/finance/accounts/{id}` | Any of those plus `is_active` |
| `GET` | `/api/finance/transactions` | `?from&to&account_id&category_id&uncategorized=true&q&before&limit`. Returns `{rows, total, next_before}` — `before` is an id cursor, the wall's pagination style |
| `GET` | `/api/finance/transactions/{id}` | One row with account/category names |
| `POST` | `/api/finance/transactions` | `account_id`, `amount`/`amount_cents`, `merchant`, `direction` (`debit` default), `posted_date` (today default), `category_id`, `notes`, `is_pending` |
| `PATCH` | `/api/finance/transactions/{id}` | Setting `category_id` stamps `category_source: manual`. `merchant_raw` is write-once by design — delete and re-log a typo |
| `DELETE` | `/api/finance/transactions/{id}` | |
| `POST` | `/api/finance/transactions/bulk` | `{action: "categorize"\|"delete", ids[], category_id}` |
| `GET` | `/api/finance/merchants` | Autocomplete: recent merchants with the category/account they last used |
| `GET` | `/api/finance/categories` | Seeded per profile; `is_transfer` rows are excluded from all spending totals |
| `POST` | `/api/finance/categories` | `name`, `parent_id` (one level max), `is_income`, `color` |
| `PATCH` | `/api/finance/categories/{id}` | `is_transfer` is not editable |
| `GET`/`POST` | `/api/finance/income-sources` | Expectation only — never counted as received income |
| `PATCH` | `/api/finance/income-sources/{id}` | `cadence`: `weekly`/`biweekly`/`semimonthly`/`monthly`/`irregular` |
| `POST` | `/api/finance/import/preview` | multipart `file` + `account_id` (+ `mapping` JSON for unknown formats). Parses, classifies new vs duplicate, **writes nothing**. `422` with the headers when the format isn't recognized |
| `POST` | `/api/finance/import/commit` | `{account_id, rows[]}` from the preview; `force: true` per row imports a duplicate as a distinct transaction. One DB transaction; keys re-derived server-side |

**Duplicates are explicit, never silent.** A manual entry identical to an
existing one (same account, day, amount, normalized merchant) returns
`409 {"duplicate": true}`; retry with `force: true` to log it as a real
second purchase. Imports mark duplicates in the preview and skip them at
commit unless forced. Identity is `sha256(account|date|cents|merchant_normalized)`
— forced twins get a `|n` suffix.

**Recognized CSV formats:** Capital One card, Capital One 360 Checking, and
Discover (auto-detected by header signature; card/account-number columns are
dropped at parse time and never stored). Anything else gets a manual
column-mapping fallback. Adding an institution is one parser + one registry
entry in `finance.py`.

**Balance anchors.** The only stored balance fact is an anchor — a
known-true balance on a date. The displayed balance is derived:
anchor + ledger after it (for credit accounts, amount *owed* =
anchor + debits − credits). A 360 Checking import anchors automatically
from its Balance column; `PATCH /accounts/{id}` with `{"balance": "123.45"}`
anchors manually. Unanchored accounts derive from an assumed $0 start and
say so in `basis`.

### Finance: rules, budgets, summary

| Method | Path | Notes |
|---|---|---|
| `GET`/`POST` | `/api/finance/rules` | `match_type` (`contains`/`starts_with`/`exact`/`regex`), `pattern` (vs `merchant_normalized`), `category_id`, `priority` (lower first), optional `account_id` |
| `PATCH`/`DELETE` | `/api/finance/rules/{id}` | |
| `POST` | `/api/finance/rules/apply?dry_run=true` | Runs over **uncategorized only** — overwriting a manual decision is structurally impossible |
| `GET` | `/api/finance/budgets?period_start=` | One envelope per category per month |
| `POST` | `/api/finance/budgets` | `{category_id, period_start, limit, rollover}` — upserts |
| `PATCH`/`DELETE` | `/api/finance/budgets/{id}` | |
| `POST` | `/api/finance/budgets/copy-from` | `{source_period, target_period}`, skips existing |
| `GET` | `/api/finance/summary?period=YYYY-MM` | **The single source of computed truth**: income received, per-category spend vs effective limit (rollover carry included), to-be-budgeted, uncategorized count, derived balances, net position |
| `GET` | `/api/finance/recurring` | Deterministic, tuned for **subscriptions, not habits**: amounts within 2% of the median (identical-cents billing), regular interval, and dropped once stale >2 months — unless the cadence is yearly (300–400d), which gets 14 months of patience. Each row carries `cadence` and `next_expected`. No AI involved |

Rules fire on manual entry (when no category is picked) and on import
commit; first match wins by priority; a match sets `category_source: rule`
and bumps the rule's `hit_count`. Regex patterns are validated — nested
quantifiers and >200 chars are rejected up front, since user input feeding
a regex engine is an injection surface.

Rollover envelopes carry `(limit − spent)` from prior rollover months —
including negative carry: overspending genuinely eats next month.

### Finance: AI

Strictly additive — every endpoint degrades to a plain error if the API
key is missing or the API is unreachable; Phases 1–2 never depend on it.
All endpoints are rate-limited (8/min each) so a UI bug cannot loop paid
calls. The model is only ever handed server-computed figures.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/finance/ai/categorize` | Runs rules first; sends only the leftovers (≤40). Returns suggestions, **writes nothing to transactions**. Proposes matching rules as `origin: ai_suggested`, `is_active: 0` |
| `POST` | `/api/finance/ai/categorize/accept` | `{accepted: [{transaction_id, category_id}]}` — applies with `category_source: ai`, and only to rows *still* uncategorized |
| `GET` | `/api/finance/ai/reviews?period=` | Stored narrative reviews |
| `POST` | `/api/finance/ai/reviews/generate` | `{period_start}` — prose over precomputed summary + deltas + recurring; stored for re-reading |
| `POST` | `/api/finance/ai/ask` | `{question}` — answers over supplied figures only |

Malformed model output gets one strict retry, then fails closed —
transactions stay uncategorized, which is a safe state. The design goal is
the flywheel: every categorization pass grows the rule table, so AI usage
shrinks as the system runs.
