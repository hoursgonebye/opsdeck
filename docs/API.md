# API reference

134 endpoints. Everything the browser UI does goes through these — there is
no private API. If you can do it by clicking, you can do it from a script.

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
