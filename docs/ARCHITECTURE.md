# Architecture

Why Ops Deck is built the way it is. If you only read one document to
understand the codebase, read this one — [API.md](API.md) and
[DATA-MODEL.md](DATA-MODEL.md) are reference material, this is reasoning.

---

## Contents

1. [The shape of the system](#1-the-shape-of-the-system)
2. [Derived state: nothing is a stored counter](#2-derived-state-nothing-is-a-stored-counter)
3. [Profiles and scoping](#3-profiles-and-scoping)
4. [The request lifecycle](#4-the-request-lifecycle)
5. [The verification system](#5-the-verification-system)
6. [The mentor: three execution modes](#6-the-mentor-three-execution-modes)
7. [Quick capture and honest uncertainty](#7-quick-capture-and-honest-uncertainty)
8. [The joint layer](#8-the-joint-layer)
9. [Security model](#9-security-model)
10. [Known limits and trade-offs](#10-known-limits-and-trade-offs)

---

## 1. The shape of the system

Three containers on one host, sharing a docker network:

```
                          ┌──────────────────────────────┐
   browser ── HTTPS ──►   │  tailscale serve (host)      │
   (tailnet only)         └──────┬──────────────┬────────┘
                                 │ :5000        │ :8443
                          ┌──────▼───────┐  ┌───▼────────────────┐
                          │  opsdeck     │  │ opsdeck-terminal   │
                          │  Flask+SQLite│  │ ttyd + chat bridge │
                          │              │◄─┤ Claude Code        │
                          └──────┬───────┘  └────────────────────┘
                                 │ bind mount
                          ┌──────▼───────┐
                          │ data/        │
                          │  opsdeck.db  │
                          │  uploads/    │
                          └──────────────┘
```

- **opsdeck** — the app. Flask serves one HTML shell plus a REST API.
  All state is one SQLite file and an uploads directory, both bind-mounted
  so rebuilding the image never touches data.
- **opsdeck-terminal** — an isolated sidecar holding Claude Code. Runs
  non-root, capabilities dropped, no docker socket. Exposes a browser
  terminal *and* a small HTTP bridge the app proxies chat through.
- **tailscale serve** — TLS termination. Both containers bind `127.0.0.1`
  only; the tailnet is the sole ingress.

There is no message queue, no cache layer, no worker process. For one
household writing a few hundred rows a day, an in-process SQLite query is
faster than the network hop to anything else would be.

### The background threads

The app runs two things on timers, both daemon threads started from
`app.py`'s `__main__` block: the calendar-feed sweeper (`start_auto_sync`
in `calendars.py`), which refetches subscribed feeds once they are
`OPSDECK_FEED_SYNC_MINUTES` stale, and the nightly mentor-briefing writer
(`start_scheduler` in `briefing.py`), which composes a deterministic
per-profile digest into Docs at `OPSDECK_BRIEFING_TIME`. The briefing
writer answers "did today's run happen" from the docs table, never from
memory — the same restart lesson the chat bridge learned with sessions.

It is worth being explicit about why this one deviates, because the
pattern everywhere else is to do periodic work lazily on read — the mailbox
delivers due messages inside `GET /joint/mailbox` rather than on a schedule
([§8](#8-the-joint-layer)), precisely so there is no scheduler to be down.

The difference is what the work costs. Delivering mail is a local `UPDATE`
measured in microseconds, so hanging it off a read is free. Syncing a feed
is an HTTP fetch with a 45-second timeout against someone else's server, and
putting that in the path of `GET /api/events` would mean an unreachable
roster host freezing the calendar. Network I/O does not belong in a request
handler that has no reason to make a network call.

Three details make the thread cheap to reason about:

- **Staleness, not a schedule.** Each tick asks which feeds are overdue
  rather than syncing everything on a fixed cadence, so a manual sync resets
  the clock and a restart doesn't trigger a thundering herd.
- **A failed sync still stamps `last_synced_at`.** Otherwise a feed whose
  host is down would be retried every tick forever; stamping backs it off to
  one attempt per interval.
- **It sweeps all profiles.** `POST /calendar/feeds/sync-all` is scoped to
  the active profile — fine for a button, wrong for a timer, since the
  partner's timetable should refresh without anyone looking at her tab.

Started from `app.py`'s `__main__` block rather than at import, so importing
the app for a test never starts fetching calendars, and daemonised so it
dies with the process and needs no shutdown handling.

### Why no frontend framework

The UI is ~3,500 lines of plain JS across 14 files, one per section, each
exporting a single `renderX()` that fetches, builds an HTML string, and
attaches handlers. There is no build step, so:

- deploying is `docker compose up -d --build` — no node toolchain in the image
- the browser loads exactly the files in the repo; what you read is what runs
- a section is genuinely independent — you can understand `routines.js`
  without knowing anything about `board.js`

The cost is no reactivity: every mutation re-renders its whole section.
At this data size that's a sub-millisecond string rebuild, and it removes an
entire category of stale-state bugs.

---

## 2. Derived state: nothing is a stored counter

**This is the most important idea in the codebase.**

Consider XP. The naive design is an `xp` column you increment. That design
rots:

- if a bug double-increments, the number is permanently wrong with no way to detect it
- "what did my week look like in March" is unanswerable
- changing how much a skill is worth can't fix the past

Ops Deck stores *events*, and computes *everything else on read*:

| Displayed number | Actually computed from |
|---|---|
| Weekly XP | `COUNT` over `routine_completions`, `cards.completed_at`, `checklist_items.done_at`, plus `skill_levels` rows in the week |
| Attribute values | `skill_levels` ⋈ `node_weights`, weighted by node tier |
| Attribute history | The same query with `WHERE local_date <= ?` |
| Routine streak | Walk backwards through `routine_completions` dates |
| Relationship XP | `SUM(weight)` over `activity_events` |
| Companion stage | `relationship_xp // stage_threshold` |

Consequences worth understanding:

**Editing a node's weights retroactively corrects history.** If you decide
"Packet analysis" should feed Defense at 0.5 instead of 0.3, every past
week's attribute chart updates. That's correct: the weights are a *model of
reality*, and improving the model should improve the picture.

**There is no repair tooling, because there is nothing to repair.** No
migration ever has to recompute a cached total.

**The cost is query time.** `attribute_history(weeks=12)` runs
`attribute_values()` twelve times, each a join over the whole ledger. At
~450 nodes and a few hundred level rows this is single-digit milliseconds.
At 100k ledger rows it would need a materialised weekly snapshot — years
away at one user's writing rate. See [§10](#10-known-limits-and-trade-offs).

---

## 3. Profiles and scoping

### The model

Rather than building a second app for a second person, a profile is a row
and every content table carries a `profile_id`:

```
profiles: primary | partner | joint
```

`joint` is a *pseudo-user* — it owns shared boards, routines, docs and
events, which means the entire existing CRUD surface works on shared content
for free. Only genuinely new behaviour (merging two calendars, relationship
XP, the wall) needed new code.

Tables carrying `profile_id`: `boards`, `events`, `routines`, `docs`,
`quick_notes` (v5), plus `skill_nodes`, `attributes`, `levelup_attempts`,
`ai_proposals` and `thm_completions` (v6). Child rows (`lists`, `cards`,
`checklist_items`, `routine_completions`, `doc_tags`, `node_weights`,
`skill_edges`, `skill_levels`) inherit scope through their parent — adding a
redundant `profile_id` to `cards` would create the possibility of a card
whose profile disagrees with its board's.

**v6 made the growth system per-profile**, which matters more than it
sounds: two people tracking skills in one app should not share a tree, a
stat set, or a verification queue. The primary profile's ~450-node
cybersecurity map would be meaningless to someone not doing security, so the
partner gets her own seed (six broad attributes — wellbeing, craft, mind,
home, people, work) rather than inheriting his. `skill_edges` are filtered by
requiring *both* endpoints to belong to the profile, so a stray cross-profile
edge can never leak a node into the wrong tree.

### Why a header, not a URL segment

The original spec proposed `/api/profiles/{id}/boards`. This implementation
uses `X-Profile-Id` instead. The reasoning:

**What a path prefix would cost.** Flask resolves a blueprint's URL prefix
at registration. Serving both the legacy `/api/boards` and the new
`/api/profiles/{id}/boards` means registering the blueprint twice, which
duplicates every endpoint name and requires unique-ifying ~60 view functions;
or rewriting every route decorator *and* threading a `profile_id` argument
through every handler signature. Both are large mechanical diffs with a real
chance of a missed handler silently serving unscoped data.

**What the header buys.** One `before_request` hook resolves the profile
once, validates it against the `profiles` table, and stashes it on `g`.
Handlers call `active_profile()`. A missed handler is a *visible* bug (data
doesn't scope) rather than a silent one, and there's exactly one place to
audit.

**Isolation is identical.** Both designs are server-side filters keyed on a
client-supplied value. Neither is an authorisation boundary — this is a
single-tenant app behind one token on a private tailnet, and profiles are
organisational, not a security perimeter. Two people who share the app can
read each other's data by design (the Joint tab literally does).

**Backward compatibility is free.** A request with no header resolves to
`primary`, so every pre-v5 script and bookmark keeps working unchanged.

Trade-off accepted: profile-specific URLs aren't shareable/bookmarkable.
For a single-page app with a persistent sidebar switcher, that costs nothing.

### enabled_modules

Each profile's settings JSON carries `enabled_modules`. The sidebar is built
from it at runtime (`buildNav()` in `profiles.js`). Dropping the skill tree
from the partner profile is a list edit, not a code branch — which is what
makes the second tab a genuine rebrand rather than a forced clone.

---

## 4. The request lifecycle

```
POST /api/boards
  Header: X-API-Token: <token>
  Header: X-Profile-Id: partner

 1. api.before_request → resolve_profile()
       reads X-Profile-Id, validates against profiles table
       unknown / missing → 'primary'
       stores on flask.g

 2. @require_token
       no OPSDECK_TOKEN configured → 500 (refuse, never run open)
       mismatch                    → 401

 3. handler
       active_profile() → 'partner'
       INSERT INTO boards (..., profile_id) VALUES (..., 'partner')

 4. side effects (if any)
       social.log_activity() appends to activity_events,
       which cascades to companion sync + milestone checks

 5. jsonify(result)
```

Every handler opens its own connection and closes it. No pooling, no ORM,
no session object. SQLite handles one writer fine at this scale, and the
explicit `connect()`/`close()` makes the transaction boundary of every
endpoint obvious at a glance.

---

## 5. The verification system

The design goal was: **make the number mean something.** A skill tracker
where you self-report levels measures enthusiasm, not skill.

### Three gates

**Gate 1 — the notes floor (mechanical, server-enforced).**
`POST /tree/nodes/{id}/levelup` requires an `evidence_doc`. If the doc is
missing or under `OPSDECK_NOTES_MIN_CHARS`, the attempt returns
`400 {"notes_gate": true}` and *no attempt row is created*. You cannot start
without having written something.

**Gate 2 — grounded questions.**
The mentor reads the attached doc and is instructed to base at least one
question on what you wrote. The point is testing whether you understand your
own notes, which a pasted walkthrough cannot survive.

**Gate 3 — judged together.**
Notes and answers are graded as one artifact. Thin notes are rejected the
same as thin answers.

### Difficulty

```python
depth_part = min(tier - 1, 3)            # 0–3, tree depth
attr_part  = min(weighted_attr / 6, 2)   # 0–2, existing strength
level_part = min(level * 0.4, 1.5)       # target level
difficulty = clamp(1 + depth*0.7 + attr + level*0.5, 1, 5)
```

The `attr_part` term is the interesting one: it reads your *current*
attribute values through this node's weights. A Pentest node gets harder to
level as your Pentest grows. Verification therefore doesn't flatten out as
you improve — the bar rises with you.

### Failure is on the record

A `rejected` verdict increments a per-node counter shown in the level-up
preview. Cancelling (`DELETE /attempts/{id}`) is the "not ready yet" exit and
is deliberately *not* counted. The distinction matters: backing out is fine,
being judged and found wanting is information.

---

## 6. The mentor: three execution modes

All three write the **same rows**. Direct mode is a convenience layer over
the queue, not a bypass — you can open an attempt in one mode and finish it
in another.

| Mode | Trigger | Where inference runs | Cost |
|---|---|---|---|
| **Queue** | always available | An external agent polls `/api/attempts?status=...` and POSTs questions/verdicts | free |
| **Direct** | `ANTHROPIC_API_KEY` set | The server calls the Anthropic API in-process (`mentor.py`) | API billing |
| **Sidecar chat** | terminal container running | The app proxies to a bridge that shells out to `claude -p` | Claude subscription |

The sidecar exists specifically to get conversational mentoring *without*
API billing. The bridge (`bridge.py` in the terminal image) is not published
to the host — only the app container can reach it, over the docker network,
and it still requires the shared token. The browser only ever talks to the
app's own origin, so there's no CORS surface and no second credential in the
page.

`ANTHROPIC_API_KEY` is deliberately stripped from the sidecar's environment
in two places (compose, and again in the subprocess env) — if it leaked in,
Claude Code would silently bill the API instead of using the subscription.

---

## 7. Quick capture and honest uncertainty

Capture is instant and free. `POST /api/notes/quick` stores the text, runs a
**local heuristic** (`quicknote.py`, pure regex + token matching, no network),
and returns. Filing happens later.

The heuristic:
- parses dates conservatively (`tomorrow`, `next friday`, `aug 14`, `in 3 days`)
- classifies as card / event / doc / routine from verb and keyword signals
- matches a board by scoring the note's tokens against *that board's own
  vocabulary* (its title plus its existing card titles), so it keeps working
  as boards are renamed or added — no hardcoded keyword table

**The interesting part is what it does when it doesn't know.** If the board
match scores zero, the suggestion is flagged `confident: false` and
"Capture & file" *refuses to file it*, leaving it in an unfiled queue with
its guessed destination shown. Silently dropping a note on the wrong board is
worse than leaving it visibly unfiled.

That queue is also the agent hand-off point: `GET /api/notes/quick?status=pending`
lets Claude Code file the ambiguous ones with full workspace context.

---

## 8. The joint layer

Everything in `/api/joint` derives from one append-only table:

```sql
activity_events(profile_id, source_type, source_id, weight, created_at)
```

`social.log_activity()` is called from `api.py` at two points — routine
completion and joint-card completion — plus from pings and companion
interactions. Each call cascades:

```
log_activity()
  ├─ INSERT INTO activity_events
  ├─ _sync_companion()    xp = SUM(weight); stage = xp // 150
  └─ _check_milestones()  any uncelebrated threshold crossed?
                            → mark celebrated, notify both profiles
```

So relationship XP, the companion's growth, and milestone celebrations are
three different *readings of the same log* — the identical pattern the
personal XP system uses. Adding a fourth consumer means writing a query, not
adding a counter.

Two features have deliberate mechanics worth noting:

**The daily question** hides both answers until both people have answered
(`both_answered` gate in `/daily-prompt/today`), so it plays as a
simultaneous reveal rather than one person anchoring on the other's answer.

**The mailbox** delivers lazily. There is no cron: `GET /joint/mailbox` and
`GET /joint/home` both call `_deliver_due_mail()`, which flips past-due rows
and fires notifications. For an app you open daily, "check on read" is
simpler than a scheduler and has no failure mode where the scheduler is down.

---

## 9. Security model

**Be clear about what this is:** a single-tenant app for one household on a
private tailnet. It is not multi-tenant and profiles are not an auth
boundary.

| Layer | Control |
|---|---|
| Network | Tailscale only. Both containers bind `127.0.0.1`; `tailscale serve` is the sole ingress. LAN cannot reach either port. |
| Transport | HTTPS with real certs via `tailscale serve`. |
| API | Bearer token in `X-API-Token`. **Missing server token → 500, never open.** |
| Terminal | HTTP basic auth on top of the tailnet + HTTPS. |
| Bridge | Not published to the host; docker-network-internal, plus shared-token check. |
| Sidecar | Non-root (uid 1000), `cap_drop: ALL`, `no-new-privileges`, no docker socket, 512 MB cap. |

### The docs iframe sandbox

Uploaded HTML renders in `<iframe sandbox="allow-scripts">` — **without**
`allow-same-origin`. That pairing is the whole trick:

- `allow-scripts` alone → the frame gets a unique **opaque origin**. Scripts
  run (so an interactive uploaded doc actually works) but same-origin policy
  blocks it from touching the parent DOM, cookies, or the API token.
- adding `allow-same-origin` would let the frame reach into the parent and
  defeat the sandbox entirely. **Never add it.**

The app deliberately embeds the API token in the served page. That is not a
leak in this model: anyone who can load the page is already inside the
tailnet and past the token. The real boundary is the network, not the DOM.

---

## 10. Known limits and trade-offs

Documented honestly so they're decisions, not surprises.

**Flask development server.** `app.run()` prints a warning that it is not
for production, and it is telling the truth. It's fine for one user on a
private tailnet. Exposing this more widely means gunicorn behind a real
proxy.

**Single SQLite writer.** Concurrent writes serialise. Invisible at
household scale; would matter with many simultaneous users.

**Derived state gets slower with history.** `attribute_history(weeks=12)`
is 12 full ledger passes. Fine at current size; if the ledger reaches ~100k
rows, add a weekly snapshot table and read from it for anything older than
the current week. Nothing about the current design blocks that.

**No true background push.** Browser notifications only fire while a tab is
open — the page polls `/api/reminders/upcoming` once a minute. Real push
needs Web Push with VAPID keys and stored subscriptions; `static/sw.js`
already has the `push` listener wired for it. That's an addition, not a
rewrite.

**TryHackMe sync is best-effort.** There is no official personal API, so
`thm.py` scrapes unofficial endpoints behind the public profile and can
break without notice. Manual logging always works and needs no network.

**Profiles are not an authorisation boundary.** Anyone with the API token
can set any `X-Profile-Id`. This is intentional — see [§3](#3-profiles-and-scoping).
The spec's optional per-profile PIN (`pin_hash` + an unlock endpoint) is not
implemented; it would be a small addition, not a redesign.
