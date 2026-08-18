# Ops Deck

> **On authorship.** This codebase was written largely with AI assistance
> (Claude). What is mine is the specification, the product decisions, the
> operation of it in production on my own hardware, and the testing and
> policy calls that shaped it. I did not type most of the implementation and
> am not going to imply otherwise. Raised here because a portfolio that
> overstates is worth less than one that is thin and honest.

A self-hosted personal dashboard for one household: boards, calendar,
routines, docs, a cybersecurity skill tree with earned (not self-granted)
levels, an AI mentor that runs on your Claude subscription, and a shared
"Us" layer for two people.

Everything is driven by a token-authenticated REST API, so scripts, cron
jobs, or an agent can change the same data you see in the browser.

**Stack:** Flask + SQLite + plain HTML/CSS/JS. No build step, no framework,
no bundler, no external CDN. It runs on a 1 GB LXC container and idles
around 25 MB of RAM.

---

## Table of contents

- [What's in it](#whats-in-it)
- [How a level-up actually works](#how-a-level-up-actually-works)
- [Profiles](#profiles)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Documentation map](#documentation-map)
- [Project layout](#project-layout)
- [Design principles](#design-principles)

---

## What's in it

### Personal

| Section | What it does |
|---|---|
| **Today** | Landing page: overdue cards, today's schedule, routines with checkboxes, everything due today. Includes **quick capture** — write a note, it gets filed automatically. |
| **Boards** | Multiple kanban boards, drag-and-drop cards, descriptions, due dates, colour labels, checklists, file attachments. |
| **Calendar** | Month grid with full RFC 5545 recurrence (`every other Tuesday`, `second Monday of the month`). Card due dates appear alongside events. Multi-day events render as continuous bars. Individual occurrences can be skipped or moved without touching the series. Subscribed `.ics` feeds (a work roster, a timetable) refresh themselves hourly. |
| **Routines** | Daily checklist grouped morning/afternoon/evening, per-routine streaks, 30-day history. Completions are stored per date, so a new day starts empty and history stays queryable. |
| **Docs** | Upload or author `.md`/`.html`/`.txt`, organised by folder and tags. HTML renders in a sandboxed iframe (scripts run, but in an opaque origin — see [ARCHITECTURE.md](docs/ARCHITECTURE.md#the-docs-iframe-sandbox)). |
| **Skill tree** | A pan/zoom map of ~450 skills across 12 domains — six security (networking, linux, pentest, defense, crypto, grc) and six programming (python, javascript, c, cpp, html, css). Nodes have levels 0–5 and feed weighted attributes. |
| **Growth** | Weekly XP derived passively from what you actually did, plus a radar chart of your attribute shape with a ghost outline of four weeks ago. |
| **Mentor** | A floating aide in the bottom-right corner — encouraging coach for goals, money math, and the week's logistics, and still a strict examiner when verifying skill level-ups. Runs on your Claude subscription via a sidecar container, and reads a nightly deterministic briefing (schedule with shift hours, balances, budgets, routines) so it starts each day already informed. |
| **Finance** | A ledger built for a sub-10-second phone entry: amount → merchant (autocompleted, pre-fills its usual category and account) → done. CSV import with a new-vs-duplicate preview (Capital One card + 360 Checking, Discover, or a manual column mapper); a 360 import anchors the account's derived balance. Deterministic category rules (first-match by priority, never overriding a manual choice), envelope budgets with rollover and a to-be-budgeted figure, recurring-charge detection, and an AI assist that only sees what rules couldn't classify — and proposes new rules each pass, so it's needed less over time. Integer cents, no stored balances, duplicates always explicit. |
| **TryHackMe** | Log or sync room completions, map rooms to tree nodes. A completion grants nothing on its own — it opens the door to a verification. |

### Shared ("Us" tab)

Relationship XP, a growing companion, a wall, a scheduled-message mailbox,
a date-idea jar, a bucket list, countdowns, song of the day, a two-player
daily question, one-tap pings, on-this-day flashbacks, and milestone
celebrations. All of it derives from one append-only activity log.

---

## How a level-up actually works

This is the core idea of the app, so it's worth spelling out. **There is no
endpoint that sets a node's level.** The only path is:

```
1. Do the work (a TryHackMe room, a project, a course module)
2. Write it up in Docs
       ↓ the server enforces a length floor (OPSDECK_NOTES_MIN_CHARS)
       ↓ no notes → the attempt will not even open
3. POST /api/tree/nodes/{id}/levelup  {"evidence_doc": 9}
       ↓ creates an attempt row, difficulty computed 1–5
4. Mentor reads your notes and asks questions grounded in what you wrote
5. You answer
6. Mentor judges notes AND answers together
       ↓ granted  → one row appended to skill_levels, attributes recompute
       ↓ rejected → recorded on the node, permanently
```

Difficulty scales with **both** tree depth and your existing strength in the
attributes that node feeds — so a Pentest node gets harder to level as your
Pentest attribute grows, rather than verification flattening out as you
improve.

"Not yet" is a legitimate final verdict. Cancelling early is the
"not ready" path and is *not* counted as a failure; a rejected verdict is.

---

## Profiles

The app runs three profiles in one instance:

| Profile | Purpose |
|---|---|
| `primary` | The original owner. All pre-v5 data belongs here. |
| `partner` | A second person. Same app, their own boards/calendar/routines/docs, **their own skill tree, attributes and mentor queue**, their own theme. |
| `joint` | A pseudo-user that owns shared content, plus the "Us" features. |

Switching profiles is a tab in the sidebar (a compact avatar switcher on
mobile). Every API call carries an `X-Profile-Id` header and the server
scopes content to it — including the skill tree, so two people track
entirely different skills without seeing each other's — the primary profile
holds a ~450-node cybersecurity map, the partner a 20-node starter tree of
her own with a different stat set entirely.

A profile's `enabled_modules` setting controls which sections even appear,
so a tab can drop a whole feature with **no code fork** — just a different
list.

See [ARCHITECTURE.md](docs/ARCHITECTURE.md#profiles-and-scoping) for why
scoping is a header rather than a URL segment.

---

## Quick start

### 1. Requirements

- A Docker host. A Debian 12 LXC with 1 vCPU / 1 GB RAM / 8 GB disk is plenty.
- Nothing on the host but Docker; Python lives in the image.

### 2. Generate a token

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy `.env.example` to `.env` and paste it in. **The app refuses every API
request if `OPSDECK_TOKEN` is unset** — it will not silently run open.

### 3. Run it

```bash
docker compose up -d --build
```

Then open `http://<host>:5000`.

On first start the database is created, migrated, and seeded: three
profiles, ten themes, a starter skill tree, and a couple of routines.

For the full production setup — HTTPS via Tailscale, the mentor sidecar,
locking down plain HTTP — see **[docs/DEPLOY.md](docs/DEPLOY.md)**.

---

## Configuration

All configuration is environment variables, read at startup.

| Variable | Default | Purpose |
|---|---|---|
| `OPSDECK_TOKEN` | *(none — required)* | API token. No token, no service. |
| `OPSDECK_TZ` | `America/New_York` | Timezone anchoring "today" and all local dates. |
| `OPSDECK_MAX_UPLOAD_MB` | `25` | Per-file upload ceiling. |
| `OPSDECK_NOTES_MIN_CHARS` | `300` | The notes gate: minimum writeup length before a level-up attempt can open. |
| `OPSDECK_FEED_SYNC_MINUTES` | `60` | How stale a subscribed `.ics` feed may get before it refetches itself. `0` turns the sweeper off and leaves feeds manual-only. |
| `OPSDECK_BRIEFING_TIME` | `23:45` | When the mentor's nightly briefing digest is written (local time). Empty disables it. |
| `OPSDECK_LOW_BALANCE_CENTS` | `2500` | The cashflow guard: liquid balance minus recurring charges due in 14 days below this → a pushed warning. `0` disables. |
| `OPSDECK_MORNING_TIME` | `08:30` | The morning nudge: a pushed one-line summary of the day (shifts, payday countdown, money state). Empty disables. |
| `OPSDECK_VAPID_SUB` | *(example value)* | Contact claim (`mailto:you@…`) sent to push services with each Web Push. |
| `ANTHROPIC_API_KEY` | *(empty)* | Optional. Set it and the mentor grades in-app via the API. Leave empty to use queue mode or the subscription-backed sidecar. |
| `OPSDECK_MENTOR_MODEL` | `claude-sonnet-4-6` | Model used in direct mode. |
| `OPSDECK_BRIDGE_URL` | `http://terminal:7682` | Where the mentor chat sidecar lives. |

---

## Documentation map

| Document | Read it when you want to… |
|---|---|
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Understand *why* it's built this way — derived state, profile scoping, the sandbox decision, request lifecycle. |
| **[docs/API.md](docs/API.md)** | Call the API. Every endpoint with request/response shapes. |
| **[docs/DATA-MODEL.md](docs/DATA-MODEL.md)** | Understand the schema — every table, every column, how migrations run. |
| **[docs/DEPLOY.md](docs/DEPLOY.md)** | Deploy it properly, add HTTPS, run the mentor sidecar, back it up. |
| **[docs/FRONTEND.md](docs/FRONTEND.md)** | Modify the UI — how the SPA routes, renders, and themes. |

---

## Project layout

```
app.py              Flask app; serves the shell page and the service worker
api.py              Personal REST API, profile-scoped
social.py           The /api/joint blueprint — everything shared
db.py               Schema, migrations, seed data
growth.py           XP derivation, attribute math, tree state
mentor.py           Verification prompts, grading, proposal application
quicknote.py        Local heuristic routing for quick capture (zero API cost)
recurrence.py       RRULE expansion and per-occurrence overrides
thm.py              TryHackMe scraping and mapping
finance.py          The /api/finance ledger - accounts, transactions, rules,
                    budgets, balances, CSV import
finance_ai.py       Finance AI layer - categorize assist, reviews, Q&A

templates/
  index.html        The entire HTML shell

static/
  style.css         Whole theme, ~1050 lines
  sw.js             Service worker (notification clicks, push hook)
  js/
    core.js         API client, modal, toasts, dates, theming
    main.js         Routing, search, notifications, boot
    profiles.js     Profile switcher, per-profile nav, Settings
    joint.js        The "Us" tab
    today.js        Today view + quick capture
    board.js        Boards, drag-drop, card modal
    calendar.js     Month grid, event modal, RRULE builder
    routines.js     Routines, streaks, history
    docs.js         Doc list, editor, markdown renderer
    skilltree.js    Pan/zoom canvas, node editor (pointer + pinch)
    mentor.js       Verification flow, proposal inbox
    growth.js       XP chart, attribute radar
    chat.js         Mentor chat panel
    thm.js          TryHackMe section
    finance.js      Ledger: quick entry, transaction list, CSV import

data/               (gitignored) opsdeck.db + uploads/
```

Adding a section means: one new JS file, one entry in the `SECTIONS` map in
`main.js`, and one entry in `ALL_MODULES` in `profiles.js`.

---

## Design principles

These are the rules the codebase actually follows. They explain most of the
things that look unusual.

**1. Nothing is a stored counter.**
Every XP total, attribute value, streak, and relationship level is a *query*
over timestamped rows (`skill_levels`, `routine_completions`,
`activity_events`). There is no counter to drift or repair, and fixing a
node's weights retroactively fixes history — which is what you want, because
the weights are a model of reality.

**2. Levels must be earned.**
No endpoint sets a level directly. Difficulty scales with depth *and*
existing strength, so it gets harder as you get better.

**3. The AI proposes; you approve.**
Autonomous changes land in a proposal queue with a plain-language diff.
Nothing is written to your boards or tree behind your back. The action
vocabulary is deliberately small rather than arbitrary SQL — approving a
summary shouldn't be signing a blank cheque.

**4. Capture must never block on filing.**
Quick notes save instantly and get filed afterwards. When the local
heuristic has no real signal it *refuses to guess* and leaves the note in an
unfiled queue rather than silently putting it somewhere wrong.

**5. Degrade honestly.**
When something can't work, the UI says why. Notifications on plain HTTP
report that browsers require HTTPS instead of a button that does nothing.
The mentor chat says "not logged in" rather than failing silently.

---

## Licence

Personal project, no licence granted. Fork it for your own use if it's
useful to you.
