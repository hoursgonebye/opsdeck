"""
The finance module's AI layer. Strictly additive: every endpoint here can
fail, time out, or be unconfigured and Phases 1-2 keep working unchanged.

Hard rules, enforced structurally rather than by prompt hygiene alone:

1. **The model never does arithmetic.** Every number it sees comes from
   compute_summary() / the recurring query; its output is language and
   category judgment.
2. **It only sees what rules could not classify.** /ai/categorize runs the
   deterministic rules first and sends only the leftovers - a merchant a
   rule covers never costs an API call.
3. **Suggestions, not writes.** /ai/categorize stores nothing on
   transactions; /ai/categorize/accept applies chosen ones with
   category_source='ai', and only onto rows still uncategorized - a manual
   decision made in the meantime wins.
4. **Every pass proposes rules.** Suggested rules land origin='ai_suggested',
   is_active=0. The rule table grows, API usage shrinks, and the system gets
   cheaper and more deterministic the longer it runs.
5. **Minimum data out.** Merchant strings, cent amounts, the category list,
   computed summary figures. Never the full ledger, never notes.

Failure posture: timeouts and one retry with backoff; malformed JSON gets
one strict retry then fails closed (transactions stay uncategorized);
missing API key degrades to 503 with a plain explanation. Amounts and
merchants are never written to the log.

Routes register on the finance blueprint - app.py imports this module for
that side effect.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from flask import jsonify, request

from api import require_token, active_profile, body
from db import connect
from recurrence import today_local
from finance import (
    finance, one, many, compute_summary, apply_rules_to, validate_pattern,
    _category, _month_of, MATCH_TYPES,
)

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("OPSDECK_FINANCE_MODEL",
                       os.environ.get("OPSDECK_MENTOR_MODEL", "claude-sonnet-4-6"))
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CATEGORIZE_BATCH = 40

# A UI bug must not be able to loop the paid API: small fixed window per
# endpoint per process. 429 beyond it.
_RATE_LIMIT = 8              # calls
_RATE_WINDOW = 60            # seconds
_rate_hits = {}


class AIUnavailable(Exception):
    pass


def available():
    return bool(API_KEY)


def _rate_ok(bucket):
    now = time.time()
    hits = [t for t in _rate_hits.get(bucket, []) if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_LIMIT:
        _rate_hits[bucket] = hits
        return False
    hits.append(now)
    _rate_hits[bucket] = hits
    return True


def _call(system, user, max_tokens=1500):
    """One Anthropic call, two attempts with backoff. Raises AIUnavailable."""
    if not API_KEY:
        raise AIUnavailable("no API key configured")
    payload = json.dumps({
        "model": MODEL, "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()

    last = None
    for attempt, delay in ((1, 0), (2, 2)):
        if delay:
            time.sleep(delay)
        req = urllib.request.Request(
            ANTHROPIC_URL, data=payload, method="POST",
            headers={"content-type": "application/json", "x-api-key": API_KEY,
                     "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                data = json.loads(res.read().decode())
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            # 4xx other than 429 will not improve on retry.
            if e.code not in (429, 500, 502, 503, 529):
                raise AIUnavailable(f"API error {e.code}")
            last = f"API error {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError):
            last = "API unreachable"
    raise AIUnavailable(last or "API unreachable")


def _extract_json(text):
    """A JSON value out of a reply that may be fenced or prefaced."""
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start > 0:
        text = text[start:]
    return json.loads(text)


# ------------------------------------------------------------- categorize

CATEGORIZE_SYSTEM = """You categorize personal spending transactions.

You are given a list of categories (with ids) and a batch of transactions
(with ids). For each transaction pick the best category, or omit it if
genuinely unsure. Where the merchant string clearly identifies a repeating
vendor, also propose a matching rule so this never needs AI again: prefer
match_type "contains" with the shortest distinctive token of the merchant
string ("starbucks", "planet fitness"), lowercase.

Reply with ONLY a JSON array, no prose:
[{"transaction_id": int, "category_id": int, "confidence": 0.0-1.0,
  "suggested_rule": {"match_type": "contains", "pattern": "..."} | null}]
Do not invent ids. Do not do any arithmetic."""


@finance.route("/ai/categorize", methods=["POST"])
@require_token
def ai_categorize():
    """Suggestions only - writes nothing to transactions. Does insert the
    proposed rules, inactive, which is the whole flywheel: rules accumulate
    and the uncategorized pile the AI sees shrinks month over month."""
    if not available():
        return jsonify({"error": "AI is not configured (ANTHROPIC_API_KEY)"}), 503
    if not _rate_ok("categorize"):
        return jsonify({"error": "rate limited - wait a minute"}), 429

    d = body()
    conn = connect()
    pid = active_profile()

    # Deterministic rules first; the model only sees what they missed.
    txs = many(conn,
        "SELECT t.id, t.merchant_normalized, t.amount_cents, t.direction, "
        "t.account_id, a.type AS account_type "
        "FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        "WHERE a.profile_id=? AND t.category_id IS NULL ORDER BY t.id DESC", (pid,))
    wanted = set(d.get("transaction_ids") or [])
    if wanted:
        txs = [t for t in txs if t["id"] in wanted]

    ruled = 0
    remaining = []
    for t in txs:
        hit = apply_rules_to(conn, t["merchant_normalized"], t["account_id"], pid)
        if hit:
            conn.execute("UPDATE fin_transactions SET category_id=?, "
                         "category_source='rule', updated_at=datetime('now') WHERE id=?",
                         (hit[1], t["id"]))
            ruled += 1
        else:
            remaining.append(t)
    conn.commit()
    remaining = remaining[:CATEGORIZE_BATCH]

    if not remaining:
        conn.close()
        return jsonify({"suggestions": [], "ruled": ruled,
                        "note": "rules covered everything"})

    cats = many(conn, "SELECT id, name, is_income FROM fin_categories "
                      "WHERE profile_id=? AND is_transfer=0", (pid,))
    user_msg = json.dumps({
        "categories": cats,
        "transactions": [{"transaction_id": t["id"],
                          "merchant": t["merchant_normalized"],
                          "amount_cents": t["amount_cents"],
                          "direction": t["direction"],
                          "account_type": t["account_type"]} for t in remaining],
    })

    suggestions, rules_created = [], 0
    try:
        raw = _call(CATEGORIZE_SYSTEM, user_msg)
        try:
            parsed = _extract_json(raw)
        except (ValueError, json.JSONDecodeError):
            # One strict retry, then fail closed - uncategorized is a safe state.
            raw = _call(CATEGORIZE_SYSTEM + "\nYour previous reply was not valid "
                        "JSON. Reply with ONLY the JSON array.", user_msg)
            parsed = _extract_json(raw)
    except AIUnavailable as e:
        conn.close()
        return jsonify({"error": f"AI unavailable: {e}", "ruled": ruled}), 502
    except (ValueError, json.JSONDecodeError):
        conn.close()
        return jsonify({"error": "AI returned malformed output twice; "
                        "transactions left uncategorized", "ruled": ruled}), 502

    valid_tx = {t["id"] for t in remaining}
    valid_cat = {c["id"]: c["name"] for c in cats}
    if not isinstance(parsed, list):
        parsed = []
    for s in parsed:
        if not isinstance(s, dict):
            continue
        tid, cid = s.get("transaction_id"), s.get("category_id")
        if tid not in valid_tx or cid not in valid_cat:
            continue          # invented ids are dropped, not guessed at
        try:
            confidence = max(0.0, min(1.0, float(s.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        suggestions.append({"transaction_id": tid, "category_id": cid,
                            "category_name": valid_cat[cid],
                            "confidence": round(confidence, 2)})

        rule = s.get("suggested_rule")
        if (isinstance(rule, dict) and rule.get("pattern")
                and rule.get("match_type") in MATCH_TYPES
                and not validate_pattern(rule["match_type"], rule["pattern"].strip())):
            pattern = rule["pattern"].strip().lower()
            dup = one(conn,
                "SELECT 1 FROM fin_category_rules WHERE profile_id=? AND pattern=? "
                "AND category_id=?", (pid, pattern, cid))
            if not dup:
                conn.execute(
                    "INSERT INTO fin_category_rules (profile_id,match_type,pattern,"
                    "category_id,priority,is_active,origin) VALUES (?,?,?,?,?,0,'ai_suggested')",
                    (pid, rule["match_type"], pattern, cid, 200))
                rules_created += 1
    conn.commit()
    conn.close()
    return jsonify({"suggestions": suggestions, "ruled": ruled,
                    "rules_suggested": rules_created,
                    "sent_to_model": len(remaining)})


@finance.route("/ai/categorize/accept", methods=["POST"])
@require_token
def ai_categorize_accept():
    d = body()
    accepted = d.get("accepted") or []
    if not isinstance(accepted, list) or not accepted:
        return jsonify({"error": "accepted[] required"}), 400
    conn = connect()
    pid = active_profile()
    applied = 0
    for s in accepted:
        if not isinstance(s, dict):
            continue
        tid, cid = s.get("transaction_id"), s.get("category_id")
        if not _category(conn, cid or 0):
            continue
        # Only lands on rows still uncategorized: if the user (or a rule)
        # categorized it since the suggestion was made, that decision wins.
        cur = conn.execute(
            "UPDATE fin_transactions SET category_id=?, category_source='ai', "
            "updated_at=datetime('now') WHERE id=? AND category_id IS NULL "
            "AND account_id IN (SELECT id FROM fin_accounts WHERE profile_id=?)",
            (cid, tid, pid))
        applied += cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({"applied": applied, "skipped": len(accepted) - applied})


# ---------------------------------------------------------------- reviews

REVIEW_SYSTEM = """You write a short spending review for a personal finance app.

You are given precomputed figures: this period's summary, the previous
period's for comparison, budget overruns, and detected recurring charges.
All numbers are in cents. Every figure you mention must come from the
provided data - never compute new totals, never estimate.

Write 120-200 words of plain, direct prose: what changed vs last period,
what is drifting or over budget, whether any recurring charge looks like a
forgotten subscription, and one or two concrete next steps. No headers, no
bullet lists, no flattery, no financial-advisor boilerplate."""


@finance.route("/ai/reviews", methods=["GET"])
@require_token
def list_reviews():
    period = request.args.get("period")
    conn = connect()
    sql = "SELECT * FROM fin_ai_reviews WHERE profile_id=?"
    params = [active_profile()]
    if period:
        sql += " AND period_start LIKE ?"
        params.append(f"{period[:7]}%")
    rows = many(conn, sql + " ORDER BY created_at DESC LIMIT 24", params)
    conn.close()
    return jsonify(rows)


@finance.route("/ai/reviews/generate", methods=["POST"])
@require_token
def generate_review():
    if not available():
        return jsonify({"error": "AI is not configured (ANTHROPIC_API_KEY)"}), 503
    if not _rate_ok("reviews"):
        return jsonify({"error": "rate limited - wait a minute"}), 429

    d = body()
    period = (d.get("period_start") or str(today_local()))[:7]
    start, end = _month_of(period)
    if not start:
        return jsonify({"error": "period_start must be YYYY-MM"}), 400
    prev_y, prev_m = int(period[:4]), int(period[5:7]) - 1
    if prev_m == 0:
        prev_y, prev_m = prev_y - 1, 12
    prev_period = f"{prev_y:04d}-{prev_m:02d}"

    conn = connect()
    pid = active_profile()
    cur_sum = compute_summary(conn, pid, period)
    prev_sum = compute_summary(conn, pid, prev_period)
    conn.close()

    # Deterministic recurring detection; the model only describes it.
    rec = json.loads(recurring_data())

    overruns = [c for c in cur_sum["categories"]
                if c.get("limit_cents") and c.get("remaining_cents", 0) < 0]
    payload = json.dumps({
        "this_period": cur_sum, "previous_period": prev_sum,
        "budget_overruns": overruns, "recurring_charges": rec[:12],
    })

    try:
        prose = _call(REVIEW_SYSTEM, payload, max_tokens=600).strip()
    except AIUnavailable as e:
        return jsonify({"error": f"AI unavailable: {e}"}), 502
    if not prose:
        return jsonify({"error": "empty review from model"}), 502

    conn = connect()
    cur = conn.execute(
        "INSERT INTO fin_ai_reviews (profile_id,kind,period_start,period_end,body) "
        "VALUES (?,?,?,?,?)",
        (pid, d.get("kind") or "monthly", start, end, prose))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_ai_reviews WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


def recurring_data():
    """The deterministic recurring endpoint's payload, for internal use."""
    from finance import recurring
    return recurring().data


# -------------------------------------------------------------------- ask

ASK_SYSTEM = """You answer a personal finance question using ONLY the data
provided: current summary (cents), account balances, budgets, recurring
charges. Rules:
- Every number in your answer must appear in, or be a plain restatement of,
  the provided data. Do not derive new totals or projections.
- If the question needs data you were not given, say exactly what is
  missing instead of guessing.
- Answer in 2-6 sentences, plain language, dollars (convert cents by
  placing the decimal, e.g. 1234 cents -> $12.34 - this is formatting, not
  arithmetic).
- If the question is not about their finances, say that is outside this
  panel's scope."""


@finance.route("/ai/ask", methods=["POST"])
@require_token
def ai_ask():
    if not available():
        return jsonify({"error": "AI is not configured (ANTHROPIC_API_KEY)"}), 503
    if not _rate_ok("ask"):
        return jsonify({"error": "rate limited - wait a minute"}), 429
    question = (body().get("question") or "").strip()
    if not question:
        return jsonify({"error": "question required"}), 400
    if len(question) > 500:
        return jsonify({"error": "keep the question under 500 characters"}), 400

    conn = connect()
    pid = active_profile()
    cur_sum = compute_summary(conn, pid, str(today_local())[:7])
    conn.close()
    rec = json.loads(recurring_data())

    payload = json.dumps({"summary": cur_sum, "recurring": rec[:12],
                          "question": question})
    try:
        answer = _call(ASK_SYSTEM, payload, max_tokens=500).strip()
    except AIUnavailable as e:
        return jsonify({"error": f"AI unavailable: {e}"}), 502
    return jsonify({"question": question, "answer": answer})
