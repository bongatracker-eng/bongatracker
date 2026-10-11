import sys
import os

for p in sys.path:
    if 'site-packages' in p:
        break
else:
    user_site = os.path.expanduser('~/.local/lib/python3.10/site-packages')
    if os.path.exists(user_site) and user_site not in sys.path:
        sys.path.append(user_site)

import json
import sqlite3
import random
import csv
import io
import requests
from datetime import timedelta
from flask import Flask, render_template, request, jsonify, make_response, session, redirect

app = Flask(__name__)

app.secret_key = "bonga_evacuation_super_secret_key_2026"
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "bonga_data.db")

# Vulnerability keywords that make a household a distribution priority
# (senior citizens, pregnant women, and persons with disabilities).
PRIORITY_KEYWORDS = ["senior", "pwd", "disab", "pregnan"]


def get_db_connection():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def add_column(conn, table, column, definition):
    try:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )
    except sqlite3.OperationalError:
        pass


def get_priority_flags(members):
    """Return a list of human-readable reasons a household should be
    prioritized (senior citizen / pregnant / PWD members present)."""
    reasons = []
    for m in members:
        vuln = (m["vulnerability"] or "").strip()
        if not vuln or vuln.lower() == "none":
            continue
        low = vuln.lower()
        if any(k in low for k in PRIORITY_KEYWORDS):
            reasons.append(f"{m['full_name']} ({vuln})")
    return reasons


def get_relief_history(conn, resident_id):
    rows = conn.execute("""
        SELECT batch_number, claim_time, location
        FROM relief_claims
        WHERE resident_id=?
        ORDER BY claim_time DESC
    """, (resident_id,)).fetchall()
    return [dict(r) for r in rows]


def init_db():
    conn = get_db_connection()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS residents(
        id TEXT PRIMARY KEY,
        head_name TEXT NOT NULL,
        purok TEXT NOT NULL,
        mobile TEXT,
        pin_hash TEXT,
        role TEXT DEFAULT 'Resident',
        current_lat REAL,
        current_lon REAL,
        last_active TIMESTAMP
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS family_members(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        resident_id TEXT,
        full_name TEXT,
        age INTEGER,
        vulnerability TEXT,
        status TEXT DEFAULT 'At Home',
        evac_center TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS transaction_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        actor TEXT,
        action TEXT,
        details TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS system_settings(
        id INTEGER PRIMARY KEY CHECK(id=1),
        evac_active INTEGER DEFAULT 0,
        evac_message TEXT,
        relief_active INTEGER DEFAULT 0,
        relief_message TEXT,
        relief_location TEXT,
        current_relief_batch INTEGER DEFAULT 1
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS inventory_additions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        location TEXT,
        item_name TEXT,
        category TEXT,
        quantity REAL DEFAULT 0,
        unit TEXT DEFAULT 'pcs',
        weight_kg REAL DEFAULT 0,
        volume_liters REAL DEFAULT 0,
        volume_ml REAL DEFAULT 0
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS relief_claims(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        resident_id TEXT,
        batch_number INTEGER,
        claim_time TEXT
    )
    """)

    add_column(conn,"system_settings","current_relief_batch","INTEGER DEFAULT 1")
    add_column(conn,"inventory_additions","item_name","TEXT")
    add_column(conn,"inventory_additions","category","TEXT")
    add_column(conn,"inventory_additions","quantity","REAL DEFAULT 0")
    add_column(conn,"inventory_additions","unit","TEXT DEFAULT 'pcs'")
    add_column(conn,"inventory_additions","weight_kg","REAL DEFAULT 0")
    add_column(conn,"inventory_additions","volume_liters","REAL DEFAULT 0")
    add_column(conn,"inventory_additions","volume_ml","REAL DEFAULT 0")
    add_column(conn,"relief_claims","location","TEXT")

    settings = conn.execute("SELECT COUNT(*) FROM system_settings").fetchone()[0]

    if settings == 0:
        conn.execute("""
        INSERT INTO system_settings
        (id,evac_active,evac_message,
        relief_active,relief_message,
        relief_location,current_relief_batch)
        VALUES
        (1,0,'',0,'','',1)
        """)

    admin_check = conn.execute("SELECT COUNT(*) FROM residents WHERE id='admin'").fetchone()[0]
    if admin_check == 0:
        conn.execute("""
        INSERT INTO residents (id, head_name, purok, mobile, pin_hash, role)
        VALUES ('admin', 'System Administrator', 'Barangay Hall', 'admin', 'admin', 'Admin')
        """)

    conn.commit()
    conn.close()

init_db()

from reports import register_report_routes
register_report_routes(app, get_db_connection)

from resident_reports import register_resident_report_routes
register_resident_report_routes(app, get_db_connection)

from volunteers import register_volunteer_features
register_volunteer_features(app, get_db_connection)

# ======================================================
# PASTE INTO app.py
# ======================================================
# 1) Next to the existing reports registration (after init_db()):
#
#        from reports import register_report_routes
#        register_report_routes(app, get_db_connection)
#
#        from resident_reports import register_resident_report_routes   # <-- add
#        register_resident_report_routes(app, get_db_connection)        # <-- add
#
# 2) Paste everything below into app.py (anywhere after `app` exists).
# 3) Copy hazard_map.html into templates/ and hazard_zones.json next to app.py.
# ======================================================

HAZARD_FILE = os.path.join(BASE_DIR, "hazard_zones.json")


def _is_admin_session():
    rid = (session.get("resident_id") or "").lower()
    role = (session.get("role") or "").lower()
    return rid == "admin" or role == "admin"


@app.route("/hazard-map")
def hazard_map_page():
    return render_template("hazard_map.html")


@app.route("/api/hazard-zones", methods=["GET"])
def get_hazard_zones():
    """Hazard geometry for the evacuation map. Readable by everyone —
    residents need it during an alert."""
    try:
        with open(HAZARD_FILE, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({"status": "error",
                        "message": "hazard_zones.json not found beside app.py"}), 404
    except json.JSONDecodeError as e:
        return jsonify({"status": "error",
                        "message": f"hazard_zones.json is not valid JSON: {e}"}), 500


@app.route("/api/hazard-zones", methods=["POST"])
def save_hazard_zones():
    """Admins trace zones on the map and save them here. The previous file is
    kept as hazard_zones.backup.json so a bad trace can be rolled back."""
    if not _is_admin_session():
        return jsonify({"status": "error", "message": "Admin access only."}), 403

    data = request.get_json() or {}
    if "barangay" not in data or "evacuation_centers" not in data:
        return jsonify({"status": "error",
                        "message": "Hazard file must keep 'barangay' and 'evacuation_centers'."}), 400

    try:
        if os.path.exists(HAZARD_FILE):
            with open(HAZARD_FILE, "r", encoding="utf-8") as f:
                old = f.read()
            with open(HAZARD_FILE + ".backup", "w", encoding="utf-8") as f:
                f.write(old)

        with open(HAZARD_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        conn = get_db_connection()
        conn.execute("""
            INSERT INTO transaction_log (time, actor, action, details)
            VALUES (datetime('now', '+8 hours'), 'ADMIN', 'HAZARD MAP UPDATED', ?)
        """, (f"Hazard zones saved: "
              f"{len(data.get('lahar_corridors', []))} lahar corridors, "
              f"{len(data.get('flood_zones', []))} flood zones, "
              f"{len(data.get('routes', []))} routes",))
        conn.commit()
        conn.close()

        return jsonify({"status": "success", "message": "Hazard zones saved."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/hazard-zones/check", methods=["POST"])
def check_hazard_point():
    """Ask the server whether a coordinate sits inside a mapped hazard zone.
    Used by the resident app so a phone can warn without loading the whole map."""
    data = request.get_json() or {}
    try:
        lat, lon = float(data.get("lat")), float(data.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "lat and lon required"}), 400

    try:
        with open(HAZARD_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return jsonify({"status": "success", "in_hazard": False, "zones": []})

    import math

    def haversine_m(a, b):
        R = 6371000.0
        p1, p2 = math.radians(a[0]), math.radians(b[0])
        dp = math.radians(b[0] - a[0])
        dl = math.radians(b[1] - a[1])
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * R * math.asin(math.sqrt(h))

    def point_in_poly(p, poly):
        inside = False
        j = len(poly) - 1
        for i in range(len(poly)):
            yi, xi = poly[i][0], poly[i][1]
            yj, xj = poly[j][0], poly[j][1]
            if (xi > p[1]) != (xj > p[1]) and \
               p[0] < (yj - yi) * (p[1] - xi) / (xj - xi) + yi:
                inside = not inside
            j = i
        return inside

    point = [lat, lon]
    hits = []

    volcano = cfg.get("volcano") or {}
    summit = volcano.get("summit")
    if summit:
        d = haversine_m(point, summit)
        for z in volcano.get("zones", []):
            if d <= z.get("radius_km", 0) * 1000:
                hits.append(z.get("label", "Danger zone"))

    for f in cfg.get("flood_zones", []):
        if point_in_poly(point, f.get("polygon", [])):
            hits.append(f.get("name", "Flood zone"))

    # Lahar corridors are stored as centrelines; treat "within half the
    # corridor width of any segment vertex" as inside.
    for c in cfg.get("lahar_corridors", []):
        half = (c.get("width_m", 200)) / 2.0
        if any(haversine_m(point, v) <= half for v in c.get("centerline", [])):
            hits.append(c.get("name", "Lahar corridor"))

    return jsonify({"status": "success", "in_hazard": bool(hits), "zones": hits})

# ==========================================
# FRONTEND ROUTES
# ==========================================

@app.route("/")
def index():
    role = session.get("role")
    rid = session.get("resident_id")
    if role and role.lower() == "admin" or rid == "admin":
        return render_template("admin_dashboard.html")
    return render_template("index.html")

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/registration")
def registration_page():
    return render_template("registration.html")

@app.route("/inventory")
def inventory_page():
    return render_template("inventory.html")

@app.route("/resident-portal")
def resident_page():
    return render_template("resident.html")

@app.route("/analytics")
def analytics():
    return render_template("analytics.html")

@app.route("/sms")
def sms_page():
    return render_template("sms.html")

# ==========================================
# LOGIN & SESSIONS
# ==========================================

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    identifier = (
        data.get("identifier")
        or data.get("mobile")
        or data.get("username")
        or data.get("id")
    )
    pin = (
        data.get("pin")
        or data.get("password")
    )

    conn = get_db_connection()
    user = conn.execute("""
        SELECT *
        FROM residents
        WHERE (id=? OR mobile=?)
        AND pin_hash=?
    """,
    (identifier, identifier, pin)).fetchone()
    conn.close()

    if not user:
        return jsonify({
            "status":"error",
            "message":"Invalid login"
        }),401

    session.permanent = True
    session["resident_id"] = user["id"]
    session["head_name"] = user["head_name"]

    user_role = user["role"]
    if user["id"].lower() == "admin" or (user_role and user_role.lower() == "admin"):
        user_role = "Admin"

    session["role"] = user_role

    response = jsonify({
        "status":"success",
        "resident_id":user["id"],
        "head_name":user["head_name"],
        "role": user_role,
        "user_type": user_role.lower()
    })
    return response

@app.route("/api/check-session", methods=["GET"])
def check_session():
    rid = session.get("resident_id")
    role = session.get("role")
    if rid:
        if rid.lower() == "admin" and (not role or role.lower() != "admin"):
            role = "Admin"
        return jsonify({
            "status": "success",
            "resident_id": rid,
            "head_name": session.get("head_name"),
            "role": role,
            "user_type": role.lower() if role else "resident"
        })
    return jsonify({"status": "error", "message": "No active session"}), 401

@app.route("/api/logout",methods=["POST"])
def logout():
    session.clear()
    response = jsonify({
        "status":"success",
        "message":"Logged out successfully"
    })
    response.set_cookie('session', '', expires=0, path='/')
    response.set_cookie('remember_token', '', expires=0, path='/')
    return response

# ==========================================
# REGISTRATION
# ==========================================

@app.route("/api/register",methods=["POST"])
def register():
    data=request.get_json() or {}

    resident_id=data.get("id")
    head_name=data.get("head_name")
    purok=data.get("purok")
    mobile=data.get("mobile","N/A")
    members=data.get("members",[])
    pin=str(random.randint(1000,9999))

    conn=get_db_connection()

    try:
        conn.execute("""
        INSERT INTO residents
        (id, head_name, purok, mobile, pin_hash, role)
        VALUES(?,?,?,?,?,'Resident')
        """,
        (resident_id, head_name, purok, mobile, pin))

        for member in members:
            conn.execute("""
            INSERT INTO family_members
            (resident_id, full_name, age, vulnerability)
            VALUES(?,?,?,?)
            """,
            (resident_id, member.get("name"), member.get("age"), member.get("vulnerability")))

        conn.execute("""
        INSERT INTO transaction_log
        (time, actor, action, details)
        VALUES (datetime('now', '+8 hours'), ?, 'REGISTRATION', ?)
        """,
        (resident_id, f"Registered {head_name}"))

        conn.commit()

    except Exception as e:
        conn.rollback()
        return jsonify({
            "status":"error",
            "message":str(e)
        }),400
    finally:
        conn.close()

    return jsonify({
        "status":"success",
        "resident_id":resident_id,
        "temporary_pin":pin
    })

# ==========================================
# RESIDENT PROFILE & SESSION ROUTE
# ==========================================

@app.route("/api/resident/current")
def resident_current():
    rid = session.get("resident_id")
    if not rid:
        rid = "BGN-13587"

    conn = get_db_connection()
    resident = conn.execute("SELECT * FROM residents WHERE id=?", (rid,)).fetchone()

    if not resident:
        conn.execute("INSERT OR IGNORE INTO residents (id, head_name, purok, mobile, pin_hash, role) VALUES ('BGN-13587', 'Testt', 'Purok 1A', '09123456789', '1234', 'Resident')")
        conn.execute("INSERT OR IGNORE INTO family_members (id, resident_id, full_name, age, vulnerability, status) VALUES (1, 'BGN-13587', 'Testt', 23, 'None', 'At Home')")
        conn.commit()
        resident = conn.execute("SELECT * FROM residents WHERE id=?", (rid,)).fetchone()

    members = conn.execute("SELECT * FROM family_members WHERE resident_id=?", (resident["id"],)).fetchall()
    priority_flags = get_priority_flags(members)
    relief_history = get_relief_history(conn, resident["id"])
    conn.close()

    return jsonify({
        "status": "success",
        "resident": {
            "id": resident["id"],
            "head_name": resident["head_name"],
            "purok": resident["purok"],
            "mobile": resident["mobile"],
            "role": resident["role"],
            "members": [dict(x) for x in members],
            "family_size": len(members),
            "is_priority": len(priority_flags) > 0,
            "priority_flags": priority_flags,
            "relief_history": relief_history
        }
    })

@app.route("/api/resident/<rid>")
def resident_profile(rid):
    conn=get_db_connection()
    resident=conn.execute(
        "SELECT * FROM residents WHERE id=?",
        (rid,)
    ).fetchone()

    if not resident:
        conn.close()
        return jsonify({
            "status":"error"
        }),404

    members=conn.execute(
        "SELECT * FROM family_members WHERE resident_id=?",
        (rid,)
    ).fetchall()
    priority_flags = get_priority_flags(members)
    relief_history = get_relief_history(conn, resident["id"])
    conn.close()

    return jsonify({
        "status":"success",
        "resident":{
            "id":resident["id"],
            "head_name":resident["head_name"],
            "purok":resident["purok"],
            "mobile":resident["mobile"],
            "role":resident["role"],
            "members":[dict(x) for x in members],
            "family_size": len(members),
            "is_priority": len(priority_flags) > 0,
            "priority_flags": priority_flags,
            "relief_history": relief_history
        }
    })

# ==========================================
# NOTIFICATIONS
# ==========================================

@app.route("/api/admin/notifications", methods=["POST"])
def update_notifications():
    data = request.get_json() or {}
    conn = get_db_connection()
    conn.execute("""
    UPDATE system_settings
    SET
    evac_active=?,
    evac_message=?,
    relief_active=?,
    relief_message=?,
    relief_location=?
    WHERE id=1
    """,
    (
        1 if data.get("evac_active") else 0,
        data.get("evac_message",""),
        1 if data.get("relief_active") else 0,
        data.get("relief_message",""),
        data.get("relief_location","")
    ))
    conn.commit()
    conn.close()

    return jsonify({
        "status":"success"
    })

@app.route("/api/get_notifications")
def get_notifications():
    conn=get_db_connection()
    row=conn.execute("SELECT * FROM system_settings WHERE id=1").fetchone()
    conn.close()

    if not row:
        return jsonify({
            "success":False
        })

    return jsonify({
        "success":True,
        "evac_active":bool(row["evac_active"]),
        "evac_message":row["evac_message"] or "",
        "relief_active":bool(row["relief_active"]),
        "relief_message":row["relief_message"] or "",
        "relief_location":row["relief_location"] or ""
    })

# ==========================================
# INVENTORY SYSTEM
# ==========================================

@app.route("/api/inventory/data")
def inventory_data():
    conn = get_db_connection()
    row = conn.execute("SELECT current_relief_batch FROM system_settings WHERE id=1").fetchone()
    current_batch = row["current_relief_batch"] if row else 1

    additions = conn.execute("""
        SELECT location, COALESCE(NULLIF(item_name, ''), NULLIF(category, ''), 'General Relief') as item_name, unit,
               SUM(quantity) as total_qty, SUM(weight_kg) as total_wt, SUM(volume_liters) as total_vol
        FROM inventory_additions
        GROUP BY location, item_name, unit
    """).fetchall()

    location_stocks = {}
    totals_by_unit = {"pcs": 0, "kg": 0, "L": 0}

    for add in additions:
        loc = add["location"]
        item = add["item_name"]
        unit = add["unit"]

        if unit == "kg":
            qty = add["total_wt"] if add["total_wt"] != 0 else add["total_qty"]
            totals_by_unit["kg"] = totals_by_unit.get("kg", 0) + qty
        elif unit in ["L", "ml"]:
            qty = add["total_vol"] if add["total_vol"] != 0 else add["total_qty"]
            totals_by_unit["L"] = totals_by_unit.get("L", 0) + qty
        else:
            qty = add["total_qty"]
            totals_by_unit["pcs"] = totals_by_unit.get("pcs", 0) + qty

        if qty <= 0:
            continue

        if loc not in location_stocks:
            location_stocks[loc] = []
        location_stocks[loc].append({
            "item_name": item,
            "quantity": round(qty, 2),
            "unit": unit
        })

    dist_row = conn.execute("SELECT COUNT(*) FROM transaction_log WHERE action LIKE '%RELIEF DISTRIBUTED%'").fetchone()
    total_distributed = dist_row[0] if dist_row[0] else 0

    logs = conn.execute("SELECT id, time, actor, action, details FROM transaction_log ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()

    return jsonify({
        "current_batch": current_batch,
        "totals_by_unit": totals_by_unit,
        "location_stocks": location_stocks,
        "distributed": total_distributed,
        "logs": [dict(x) for x in logs]
    })

@app.route("/api/inventory/add", methods=["POST"])
def add_inventory():
    data = request.get_json() or {}
    category = data.get("category", "Family Food Packs")
    other_item = data.get("other_item", "")

    if category == "Others" and other_item.strip():
        item_name = other_item.strip()
    else:
        item_name = category

    containers_count = float(data.get("quantity", 0))
    unit = data.get("unit", "pcs")
    location = data.get("location", "Central Depot")

    items_per_container = float(data.get("items_per_container", 1))
    container_size = float(data.get("container_size", 0))

    if category == "Sacks of Rice":
        unit = "kg"
        total_pieces = containers_count
    elif category == "Water Bottles":
        total_pieces = containers_count * items_per_container if items_per_container > 0 else containers_count
    elif category == "Family Food Packs":
        unit = "pcs"
        total_pieces = containers_count * items_per_container if items_per_container > 0 else containers_count
    else:
        total_pieces = containers_count * items_per_container if items_per_container > 0 else containers_count

    weight_kg = 0.0
    volume_liters = 0.0

    if category == "Sacks of Rice":
        weight_kg = (containers_count * container_size) if container_size > 0 else containers_count
    elif unit == "kg":
        weight_kg = (containers_count * container_size) if container_size > 0 else containers_count
    elif unit == "L":
        volume_liters = (containers_count * container_size) if container_size > 0 else containers_count
    elif unit == "ml":
        total_vol_ml = (containers_count * container_size) if container_size > 0 else containers_count
        volume_liters = total_vol_ml / 1000.0

    if containers_count <= 0:
        return jsonify({"status": "error", "message": "Invalid quantity"}), 400

    conn = get_db_connection()

    conn.execute("""
    INSERT INTO inventory_additions
    (time, location, item_name, category, quantity, unit, weight_kg, volume_liters)
    VALUES (datetime('now', '+8 hours'), ?, ?, ?, ?, ?, ?, ?)
    """,
    (location, item_name, category, weight_kg if category == 'Sacks of Rice' else total_pieces, unit, weight_kg, volume_liters))

    detail_str = f"Added {containers_count} of {item_name} ({weight_kg}kg / {volume_liters}L) at {location}"

    conn.execute("""
    INSERT INTO transaction_log
    (time, actor, action, details)
    VALUES (datetime('now', '+8 hours'), 'ADMIN', 'RESTOCK', ?)
    """,
    (detail_str,))

    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "message": f"Successfully added stock for {item_name}!"
    })

@app.route("/api/inventory/distribute-bundle", methods=["POST"])
def api_distribute_bundle():
    data = request.get_json() or {}
    resident_id = data.get("id") or data.get("resident_id")
    location = data.get("location", "Central Depot (Barangay Hall)")
    items_to_distribute = data.get("items", [])

    if not resident_id:
        return jsonify({"status": "error", "message": "Missing resident ID"}), 400

    if not items_to_distribute:
        return jsonify({"status": "error", "message": "No items selected for distribution"}), 400

    conn = get_db_connection()
    resident = conn.execute("SELECT * FROM residents WHERE id=?", (resident_id,)).fetchone()

    if not resident:
        conn.close()
        return jsonify({"status": "error", "message": "Resident ID not found."}), 404

    batch = conn.execute("SELECT current_relief_batch FROM system_settings WHERE id=1").fetchone()
    current_batch = batch["current_relief_batch"] if batch else 1

    claimed = conn.execute("SELECT * FROM relief_claims WHERE resident_id=? AND batch_number=?", (resident_id, current_batch)).fetchone()
    if claimed:
        conn.close()
        return jsonify({"status": "error", "message": f"Household already claimed for Batch #{current_batch}!"}), 400

    item_details_desc = []
    for item in items_to_distribute:
        name = item["item_name"]
        req_qty = float(item["quantity"])
        unit = item.get("unit", "pcs")

        weight_deduction = req_qty if unit == 'kg' else 0
        volume_deduction = req_qty if unit == 'L' else (req_qty / 1000.0 if unit == 'ml' else 0)

        conn.execute("""
            INSERT INTO inventory_additions (time, location, item_name, category, quantity, unit, weight_kg, volume_liters)
            VALUES (datetime('now', '+8 hours'), ?, ?, 'Distribution Deduction', ?, ?, ?, ?)
        """, (
            location,
            name,
            -req_qty if unit == 'pcs' else 0,
            unit,
            -weight_deduction,
            -volume_deduction
        ))
        item_details_desc.append(f"{req_qty} {unit} of {name}")

    bundle_str = ", ".join(item_details_desc)
    conn.execute(
        "INSERT INTO relief_claims (resident_id, batch_number, claim_time, location) VALUES (?, ?, datetime('now', '+8 hours'), ?)",
        (resident_id, current_batch, location)
    )
    conn.execute("""
        INSERT INTO transaction_log (time, actor, action, details)
        VALUES (datetime('now', '+8 hours'), ?, 'RELIEF DISTRIBUTED', ?)
    """, (resident_id, f"Distributed bundle [{bundle_str}] from {location} to {resident['head_name']} [Batch #{current_batch}]"))

    conn.commit()
    conn.close()

    return jsonify({"status": "success", "message": "Bundle successfully distributed!", "head_name": resident["head_name"]})

@app.route("/api/inventory/edit-log", methods=["POST"])
def api_edit_log():
    data = request.get_json() or {}
    log_id = data.get("log_id")
    new_details = data.get("new_details")
    conn = get_db_connection()
    conn.execute("UPDATE transaction_log SET details=? WHERE id=?", (new_details, log_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/api/inventory/delete-log", methods=["POST"])
def api_delete_log():
    data = request.get_json() or {}
    log_id = data.get("log_id")
    conn = get_db_connection()

    log = conn.execute("SELECT action FROM transaction_log WHERE id=?", (log_id,)).fetchone()
    if log and log["action"] == "RESTOCK":
        conn.execute("DELETE FROM inventory_additions WHERE id = (SELECT seq FROM sqlite_sequence WHERE name='inventory_additions') OR 1=1 ORDER BY id DESC LIMIT 1")

    conn.execute("DELETE FROM transaction_log WHERE id=?", (log_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/api/log/<int:log_id>/delete", methods=["POST"])
def delete_log_entry(log_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM transaction_log WHERE id=?", (log_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

# ==========================================
# START NEW RELIEF BATCH / RESET BATCH
# ==========================================

@app.route("/api/inventory/new-batch",methods=["POST"])
def new_batch():
    conn=get_db_connection()
    row=conn.execute("SELECT current_relief_batch FROM system_settings WHERE id=1").fetchone()

    current=1
    if row:
        current=row["current_relief_batch"]
    current+=1

    conn.execute("UPDATE system_settings SET current_relief_batch=? WHERE id=1", (current,))

    conn.execute("""
    INSERT INTO transaction_log
    (time, actor, action, details)
    VALUES (datetime('now', '+8 hours'), 'ADMIN', 'BATCH RESET', ?)
    """,
    (f"Started new relief batch: now on Batch #{current}",))

    conn.commit()
    conn.close()

    return jsonify({
        "status":"success",
        "message": f"Successfully switched to Batch #{current}",
        "batch":current
    })

@app.route("/api/inventory/reset-batch", methods=["POST"])
def reset_batch_to_one():
    conn = get_db_connection()

    conn.execute("UPDATE system_settings SET current_relief_batch=1 WHERE id=1")

    conn.execute("""
    INSERT INTO transaction_log
    (time, actor, action, details)
    VALUES (datetime('now', '+8 hours'), 'ADMIN', 'BATCH RESET', ?)
    """,
    ("Batch counter manually reset back to Batch #1",))

    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "message": "Batch counter reset back to Batch #1.",
        "batch": 1
    })

# ==========================================
# QR SCANNING - EVACUATION
# ==========================================

@app.route("/api/scan-evac-qr", methods=["POST"])
def scan_evac_qr():
    data=request.get_json() or {}
    resident_id=data.get("resident_id")
    evac_center=data.get("evac_center", "Boñga Evacuation Center")

    conn=get_db_connection()
    resident=conn.execute("SELECT * FROM residents WHERE id=?", (resident_id,)).fetchone()

    if not resident:
        conn.close()
        return jsonify({
            "status":"error",
            "message":"Resident not found"
        }),404

    conn.execute("""
    UPDATE family_members
    SET status='Evacuated', evac_center=?
    WHERE resident_id=?
    """,
    (evac_center, resident_id))

    conn.execute("""
    INSERT INTO transaction_log
    (time, actor, action, details)
    VALUES (datetime('now', '+8 hours'), ?, 'EVACUATION ARRIVAL', ?)
    """,
    (resident_id, f"{resident['head_name']} checked into {evac_center}"))

    conn.commit()
    conn.close()

    return jsonify({
        "status":"success",
        "message":"Evacuation recorded"
    })

# ==========================================
# QR RELIEF DISTRIBUTION (legacy single-item)
# ==========================================

@app.route("/api/scan-qr",methods=["POST"])
def scan_qr():
    data=request.get_json() or {}
    resident_id=data.get("resident_id")
    location=data.get("location", "Central Depot (Barangay Hall)")

    conn=get_db_connection()
    resident=conn.execute("SELECT * FROM residents WHERE id=?", (resident_id,)).fetchone()

    if not resident:
        conn.close()
        return jsonify({
            "status":"error",
            "message":"Resident not found"
        }),404

    batch=conn.execute("SELECT current_relief_batch FROM system_settings WHERE id=1").fetchone()
    current_batch=1
    if batch:
        current_batch=batch["current_relief_batch"]

    claimed=conn.execute("""
    SELECT * FROM relief_claims
    WHERE resident_id=? AND batch_number=?
    """,
    (resident_id, current_batch)).fetchone()

    if claimed:
        conn.close()
        return jsonify({
            "status":"error",
            "message": "Already claimed this batch"
        }),400

    conn.execute("""
    INSERT INTO relief_claims (resident_id, batch_number, claim_time, location)
    VALUES (?, ?, datetime('now', '+8 hours'), ?)
    """,
    (resident_id, current_batch, location))

    conn.execute("""
    INSERT INTO transaction_log (time, actor, action, details)
    VALUES (datetime('now', '+8 hours'), ?, 'RELIEF DISTRIBUTED', ?)
    """,
    (resident_id, f"Distributed relief to {resident['head_name']} at {location}"))

    conn.commit()
    conn.close()

    return jsonify({
        "status":"success",
        "message": "Relief successfully distributed",
        "head_name": resident["head_name"]
    })

# ==========================================
# OFFLINE SYNC ENDPOINT
# ==========================================

@app.route("/api/sync-offline-claims", methods=["POST"])
def sync_offline_claims():
    data = request.get_json() or {}
    claims = data.get("claims", [])

    conn = get_db_connection()
    try:
        batch = conn.execute("SELECT current_relief_batch FROM system_settings WHERE id=1").fetchone()
        current_batch = batch["current_relief_batch"] if batch else 1

        synced_count = 0
        for claim in claims:
            resident_id = claim.get("resident_id") or claim.get("household_id")
            if not resident_id:
                continue

            claimed = conn.execute(
                "SELECT * FROM relief_claims WHERE resident_id=? AND batch_number=?",
                (resident_id, current_batch)
            ).fetchone()

            if not claimed:
                location = claim.get("location", "Central Depot (Barangay Hall)")
                conn.execute(
                    "INSERT INTO relief_claims (resident_id, batch_number, claim_time, location) VALUES (?, ?, datetime('now', '+8 hours'), ?)",
                    (resident_id, current_batch, location)
                )
                resident = conn.execute("SELECT head_name FROM residents WHERE id=?", (resident_id,)).fetchone()
                head_name = resident["head_name"] if resident else resident_id

                conn.execute("""
                    INSERT INTO transaction_log (time, actor, action, details)
                    VALUES (datetime('now', '+8 hours'), ?, 'RELIEF DISTRIBUTED (OFFLINE SYNC)', ?)
                """, (resident_id, f"Synced offline relief distribution to {head_name} [Batch #{current_batch}]"))
                synced_count += 1

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "synced_count": synced_count})
    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 400

# ==========================================
# GPS LOCATION
# ==========================================

@app.route("/api/update_location",methods=["POST"])
def update_location():
    data=request.get_json() or {}
    resident_id=data.get("resident_id")
    lat=data.get("lat")
    lon=data.get("lon")

    if not resident_id:
        return jsonify({
            "status":"error",
            "message":"Missing resident"
        }),400

    conn=get_db_connection()
    conn.execute("""
    UPDATE residents
    SET current_lat=?, current_lon=?, last_active=datetime('now', '+8 hours')
    WHERE id=?
    """,
    (lat, lon, resident_id))

    conn.commit()
    conn.close()

    return jsonify({
        "status":"success"
    })

@app.route("/api/live_locations")
def live_locations():
    conn=get_db_connection()
    rows=conn.execute("""
    SELECT id, head_name, current_lat, current_lon
    FROM residents
    WHERE current_lat IS NOT NULL
    """).fetchall()
    conn.close()

    return jsonify([
        {
        "resident_id":r["id"],
        "name":r["head_name"],
        "lat":r["current_lat"],
        "lon":r["current_lon"]
        }
        for r in rows
    ])

# ==========================================
# DASHBOARD ANALYTICS
# ==========================================

@app.route("/api/dashboard-analytics")
def dashboard_analytics():
    conn=get_db_connection()
    evacuated=conn.execute("SELECT COUNT(*) FROM family_members WHERE status='Evacuated'").fetchone()[0]
    at_home=conn.execute("SELECT COUNT(*) FROM family_members WHERE status='At Home'").fetchone()[0]
    vulnerable=conn.execute("SELECT COUNT(*) FROM family_members WHERE vulnerability!='None'").fetchone()[0]

    school_count=conn.execute("SELECT COUNT(*) FROM family_members WHERE status='Evacuated' AND evac_center LIKE '%School%'").fetchone()[0]
    hall_count=conn.execute("SELECT COUNT(*) FROM family_members WHERE status='Evacuated' AND evac_center LIKE '%Hall%'").fetchone()[0]

    logs=conn.execute("SELECT * FROM transaction_log ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()

    return jsonify({
        "evacuated": evacuated,
        "at_home": at_home,
        "vulnerable": vulnerable,
        "missing": 0,
        "centers": [
            {"name": "Gogon High School", "current_occupancy": school_count, "capacity": 500},
        ],
        "logs": [dict(x) for x in logs]
    })

# ==========================================
# SEARCH RESIDENT
# ==========================================

@app.route("/api/search-resident")
def search_resident():
    q = request.args.get("q", "")
    conn = get_db_connection()
    if q.strip() == "":
        rows = conn.execute("SELECT * FROM residents").fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM residents
            WHERE id LIKE ? OR head_name LIKE ? OR purok LIKE ? OR mobile LIKE ?
        """, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    conn.close()
    return jsonify([dict(x) for x in rows])

# ==========================================
# CHANGE PIN
# ==========================================

@app.route("/api/change-pin",methods=["POST"])
def change_pin():
    data=request.get_json() or {}
    resident_id=data.get("resident_id")
    new_pin=data.get("new_pin")

    conn=get_db_connection()
    conn.execute("UPDATE residents SET pin_hash=? WHERE id=?", (new_pin, resident_id))
    conn.commit()
    conn.close()

    return jsonify({
        "status":"success",
        "message":"PIN updated"
    })

# ==========================================
# DELETE RESIDENT & MEMBERS
# ==========================================

@app.route("/api/resident/<rid>/delete", methods=["POST"])
def delete_resident(rid):
    conn=get_db_connection()
    conn.execute("DELETE FROM family_members WHERE resident_id=?", (rid,))
    conn.execute("DELETE FROM relief_claims WHERE resident_id=?", (rid,))
    conn.execute("DELETE FROM residents WHERE id=?", (rid,))
    conn.commit()
    conn.close()

    return jsonify({
        "status":"success"
    })

@app.route("/api/resident/<rid>/add-member", methods=["POST"])
def add_family_member(rid):
    data = request.get_json() or {}
    name = data.get("full_name")
    age = data.get("age", 0)
    vuln = data.get("vulnerability", "None")
    if not name:
        return jsonify({"status": "error", "message": "Name required"}), 400
    conn = get_db_connection()
    conn.execute("INSERT INTO family_members (resident_id, full_name, age, vulnerability, status) VALUES (?, ?, ?, ?, 'At Home')", (rid, name, age, vuln))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/api/member/<int:mid>/delete", methods=["POST"])
def delete_family_member(mid):
    conn = get_db_connection()
    conn.execute("DELETE FROM family_members WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/api/member/<int:mid>/checkout", methods=["POST"])
def checkout_family_member(mid):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM family_members WHERE id=?", (mid,)).fetchone()

    if not member:
        conn.close()
        return jsonify({"status": "error", "message": "Member not found"}), 404

    conn.execute("""
        UPDATE family_members
        SET status='At Home', evac_center=NULL
        WHERE id=?
    """, (mid,))

    conn.execute("""
        INSERT INTO transaction_log (time, actor, action, details)
        VALUES (datetime('now', '+8 hours'), ?, 'RETURNED HOME', ?)
    """, (member["resident_id"], f"{member['full_name']} checked out and returned home."))

    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

# ==========================================
# EXCEL / CSV & PDF REPORTS
# ==========================================

@app.route("/api/reports/cdrrmo")
def report():
    conn=get_db_connection()
    total=conn.execute("SELECT COUNT(*) FROM family_members").fetchone()[0]
    evac=conn.execute("SELECT COUNT(*) FROM family_members WHERE status='Evacuated'").fetchone()[0]
    conn.close()

    output=io.StringIO()
    writer=csv.writer(output)
    writer.writerow(["BoñgaTrack Report"])
    writer.writerow([])
    writer.writerow(["Total Family Members", total])
    writer.writerow(["Evacuated", evac])

    response=make_response(output.getvalue())
    response.headers["Content-Disposition"]="attachment; filename=report.csv"
    response.headers["Content-Type"]="text/csv"
    return response

@app.route("/api/reports/excel")
def report_excel():
    conn = get_db_connection()
    residents = conn.execute("SELECT * FROM residents").fetchall()
    logs = conn.execute("SELECT * FROM transaction_log ORDER BY id DESC").fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["BoñgaTrack Comprehensive Analytics Report"])
    writer.writerow([])
    writer.writerow(["RESIDENT DIRECTORY"])
    writer.writerow(["ID", "Head Name", "Purok", "Mobile", "Role"])
    for r in residents:
        writer.writerow([r["id"], r["head_name"], r["purok"], r["mobile"], r["role"]])

    writer.writerow([])
    writer.writerow(["TRANSACTION LOGS"])
    writer.writerow(["Time", "Actor", "Action", "Details"])
    for l in logs:
        writer.writerow([l["time"], l["actor"], l["action"], l["details"]])

    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=BongaTrack_CDRMMO_Report.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

# ==========================================
# TELERIVET CLOUD-RELAYED SMS GATEWAY API
# ==========================================

@app.route("/api/sms/send", methods=["POST"])
def send_sms():
    data = request.get_json() or {}
    message = data.get("message", "")
    target = data.get("target", "ALL REGISTERED RESIDENTS")

    if not message:
        return jsonify({"status": "error", "message": "Message cannot be empty"}), 400

    conn = get_db_connection()

    if target == "VULNERABLE SECTORS ONLY":
        rows = conn.execute("""
            SELECT DISTINCT r.mobile FROM residents r
            JOIN family_members f ON r.id = f.resident_id
            WHERE f.vulnerability != 'None' AND r.mobile IS NOT NULL AND r.mobile != 'N/A' AND r.mobile != ''
        """).fetchall()
    elif target == "CURRENTLY EVACUATED":
        rows = conn.execute("""
            SELECT DISTINCT r.mobile FROM residents r
            JOIN family_members f ON r.id = f.resident_id
            WHERE f.status = 'Evacuated' AND r.mobile IS NOT NULL AND r.mobile != 'N/A' AND r.mobile != ''
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT mobile FROM residents
            WHERE mobile IS NOT NULL AND mobile != 'N/A' AND mobile != '' AND id != 'admin'
        """).fetchall()

    mobile_numbers = [str(r["mobile"]).strip() for r in rows if r["mobile"] and str(r["mobile"]).strip() != ""]

    telerivet_api_key = "yV44K_aI2GHinWrjmAJmYHvlIreOZZ7kilb4"
    telerivet_project_id = "PJ28ec6add856a5fd3"
    gateway_endpoint = f"https://api.telerivet.com/v1/projects/{telerivet_project_id}/messages/send"

    sent_count = 0
    try:
        for number in mobile_numbers:
            formatted_num = number if number.startswith("+") else "+63" + number.lstrip("0")

            payload = {
                "to_number": formatted_num,
                "content": message
            }

            try:
                res = requests.post(gateway_endpoint, json=payload, auth=(telerivet_api_key, ""), timeout=5)
                if res.status_code == 200 or res.status_code == 201:
                    sent_count += 1
            except Exception as req_err:
                print(f"OUTBOUND BLOCKED: {str(req_err)}")

    except Exception as e:
        print("Telerivet connection error:", str(e))

    conn.execute("""
    INSERT INTO transaction_log
    (time, actor, action, details)
    VALUES (datetime('now', '+8 hours'), 'ADMIN', 'SMS BLAST', ?)
    """,
    (f"Dispatched SMS via Telerivet Cloud Relay to {sent_count} recipients ({target}): {message}",))
    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "sent_count": sent_count,
        "message": f"SMS broadcast successfully dispatched via Telerivet to {sent_count} numbers!"
    })

# ==========================================
# START SERVER
# ==========================================

if __name__=="__main__":
    app.run(debug=True, port=5000)