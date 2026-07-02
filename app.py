"""
Border to Border Inventory Manager — FastAPI + SQLite backend
Run: python app.py
Access: http://<your-pi-ip>:8000
"""

import sqlite3
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

DB_PATH = "prices.db"

# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS markup_codes (
            code        TEXT PRIMARY KEY,
            description TEXT,
            markup_pct  REAL NOT NULL DEFAULT 100.0
        );

        CREATE TABLE IF NOT EXISTS products (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            name                 TEXT,
            sku                  TEXT,
            category             TEXT,
            pack_qty             REAL    DEFAULT 1.0,
            pack_cost            REAL    DEFAULT 0.0,
            markup_code          TEXT    REFERENCES markup_codes(code),
            preferred_supplier   TEXT,
            bin_location         TEXT,
            supplier_part_number TEXT,
            is_stocked           INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS categories (
            name TEXT PRIMARY KEY
        );
    """)
    # Migrate: add new columns to existing databases
    cur = conn.execute("PRAGMA table_info(products)")
    existing_cols = {row[1] for row in cur.fetchall()}
    new_cols = {
        "preferred_supplier":   "TEXT",
        "bin_location":         "TEXT",
        "supplier_part_number": "TEXT",
        "is_stocked":           "INTEGER DEFAULT 0",
    }
    for col, typedef in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col} {typedef}")

    # Seed categories from existing product category values
    conn.execute("""
        INSERT OR IGNORE INTO categories (name)
        SELECT DISTINCT TRIM(category) FROM products
        WHERE category IS NOT NULL AND TRIM(category) != ''
    """)

    # Seed default markup codes if empty
    count = conn.execute("SELECT COUNT(*) FROM markup_codes").fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO markup_codes (code, description, markup_pct) VALUES (?,?,?)",
            [
                ("DEFAULT", "Default markup (unclassified items)", 30.0),
                ("SMALL",   "Small / low-value items",             10.0),
                ("MED",     "Medium value items",                   50.0),
                ("LARGE",   "Large / high-value items",            100.0),
                ("ELEC",    "Electronics",                         200.0),
                ("BULK",    "Bulk / commodity",                     15.0),
            ]
        )
    else:
        # Ensure DEFAULT code exists in older databases
        conn.execute(
            "INSERT OR IGNORE INTO markup_codes (code, description, markup_pct) VALUES (?,?,?)",
            ("DEFAULT", "Default markup (unclassified items)", 30.0)
        )
    conn.commit()
    conn.close()

def markup_dict(conn) -> dict:
    rows = conn.execute("SELECT code, markup_pct FROM markup_codes").fetchall()
    return {r["code"]: r["markup_pct"] for r in rows}

def enrich_product(p: dict, markups: dict) -> dict:
    """Add computed fields to a product dict."""
    pack_qty  = max(0.001, float(p.get("pack_qty") or 1.0))
    pack_cost = p.get("pack_cost") or 0.0
    code      = p.get("markup_code")
    pct       = markups.get(code, 0.0) if code else 0.0

    unit_cost  = pack_cost / pack_qty
    sell_price = unit_cost * (1 + pct / 100.0)
    margin     = sell_price - unit_cost
    margin_pct = (margin / sell_price * 100) if sell_price else 0.0

    return {
        **p,
        "unit_cost":  round(unit_cost,  4),
        "markup_pct": pct,
        "sell_price": round(sell_price, 4),
        "margin":     round(margin,     4),
        "margin_pct": round(margin_pct, 2),
    }

PRODUCT_COLS = (
    "id, name, sku, category, pack_qty, pack_cost, markup_code, "
    "preferred_supplier, bin_location, supplier_part_number, is_stocked"
)

# ══════════════════════════════════════════════
#  PYDANTIC MODELS
# ══════════════════════════════════════════════

class ProductIn(BaseModel):
    name:                 Optional[str]   = None
    sku:                  Optional[str]   = None
    category:             Optional[str]   = None
    pack_qty:             Optional[float] = 1.0
    pack_cost:            Optional[float] = 0.0
    markup_code:          Optional[str]   = None
    preferred_supplier:   Optional[str]   = None
    bin_location:         Optional[str]   = None
    supplier_part_number: Optional[str]   = None
    is_stocked:           Optional[bool]  = False

class ProductUpdate(BaseModel):
    name:                 Optional[str]   = None
    sku:                  Optional[str]   = None
    category:             Optional[str]   = None
    pack_qty:             Optional[float] = None
    pack_cost:            Optional[float] = None
    markup_code:          Optional[str]   = None
    preferred_supplier:   Optional[str]   = None
    bin_location:         Optional[str]   = None
    supplier_part_number: Optional[str]   = None
    is_stocked:           Optional[bool]  = None

class MarkupCodeIn(BaseModel):
    code:        str
    description: Optional[str] = ""
    markup_pct:  float = 100.0

class MarkupCodeUpdate(BaseModel):
    description: Optional[str]   = None
    markup_pct:  Optional[float] = None

class SettingsIn(BaseModel):
    default_markup_code: Optional[str] = None


# ══════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════

init_db()
app = FastAPI(title="Border to Border Inventory Manager")

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
def root():
    return FileResponse(str(static_dir / "index.html"))


# ── Markup Codes ──────────────────────────────

@app.get("/api/markup-codes")
def list_markup_codes():
    conn = get_conn()
    rows = conn.execute(
        "SELECT code, description, markup_pct FROM markup_codes ORDER BY code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/markup-codes", status_code=201)
def create_markup_code(body: MarkupCodeIn):
    conn = get_conn()
    code = body.code.upper().strip()
    try:
        conn.execute(
            "INSERT INTO markup_codes (code, description, markup_pct) VALUES (?,?,?)",
            (code, body.description, body.markup_pct)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"Code '{code}' already exists")
    conn.close()
    return {"code": code, "description": body.description, "markup_pct": body.markup_pct}

@app.patch("/api/markup-codes/{code}")
def update_markup_code(code: str, body: MarkupCodeUpdate):
    conn = get_conn()
    row = conn.execute("SELECT * FROM markup_codes WHERE code=?", (code,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Code not found")
    if body.description is not None:
        conn.execute("UPDATE markup_codes SET description=? WHERE code=?", (body.description, code))
    if body.markup_pct is not None:
        conn.execute("UPDATE markup_codes SET markup_pct=? WHERE code=?", (body.markup_pct, code))
    conn.commit()
    updated = conn.execute("SELECT * FROM markup_codes WHERE code=?", (code,)).fetchone()
    conn.close()
    return dict(updated)

@app.delete("/api/markup-codes/{code}", status_code=204)
def delete_markup_code(code: str):
    conn = get_conn()
    conn.execute("UPDATE products SET markup_code=NULL WHERE markup_code=?", (code,))
    conn.execute("DELETE FROM markup_codes WHERE code=?", (code,))
    conn.commit()
    conn.close()


# ── Products ──────────────────────────────────

@app.get("/api/products")
def list_products():
    conn    = get_conn()
    markups = markup_dict(conn)
    rows    = conn.execute(
        f"SELECT {PRODUCT_COLS} FROM products ORDER BY name COLLATE NOCASE"
    ).fetchall()
    conn.close()
    return [enrich_product(dict(r), markups) for r in rows]

@app.post("/api/products", status_code=201)
def create_product(body: ProductIn):
    conn = get_conn()
    cur  = conn.execute(
        "INSERT INTO products (name, sku, category, pack_qty, pack_cost, markup_code, "
        "preferred_supplier, bin_location, supplier_part_number, is_stocked) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (body.name, body.sku, body.category,
         max(0.001, float(body.pack_qty or 1.0)), body.pack_cost or 0.0,
         body.markup_code.upper().strip() if body.markup_code else None,
         body.preferred_supplier, body.bin_location, body.supplier_part_number,
         1 if body.is_stocked else 0)
    )
    conn.commit()
    markups = markup_dict(conn)
    row     = conn.execute(f"SELECT {PRODUCT_COLS} FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return enrich_product(dict(row), markups)

@app.patch("/api/products/{pid}")
def update_product(pid: int, body: ProductUpdate):
    conn = get_conn()
    if not conn.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone():
        conn.close()
        raise HTTPException(404, "Product not found")
    fields = {}
    if body.name                 is not None: fields["name"]                 = body.name
    if body.sku                  is not None: fields["sku"]                  = body.sku
    if body.category             is not None: fields["category"]             = body.category
    if body.pack_qty             is not None: fields["pack_qty"]             = max(0.001, float(body.pack_qty))
    if body.pack_cost            is not None: fields["pack_cost"]            = body.pack_cost
    if body.markup_code          is not None: fields["markup_code"]          = body.markup_code.upper().strip() or None
    if body.preferred_supplier   is not None: fields["preferred_supplier"]   = body.preferred_supplier
    if body.bin_location         is not None: fields["bin_location"]         = body.bin_location
    if body.supplier_part_number is not None: fields["supplier_part_number"] = body.supplier_part_number
    if body.is_stocked           is not None: fields["is_stocked"]           = 1 if body.is_stocked else 0
    if fields:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE products SET {set_clause} WHERE id=?", (*fields.values(), pid))
        conn.commit()
    markups = markup_dict(conn)
    updated = conn.execute(f"SELECT {PRODUCT_COLS} FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return enrich_product(dict(updated), markups)

@app.delete("/api/products/{pid}", status_code=204)
def delete_product(pid: int):
    conn = get_conn()
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    conn.close()

@app.delete("/api/products", status_code=204)
def delete_products_bulk(ids: list[int]):
    conn = get_conn()
    ph   = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM products WHERE id IN ({ph})", ids)
    conn.commit()
    conn.close()

@app.post("/api/products/recalculate")
def recalculate_prices():
    """
    Re-compute sell_price for every product that has a markup_code.
    Since sell_price is a computed field (not stored), this just returns
    the full refreshed product list — the frontend re-renders from it.
    Products without a markup_code are returned with sell_price = unit_cost (0% markup).
    """
    conn    = get_conn()
    markups = markup_dict(conn)
    rows    = conn.execute(
        f"SELECT {PRODUCT_COLS} FROM products ORDER BY name COLLATE NOCASE"
    ).fetchall()
    conn.close()
    enriched = [enrich_product(dict(r), markups) for r in rows]
    return {"updated": len(enriched), "products": enriched}


# ── Categories ────────────────────────────────

@app.get("/api/categories")
def list_categories():
    conn = get_conn()
    rows = conn.execute("SELECT name FROM categories ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return [r["name"] for r in rows]

@app.post("/api/categories", status_code=201)
def create_category(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"Category '{name}' already exists")
    conn.close()
    return {"name": name}

@app.put("/api/categories/{old_name}")
def rename_category(old_name: str, body: dict):
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "Name required")
    conn = get_conn()
    if not conn.execute("SELECT name FROM categories WHERE name=?", (old_name,)).fetchone():
        conn.close()
        raise HTTPException(404, "Category not found")
    try:
        conn.execute("UPDATE categories SET name=? WHERE name=?", (new_name, old_name))
        conn.execute("UPDATE products SET category=? WHERE category=?", (new_name, old_name))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"Category '{new_name}' already exists")
    conn.close()
    return {"name": new_name}

@app.delete("/api/categories/{name}", status_code=204)
def delete_category(name: str):
    conn = get_conn()
    conn.execute("DELETE FROM categories WHERE name=?", (name,))
    conn.commit()
    conn.close()


# ── Settings ──────────────────────────────────

@app.get("/api/settings")
def get_settings():
    conn   = get_conn()
    rows   = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    result = {r["key"]: r["value"] for r in rows}
    return {"default_markup_code": result.get("default_markup_code", "")}

@app.put("/api/settings")
def save_settings(body: SettingsIn):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("default_markup_code", body.default_markup_code or "")
    )
    conn.commit()
    conn.close()
    return {"default_markup_code": body.default_markup_code or ""}


# ── Backup & Restore ──────────────────────────

@app.get("/api/backup")
def backup_database():
    """Stream the SQLite database file as a download."""
    if not Path(DB_PATH).exists():
        raise HTTPException(404, "Database not found")
    from datetime import datetime
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"b2b_inventory_backup_{ts}.db"
    return FileResponse(
        path        = DB_PATH,
        media_type  = "application/octet-stream",
        filename    = filename,
    )

@app.post("/api/restore")
async def restore_database(file: UploadFile = File(...)):
    """Replace the database with an uploaded .db file."""
    if not file.filename.endswith(".db"):
        raise HTTPException(400, "File must be a .db file")

    # Write upload to a temp file first, validate it's a real SQLite DB
    tmp = Path(tempfile.mktemp(suffix=".db"))
    try:
        contents = await file.read()
        tmp.write_bytes(contents)

        # Validate: SQLite files start with this magic string
        if contents[:16] != b"SQLite format 3\x00":
            raise HTTPException(400, "File does not appear to be a valid SQLite database")

        # Quick sanity check — can we open and read it?
        test_conn = sqlite3.connect(str(tmp))
        tables = {r[0] for r in test_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        test_conn.close()

        required = {"products", "markup_codes"}
        missing  = required - tables
        if missing:
            raise HTTPException(400, f"Database missing expected tables: {missing}")

        # Replace live database
        shutil.copy2(str(tmp), DB_PATH)
        return {"ok": True, "tables": list(tables)}

    finally:
        if tmp.exists():
            tmp.unlink()


# ── Reports ───────────────────────────────────

@app.get("/api/reports/bin-location")
def report_bin_location():
    """Return stocked products grouped by bin location for the report."""
    conn    = get_conn()
    markups = markup_dict(conn)
    rows    = conn.execute(
        f"SELECT {PRODUCT_COLS} FROM products "
        "ORDER BY COALESCE(NULLIF(TRIM(bin_location),''), 'ZZZ_UNASSIGNED') COLLATE NOCASE, "
        "name COLLATE NOCASE"
    ).fetchall()
    conn.close()

    enriched = [enrich_product(dict(r), markups) for r in rows]

    # Group by bin_location
    groups = {}
    for p in enriched:
        key = p.get("bin_location") or ""
        key = key.strip() if key else ""
        label = key if key else "— No Bin Assigned —"
        groups.setdefault(label, []).append(p)

    return {
        "total":  len(enriched),
        "groups": [{"bin": k, "items": v} for k, v in groups.items()],
    }


# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
