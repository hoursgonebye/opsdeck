# Deployment

The reference deployment: a Debian 12 LXC on Proxmox, reachable only over
Tailscale, with the app and an isolated Claude Code sidecar as two
containers.

---

## 1. Host

Any Docker host works. The reference is deliberately small:

| | |
|---|---|
| Container | Debian 12 LXC (Proxmox) |
| Resources | 1 vCPU, 1 GB RAM, 8 GB disk |
| Actual usage | ~25 MB app + ~11 MB sidecar idle, ~1.8 GB disk |

```bash
curl -fsSL https://get.docker.com | sh
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

---

## 2. The app

```bash
git clone <your-repo> opsdeck && cd opsdeck
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into .env
docker compose up -d --build
```

`.env`:

```ini
OPSDECK_TOKEN=<the long random string>
OPSDECK_TZ=America/New_York
OPSDECK_MAX_UPLOAD_MB=25
OPSDECK_NOTES_MIN_CHARS=300

# Optional: enables in-app mentor grading via the Anthropic API.
# Leave BLANK to use queue mode or the subscription-backed sidecar.
ANTHROPIC_API_KEY=
OPSDECK_MENTOR_MODEL=claude-sonnet-4-6
```

The app **refuses all API requests** if `OPSDECK_TOKEN` is unset — it returns
500 rather than silently running open.

Data lives in `./data` (bind-mounted): `opsdeck.db` and `uploads/`.
`docker compose up --build` never touches it.

---

## 3. HTTPS via Tailscale

Plain HTTP works, but browsers refuse notifications outside a secure context
and any password crosses the wire in clear. `tailscale serve` gives you real
certs with no cert management.

**First, enable Serve on the tailnet.** It's an admin-console toggle:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:5000
# If Serve isn't enabled yet, this prints a login.tailscale.com/f/serve link.
# Open it once, then re-run.
```

Add the terminal on a second port:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:7681
tailscale serve status
```

Result:

```
https://<host>.<tailnet>.ts.net        → app
https://<host>.<tailnet>.ts.net:8443   → terminal
```

> **Note:** the host resolves its own MagicDNS name to `127.0.1.1` via
> `/etc/hosts`, so `curl https://<host>.ts.net/` *from the box itself* fails
> while working perfectly from every other device. Test from another
> tailnet machine, or `curl --resolve <host>.ts.net:443:<tailscale-ip>`.

### Lock down plain HTTP

Once HTTPS works, bind both containers to loopback so `tailscale serve` is
the only ingress. In each `docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:5000:5000"     # was "5000:5000" (all interfaces, LAN-reachable)
```

```bash
docker compose up -d
ss -tlnp | grep -E '5000|7681'    # expect 127.0.0.1 only
```

Verify from another machine: the `https://` URLs work, `http://<ip>:5000`
and the LAN address refuse.

---

## 4. The mentor sidecar (optional)

A separate container holding Claude Code, exposing a browser terminal and an
HTTP bridge the app proxies chat through. It exists so the mentor runs on a
**Claude subscription rather than API billing**.

```
opsdeck-terminal/
  Dockerfile          node:20-slim + ttyd static binary + @anthropic-ai/claude-code
  docker-compose.yml
  start.sh            runs ttyd (7681) and bridge.py (7682)
  bridge.py           POST /chat → `claude -p` → JSON
  CLAUDE.md           mentor role + API crib, mounted into /workspace
```

Key configuration:

```yaml
ports:
  - "127.0.0.1:7681:7681"    # bridge port 7682 deliberately NOT published
environment:
  - OPSDECK_URL=http://opsdeck:5000
  - OPSDECK_TOKEN=${OPSDECK_TOKEN}
  - TTYD_USER=${TTYD_USER}
  - TTYD_PASS=${TTYD_PASS}
  # ANTHROPIC_API_KEY deliberately absent — if set, Claude Code bills the
  # API instead of using the subscription.
security_opt: [ "no-new-privileges:true" ]
cap_drop:     [ ALL ]
mem_limit: 512m
networks: [ opsdeck_default ]     # external: the app's network
volumes:
  - workspace:/workspace
  - claude-config:/home/node/.claude    # persists the login
```

Runs as uid 1000 (`node`), never root, no docker socket. If it were
compromised the blast radius is the Ops Deck API and its own workspace — not
the host.

**One-time setup:** open the terminal, run `claude`, complete the browser
login. Both the terminal and the in-app chat panel then work off the same
stored credentials. Until then the chat shows "not logged in" and says so
explicitly.

Point the app at it with `OPSDECK_BRIDGE_URL` (default
`http://terminal:7682`).

---

## 5. Backups

The `.db` file *is* the whole app — boards, docs, tree, ledgers, settings.
Uploads are the only thing outside it.

```bash
# consistent snapshot without stopping the container
docker exec opsdeck python3 -c \
  "import sqlite3;s=sqlite3.connect('data/opsdeck.db');d=sqlite3.connect('data/backup.db');s.backup(d)"

# or just stop and copy
docker compose stop
cp -a data /root/opsdeck-backups/data-$(date +%F)
docker compose start
```

Always snapshot before deploying a schema change:

```bash
cp data/opsdeck.db /root/opsdeck-backups/opsdeck-$(date +%Y%m%d-%H%M%S).db
```

Restore = stop, drop the file back, start. Migrations are idempotent, so an
older DB is brought forward automatically on boot.

---

## 6. Updating

```bash
git pull
cp data/opsdeck.db /root/opsdeck-backups/opsdeck-$(date +%Y%m%d-%H%M%S).db
docker compose up -d --build
docker compose logs --tail 30
```

Migrations run at startup. Verify:

```bash
docker exec opsdeck python3 -c \
  "import sqlite3;c=sqlite3.connect('data/opsdeck.db');\
print(c.execute(\"SELECT value FROM settings WHERE key='schema_version'\").fetchone())"
```

---

## 7. Health checks

```bash
. ./.env
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5000/                    # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5000/api/today           # 401 (no token)
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Token: $OPSDECK_TOKEN" \
     http://localhost:5000/api/today                                               # 200
curl -s -H "X-API-Token: $OPSDECK_TOKEN" http://localhost:5000/api/mentor/chat/health
# {"available":true,"logged_in":true,"ok":true}
```

Per-profile:

```bash
for p in primary partner joint; do
  echo -n "$p: "
  curl -s -o /dev/null -w '%{http_code}\n' \
    -H "X-API-Token: $OPSDECK_TOKEN" -H "X-Profile-Id: $p" \
    http://localhost:5000/api/today
done
```

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every API call returns 500 | `OPSDECK_TOKEN` unset | Set it in `.env`, `docker compose up -d` |
| Notification button says "needs HTTPS" | Page served over plain HTTP | Use the `tailscale serve` URL |
| Chat says "not logged in" | Sidecar has no Claude credentials | Open the terminal, run `claude`, log in once |
| Chat says "terminal unreachable" | Sidecar down or not on the network | `docker compose ps`; confirm `networks: opsdeck_default` |
| `tailscale serve` 404/timeout from the host itself | MagicDNS resolves to `127.0.1.1` locally | Expected — test from another device |
| Uploads fail at ~25 MB | `OPSDECK_MAX_UPLOAD_MB` | Raise it; Flask's own cap is derived from it |
| A profile's tab is missing a section | `enabled_modules` | Settings → Modules, or `PATCH /api/profiles/{id}/settings` |

Logs:

```bash
docker compose logs -f
docker compose logs --tail 100 | grep -iE 'error|traceback'
```
