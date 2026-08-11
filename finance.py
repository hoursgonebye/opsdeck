"""
Finance - a personal ledger under /api/finance.

Phase 1 of the finance module: accounts, transactions, categories, income
sources, fast manual entry support, and CSV import with preview. No AI, no
budgets yet - those build on top of this ledger later.

Ground rules carried through every endpoint here:

- **Money is integer cents.** Floats never touch an amount. Conversion from
  a decimal string happens once, in to_cents(), which rejects anything it
  cannot represent exactly.
- **Nothing stores a balance.** Totals are queries over transactions, the
  same discipline as the XP ledgers (ARCHITECTURE 2).
- **merchant_raw is written once and never mutated.** merchant_normalized
  is the matching form (lowered, punctuation stripped, whitespace
  collapsed) used by dedupe keys and, in the next phase, category rules.
- **Imports never write on upload.** /import/preview parses and reports;
  only /import/commit writes, in one transaction, after the user has seen
  exactly what is new and what is a duplicate.
- **Scope inherits through accounts.** fin_transactions carries no
  profile_id; every transaction query joins fin_accounts and filters on the
  active profile, exactly like cards join up to boards.

Mounted as its own blueprint (like social.py) but profile-scoped (like
api.py) - it borrows api.py's resolve_profile so there is still exactly one
place scoping happens.
"""
import csv
import hashlib
import io
import re
from datetime import datetime

from flask import Blueprint, jsonify, request

from api import require_token, resolve_profile, active_profile, body, one, many, MAX_UPLOAD_MB
from db import connect
from recurrence import today_local

finance = Blueprint("finance", __name__, url_prefix="/api/finance")
finance.before_request(resolve_profile)

ACCOUNT_TYPES = ("checking", "credit", "cash", "other")
DIRECTIONS = ("debit", "credit")
CADENCES = ("weekly", "biweekly", "semimonthly", "monthly", "irregular")
CATEGORY_SOURCES = ("manual", "rule", "ai", "import")

TX_PAGE_LIMIT = 100


# ------------------------------------------------------------------ money

def to_cents(value):
    """
    A user-entered amount into integer cents. Raises ValueError on anything
    that cannot be represented exactly.

    Accepts "4.50", "1,234.56", "$12", 12, and - because JSON has no decimal
    type - floats like 4.1, which are routed through str() so 4.1 means the
    "4.1" the user typed, not the 4.0999... the float actually stores. More
    than two decimal places is rejected rather than rounded: silently
    turning 4.999 into 500 cents is how ledgers stop being trusted.
    """
    if isinstance(value, bool):
        raise ValueError("amount must be a number")
    if isinstance(value, int):
        cents = value * 100
        if cents <= 0:
            raise ValueError("amount must be positive")
        return cents

    s = str(value).strip().replace(",", "").replace("$", "")
    if not s:
        raise ValueError("amount required")
    if s.startswith("(") and s.endswith(")"):      # accounting negative
        s = "-" + s[1:-1]
    m = re.fullmatch(r"(-?)(\d+)(?:\.(\d{1,2}))?", s)
    if not m:
        raise ValueError(f"not a money amount: {value!r}")
    sign, whole, frac = m.groups()
    cents = int(whole) * 100 + int((frac or "").ljust(2, "0"))
    if sign:
        cents = -cents
    if cents == 0:
        raise ValueError("amount must be non-zero")
    return cents


def to_cents_signed(value):
    """to_cents, but zero and negative are legitimate (balance anchors:
    an overdrawn account or a fully-paid card is $0 or less)."""
    if isinstance(value, bool):
        raise ValueError("amount must be a number")
    if isinstance(value, int):
        return value * 100
    s = str(value).strip().replace(",", "").replace("$", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    m = re.fullmatch(r"(-?)(\d+)(?:\.(\d{1,2}))?", s or "")
    if not m:
        raise ValueError(f"not a money amount: {value!r}")
    sign, whole, frac = m.groups()
    cents = int(whole) * 100 + int((frac or "").ljust(2, "0"))
    return -cents if sign else cents


def fmt_cents(cents):
    return f"${abs(cents) // 100}.{abs(cents) % 100:02d}"


def normalize_merchant(raw):
    """Lowercase, punctuation to spaces, whitespace collapsed."""
    s = re.sub(r"[^a-z0-9\s]+", " ", (raw or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def dedupe_key(account_id, posted_date, amount_cents, merchant_normalized):
    basis = f"{account_id}|{posted_date}|{amount_cents}|{merchant_normalized}"
    return hashlib.sha256(basis.encode()).hexdigest()


def _free_dedupe_key(conn, base_key):
    """
    The explicit same-day-duplicate override: two identical coffees are
    legitimate, so a forced import appends a disambiguating suffix rather
    than silently dropping the row - or silently keeping both by default.
    """
    if not one(conn, "SELECT 1 FROM fin_transactions WHERE dedupe_key=?", (base_key,)):
        return base_key
    n = 2
    while one(conn, "SELECT 1 FROM fin_transactions WHERE dedupe_key=?", (f"{base_key}|{n}",)):
        n += 1
    return f"{base_key}|{n}"


def _valid_date(s):
    try:
        datetime.strptime(str(s), "%Y-%m-%d")
        return str(s)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------- ownership
# Transactions inherit profile scope through their account; categories are
# scoped directly. Every mutating endpoint resolves ownership before
# touching anything, so a stray id from another profile 404s instead of
# leaking.

def _account(conn, account_id):
    return one(conn, "SELECT * FROM fin_accounts WHERE id=? AND profile_id=?",
               (account_id, active_profile()))


def _category(conn, category_id):
    return one(conn, "SELECT * FROM fin_categories WHERE id=? AND profile_id=?",
               (category_id, active_profile()))


def _transaction(conn, tx_id):
    return one(conn,
        "SELECT t.* FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        "WHERE t.id=? AND a.profile_id=?", (tx_id, active_profile()))


# ---------------------------------------------------------------- accounts

@finance.route("/accounts", methods=["GET"])
@require_token
def list_accounts():
    conn = connect()
    rows = many(conn,
        "SELECT a.*, (SELECT COUNT(*) FROM fin_transactions t WHERE t.account_id=a.id) AS tx_count "
        "FROM fin_accounts a WHERE a.profile_id=? ORDER BY a.is_active DESC, a.id",
        (active_profile(),))
    conn.close()
    return jsonify(rows)


@finance.route("/accounts", methods=["POST"])
@require_token
def create_account():
    d = body()
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    acc_type = d.get("type") or "checking"
    if acc_type not in ACCOUNT_TYPES:
        return jsonify({"error": f"type must be one of {', '.join(ACCOUNT_TYPES)}"}), 400
    conn = connect()
    cur = conn.execute(
        "INSERT INTO fin_accounts (profile_id,name,type,institution) VALUES (?,?,?,?)",
        (active_profile(), name, acc_type, (d.get("institution") or "").strip() or None),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM fin_accounts WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@finance.route("/accounts/<int:aid>", methods=["PATCH"])
@require_token
def update_account(aid):
    conn = connect()
    if not _account(conn, aid):
        conn.close()
        return jsonify({"error": "no such account"}), 404
    d = body()
    if "type" in d and d["type"] not in ACCOUNT_TYPES:
        conn.close()
        return jsonify({"error": f"type must be one of {', '.join(ACCOUNT_TYPES)}"}), 400
    for f in ("name", "type", "institution", "is_active"):
        if f in d:
            conn.execute(f"UPDATE fin_accounts SET {f}=?, updated_at=datetime('now') "
                         "WHERE id=?", (d[f], aid))
    if "balance" in d or "balance_anchor_cents" in d:
        # "The balance is $X as of date D" - the one true fact the derived
        # balance builds on. For credit accounts this is the amount owed.
        try:
            cents = (d["balance_anchor_cents"] if "balance_anchor_cents" in d
                     else to_cents_signed(d.get("balance")))
        except ValueError as e:
            conn.close()
            return jsonify({"error": str(e)}), 400
        date = _valid_date(d.get("balance_date")) or str(today_local())
        conn.execute("UPDATE fin_accounts SET balance_anchor_cents=?, "
                     "balance_anchor_date=?, updated_at=datetime('now') WHERE id=?",
                     (cents, date, aid))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_accounts WHERE id=?", (aid,))
    conn.close()
    return jsonify(out)


# -------------------------------------------------------------- categories

@finance.route("/categories", methods=["GET"])
@require_token
def list_categories():
    conn = connect()
    rows = many(conn,
        "SELECT c.*, (SELECT COUNT(*) FROM fin_transactions t "
        " JOIN fin_accounts a ON a.id=t.account_id "
        " WHERE t.category_id=c.id AND a.profile_id=?) AS tx_count "
        "FROM fin_categories c WHERE c.profile_id=? ORDER BY c.sort_order, c.id",
        (active_profile(), active_profile()))
    conn.close()
    return jsonify(rows)


@finance.route("/categories", methods=["POST"])
@require_token
def create_category():
    d = body()
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = connect()
    parent_id = d.get("parent_id")
    if parent_id is not None:
        parent = _category(conn, parent_id)
        if not parent:
            conn.close()
            return jsonify({"error": "no such parent category"}), 400
        if parent["parent_id"] is not None:
            # One level of nesting, enforced here rather than hoped for.
            conn.close()
            return jsonify({"error": "categories nest one level deep"}), 400
    if one(conn, "SELECT 1 FROM fin_categories WHERE profile_id=? AND name=?",
           (active_profile(), name)):
        conn.close()
        return jsonify({"error": "category already exists"}), 409
    pos = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM fin_categories "
                       "WHERE profile_id=?", (active_profile(),)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO fin_categories (profile_id,name,parent_id,is_income,color,sort_order) "
        "VALUES (?,?,?,?,?,?)",
        (active_profile(), name, parent_id, 1 if d.get("is_income") else 0,
         d.get("color"), pos),
    )
    conn.commit()
    out = one(conn, "SELECT * FROM fin_categories WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@finance.route("/categories/<int:cid>", methods=["PATCH"])
@require_token
def update_category(cid):
    conn = connect()
    cat = _category(conn, cid)
    if not cat:
        conn.close()
        return jsonify({"error": "no such category"}), 404
    d = body()
    # is_transfer is deliberately not editable: it is what keeps account-to-
    # account moves out of the spending numbers, and flipping it should be a
    # considered schema decision, not a stray PATCH.
    for f in ("name", "color", "sort_order", "parent_id", "is_income"):
        if f in d:
            conn.execute(f"UPDATE fin_categories SET {f}=? WHERE id=?", (d[f], cid))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_categories WHERE id=?", (cid,))
    conn.close()
    return jsonify(out)


# ------------------------------------------------------------ transactions

def _tx_filters(args):
    """Shared WHERE builder for list + count so they can never disagree."""
    where = ["a.profile_id=?"]
    params = [active_profile()]
    if args.get("from"):
        where.append("t.posted_date>=?"); params.append(args["from"])
    if args.get("to"):
        where.append("t.posted_date<=?"); params.append(args["to"])
    if args.get("account_id"):
        where.append("t.account_id=?"); params.append(args["account_id"])
    if args.get("category_id"):
        where.append("t.category_id=?"); params.append(args["category_id"])
    if args.get("uncategorized") == "true":
        where.append("t.category_id IS NULL")
    if args.get("q"):
        where.append("(t.merchant_raw LIKE ? OR t.notes LIKE ?)")
        like = f"%{args['q']}%"
        params += [like, like]
    return " AND ".join(where), params


@finance.route("/transactions", methods=["GET"])
@require_token
def list_transactions():
    args = request.args
    where, params = _tx_filters(args)
    limit = min(int(args.get("limit", TX_PAGE_LIMIT)), 500)

    conn = connect()
    total = conn.execute(
        f"SELECT COUNT(*) FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        f"WHERE {where}", params).fetchone()[0]

    page_params = list(params)
    before = args.get("before")
    cursor = ""
    if before:
        cursor = " AND t.id<?"
        page_params.append(before)
    rows = many(conn,
        f"SELECT t.*, a.name AS account_name, a.type AS account_type, "
        f"c.name AS category_name, c.color AS category_color, "
        f"c.is_income AS category_is_income, c.is_transfer AS category_is_transfer "
        f"FROM fin_transactions t "
        f"JOIN fin_accounts a ON a.id=t.account_id "
        f"LEFT JOIN fin_categories c ON c.id=t.category_id "
        f"WHERE {where}{cursor} ORDER BY t.posted_date DESC, t.id DESC LIMIT ?",
        page_params + [limit])
    conn.close()
    return jsonify({"rows": rows, "total": total,
                    "next_before": rows[-1]["id"] if len(rows) == limit else None})


@finance.route("/transactions", methods=["POST"])
@require_token
def create_transaction():
    d = body()
    conn = connect()
    account = _account(conn, d.get("account_id") or 0)
    if not account:
        conn.close()
        return jsonify({"error": "no such account"}), 400

    posted = _valid_date(d.get("posted_date") or str(today_local()))
    if not posted:
        conn.close()
        return jsonify({"error": "posted_date must be YYYY-MM-DD"}), 400

    try:
        cents = d["amount_cents"] if "amount_cents" in d else to_cents(d.get("amount"))
        if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
            raise ValueError("amount_cents must be a positive integer")
    except (ValueError, KeyError) as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    direction = d.get("direction") or "debit"
    if direction not in DIRECTIONS:
        conn.close()
        return jsonify({"error": "direction must be debit or credit"}), 400

    merchant_raw = (d.get("merchant") or d.get("merchant_raw") or "").strip()
    if not merchant_raw:
        conn.close()
        return jsonify({"error": "merchant required"}), 400
    normalized = normalize_merchant(merchant_raw)

    category_id = d.get("category_id")
    if category_id is not None and not _category(conn, category_id):
        conn.close()
        return jsonify({"error": "no such category"}), 400

    # No category given -> let the rules try before the row lands
    # uncategorized. A category the user picked always wins.
    category_source = "manual"
    if category_id is None:
        hit = apply_rules_to(conn, normalized, account["id"], active_profile())
        if hit:
            category_id = hit[1]
            category_source = "rule"

    base_key = dedupe_key(account["id"], posted, cents, normalized)
    existing = one(conn, "SELECT id, merchant_raw FROM fin_transactions WHERE dedupe_key=?",
                   (base_key,))
    if existing and not d.get("force"):
        # Same account, day, amount and merchant. Sometimes real (two
        # identical coffees) - so this is a 409 the client can retry with
        # force:true, never a silent drop and never a silent double-log.
        conn.close()
        return jsonify({"duplicate": True, "existing_id": existing["id"],
                        "error": "identical transaction already logged for this day"}), 409
    key = _free_dedupe_key(conn, base_key) if existing else base_key

    cur = conn.execute(
        "INSERT INTO fin_transactions "
        "(account_id,posted_date,amount_cents,direction,merchant_raw,merchant_normalized,"
        " category_id,category_source,is_pending,notes,source,dedupe_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (account["id"], posted, cents, direction, merchant_raw, normalized,
         category_id, category_source, 1 if d.get("is_pending") else 0,
         (d.get("notes") or "").strip() or None, "manual", key),
    )
    conn.commit()
    out = _transaction(conn, cur.lastrowid)
    conn.close()
    return jsonify(out), 201


@finance.route("/transactions/<int:tid>", methods=["GET"])
@require_token
def get_transaction(tid):
    conn = connect()
    tx = one(conn,
        "SELECT t.*, a.name AS account_name, c.name AS category_name, "
        "c.color AS category_color "
        "FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        "LEFT JOIN fin_categories c ON c.id=t.category_id "
        "WHERE t.id=? AND a.profile_id=?", (tid, active_profile()))
    conn.close()
    if not tx:
        return jsonify({"error": "no such transaction"}), 404
    return jsonify(tx)


@finance.route("/transactions/<int:tid>", methods=["PATCH"])
@require_token
def update_transaction(tid):
    conn = connect()
    tx = _transaction(conn, tid)
    if not tx:
        conn.close()
        return jsonify({"error": "no such transaction"}), 404
    d = body()

    updates = {}
    if "category_id" in d:
        if d["category_id"] is not None and not _category(conn, d["category_id"]):
            conn.close()
            return jsonify({"error": "no such category"}), 400
        updates["category_id"] = d["category_id"]
        # A category set by hand is a user decision; rules and AI never
        # overwrite these (enforced when those arrive in later phases).
        updates["category_source"] = "manual"
    if "account_id" in d:
        if not _account(conn, d["account_id"]):
            conn.close()
            return jsonify({"error": "no such account"}), 400
        updates["account_id"] = d["account_id"]
    if "posted_date" in d:
        posted = _valid_date(d["posted_date"])
        if not posted:
            conn.close()
            return jsonify({"error": "posted_date must be YYYY-MM-DD"}), 400
        updates["posted_date"] = posted
    if "amount" in d or "amount_cents" in d:
        try:
            cents = d["amount_cents"] if "amount_cents" in d else to_cents(d.get("amount"))
            if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
                raise ValueError("amount_cents must be a positive integer")
        except ValueError as e:
            conn.close()
            return jsonify({"error": str(e)}), 400
        updates["amount_cents"] = cents
    if "direction" in d:
        if d["direction"] not in DIRECTIONS:
            conn.close()
            return jsonify({"error": "direction must be debit or credit"}), 400
        updates["direction"] = d["direction"]
    for f in ("is_pending", "notes"):
        if f in d:
            updates[f] = d[f]
    # merchant_raw is deliberately not editable: the spec's contract is that
    # it is written once, exactly as entered. Delete and re-log a typo - the
    # quick form makes that a five-second fix.

    if not updates:
        conn.close()
        return jsonify(tx)

    # Anything identity-bearing changed -> the dedupe key must follow, or a
    # future import would judge duplicates against a stale fingerprint.
    if {"account_id", "posted_date", "amount_cents"} & set(updates):
        merged = {**tx, **updates}
        base = dedupe_key(merged["account_id"], merged["posted_date"],
                          merged["amount_cents"], tx["merchant_normalized"])
        if base != tx["dedupe_key"].split("|")[0]:
            updates["dedupe_key"] = _free_dedupe_key(conn, base)

    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE fin_transactions SET {sets}, updated_at=datetime('now') WHERE id=?",
        list(updates.values()) + [tid])
    conn.commit()
    out = _transaction(conn, tid)
    conn.close()
    return jsonify(out)


@finance.route("/transactions/<int:tid>", methods=["DELETE"])
@require_token
def delete_transaction(tid):
    conn = connect()
    if not _transaction(conn, tid):
        conn.close()
        return jsonify({"error": "no such transaction"}), 404
    conn.execute("DELETE FROM fin_transactions WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": tid})


@finance.route("/transactions/bulk", methods=["POST"])
@require_token
def bulk_transactions():
    d = body()
    action = d.get("action")
    ids = d.get("ids") or []
    if action not in ("categorize", "delete") or not isinstance(ids, list) or not ids:
        return jsonify({"error": "action (categorize|delete) and ids[] required"}), 400

    conn = connect()
    # Resolve ownership up front; ids from another profile simply don't match.
    owned = [r["id"] for r in many(conn,
        f"SELECT t.id FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        f"WHERE a.profile_id=? AND t.id IN ({','.join('?' * len(ids))})",
        [active_profile()] + ids)]
    if not owned:
        conn.close()
        return jsonify({"error": "no matching transactions"}), 404

    marks = ",".join("?" * len(owned))
    if action == "delete":
        conn.execute(f"DELETE FROM fin_transactions WHERE id IN ({marks})", owned)
    else:
        category_id = d.get("category_id")
        if category_id is not None and not _category(conn, category_id):
            conn.close()
            return jsonify({"error": "no such category"}), 400
        conn.execute(
            f"UPDATE fin_transactions SET category_id=?, category_source='manual', "
            f"updated_at=datetime('now') WHERE id IN ({marks})",
            [category_id] + owned)
    conn.commit()
    conn.close()
    return jsonify({"affected": len(owned), "skipped": len(ids) - len(owned)})


@finance.route("/merchants", methods=["GET"])
@require_token
def merchants():
    """
    Autocomplete support for the quick-entry form: recent merchants with the
    category and account they last used, so picking "wawa" pre-fills both.
    Aggregated in code over the recent window - most recent first, frequency
    as the tiebreak.
    """
    conn = connect()
    rows = many(conn,
        "SELECT t.id, t.merchant_raw, t.merchant_normalized, t.category_id, t.account_id "
        "FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        "WHERE a.profile_id=? ORDER BY t.id DESC LIMIT 500",
        (active_profile(),))
    conn.close()

    seen = {}
    for r in rows:                       # rows arrive newest-first
        key = r["merchant_normalized"]
        if key in seen:
            seen[key]["count"] += 1
        else:
            seen[key] = {"merchant": r["merchant_raw"], "category_id": r["category_id"],
                         "account_id": r["account_id"], "count": 1, "last_id": r["id"]}
    out = sorted(seen.values(), key=lambda m: (-m["last_id"], -m["count"]))[:60]
    return jsonify(out)


# ----------------------------------------------------------- income sources

@finance.route("/income-sources", methods=["GET"])
@require_token
def list_income_sources():
    conn = connect()
    rows = many(conn, "SELECT * FROM fin_income_sources WHERE profile_id=? "
                      "ORDER BY is_active DESC, id", (active_profile(),))
    conn.close()
    return jsonify(rows)


@finance.route("/income-sources", methods=["POST"])
@require_token
def create_income_source():
    d = body()
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    cadence = d.get("cadence") or "irregular"
    if cadence not in CADENCES:
        return jsonify({"error": f"cadence must be one of {', '.join(CADENCES)}"}), 400
    expected = None
    if d.get("expected_amount") not in (None, ""):
        try:
            expected = to_cents(d["expected_amount"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    elif d.get("expected_amount_cents") is not None:
        expected = d["expected_amount_cents"]

    conn = connect()
    if d.get("account_id") is not None and not _account(conn, d["account_id"]):
        conn.close()
        return jsonify({"error": "no such account"}), 400
    cur = conn.execute(
        "INSERT INTO fin_income_sources (profile_id,name,expected_amount_cents,cadence,account_id) "
        "VALUES (?,?,?,?,?)",
        (active_profile(), name, expected, cadence, d.get("account_id")))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_income_sources WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@finance.route("/income-sources/<int:iid>", methods=["PATCH"])
@require_token
def update_income_source(iid):
    conn = connect()
    row = one(conn, "SELECT * FROM fin_income_sources WHERE id=? AND profile_id=?",
              (iid, active_profile()))
    if not row:
        conn.close()
        return jsonify({"error": "no such income source"}), 404
    d = body()
    if "cadence" in d and d["cadence"] not in CADENCES:
        conn.close()
        return jsonify({"error": f"cadence must be one of {', '.join(CADENCES)}"}), 400
    for f in ("name", "expected_amount_cents", "cadence", "account_id", "is_active"):
        if f in d:
            conn.execute(f"UPDATE fin_income_sources SET {f}=? WHERE id=?", (d[f], iid))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_income_sources WHERE id=?", (iid,))
    conn.close()
    return jsonify(out)


# ------------------------------------------------------------------- rules
#
# Deterministic categorization, evaluated before any AI is ever consulted:
# active rules in priority order (lower first), first match wins, matched
# against merchant_normalized. Two invariants:
#   1. A rule never overwrites category_source='manual'. User decisions are
#      final - enforced here, in one place, not per caller.
#   2. AI-suggested rules are born inactive. They do nothing until a person
#      turns them on.

MATCH_TYPES = ("contains", "starts_with", "exact", "regex")
RULE_ORIGINS = ("user", "ai_suggested")
MAX_PATTERN_LEN = 200

# User input compiled into a regex engine deserves suspicion. Python's re
# has no timeout, so catastrophic patterns are rejected up front: nested
# quantifiers - (a+)+, (a*)*{2} and friends - are what turn a 40-character
# merchant string into exponential backtracking. The subject is always a
# short normalized merchant, so this heuristic plus the length cap closes
# the practical attack surface.
_CATASTROPHIC = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*{]")


def validate_pattern(match_type, pattern):
    """Returns an error string, or None if the pattern is acceptable."""
    if not pattern or len(pattern) > MAX_PATTERN_LEN:
        return f"pattern required, max {MAX_PATTERN_LEN} chars"
    if match_type == "regex":
        if _CATASTROPHIC.search(pattern):
            return "pattern rejected: nested quantifiers invite catastrophic backtracking"
        try:
            re.compile(pattern)
        except re.error as e:
            return f"invalid regex: {e}"
    return None


def _rule_matches(rule, merchant_normalized, account_id):
    if rule["account_id"] and rule["account_id"] != account_id:
        return False
    p = rule["pattern"].lower() if rule["match_type"] != "regex" else rule["pattern"]
    m = merchant_normalized
    if rule["match_type"] == "contains":
        return p in m
    if rule["match_type"] == "starts_with":
        return m.startswith(p)
    if rule["match_type"] == "exact":
        return m == p
    try:
        return bool(re.search(p, m[:MAX_PATTERN_LEN]))
    except re.error:
        return False


def apply_rules_to(conn, merchant_normalized, account_id, profile_id):
    """First matching active rule -> (rule_id, category_id), else None."""
    rules = many(conn,
        "SELECT * FROM fin_category_rules WHERE profile_id=? AND is_active=1 "
        "ORDER BY priority, id", (profile_id,))
    for rule in rules:
        if _rule_matches(rule, merchant_normalized, account_id):
            conn.execute(
                "UPDATE fin_category_rules SET hit_count=hit_count+1, "
                "last_matched_at=datetime('now') WHERE id=?", (rule["id"],))
            return rule["id"], rule["category_id"]
    return None


@finance.route("/rules", methods=["GET"])
@require_token
def list_rules():
    conn = connect()
    rows = many(conn,
        "SELECT r.*, c.name AS category_name, c.color AS category_color, "
        "a.name AS account_name "
        "FROM fin_category_rules r "
        "JOIN fin_categories c ON c.id=r.category_id "
        "LEFT JOIN fin_accounts a ON a.id=r.account_id "
        "WHERE r.profile_id=? ORDER BY r.is_active DESC, r.priority, r.id",
        (active_profile(),))
    conn.close()
    return jsonify(rows)


@finance.route("/rules", methods=["POST"])
@require_token
def create_rule():
    d = body()
    match_type = d.get("match_type") or "contains"
    if match_type not in MATCH_TYPES:
        return jsonify({"error": f"match_type must be one of {', '.join(MATCH_TYPES)}"}), 400
    pattern = (d.get("pattern") or "").strip()
    err = validate_pattern(match_type, pattern)
    if err:
        return jsonify({"error": err}), 400
    origin = d.get("origin") or "user"
    if origin not in RULE_ORIGINS:
        return jsonify({"error": "bad origin"}), 400

    conn = connect()
    if not _category(conn, d.get("category_id") or 0):
        conn.close()
        return jsonify({"error": "no such category"}), 400
    if d.get("account_id") is not None and not _account(conn, d["account_id"]):
        conn.close()
        return jsonify({"error": "no such account"}), 400
    cur = conn.execute(
        "INSERT INTO fin_category_rules "
        "(profile_id,match_type,pattern,category_id,account_id,priority,is_active,origin) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (active_profile(), match_type, pattern, d["category_id"], d.get("account_id"),
         int(d.get("priority", 100)),
         # AI suggestions are inactive until a person flips them on.
         0 if origin == "ai_suggested" else (1 if d.get("is_active", 1) else 0),
         origin))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_category_rules WHERE id=?", (cur.lastrowid,))
    conn.close()
    return jsonify(out), 201


@finance.route("/rules/<int:rid>", methods=["PATCH", "DELETE"])
@require_token
def modify_rule(rid):
    conn = connect()
    rule = one(conn, "SELECT * FROM fin_category_rules WHERE id=? AND profile_id=?",
               (rid, active_profile()))
    if not rule:
        conn.close()
        return jsonify({"error": "no such rule"}), 404
    if request.method == "DELETE":
        conn.execute("DELETE FROM fin_category_rules WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": rid})

    d = body()
    if "match_type" in d and d["match_type"] not in MATCH_TYPES:
        conn.close()
        return jsonify({"error": "bad match_type"}), 400
    if "pattern" in d or "match_type" in d:
        err = validate_pattern(d.get("match_type", rule["match_type"]),
                               (d.get("pattern", rule["pattern"]) or "").strip())
        if err:
            conn.close()
            return jsonify({"error": err}), 400
    if "category_id" in d and not _category(conn, d["category_id"]):
        conn.close()
        return jsonify({"error": "no such category"}), 400
    for f in ("match_type", "pattern", "category_id", "account_id",
              "priority", "is_active"):
        if f in d:
            conn.execute(f"UPDATE fin_category_rules SET {f}=? WHERE id=?", (d[f], rid))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_category_rules WHERE id=?", (rid,))
    conn.close()
    return jsonify(out)


@finance.route("/rules/apply", methods=["POST"])
@require_token
def apply_rules_endpoint():
    """
    Re-run rules over uncategorized transactions only - never over anything
    already categorized, which makes overwriting a manual decision
    structurally impossible here. ?dry_run=true previews without writing.
    """
    dry = request.args.get("dry_run") == "true"
    conn = connect()
    txs = many(conn,
        "SELECT t.id, t.merchant_raw, t.merchant_normalized, t.account_id "
        "FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        "WHERE a.profile_id=? AND t.category_id IS NULL", (active_profile(),))
    changes = []
    for t in txs:
        hit = apply_rules_to(conn, t["merchant_normalized"], t["account_id"],
                             active_profile())
        if hit:
            rule_id, category_id = hit
            cat = one(conn, "SELECT name FROM fin_categories WHERE id=?", (category_id,))
            changes.append({"transaction_id": t["id"], "merchant": t["merchant_raw"],
                            "rule_id": rule_id, "category_id": category_id,
                            "category_name": cat["name"] if cat else None})
            if not dry:
                conn.execute(
                    "UPDATE fin_transactions SET category_id=?, category_source='rule', "
                    "updated_at=datetime('now') WHERE id=?", (category_id, t["id"]))
    if dry:
        conn.rollback()      # discard hit_count bumps from a preview
    else:
        conn.commit()
    conn.close()
    return jsonify({"dry_run": dry, "scanned": len(txs),
                    "matched": len(changes), "changes": changes})


# --------------------------------------------------------------- CSV import
#
# One small parser per institution, in a registry keyed by format id. Each
# declares the header signature it is detected by and a row -> fields
# mapping. Adding a bank is a new entry here; the pipeline (preview/commit)
# never changes. When nothing matches, the client falls back to a manual
# column mapping built from the same primitives.

def _parse_money_cell(s):
    """A CSV money cell into (abs_cents, was_negative). '' -> (None, False)."""
    s = (s or "").strip()
    if not s:
        return None, False
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = s.strip("()").lstrip("-")
    cents = to_cents(s)
    return abs(cents), neg


def _parse_date_cell(s, fmts):
    s = (s or "").strip()
    for f in fmts:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")

# Header names that smell like card or account numbers are dropped at parse
# time - never stored, never echoed back in a preview.
_SENSITIVE_HEADER = re.compile(r"card|acct|account\s*(no|num)|member", re.I)


def _capital_one_row(row):
    debit, _ = _parse_money_cell(row.get("Debit"))
    credit, _ = _parse_money_cell(row.get("Credit"))
    if debit is None and credit is None:
        return None
    return {
        "posted_date": _parse_date_cell(row.get("Posted Date"), _DATE_FORMATS),
        "amount_cents": debit if debit is not None else credit,
        "direction": "debit" if debit is not None else "credit",
        "merchant_raw": (row.get("Description") or "").strip(),
    }


def _discover_row(row):
    # Discover reports charges positive and payments/credits negative.
    cents, neg = _parse_money_cell(row.get("Amount"))
    if cents is None:
        return None
    return {
        "posted_date": _parse_date_cell(row.get("Post Date"), _DATE_FORMATS),
        "amount_cents": cents,
        "direction": "credit" if neg else "debit",
        "merchant_raw": (row.get("Description") or "").strip(),
    }


def _capital_one_360_row(row):
    # 360 Checking: direction is its own column, amounts always positive,
    # dates are MM/DD/YY. The Balance column is not imported per-row (it is
    # derived state by definition) - but the newest row's value becomes the
    # account's balance anchor at commit time, which is what makes the
    # derived balance exact instead of assuming the account started at $0.
    cents, _ = _parse_money_cell(row.get("Transaction Amount"))
    if cents is None:
        return None
    ttype = (row.get("Transaction Type") or "").strip().lower()
    if ttype not in ("debit", "credit"):
        return None
    return {
        "posted_date": _parse_date_cell(row.get("Transaction Date"), _DATE_FORMATS),
        "amount_cents": cents,
        "direction": ttype,
        "merchant_raw": (row.get("Transaction Description") or "").strip(),
    }


def _capital_one_360_anchor(raw_rows):
    """The newest row's running balance -> {date, cents}, or None."""
    best = None
    for row in raw_rows:
        date = _parse_date_cell(row.get("Transaction Date"), _DATE_FORMATS)
        try:
            cents, neg = _parse_money_cell(row.get("Balance"))
        except ValueError:
            continue
        if date and cents is not None:
            if not best or date > best["date"]:
                best = {"date": date, "cents": -cents if neg else cents}
    return best


CSV_FORMATS = {
    "capital_one": {
        "label": "Capital One card",
        "signature": {"Transaction Date", "Posted Date", "Description", "Debit", "Credit"},
        "row": _capital_one_row,
    },
    "capital_one_360": {
        "label": "Capital One 360 Checking",
        "signature": {"Transaction Description", "Transaction Date",
                      "Transaction Type", "Transaction Amount"},
        "row": _capital_one_360_row,
        "anchor": _capital_one_360_anchor,
    },
    "discover": {
        "label": "Discover",
        "signature": {"Trans. Date", "Post Date", "Description", "Amount"},
        "row": _discover_row,
    },
}


def detect_format(headers):
    hs = set(h.strip() for h in headers)
    for fid, f in CSV_FORMATS.items():
        if f["signature"] <= hs:
            return fid
    return None


def _generic_row_fn(mapping):
    """
    Build a row parser from a user-supplied column mapping - the fallback
    when no registered format matches. mapping: {date_col, merchant_col,
    amount_col} or {..., debit_col, credit_col}; flip_sign inverts the
    charges-are-negative convention some issuers use.
    """
    def parse(row):
        merchant = (row.get(mapping.get("merchant_col") or "") or "").strip()
        date = _parse_date_cell(row.get(mapping.get("date_col") or ""), _DATE_FORMATS)
        if mapping.get("debit_col") or mapping.get("credit_col"):
            debit, _ = _parse_money_cell(row.get(mapping.get("debit_col") or ""))
            credit, _ = _parse_money_cell(row.get(mapping.get("credit_col") or ""))
            if debit is None and credit is None:
                return None
            cents = debit if debit is not None else credit
            direction = "debit" if debit is not None else "credit"
        else:
            cents, neg = _parse_money_cell(row.get(mapping.get("amount_col") or ""))
            if cents is None:
                return None
            if mapping.get("flip_sign"):
                neg = not neg
            direction = "credit" if neg else "debit"
        return {"posted_date": date, "amount_cents": cents,
                "direction": direction, "merchant_raw": merchant}
    return parse


def _read_csv(file_storage):
    """Decode and parse an uploaded CSV. Returns (headers, dict_rows)."""
    raw = file_storage.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"file larger than {MAX_UPLOAD_MB}MB")
    text = raw.decode("utf-8-sig", errors="replace")   # -sig eats Excel's BOM
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = list(reader)
    if not headers or not rows:
        raise ValueError("empty or headerless CSV")
    return headers, rows


def _parse_upload(account, form):
    """Shared by preview and commit: upload -> normalized candidate rows."""
    if "file" not in request.files:
        raise ValueError("multipart 'file' required")
    headers, raw_rows = _read_csv(request.files["file"])

    fmt = form.get("format") or detect_format(headers)
    anchor = None
    if fmt and fmt in CSV_FORMATS:
        row_fn = CSV_FORMATS[fmt]["row"]
        anchor_fn = CSV_FORMATS[fmt].get("anchor")
        if anchor_fn:
            # Computed from the raw rows before the sensitive-column strip:
            # the running-balance column is read here and never stored per-row.
            anchor = anchor_fn(raw_rows)
    elif form.get("mapping"):
        import json as _json
        row_fn = _generic_row_fn(_json.loads(form["mapping"]))
        fmt = "custom"
    else:
        return fmt, headers, None      # undetected: caller returns headers for the mapping UI

    parsed, bad = [], 0
    for raw in raw_rows:
        # Drop card/account-number-ish columns before anything is retained.
        raw = {k: v for k, v in raw.items() if k and not _SENSITIVE_HEADER.search(k)}
        try:
            r = row_fn(raw)
        except ValueError:
            r = None
        if not r or not r["posted_date"] or not r["merchant_raw"] or not r["amount_cents"]:
            bad += 1
            continue
        r["merchant_normalized"] = normalize_merchant(r["merchant_raw"])
        r["dedupe_key"] = dedupe_key(account["id"], r["posted_date"],
                                     r["amount_cents"], r["merchant_normalized"])
        parsed.append(r)
    return fmt, headers, {"rows": parsed, "skipped_unparseable": bad, "anchor": anchor}


@finance.route("/import/preview", methods=["POST"])
@require_token
def import_preview():
    """Parse and classify; writes nothing. The preview is the contract:
    what you see marked "new" is exactly what commit will write."""
    conn = connect()
    account = _account(conn, request.form.get("account_id") or 0)
    if not account:
        conn.close()
        return jsonify({"error": "account_id (an existing account) required"}), 400
    try:
        fmt, headers, parsed = _parse_upload(account, request.form)
    except ValueError as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    if parsed is None:
        conn.close()
        return jsonify({"format": None, "headers": headers,
                        "error": "unrecognized format - supply a column mapping"}), 422

    known = {r["dedupe_key"] for r in many(
        conn, "SELECT dedupe_key FROM fin_transactions WHERE account_id=?", (account["id"],))}
    conn.close()

    seen_in_file = set()
    out_rows = []
    for r in parsed["rows"]:
        dup = r["dedupe_key"] in known or r["dedupe_key"] in seen_in_file
        seen_in_file.add(r["dedupe_key"])
        out_rows.append({**r, "duplicate": dup})
    new = sum(1 for r in out_rows if not r["duplicate"])
    return jsonify({
        "format": fmt, "account_id": account["id"], "rows": out_rows,
        "new_count": new, "duplicate_count": len(out_rows) - new,
        "skipped_unparseable": parsed["skipped_unparseable"],
        "anchor": parsed.get("anchor"),
    })


@finance.route("/import/commit", methods=["POST"])
@require_token
def import_commit():
    """
    Write the reviewed rows, in one transaction. The client sends back the
    previewed rows (with force:true on any duplicate the user chose to keep);
    the server re-derives normalization and dedupe keys rather than trusting
    the client's, and re-checks duplicates at commit time - the ledger may
    have changed since the preview.
    """
    d = body()
    conn = connect()
    account = _account(conn, d.get("account_id") or 0)
    if not account:
        conn.close()
        return jsonify({"error": "no such account"}), 400
    rows = d.get("rows") or []
    if not isinstance(rows, list) or not rows:
        conn.close()
        return jsonify({"error": "rows[] required"}), 400

    imported, skipped, forced = 0, 0, 0
    try:
        for r in rows:
            posted = _valid_date(r.get("posted_date"))
            cents = r.get("amount_cents")
            direction = r.get("direction")
            merchant_raw = (r.get("merchant_raw") or "").strip()
            if (not posted or direction not in DIRECTIONS or not merchant_raw
                    or not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0):
                skipped += 1
                continue
            normalized = normalize_merchant(merchant_raw)
            base = dedupe_key(account["id"], posted, cents, normalized)
            exists = one(conn, "SELECT 1 FROM fin_transactions WHERE dedupe_key=?", (base,))
            if exists and not r.get("force"):
                skipped += 1
                continue
            key = _free_dedupe_key(conn, base) if exists else base
            if exists:
                forced += 1
            # Rules run at commit, so a roster of known merchants files
            # itself; anything unmatched lands uncategorized for AI/manual.
            hit = apply_rules_to(conn, normalized, account["id"], active_profile())
            category_id, cat_source = (hit[1], "rule") if hit else (None, "import")
            conn.execute(
                "INSERT INTO fin_transactions "
                "(account_id,posted_date,amount_cents,direction,merchant_raw,"
                " merchant_normalized,category_id,category_source,source,dedupe_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (account["id"], posted, cents, direction, merchant_raw,
                 normalized, category_id, cat_source, "csv", key))
            imported += 1

        # Some formats carry a running balance; the newest row's value
        # anchors this account's derived balance. Only ever moves forward.
        anchor = d.get("anchor")
        anchored = False
        if (isinstance(anchor, dict) and _valid_date(anchor.get("date"))
                and isinstance(anchor.get("cents"), int)
                and not isinstance(anchor.get("cents"), bool)):
            if (not account["balance_anchor_date"]
                    or anchor["date"] >= account["balance_anchor_date"]):
                conn.execute(
                    "UPDATE fin_accounts SET balance_anchor_cents=?, "
                    "balance_anchor_date=?, updated_at=datetime('now') WHERE id=?",
                    (anchor["cents"], anchor["date"], account["id"]))
                anchored = True
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return jsonify({"imported": imported, "skipped_duplicates": skipped,
                    "imported_despite_duplicate": forced, "anchored": anchored}), 201


# ------------------------------------------------- balances, budgets, summary
#
# /summary is the single source of computed truth: the UI, the Today
# widget and the AI layer all read figures from here and none of them
# recompute totals independently. Everything is derived on read from the
# transactions ledger - the only stored "balance" fact is each account's
# anchor (a known-true balance on a date), and the current balance is
# anchor + ledger-after-anchor.


def derived_balance(conn, account):
    """
    (balance_cents, basis) for one account.

    Checking/cash: balance = anchor + credits - debits since the anchor.
    Credit:        balance = amount OWED = anchor + debits - credits.
    No anchor -> derived across the whole ledger from an assumed $0 start,
    and basis says so - an honest "probably incomplete" beats a confident
    wrong number.
    """
    anchor_cents = account["balance_anchor_cents"]
    anchor_date = account["balance_anchor_date"]
    where = "account_id=?"
    params = [account["id"]]
    if anchor_cents is not None and anchor_date:
        where += " AND posted_date>?"
        params.append(anchor_date)
        base = anchor_cents
        basis = f"anchored {anchor_date}"
    else:
        base = 0
        basis = "no anchor: assumes $0 start"

    row = conn.execute(
        f"SELECT COALESCE(SUM(CASE WHEN direction='credit' THEN amount_cents ELSE 0 END),0) AS cr, "
        f"COALESCE(SUM(CASE WHEN direction='debit' THEN amount_cents ELSE 0 END),0) AS db "
        f"FROM fin_transactions WHERE {where}", params).fetchone()
    if account["type"] == "credit":
        return base + row["db"] - row["cr"], basis
    return base + row["cr"] - row["db"], basis


def _month_of(period):
    """'YYYY-MM' -> ('YYYY-MM-01', 'YYYY-MM-<last day>') via string math."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", period or "")
    if not m:
        return None, None
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        return None, None
    days = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
    return f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{days:02d}"


def _category_spent(conn, category_id, start, end):
    """Net spend (debits minus refund credits) for one category in a window."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN t.direction='debit' THEN t.amount_cents "
        "ELSE -t.amount_cents END),0) AS net "
        "FROM fin_transactions t WHERE t.category_id=? "
        "AND t.posted_date>=? AND t.posted_date<=?",
        (category_id, start, end)).fetchone()
    return row["net"]


def _rollover_carry(conn, category_id, before_period_start):
    """
    What earlier envelopes pass forward: sum of (limit - spent) across this
    category's prior rollover budgets. Can go negative - overspending an
    envelope genuinely eats next month, and hiding that would defeat the
    point of envelopes.
    """
    carry = 0
    for b in many(conn,
            "SELECT * FROM fin_budgets WHERE category_id=? AND period_start<? "
            "AND rollover=1 ORDER BY period_start", (category_id, before_period_start)):
        s, e = _month_of(b["period_start"][:7])
        carry += b["limit_cents"] - _category_spent(conn, category_id, s, e)
    return carry


@finance.route("/budgets", methods=["GET"])
@require_token
def list_budgets():
    period = request.args.get("period_start", "")[:7] or str(today_local())[:7]
    start, _ = _month_of(period)
    if not start:
        return jsonify({"error": "period_start must be YYYY-MM-01"}), 400
    conn = connect()
    rows = many(conn,
        "SELECT b.*, c.name AS category_name, c.color AS category_color "
        "FROM fin_budgets b JOIN fin_categories c ON c.id=b.category_id "
        "WHERE c.profile_id=? AND b.period_start=? ORDER BY c.sort_order",
        (active_profile(), start))
    conn.close()
    return jsonify(rows)


@finance.route("/budgets", methods=["POST"])
@require_token
def create_budget():
    d = body()
    conn = connect()
    cat = _category(conn, d.get("category_id") or 0)
    if not cat:
        conn.close()
        return jsonify({"error": "no such category"}), 400
    if cat["is_income"] or cat["is_transfer"]:
        conn.close()
        return jsonify({"error": "budgets apply to spending categories"}), 400
    start, _ = _month_of((d.get("period_start") or "")[:7])
    if not start:
        conn.close()
        return jsonify({"error": "period_start must be YYYY-MM-01"}), 400
    try:
        limit_cents = (d["limit_cents"] if "limit_cents" in d
                       else to_cents(d.get("limit")))
    except (ValueError, KeyError) as e:
        conn.close()
        return jsonify({"error": str(e) or "limit required"}), 400

    # Upsert: setting this month's Groceries envelope twice should move it,
    # not error - the unique constraint is identity, not access control.
    conn.execute(
        "INSERT INTO fin_budgets (category_id,period_start,limit_cents,rollover) "
        "VALUES (?,?,?,?) ON CONFLICT(category_id,period_start) "
        "DO UPDATE SET limit_cents=excluded.limit_cents, rollover=excluded.rollover",
        (cat["id"], start, limit_cents, 1 if d.get("rollover") else 0))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_budgets WHERE category_id=? AND period_start=?",
              (cat["id"], start))
    conn.close()
    return jsonify(out), 201


@finance.route("/budgets/<int:bid>", methods=["PATCH", "DELETE"])
@require_token
def modify_budget(bid):
    conn = connect()
    b = one(conn,
        "SELECT b.* FROM fin_budgets b JOIN fin_categories c ON c.id=b.category_id "
        "WHERE b.id=? AND c.profile_id=?", (bid, active_profile()))
    if not b:
        conn.close()
        return jsonify({"error": "no such budget"}), 404
    if request.method == "DELETE":
        conn.execute("DELETE FROM fin_budgets WHERE id=?", (bid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": bid})
    d = body()
    if "limit" in d:
        try:
            d["limit_cents"] = to_cents(d.pop("limit"))
        except ValueError as e:
            conn.close()
            return jsonify({"error": str(e)}), 400
    for f in ("limit_cents", "rollover"):
        if f in d:
            conn.execute(f"UPDATE fin_budgets SET {f}=? WHERE id=?", (d[f], bid))
    conn.commit()
    out = one(conn, "SELECT * FROM fin_budgets WHERE id=?", (bid,))
    conn.close()
    return jsonify(out)


@finance.route("/budgets/copy-from", methods=["POST"])
@require_token
def copy_budgets():
    """Copy one month's envelopes into another. Nobody rebuilds them from
    scratch monthly; existing target rows are left alone, not overwritten."""
    d = body()
    src, _ = _month_of((d.get("source_period") or "")[:7])
    dst, _ = _month_of((d.get("target_period") or "")[:7])
    if not src or not dst or src == dst:
        return jsonify({"error": "source_period and target_period (YYYY-MM) required"}), 400
    conn = connect()
    copied = 0
    for b in many(conn,
            "SELECT b.* FROM fin_budgets b JOIN fin_categories c ON c.id=b.category_id "
            "WHERE c.profile_id=? AND b.period_start=?", (active_profile(), src)):
        cur = conn.execute(
            "INSERT OR IGNORE INTO fin_budgets (category_id,period_start,limit_cents,rollover) "
            "VALUES (?,?,?,?)", (b["category_id"], dst, b["limit_cents"], b["rollover"]))
        copied += cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({"copied": copied, "source": src, "target": dst})


def compute_summary(conn, pid, period, end_override=None):
    """
    The one place computed figures come from. The /summary endpoint, the
    Today widget and the AI layer all read this - the AI is handed these
    numbers and never derives its own (spec rule: the model does language
    and judgment, not math).

    Returns a dict, or None for a malformed period.
    """
    start, end = _month_of(period)
    if not start:
        return None
    if end_override:
        end = end_override

    income = conn.execute(
        "SELECT COALESCE(SUM(t.amount_cents),0) FROM fin_transactions t "
        "JOIN fin_accounts a ON a.id=t.account_id "
        "JOIN fin_categories c ON c.id=t.category_id "
        "WHERE a.profile_id=? AND c.is_income=1 AND t.direction='credit' "
        "AND t.posted_date>=? AND t.posted_date<=?", (pid, start, end)).fetchone()[0]

    # Per-category net spend. Transfer and income categories are excluded
    # here, in the query - not in the UI.
    cat_rows = many(conn,
        "SELECT c.id, c.name, c.color, "
        "COALESCE(SUM(CASE WHEN t.direction='debit' THEN t.amount_cents "
        "                  WHEN t.direction='credit' THEN -t.amount_cents END),0) AS spent_cents "
        "FROM fin_categories c "
        "LEFT JOIN fin_transactions t ON t.category_id=c.id "
        "  AND t.posted_date>=? AND t.posted_date<=? "
        "WHERE c.profile_id=? AND c.is_income=0 AND c.is_transfer=0 "
        "GROUP BY c.id ORDER BY c.sort_order", (start, end, pid))

    uncat = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN t.direction='debit' "
        "THEN t.amount_cents ELSE -t.amount_cents END),0) "
        "FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        "WHERE a.profile_id=? AND t.category_id IS NULL "
        "AND t.posted_date>=? AND t.posted_date<=?", (pid, start, end)).fetchone()
    uncat_count, uncat_spent = uncat[0], uncat[1]

    budgets = {b["category_id"]: b for b in many(conn,
        "SELECT b.* FROM fin_budgets b JOIN fin_categories c ON c.id=b.category_id "
        "WHERE c.profile_id=? AND b.period_start=?", (pid, start))}

    categories = []
    for c in cat_rows:
        entry = dict(c)
        b = budgets.get(c["id"])
        if b:
            carry = _rollover_carry(conn, c["id"], start) if b["rollover"] else 0
            effective = b["limit_cents"] + carry
            entry.update(budget_id=b["id"], limit_cents=b["limit_cents"],
                         rollover=bool(b["rollover"]), carry_cents=carry,
                         effective_limit_cents=effective,
                         remaining_cents=effective - c["spent_cents"])
        categories.append(entry)

    spend_total = sum(c["spent_cents"] for c in cat_rows) + uncat_spent
    budget_total = sum(b["limit_cents"] for b in budgets.values())

    accounts = many(conn, "SELECT * FROM fin_accounts WHERE profile_id=? AND is_active=1 "
                          "ORDER BY id", (pid,))
    balances, net = [], 0
    for a in accounts:
        bal, basis = derived_balance(conn, a)
        balances.append({"account_id": a["id"], "name": a["name"], "type": a["type"],
                         "balance_cents": bal, "basis": basis})
        net += -bal if a["type"] == "credit" else bal

    return {
        "period": period, "from": start, "to": end,
        "income_received_cents": income,
        "spend_total_cents": spend_total,
        "budget_total_cents": budget_total,
        "to_be_budgeted_cents": income - budget_total,
        "uncategorized": {"count": uncat_count, "spent_cents": uncat_spent},
        "categories": categories,
        "balances": balances,
        "net_cents": net,
    }


@finance.route("/summary", methods=["GET"])
@require_token
def summary():
    """
    Every computed figure in one response. Transfers are excluded from all
    spending totals in the SQL itself (moving money between your own
    accounts is not spending), and income counts only when actually
    received as a transaction - expectations never masquerade as money.
    """
    period = (request.args.get("period") or request.args.get("from") or
              str(today_local()))[:7]
    conn = connect()
    out = compute_summary(conn, active_profile(), period,
                          end_override=request.args.get("to"))
    conn.close()
    if out is None:
        return jsonify({"error": "period must be YYYY-MM"}), 400
    return jsonify(out)


@finance.route("/recurring", methods=["GET"])
@require_token
def recurring():
    """
    Deterministic recurring-charge detection - a query, not an AI task:
    same normalized merchant, 3+ debits, amounts within 10% of the median,
    at a roughly regular interval. The AI layer may *describe* these; it
    never decides what qualifies.
    """
    conn = connect()
    rows = many(conn,
        "SELECT t.merchant_raw, t.merchant_normalized, t.posted_date, t.amount_cents "
        "FROM fin_transactions t JOIN fin_accounts a ON a.id=t.account_id "
        "WHERE a.profile_id=? AND t.direction='debit' "
        "ORDER BY t.merchant_normalized, t.posted_date", (active_profile(),))
    conn.close()

    groups = {}
    for r in rows:
        groups.setdefault(r["merchant_normalized"], []).append(r)

    found = []
    for key, txs in groups.items():
        if len(txs) < 3:
            continue
        amounts = sorted(t["amount_cents"] for t in txs)
        median = amounts[len(amounts) // 2]
        similar = [t for t in txs if abs(t["amount_cents"] - median) <= median * 0.10]
        if len(similar) < 3:
            continue
        dates = sorted(datetime.strptime(t["posted_date"], "%Y-%m-%d") for t in similar)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        if not gaps:
            continue
        gaps.sort()
        med_gap = gaps[len(gaps) // 2]
        if not 5 <= med_gap <= 40:
            continue
        # Regularity: most gaps within a third of the median gap.
        regular = sum(1 for g in gaps if abs(g - med_gap) <= max(3, med_gap / 3))
        if regular < len(gaps) * 0.6:
            continue
        found.append({
            "merchant": similar[-1]["merchant_raw"],
            "merchant_normalized": key,
            "amount_cents": median,
            "interval_days": med_gap,
            "times_seen": len(similar),
            "last_seen": similar[-1]["posted_date"],
            "monthly_cost_cents": round(median * 30 / med_gap),
        })
    found.sort(key=lambda f: -f["monthly_cost_cents"])
    return jsonify(found)
