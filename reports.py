"""
reports.py - Inventory/Relief and Resident/Vulnerable-Sector reports
(Excel + PDF) for BoñgaTrack.

Setup
-----
1. pip install openpyxl reportlab
2. Put this file next to app.py
3. In app.py, after get_db_connection() is defined, add:

       from reports import register_report_routes
       register_report_routes(app, get_db_connection)

Routes added (admin session required)
-------------------------------------
    GET /api/reports/inventory.xlsx?batch=all|current|<number>
    GET /api/reports/inventory.pdf?batch=all|current|<number>
    GET /api/reports/residents.xlsx
    GET /api/reports/residents.pdf

`batch` only filters the Relief Claims section of the inventory report.
Stock, restocks and distributed-item totals are always all-time.

The residents report has no batch scope - it is a snapshot of the
current Resident Directory (households + family roster) plus a
dedicated Vulnerable Sectors sheet/section (senior citizens, pregnant /
lactating, PWD, single parent).
"""

import io
import re
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from flask import jsonify, request, send_file, session

PH_TZ = timezone(timedelta(hours=8))

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Colors shared by both formats
HEADER_HEX = "1F3A5F"
ZEBRA_HEX = "F3F6FA"
MUTED_HEX = "5B6675"

# Vulnerability keywords that make a household a distribution priority
# (kept in sync with app.py's PRIORITY_KEYWORDS).
PRIORITY_KEYWORDS = ["senior", "pwd", "disab", "pregnan"]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _is_admin():
    rid = (session.get("resident_id") or "").lower()
    role = (session.get("role") or "").lower()
    return rid == "admin" or role == "admin"


def _num(v):
    """Round to 2 decimals and drop a trailing .0 so 5.0 shows as 5."""
    v = round(float(v or 0), 2)
    return int(v) if v == int(v) else v


def _net(unit, qty, wt, vol):
    """
    Same unit rules the Inventory page uses:
      kg      -> weight_kg (falls back to quantity)
      L / ml  -> volume_liters (falls back to quantity), shown in liters
      other   -> quantity (pieces)
    Returns (amount, display_unit).
    """
    qty, wt, vol = qty or 0, wt or 0, vol or 0
    if unit == "kg":
        return (wt if wt != 0 else qty), "kg"
    if unit in ("L", "ml"):
        return (vol if vol != 0 else qty), "L"
    return qty, (unit or "pcs")


def _scope_label(data):
    bf = data["batch_filter"]
    if bf is None:
        return "All batches"
    label = f"Batch #{bf}"
    if bf == data["current_batch"]:
        label += " (current)"
    return label


def _filename(data, ext):
    bf = data["batch_filter"]
    scope = "AllBatches" if bf is None else f"Batch{bf}"
    return f"BongaTrack_Inventory_Report_{scope}_{data['generated'][:10]}.{ext}"


def _priority_reasons(vulnerability):
    """Given one member's vulnerability text, return it if it matches a
    priority keyword (senior/PWD/pregnant), else None."""
    vuln = (vulnerability or "").strip()
    if not vuln or vuln.lower() == "none":
        return None
    low = vuln.lower()
    return vuln if any(k in low for k in PRIORITY_KEYWORDS) else None


# ----------------------------------------------------------------------
# Data collection
# ----------------------------------------------------------------------
def _collect(conn, batch_param):
    row = conn.execute(
        "SELECT current_relief_batch FROM system_settings WHERE id=1"
    ).fetchone()
    current_batch = row["current_relief_batch"] if row else 1

    batch_param = (batch_param or "all").strip().lower()
    if batch_param == "current":
        batch_filter = current_batch
    elif batch_param.isdigit():
        batch_filter = int(batch_param)
    else:
        batch_filter = None

    # ---- Current stock by location (net of distributions) -------------
    stock_rows = conn.execute("""
        SELECT location,
               COALESCE(NULLIF(item_name, ''), NULLIF(category, ''), 'General Relief') AS item_name,
               unit,
               SUM(quantity) AS q, SUM(weight_kg) AS w, SUM(volume_liters) AS v
        FROM inventory_additions
        GROUP BY location, item_name, unit
        ORDER BY location, item_name
    """).fetchall()

    stock = []
    totals = {"pcs": 0, "kg": 0, "L": 0}
    for r in stock_rows:
        amount, unit = _net(r["unit"], r["q"], r["w"], r["v"])
        if amount <= 0:
            continue
        stock.append({
            "location": r["location"] or "-",
            "item": r["item_name"],
            "qty": _num(amount),
            "unit": unit,
        })
        key = unit if unit in ("kg", "L") else "pcs"
        totals[key] += amount
    totals = {k: _num(v) for k, v in totals.items()}

    # ---- Restock history ---------------------------------------------
    restock_rows = conn.execute("""
        SELECT time, location,
               COALESCE(NULLIF(item_name, ''), NULLIF(category, ''), 'General Relief') AS item_name,
               category, unit, quantity, weight_kg, volume_liters
        FROM inventory_additions
        WHERE COALESCE(category, '') != 'Distribution Deduction'
        ORDER BY id DESC
    """).fetchall()

    restocks = []
    for r in restock_rows:
        amount, unit = _net(r["unit"], r["quantity"], r["weight_kg"], r["volume_liters"])
        restocks.append({
            "time": r["time"] or "-",
            "location": r["location"] or "-",
            "item": r["item_name"],
            "qty": _num(amount),
            "unit": unit,
        })

    # ---- Total distributed per item (all-time) -----------------------
    dist_rows = conn.execute("""
        SELECT COALESCE(NULLIF(item_name, ''), 'General Relief') AS item_name,
               unit, SUM(quantity) AS q, SUM(weight_kg) AS w, SUM(volume_liters) AS v
        FROM inventory_additions
        WHERE category = 'Distribution Deduction'
        GROUP BY item_name, unit
        ORDER BY item_name
    """).fetchall()

    distributed = []
    for r in dist_rows:
        amount, unit = _net(r["unit"], r["q"], r["w"], r["v"])
        amount = abs(amount)  # deductions are stored as negatives
        if amount > 0:
            distributed.append({"item": r["item_name"], "qty": _num(amount), "unit": unit})

    # ---- Relief claims ----------------------------------------------
    # c.location is where the household actually picked up/received the
    # relief bundle (captured at distribution time in app.py).
    sql = """
        SELECT c.id, c.batch_number, c.claim_time, c.resident_id, c.location,
               r.head_name, r.purok, r.mobile
        FROM relief_claims c
        LEFT JOIN residents r ON r.id = c.resident_id
    """
    params = ()
    if batch_filter is not None:
        sql += " WHERE c.batch_number = ?"
        params = (batch_filter,)
    sql += " ORDER BY c.id DESC"
    claim_rows = conn.execute(sql, params).fetchall()

    # Priority (senior/PWD/pregnant) flags per household, for the claims table.
    priority_by_resident = {}
    for m in conn.execute(
        "SELECT resident_id, full_name, vulnerability FROM family_members"
    ).fetchall():
        vuln = (m["vulnerability"] or "").strip()
        if not vuln or vuln.lower() == "none":
            continue
        low = vuln.lower()
        if any(k in low for k in PRIORITY_KEYWORDS):
            priority_by_resident.setdefault(m["resident_id"], []).append(
                f"{m['full_name']} ({vuln})"
            )

    # What was handed out comes from the transaction log text.
    bundles = {}
    for lg in conn.execute(
        "SELECT actor, details FROM transaction_log "
        "WHERE action LIKE 'RELIEF DISTRIBUTED%' ORDER BY id"
    ).fetchall():
        d = lg["details"] or ""
        mb = re.search(r"\[Batch #(\d+)\]", d)
        mi = re.search(r"bundle \[(.*?)\]", d)
        key = (lg["actor"], int(mb.group(1)) if mb else None)
        items_txt = re.sub(r"(\d+)\.0\b", r"\1", mi.group(1)) if mi else None  # "1.0 pcs" -> "1 pcs"
        bundles[key] = items_txt or "Relief pack (no item breakdown recorded)"

    claims = []
    for c in claim_rows:
        items = bundles.get((c["resident_id"], c["batch_number"])) \
            or bundles.get((c["resident_id"], None)) or "-"
        priority_reasons = priority_by_resident.get(c["resident_id"], [])
        claims.append({
            "time": c["claim_time"] or "-",
            "batch": c["batch_number"],
            "resident_id": c["resident_id"],
            "head_name": c["head_name"] or "(resident removed)",
            "purok": c["purok"] or "-",
            "mobile": c["mobile"] or "-",
            "location": c["location"] or "-",
            "items": items,
            "priority": "Yes - " + "; ".join(priority_reasons) if priority_reasons else "No",
        })

    by_batch = conn.execute("""
        SELECT batch_number, COUNT(*) AS n
        FROM relief_claims GROUP BY batch_number ORDER BY batch_number
    """).fetchall()

    return {
        "generated": datetime.now(PH_TZ).strftime("%Y-%m-%d %H:%M"),
        "current_batch": current_batch,
        "batch_filter": batch_filter,
        "stock": stock,
        "totals": totals,
        "restocks": restocks,
        "distributed": distributed,
        "claims": claims,
        "households": len({c["resident_id"] for c in claims}),
        "claims_by_batch": [(b["batch_number"], b["n"]) for b in by_batch],
    }


# ----------------------------------------------------------------------
# Data collection - Resident Directory & Vulnerable Sectors
# ----------------------------------------------------------------------
def _collect_residents(conn):
    residents = conn.execute("""
        SELECT * FROM residents WHERE id != 'admin' ORDER BY head_name COLLATE NOCASE
    """).fetchall()

    members_rows = conn.execute("""
        SELECT * FROM family_members ORDER BY resident_id
    """).fetchall()

    members_by_resident = {}
    for m in members_rows:
        members_by_resident.setdefault(m["resident_id"], []).append(m)

    households = []
    vulnerable_members = []
    purok_counts = {}
    vuln_type_counts = {}
    total_members = 0

    for r in residents:
        members = members_by_resident.get(r["id"], [])
        total_members += len(members)
        purok_counts[r["purok"]] = purok_counts.get(r["purok"], 0) + 1

        household_priority_reasons = []
        for m in members:
            reason = _priority_reasons(m["vulnerability"])
            if reason:
                household_priority_reasons.append(f"{m['full_name']} ({reason})")
                vuln_type_counts[reason] = vuln_type_counts.get(reason, 0) + 1
                vulnerable_members.append({
                    "full_name": m["full_name"],
                    "age": m["age"],
                    "vulnerability": reason,
                    "resident_id": r["id"],
                    "head_name": r["head_name"],
                    "purok": r["purok"],
                    "mobile": r["mobile"] or "-",
                    "status": m["status"] or "-",
                })

        households.append({
            "id": r["id"],
            "head_name": r["head_name"],
            "purok": r["purok"],
            "mobile": r["mobile"] or "-",
            "family_size": len(members),
            "has_priority": len(household_priority_reasons) > 0,
            "priority_reasons": "; ".join(household_priority_reasons) if household_priority_reasons else "-",
        })

    return {
        "generated": datetime.now(PH_TZ).strftime("%Y-%m-%d %H:%M"),
        "households": households,
        "total_households": len(households),
        "total_members": total_members,
        "vulnerable_members": vulnerable_members,
        "total_vulnerable": len(vulnerable_members),
        "purok_counts": sorted(purok_counts.items()),
        "vuln_type_counts": sorted(vuln_type_counts.items()),
    }


def _residents_filename(data, ext):
    return f"BongaTrack_Resident_Directory_{data['generated'][:10]}.{ext}"


# ----------------------------------------------------------------------
# Excel
# ----------------------------------------------------------------------
def _build_xlsx(data):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    base = Font(name="Arial", size=10)
    bold = Font(name="Arial", size=10, bold=True)
    title_font = Font(name="Arial", size=14, bold=True)
    muted = Font(name="Arial", size=9, color=MUTED_HEX)
    head_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=HEADER_HEX)
    zebra = PatternFill("solid", fgColor=ZEBRA_HEX)
    line = Side(style="thin", color="D5DBE3")
    box = Border(left=line, right=line, top=line, bottom=line)

    wb = Workbook()
    scope = _scope_label(data)
    subtitle = f"Generated {data['generated']} (PH time)  |  Claims scope: {scope}"

    def prep(ws, heading, sub):
        ws["A1"] = heading
        ws["A1"].font = title_font
        ws["A2"] = sub
        ws["A2"].font = muted
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0

    def table(ws, headers, rows, widths, start=4, wrap_cols=()):
        for i, h in enumerate(headers, 1):
            c = ws.cell(row=start, column=i, value=h)
            c.font, c.fill, c.border = head_font, head_fill, box
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r_i, row in enumerate(rows, start + 1):
            for c_i, val in enumerate(row, 1):
                c = ws.cell(row=r_i, column=c_i, value=val)
                c.font, c.border = base, box
                c.alignment = Alignment(vertical="top", wrap_text=(c_i in wrap_cols))
                if (r_i - start) % 2 == 0:
                    c.fill = zebra
        if not rows:
            ws.cell(row=start + 1, column=1, value="No records.").font = muted
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = ws.cell(row=start + 1, column=1)
        if rows:
            ws.auto_filter.ref = f"A{start}:{get_column_letter(len(headers))}{start + len(rows)}"

    # ---- Summary ------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    prep(ws, "BoñgaTrack - Relief Inventory & Claims Report", subtitle)
    summary = [
        ("Current relief batch", f"#{data['current_batch']}"),
        ("Claims scope", scope),
        ("Claims in scope", len(data["claims"])),
        ("Households served in scope", data["households"]),
        ("Stock on hand - pieces (pcs)", data["totals"]["pcs"]),
        ("Stock on hand - weight (kg)", data["totals"]["kg"]),
        ("Stock on hand - volume (L)", data["totals"]["L"]),
    ]
    for i, (k, v) in enumerate(summary, 4):
        a, b = ws.cell(row=i, column=1, value=k), ws.cell(row=i, column=2, value=v)
        a.font, b.font = bold, base
        a.border = b.border = box
        b.alignment = Alignment(horizontal="left")
    r = 4 + len(summary) + 1
    ws.cell(row=r, column=1, value="Claims per batch (all batches)").font = bold
    for i, h in enumerate(("Batch", "Claims"), 1):
        c = ws.cell(row=r + 1, column=i, value=h)
        c.font, c.fill, c.border = head_font, head_fill, box
    for j, (b, n) in enumerate(data["claims_by_batch"], r + 2):
        for i, val in enumerate((f"#{b}", n), 1):
            c = ws.cell(row=j, column=i, value=val)
            c.font, c.border = base, box
            c.alignment = Alignment(horizontal="left")
    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 24

    # ---- Current Stock ------------------------------------------------
    ws = wb.create_sheet("Current Stock")
    prep(ws, "Current Stock by Location", "Net of everything already distributed.  " + subtitle)
    table(ws, ["Location", "Item", "Quantity", "Unit"],
          [[s["location"], s["item"], s["qty"], s["unit"]] for s in data["stock"]],
          [36, 32, 14, 10])

    # ---- Distributed items -------------------------------------------
    ws = wb.create_sheet("Distributed Items")
    prep(ws, "Total Distributed per Item (all batches)", subtitle)
    table(ws, ["Item", "Total Distributed", "Unit"],
          [[d["item"], d["qty"], d["unit"]] for d in data["distributed"]],
          [36, 20, 10])

    # ---- Restock history ---------------------------------------------
    ws = wb.create_sheet("Restock History")
    prep(ws, "Restock / Incoming Delivery History", subtitle)
    table(ws, ["Date & Time", "Location", "Item", "Quantity", "Unit"],
          [[x["time"], x["location"], x["item"], x["qty"], x["unit"]] for x in data["restocks"]],
          [20, 34, 30, 14, 10])

    # ---- Relief claims -----------------------------------------------
    ws = wb.create_sheet("Relief Claims")
    prep(ws, f"Relief Claims - {scope}", subtitle)
    table(ws, ["Date & Time", "Batch", "Household ID", "Household Head", "Purok", "Mobile",
               "Relief Location", "Priority Household", "Items Received"],
          [[c["time"], c["batch"], c["resident_id"], c["head_name"], c["purok"], c["mobile"],
            c["location"], c["priority"], c["items"]]
           for c in data["claims"]],
          [20, 8, 16, 28, 12, 16, 26, 30, 46], wrap_cols=(8, 9))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _build_residents_xlsx(data):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    base = Font(name="Arial", size=10)
    bold = Font(name="Arial", size=10, bold=True)
    title_font = Font(name="Arial", size=14, bold=True)
    muted = Font(name="Arial", size=9, color=MUTED_HEX)
    head_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=HEADER_HEX)
    priority_fill = PatternFill("solid", fgColor="FDEAEA")
    zebra = PatternFill("solid", fgColor=ZEBRA_HEX)
    line = Side(style="thin", color="D5DBE3")
    box = Border(left=line, right=line, top=line, bottom=line)

    wb = Workbook()
    subtitle = f"Generated {data['generated']} (PH time)"

    def prep(ws, heading, sub):
        ws["A1"] = heading
        ws["A1"].font = title_font
        ws["A2"] = sub
        ws["A2"].font = muted
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0

    def table(ws, headers, rows, widths, start=4, wrap_cols=(), highlight_col=None):
        for i, h in enumerate(headers, 1):
            c = ws.cell(row=start, column=i, value=h)
            c.font, c.fill, c.border = head_font, head_fill, box
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r_i, row in enumerate(rows, start + 1):
            is_priority = highlight_col is not None and row[highlight_col] not in ("-", "No", False, None)
            for c_i, val in enumerate(row, 1):
                c = ws.cell(row=r_i, column=c_i, value=val)
                c.font, c.border = base, box
                c.alignment = Alignment(vertical="top", wrap_text=(c_i in wrap_cols))
                if is_priority:
                    c.fill = priority_fill
                elif (r_i - start) % 2 == 0:
                    c.fill = zebra
        if not rows:
            ws.cell(row=start + 1, column=1, value="No records.").font = muted
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = ws.cell(row=start + 1, column=1)
        if rows:
            ws.auto_filter.ref = f"A{start}:{get_column_letter(len(headers))}{start + len(rows)}"

    # ---- Summary ------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    prep(ws, "BoñgaTrack - Resident Directory & Vulnerable Sectors Report", subtitle)
    summary = [
        ("Total registered households", data["total_households"]),
        ("Total family members", data["total_members"]),
        ("Total vulnerable individuals", data["total_vulnerable"]),
    ]
    for i, (k, v) in enumerate(summary, 4):
        a, b = ws.cell(row=i, column=1, value=k), ws.cell(row=i, column=2, value=v)
        a.font, b.font = bold, base
        a.border = b.border = box

    r = 4 + len(summary) + 1
    ws.cell(row=r, column=1, value="Vulnerable individuals by type").font = bold
    for i, h in enumerate(("Vulnerability Type", "Count"), 1):
        c = ws.cell(row=r + 1, column=i, value=h)
        c.font, c.fill, c.border = head_font, head_fill, box
    for j, (t, n) in enumerate(data["vuln_type_counts"], r + 2):
        for i, val in enumerate((t, n), 1):
            c = ws.cell(row=j, column=i, value=val)
            c.font, c.border = base, box

    r2 = r + 2 + len(data["vuln_type_counts"]) + 1
    ws.cell(row=r2, column=1, value="Households by Purok").font = bold
    for i, h in enumerate(("Purok", "Households"), 1):
        c = ws.cell(row=r2 + 1, column=i, value=h)
        c.font, c.fill, c.border = head_font, head_fill, box
    for j, (p, n) in enumerate(data["purok_counts"], r2 + 2):
        for i, val in enumerate((p, n), 1):
            c = ws.cell(row=j, column=i, value=val)
            c.font, c.border = base, box

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 20

    # ---- Household Directory ------------------------------------------
    ws = wb.create_sheet("Household Directory")
    prep(ws, "Registered Household Directory", subtitle)
    table(ws, ["Household ID", "Household Head", "Purok", "Mobile", "Family Size",
               "Has Priority Member", "Priority Reasons"],
          [[h["id"], h["head_name"], h["purok"], h["mobile"], h["family_size"],
            "Yes" if h["has_priority"] else "No", h["priority_reasons"]]
           for h in data["households"]],
          [16, 28, 12, 16, 12, 16, 50], wrap_cols=(7,), highlight_col=5)

    # ---- Vulnerable Sectors --------------------------------------------
    ws = wb.create_sheet("Vulnerable Sectors")
    prep(ws, "Vulnerable Sector Individuals (Senior Citizen / Pregnant / PWD)", subtitle)
    table(ws, ["Full Name", "Age", "Vulnerability", "Household ID", "Household Head",
               "Purok", "Mobile", "Status"],
          [[v["full_name"], v["age"], v["vulnerability"], v["resident_id"], v["head_name"],
            v["purok"], v["mobile"], v["status"]]
           for v in data["vulnerable_members"]],
          [26, 8, 22, 16, 26, 12, 16, 16])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------
def _build_pdf(data):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    header_c = colors.HexColor("#" + HEADER_HEX)
    zebra_c = colors.HexColor("#" + ZEBRA_HEX)
    muted_c = colors.HexColor("#" + MUTED_HEX)
    grid_c = colors.HexColor("#D5DBE3")

    ss = getSampleStyleSheet()
    st = {
        "title": ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                                fontSize=18, alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=ss["Normal"], fontSize=9, textColor=muted_c, spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                             fontSize=12, spaceBefore=14, spaceAfter=6, keepWithNext=1),
        "th": ParagraphStyle("th", parent=ss["Normal"], fontName="Helvetica-Bold",
                             fontSize=8.5, textColor=colors.white, leading=10),
        "td": ParagraphStyle("td", parent=ss["Normal"], fontSize=8.5, leading=10.5),
        "tdr": ParagraphStyle("tdr", parent=ss["Normal"], fontSize=8.5, leading=10.5, alignment=TA_RIGHT),
        "note": ParagraphStyle("n", parent=ss["Normal"], fontSize=9, textColor=muted_c),
    }

    def P(text, style):
        return Paragraph(escape(str(text)), st[style])

    def make_table(headers, rows, widths, right_cols=()):
        head = [P(h, "th") for h in headers]
        body = [[P(v, "tdr" if i in right_cols else "td") for i, v in enumerate(r)] for r in rows]
        t = Table([head] + body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), header_c),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, zebra_c]),
            ("GRID", (0, 0), (-1, -1), 0.4, grid_c),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    def section(title, headers, rows, widths, right_cols=()):
        out = [Paragraph(escape(title), st["h2"])]
        if rows:
            out.append(make_table(headers, rows, widths, right_cols))
        else:
            out.append(Paragraph("No records.", st["note"]))
        return out

    page = landscape(A4)
    margin = 15 * mm
    width = page[0] - 2 * margin
    scope = _scope_label(data)

    story = [
        Paragraph("BoñgaTrack - Relief Inventory &amp; Claims Report", st["title"]),
        Paragraph(escape(f"Generated {data['generated']} (PH time)  |  Claims scope: {scope}"), st["sub"]),
    ]

    # Summary: two small tables side by side
    left = make_table(
        ["Summary", ""],
        [["Current relief batch", f"#{data['current_batch']}"],
         ["Claims in scope", len(data["claims"])],
         ["Households served in scope", data["households"]],
         ["Stock on hand - pieces (pcs)", data["totals"]["pcs"]],
         ["Stock on hand - weight (kg)", data["totals"]["kg"]],
         ["Stock on hand - volume (L)", data["totals"]["L"]]],
        [width * 0.32, width * 0.14], right_cols=(1,))
    right = make_table(
        ["Batch", "Claims"],
        [[f"#{b}", n] for b, n in data["claims_by_batch"]] or [["-", 0]],
        [width * 0.18, width * 0.14], right_cols=(1,))
    wrapper = Table([[left, right]], colWidths=[width * 0.5, width * 0.5])
    wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(wrapper)

    story += section(
        "Current Stock by Location (net of distributions)",
        ["Location", "Item", "Quantity", "Unit"],
        [[s["location"], s["item"], s["qty"], s["unit"]] for s in data["stock"]],
        [width * 0.38, width * 0.34, width * 0.16, width * 0.12], right_cols=(2,))

    story += section(
        "Total Distributed per Item (all batches)",
        ["Item", "Total Distributed", "Unit"],
        [[d["item"], d["qty"], d["unit"]] for d in data["distributed"]],
        [width * 0.5, width * 0.3, width * 0.2], right_cols=(1,))

    story += section(
        f"Relief Claims - {scope}",
        ["Date & Time", "Batch", "Household ID", "Household Head", "Purok", "Mobile",
         "Relief Location", "Priority", "Items Received"],
        [[c["time"], c["batch"], c["resident_id"], c["head_name"], c["purok"], c["mobile"],
          c["location"], c["priority"], c["items"]]
         for c in data["claims"]],
        [width * 0.10, width * 0.05, width * 0.09, width * 0.13, width * 0.06,
         width * 0.09, width * 0.13, width * 0.13, width * 0.22])

    story += section(
        "Restock / Incoming Delivery History",
        ["Date & Time", "Location", "Item", "Quantity", "Unit"],
        [[x["time"], x["location"], x["item"], x["qty"], x["unit"]] for x in data["restocks"]],
        [width * 0.16, width * 0.32, width * 0.28, width * 0.14, width * 0.10], right_cols=(3,))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(muted_c)
        canvas.drawString(margin, 9 * mm, f"BoñgaTrack - generated {data['generated']}")
        canvas.drawRightString(page[0] - margin, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=page, leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=16 * mm,
        title="BoñgaTrack Relief Inventory & Claims Report", author="BoñgaTrack")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    buf.seek(0)
    return buf


def _build_residents_pdf(data):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    header_c = colors.HexColor("#" + HEADER_HEX)
    zebra_c = colors.HexColor("#" + ZEBRA_HEX)
    muted_c = colors.HexColor("#" + MUTED_HEX)
    grid_c = colors.HexColor("#D5DBE3")
    priority_c = colors.HexColor("#FDEAEA")

    ss = getSampleStyleSheet()
    st = {
        "title": ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                                fontSize=18, alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=ss["Normal"], fontSize=9, textColor=muted_c, spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                             fontSize=12, spaceBefore=14, spaceAfter=6, keepWithNext=1),
        "th": ParagraphStyle("th", parent=ss["Normal"], fontName="Helvetica-Bold",
                             fontSize=8.5, textColor=colors.white, leading=10),
        "td": ParagraphStyle("td", parent=ss["Normal"], fontSize=8.5, leading=10.5),
        "note": ParagraphStyle("n", parent=ss["Normal"], fontSize=9, textColor=muted_c),
    }

    def P(text, style):
        return Paragraph(escape(str(text)), st[style])

    def make_table(headers, rows, widths, priority_rows=()):
        head = [P(h, "th") for h in headers]
        body = [[P(v, "td") for v in r] for r in rows]
        t = Table([head] + body, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), header_c),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, zebra_c]),
            ("GRID", (0, 0), (-1, -1), 0.4, grid_c),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for ridx in priority_rows:
            style.append(("BACKGROUND", (0, ridx + 1), (-1, ridx + 1), priority_c))
        t.setStyle(TableStyle(style))
        return t

    def section(title, headers, rows, widths, priority_rows=()):
        out = [Paragraph(escape(title), st["h2"])]
        if rows:
            out.append(make_table(headers, rows, widths, priority_rows))
        else:
            out.append(Paragraph("No records.", st["note"]))
        return out

    page = landscape(A4)
    margin = 15 * mm
    width = page[0] - 2 * margin

    story = [
        Paragraph("BoñgaTrack - Resident Directory &amp; Vulnerable Sectors Report", st["title"]),
        Paragraph(escape(f"Generated {data['generated']} (PH time)"), st["sub"]),
    ]

    left = make_table(
        ["Summary", ""],
        [["Total registered households", data["total_households"]],
         ["Total family members", data["total_members"]],
         ["Total vulnerable individuals", data["total_vulnerable"]]],
        [width * 0.32, width * 0.14])
    right = make_table(
        ["Vulnerability Type", "Count"],
        [[t, n] for t, n in data["vuln_type_counts"]] or [["-", 0]],
        [width * 0.24, width * 0.12])
    wrapper = Table([[left, right]], colWidths=[width * 0.5, width * 0.5])
    wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(wrapper)

    hh_rows = [[h["id"], h["head_name"], h["purok"], h["mobile"], h["family_size"],
                "Yes" if h["has_priority"] else "No", h["priority_reasons"]]
               for h in data["households"]]
    hh_priority_rows = [i for i, h in enumerate(data["households"]) if h["has_priority"]]
    story += section(
        "Registered Household Directory",
        ["Household ID", "Head", "Purok", "Mobile", "Family Size", "Priority?", "Priority Reasons"],
        hh_rows,
        [width * 0.11, width * 0.20, width * 0.09, width * 0.12, width * 0.09,
         width * 0.08, width * 0.31],
        priority_rows=hh_priority_rows)

    story += section(
        "Vulnerable Sector Individuals (Senior Citizen / Pregnant / PWD)",
        ["Full Name", "Age", "Vulnerability", "Household ID", "Head", "Purok", "Mobile"],
        [[v["full_name"], v["age"], v["vulnerability"], v["resident_id"], v["head_name"],
          v["purok"], v["mobile"]] for v in data["vulnerable_members"]],
        [width * 0.19, width * 0.06, width * 0.17, width * 0.12, width * 0.19,
         width * 0.10, width * 0.17])

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(muted_c)
        canvas.drawString(margin, 9 * mm, f"BoñgaTrack - generated {data['generated']}")
        canvas.drawRightString(page[0] - margin, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=page, leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=16 * mm,
        title="BoñgaTrack Resident Directory & Vulnerable Sectors Report", author="BoñgaTrack")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    buf.seek(0)
    return buf


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
def register_report_routes(app, get_db_connection):

    def build(builder, ext, mimetype):
        if not session.get("resident_id"):
            return jsonify({"status": "error", "message": "Please log in first."}), 401
        if not _is_admin():
            return jsonify({"status": "error", "message": "Admin access only."}), 403

        conn = get_db_connection()
        try:
            data = _collect(conn, request.args.get("batch"))
        finally:
            conn.close()

        try:
            buf = builder(data)
        except ImportError as e:
            return jsonify({
                "status": "error",
                "message": f"Missing library on the server: {e.name}. "
                           f"Run: pip install openpyxl reportlab"
            }), 500

        return send_file(buf, as_attachment=True,
                         download_name=_filename(data, ext), mimetype=mimetype)

    @app.route("/api/reports/inventory.xlsx")
    def report_inventory_xlsx():
        return build(_build_xlsx, "xlsx", XLSX_MIME)

    @app.route("/api/reports/inventory.pdf")
    def report_inventory_pdf():
        return build(_build_pdf, "pdf", "application/pdf")

