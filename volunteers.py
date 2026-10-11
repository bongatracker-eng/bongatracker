"""
volunteers.py - Volunteer accounts + role-based access for BoñgaTrack.

Three kinds of login, one shared database:

    admin      -> everything (dashboard, analytics, SMS, reports, volunteers ...)
    volunteer  -> ONLY the inventory page (log incoming stock + relief
                  distribution), registration, evacuee check-in, and their own PIN
    resident   -> the resident portal

Everything a volunteer does is saved in the SAME tables the admin dashboard
reads, so the admin sees it instantly. This module also keeps an audit trail
of who did what (table staff_activity).

Setup (2 lines in app.py, after init_db() and the other register_* lines):

    from volunteers import register_volunteer_features
    register_volunteer_features(app, get_db_connection)

Then copy volunteer_home.html and admin_volunteers.html into templates/.

How it protects the app
-----------------------
A single before_request guard checks EVERY request against the RULES table
below, so you do not have to add decorators to your existing routes.
Any /api/ path that is not listed is ADMIN-ONLY by default. If a page you
use suddenly gets 401/403, add its path to RULES with the right level.
"""

import json
import re
import secrets
import time

from flask import g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

# --- settings you may want to change ------------------------------------
VOLUNTEER_SESSION_HOURS = 24      # a volunteer must log in again after this long
MAX_FAILED_LOGINS = 8             # wrong PINs allowed ...
LOCKOUT_MINUTES = 10              # ... within this window, per account + device

# --- who may open what ---------------------------------------------------
# (regex, allowed methods or None for all, level)
# levels: public | user (any login) | resident | volunteer | staff (admin or
#         volunteer) | admin | staff_or_self | self_or_admin | root
RULES = [
    (r"^/static/", None, "public"),
    (r"^/(login|api/login|api/logout|api/check-session)$", None, "public"),
    (r"^/(favicon\.ico|manifest\.json|sw\.js|service-worker\.js)$", None, "public"),
    (r"^/hazard-map$", None, "public"),
    (r"^/api/get_notifications$", None, "public"),
    (r"^/api/hazard-zones$", ("GET",), "public"),
    (r"^/api/hazard-zones/check$", None, "public"),
    (r"^/api/resident/current$", None, "public"),

    (r"^/$", None, "root"),
    (r"^/resident-portal$", None, "resident"),
    (r"^/volunteer$", None, "volunteer"),
    (r"^/api/volunteer/", None, "volunteer"),

    # Volunteer-allowed: stock in, distribution, registration, evacuee check-in
    (r"^/(registration|inventory)$", None, "staff"),
    (r"^/api/resident/[^/]+$", ("GET",), "staff_or_self"),
    (r"^/api/register$", ("POST",), "staff"),
    (r"^/api/inventory/(data|add|distribute-bundle)$", None, "staff"),
    (r"^/api/(scan-qr|scan-evac-qr|sync-offline-claims)$", None, "staff"),

    (r"^/api/update_location$", None, "user"),
    (r"^/api/change-pin$", None, "self_or_admin"),

    # Admin only (also the default for any other /api/ path)
    (r"^/(analytics|sms)$", None, "admin"),
    (r"^/admin/", None, "admin"),
]
_RULES = [(re.compile(p), m, lvl) for p, m, lvl in RULES]

# Actions saved to the audit trail when a VOLUNTEER completes them
AUDITED = {
    "/api/inventory/distribute-bundle": "RELIEF DISTRIBUTED",
    "/api/inventory/add": "STOCK ADDED",
    "/api/scan-qr": "RELIEF DISTRIBUTED",
    "/api/scan-evac-qr": "EVACUATION CHECK-IN",
    "/api/register": "REGISTERED HOUSEHOLD",
}

HOMES = {"admin": "/", "volunteer": "/volunteer", "resident": "/resident-portal"}


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------
def _init_tables(get_db):
    conn = get_db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS volunteers(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        mobile TEXT,
        pin_hash TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        must_change_pin INTEGER DEFAULT 1,
        created_at TEXT,
        last_login TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS staff_activity(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        user_id TEXT,
        user_name TEXT,
        action TEXT,
        resident_id TEXT,
        details TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS login_attempts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT,
        ts REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_login_attempts_key ON login_attempts(key, ts)")
    conn.commit()
    conn.close()


def _new_volunteer_id(conn):
    while True:
        vid = "VOL-%05d" % (10000 + secrets.randbelow(90000))
        if not conn.execute("SELECT 1 FROM volunteers WHERE id=?", (vid,)).fetchone():
            return vid


def _new_pin():
    return "%06d" % secrets.randbelow(1000000)


def _log(conn, action, details):
    conn.execute(
        "INSERT INTO transaction_log (time, actor, action, details) "
        "VALUES (datetime('now', '+8 hours'), 'ADMIN', ?, ?)", (action, details))


# ----------------------------------------------------------------------
# Who is calling?
# ----------------------------------------------------------------------
def _identity(get_db):
    rid = session.get("resident_id")
    if not rid:
        return None
    raw_role = (session.get("role") or "").lower()
    if str(rid).lower() == "admin" or raw_role == "admin":
        role = "admin"
    elif raw_role == "volunteer":
        role = "volunteer"
    else:
        role = "resident"

    if role == "volunteer":
        # Too old, disabled or deleted? Then this session is over immediately.
        if time.time() - float(session.get("login_at", 0)) > VOLUNTEER_SESSION_HOURS * 3600:
            session.clear()
            return None
        conn = get_db()
        row = conn.execute("SELECT active FROM volunteers WHERE id=?", (rid,)).fetchone()
        conn.close()
        if not row or not row["active"]:
            session.clear()
            return None

    return {"id": str(rid), "role": role, "name": session.get("head_name") or str(rid)}


def _level_for(path, method):
    for rx, methods, level in _RULES:
        if rx.match(path) and (methods is None or method in methods):
            return level
    return "admin" if path.startswith("/api/") else None


def _allowed(level, ident):
    if level == "public":
        return True
    if level == "root":
        return True
    if not ident:
        return False
    role = ident["role"]
    if level == "user":
        return True
    if level in ("admin", "volunteer", "resident"):
        return role == level
    if level == "staff":
        return role in ("admin", "volunteer")
    if level == "staff_or_self":
        if role in ("admin", "volunteer"):
            return True
        m = re.match(r"^/api/resident/([^/]+)$", request.path)
        return bool(m) and m.group(1) == ident["id"]
    if level == "self_or_admin":
        if role == "admin":
            return True
        body = request.get_json(silent=True) or {}
        return str(body.get("resident_id", "")) == ident["id"]
    return False


def _deny(ident):
    if request.path.startswith("/api/"):
        if not ident:
            return jsonify({"status": "error", "message": "Please log in again."}), 401
        return jsonify({"status": "error", "message": "You do not have access to this."}), 403
    if not ident:
        return redirect("/login")
    return redirect(HOMES[ident["role"]])


# ----------------------------------------------------------------------
# Login (volunteers) + brute-force protection
# ----------------------------------------------------------------------
def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.remote_addr or "?")


def _login_parts():
    body = request.get_json(silent=True) or {}
    identifier = str(body.get("identifier") or body.get("mobile")
                     or body.get("username") or body.get("id") or "").strip()
    pin = str(body.get("pin") or body.get("password") or "").strip()
    return identifier, pin


def _login_key(identifier):
    return f"{identifier.lower()}|{_client_ip()}"


def _is_locked(conn, key):
    since = time.time() - LOCKOUT_MINUTES * 60
    n = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE key=? AND ts>?", (key, since)).fetchone()[0]
    return n >= MAX_FAILED_LOGINS


def _handle_login(get_db):
    """Runs before your normal /api/login. Volunteers are handled here;
    everyone else falls through to your existing login route untouched."""
    identifier, pin = _login_parts()
    if not identifier:
        return None

    conn = get_db()
    try:
        if _is_locked(conn, _login_key(identifier)):
            return jsonify({
                "status": "error",
                "message": f"Too many wrong PINs. Try again in {LOCKOUT_MINUTES} minutes."
            }), 429

        vol = conn.execute("""
            SELECT * FROM volunteers
            WHERE UPPER(id) = UPPER(?) OR (mobile IS NOT NULL AND mobile != '' AND mobile = ?)
        """, (identifier, identifier)).fetchone()

        if not vol or not check_password_hash(vol["pin_hash"], pin):
            return None                     # not a volunteer login -> normal route decides

        if not vol["active"]:
            return jsonify({
                "status": "error",
                "message": "This volunteer account is disabled. Please contact the barangay admin."
            }), 401

        conn.execute("UPDATE volunteers SET last_login=datetime('now', '+8 hours') WHERE id=?", (vol["id"],))
        conn.commit()

        session.clear()
        session.permanent = True
        session["resident_id"] = vol["id"]
        session["head_name"] = vol["name"]
        session["role"] = "Volunteer"
        session["login_at"] = time.time()

        return jsonify({
            "status": "success",
            "resident_id": vol["id"],
            "head_name": vol["name"],
            "role": "Volunteer",
            "user_type": "volunteer",
            "redirect": "/volunteer"
        })
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Guard (before) and bookkeeping (after)
# ----------------------------------------------------------------------
def _enforce(get_db):
    if request.method == "OPTIONS":
        return None

    ident = _identity(get_db)
    g.ident = ident

    if request.path == "/api/login" and request.method == "POST":
        return _handle_login(get_db)

    level = _level_for(request.path, request.method)
    if level is None:
        return None

    if level == "root":
        if ident and ident["role"] == "volunteer":
            return redirect(HOMES["volunteer"])
        return None

    if not _allowed(level, ident):
        return _deny(ident)
    return None


def _after(resp, get_db):
    try:
        # 1) count wrong PINs / forget them after a good login
        if request.path == "/api/login" and request.method == "POST":
            identifier, _pin = _login_parts()
            if identifier and resp.status_code in (200, 401):
                key = _login_key(identifier)
                conn = get_db()
                if resp.status_code == 401:
                    conn.execute("INSERT INTO login_attempts (key, ts) VALUES (?, ?)", (key, time.time()))
                else:
                    conn.execute("DELETE FROM login_attempts WHERE key=?", (key,))
                conn.execute("DELETE FROM login_attempts WHERE ts < ?", (time.time() - 86400,))
                conn.commit()
                conn.close()

        # 2) audit trail: what did the volunteer just do?
        elif request.method == "POST" and request.path in AUDITED and resp.status_code == 200:
            ident = getattr(g, "ident", None)
            if ident and ident["role"] == "volunteer":
                payload = json.loads(resp.get_data(as_text=True))
                if isinstance(payload, dict) and payload.get("status") == "success":
                    body = request.get_json(silent=True) or {}
                    if request.path == "/api/register":
                        who = body.get("id")
                        details = f"{body.get('head_name', '')} ({body.get('purok', '')})"
                    elif request.path == "/api/scan-evac-qr":
                        who = body.get("resident_id")
                        details = body.get("evac_center") or ""
                    elif request.path == "/api/scan-qr":
                        who = body.get("resident_id")
                        details = body.get("location") or ""
                    elif request.path == "/api/inventory/add":
                        if not body.get("quantity"):
                            return resp
                        who = "-"        # stock has no household
                        item = body.get("other_item") if body.get("category") == "Others" else body.get("category")
                        details = f"{item or ''}: {body.get('quantity', '')} at {body.get('location', '')}"
                    else:
                        who = body.get("id") or body.get("resident_id")
                        items = ", ".join(
                            f"{i.get('quantity')} {i.get('unit', '')} {i.get('item_name', '')}".strip()
                            for i in (body.get("items") or []))
                        details = f"{body.get('location', '')}: {items}"
                    if not who:
                        return resp          # nothing identifiable to record
                    conn = get_db()
                    conn.execute("""
                        INSERT INTO staff_activity (time, user_id, user_name, action, resident_id, details)
                        VALUES (datetime('now', '+8 hours'), ?, ?, ?, ?, ?)
                    """, (ident["id"], ident["name"], AUDITED[request.path], who, details[:500]))
                    conn.commit()
                    conn.close()
    except Exception as e:      # bookkeeping must never break a real request
        print("volunteers.py bookkeeping error:", e)
    return resp


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
def register_volunteer_features(app, get_db_connection):
    _init_tables(get_db_connection)

    @app.before_request
    def _volunteer_guard():
        return _enforce(get_db_connection)

    @app.after_request
    def _volunteer_bookkeeping(resp):
        return _after(resp, get_db_connection)

    @app.context_processor
    def _inject_role():
        ident = getattr(g, "ident", None)
        role = ident["role"] if ident else None
        return {
            "is_admin": role == "admin",
            "is_volunteer": role == "volunteer",
            "is_resident": role == "resident",
            "current_role": role,
            "current_user_name": ident["name"] if ident else None,
        }

    # ---- pages ----
    @app.route("/volunteer")
    def volunteer_home_page():
        conn = get_db_connection()
        row = conn.execute("SELECT id, name, must_change_pin FROM volunteers WHERE id=?",
                           (session.get("resident_id"),)).fetchone()
        conn.close()
        return render_template("volunteer_home.html",
                               name=row["name"] if row else session.get("head_name"),
                               vid=row["id"] if row else session.get("resident_id"),
                               must_change_pin=bool(row["must_change_pin"]) if row else False)

    @app.route("/admin/volunteers")
    def admin_volunteers_page():
        return render_template("admin_volunteers.html")

    # ---- volunteer: change own PIN ----
    @app.route("/api/volunteer/change-pin", methods=["POST"])
    def volunteer_change_pin():
        data = request.get_json() or {}
        old_pin = str(data.get("old_pin") or "").strip()
        new_pin = str(data.get("new_pin") or "").strip()
        if not re.fullmatch(r"\d{4,8}", new_pin):
            return jsonify({"status": "error", "message": "New PIN must be 4 to 8 digits."}), 400
        if new_pin == old_pin:
            return jsonify({"status": "error", "message": "Choose a different PIN from your current one."}), 400

        conn = get_db_connection()
        row = conn.execute("SELECT pin_hash FROM volunteers WHERE id=?", (session.get("resident_id"),)).fetchone()
        if not row or not check_password_hash(row["pin_hash"], old_pin):
            conn.close()
            return jsonify({"status": "error", "message": "Your current PIN is not correct."}), 400
        conn.execute("UPDATE volunteers SET pin_hash=?, must_change_pin=0 WHERE id=?",
                     (generate_password_hash(new_pin, method="pbkdf2:sha256"), session.get("resident_id")))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "PIN updated."})

    # ---- admin: manage volunteers ----
    @app.route("/api/admin/volunteers", methods=["GET"])
    def admin_list_volunteers():
        conn = get_db_connection()
        rows = conn.execute("""
            SELECT v.id, v.name, v.mobile, v.active, v.must_change_pin, v.created_at, v.last_login,
              (SELECT COUNT(*) FROM staff_activity a WHERE a.user_id = v.id AND a.action = 'RELIEF DISTRIBUTED') AS claims,
              (SELECT COUNT(*) FROM staff_activity a WHERE a.user_id = v.id AND a.action = 'REGISTERED HOUSEHOLD') AS registrations,
              (SELECT COUNT(*) FROM staff_activity a WHERE a.user_id = v.id AND a.action = 'EVACUATION CHECK-IN') AS checkins,
              (SELECT COUNT(*) FROM staff_activity a WHERE a.user_id = v.id AND a.action = 'STOCK ADDED') AS restocks,
              (SELECT MAX(time) FROM staff_activity a WHERE a.user_id = v.id) AS last_activity
            FROM volunteers v ORDER BY v.created_at DESC, v.name
        """).fetchall()
        conn.close()
        return jsonify({"status": "success", "volunteers": [dict(r) for r in rows]})

    @app.route("/api/admin/volunteers", methods=["POST"])
    def admin_create_volunteer():
        data = request.get_json() or {}
        name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()
        mobile = re.sub(r"\D", "", str(data.get("mobile") or ""))
        if len(name) < 2 or len(name) > 60:
            return jsonify({"status": "error", "message": "Enter the volunteer's full name."}), 400
        if mobile and not (10 <= len(mobile) <= 11):
            return jsonify({"status": "error", "message": "Mobile number should be 10 or 11 digits, or leave it blank."}), 400

        conn = get_db_connection()
        if mobile and conn.execute("SELECT 1 FROM volunteers WHERE mobile=?", (mobile,)).fetchone():
            conn.close()
            return jsonify({"status": "error", "message": "Another volunteer already uses that mobile number."}), 400

        vid, pin = _new_volunteer_id(conn), _new_pin()
        conn.execute("""
            INSERT INTO volunteers (id, name, mobile, pin_hash, active, must_change_pin, created_at)
            VALUES (?, ?, ?, ?, 1, 1, datetime('now', '+8 hours'))
        """, (vid, name, mobile or None, generate_password_hash(pin, method="pbkdf2:sha256")))
        _log(conn, "VOLUNTEER CREATED", f"Created volunteer account {vid} for {name}")
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "id": vid, "name": name, "pin": pin})

    @app.route("/api/admin/volunteers/<vid>/reset-pin", methods=["POST"])
    def admin_reset_volunteer_pin(vid):
        conn = get_db_connection()
        row = conn.execute("SELECT name FROM volunteers WHERE id=?", (vid,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"status": "error", "message": "Volunteer not found."}), 404
        pin = _new_pin()
        conn.execute("UPDATE volunteers SET pin_hash=?, must_change_pin=1 WHERE id=?",
                     (generate_password_hash(pin, method="pbkdf2:sha256"), vid))
        conn.execute("DELETE FROM login_attempts WHERE key LIKE ?", (vid.lower() + "|%",))
        _log(conn, "VOLUNTEER PIN RESET", f"Reset PIN for {vid} ({row['name']})")
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "id": vid, "name": row["name"], "pin": pin})

    @app.route("/api/admin/volunteers/<vid>/active", methods=["POST"])
    def admin_set_volunteer_active(vid):
        active = 1 if (request.get_json() or {}).get("active") else 0
        conn = get_db_connection()
        row = conn.execute("SELECT name FROM volunteers WHERE id=?", (vid,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"status": "error", "message": "Volunteer not found."}), 404
        conn.execute("UPDATE volunteers SET active=? WHERE id=?", (active, vid))
        _log(conn, "VOLUNTEER ENABLED" if active else "VOLUNTEER DISABLED",
             f"{'Enabled' if active else 'Disabled'} {vid} ({row['name']})")
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "active": bool(active)})

    @app.route("/api/admin/volunteers/<vid>/delete", methods=["POST"])
    def admin_delete_volunteer(vid):
        conn = get_db_connection()
        row = conn.execute("SELECT name FROM volunteers WHERE id=?", (vid,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"status": "error", "message": "Volunteer not found."}), 404
        conn.execute("DELETE FROM volunteers WHERE id=?", (vid,))
        _log(conn, "VOLUNTEER DELETED", f"Deleted volunteer account {vid} ({row['name']}). Their past activity is kept.")
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})

    @app.route("/api/admin/volunteers/activity", methods=["GET"])
    def admin_volunteer_activity():
        try:
            limit = max(1, min(int(request.args.get("limit", 100)), 300))
        except ValueError:
            limit = 100
        conn = get_db_connection()
        rows = conn.execute("""
            SELECT a.time, a.user_id, a.user_name, a.action, a.resident_id, a.details,
                   r.head_name AS household
            FROM staff_activity a
            LEFT JOIN residents r ON r.id = a.resident_id
            ORDER BY a.id DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return jsonify({"status": "success", "activity": [dict(r) for r in rows]})