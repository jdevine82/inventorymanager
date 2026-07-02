"""
Border to Border Inventory Manager — FastAPI + SQLite backend
Run: python app.py
Access: http://<your-pi-ip>:8000
"""

import re
import sqlite3
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

DB_PATH = "prices.db"
SM8_API_BASE = "https://api.servicem8.com/api_1.0"

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
        "sm8_uuid":             "TEXT",
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
    servicem8_api_key:   Optional[str] = None


class SM8SyncRequest(BaseModel):
    product_ids: Optional[list[int]] = None

class SM8SyncOneRequest(BaseModel):
    product_id: int


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
    return {
        "default_markup_code": result.get("default_markup_code", ""),
        "servicem8_api_key":   result.get("servicem8_api_key", ""),
    }

@app.put("/api/settings")
def save_settings(body: SettingsIn):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("default_markup_code", body.default_markup_code or "")
    )
    if body.servicem8_api_key is not None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("servicem8_api_key", body.servicem8_api_key.strip())
        )
    conn.commit()
    conn.close()
    return get_settings()


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


# ── ServiceM8 API Sync ────────────────────────
# Field mapping mirrors the SM8 CSV export: sku → item_number, name → name,
# unit_cost → cost, sell_price → price, is_stocked → item_is_inventoried.
# quantity_in_stock is intentionally omitted so existing SM8 stock counts
# aren't reset — this app doesn't track stock quantity.

def get_sm8_api_key(conn) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key='servicem8_api_key'").fetchone()
    key = (row["value"] if row else "") or ""
    if not key:
        raise HTTPException(400, "ServiceM8 API key not set — add it in Settings first")
    return key

def sm8_headers(api_key: str) -> dict:
    return {"X-Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"}

def sm8_request(method: str, url: str, *, timeout: int = 15, **kwargs) -> requests.Response:
    """
    requests.request with retry/backoff for transient SM8 errors (429 rate
    limiting, 5xx). A full-catalog sync makes hundreds of sequential calls,
    and that was observed to trigger 429s partway through, surfacing as
    permanent "failed" items even though the same request succeeds instantly
    on its own. Retry a few times with backoff before giving up for real.
    """
    max_attempts = 4
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.RequestException:
            if attempt == max_attempts:
                raise
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else delay
            time.sleep(wait)
            delay *= 2
            continue

        return resp
    return resp  # pragma: no cover — loop always returns or raises above

def sm8_find_uuid_by_item_number(api_key: str, item_number: str) -> Optional[str]:
    """
    Match by item_number, active records only. SM8 accounts can end up with
    multiple Materials sharing an item_number (e.g. an old deleted/inactive
    duplicate) — matching on item_number alone can pick the wrong one and
    push updates into a dead record forever instead of the real one.
    """
    escaped = item_number.replace("'", "''")
    resp = sm8_request(
        "GET", f"{SM8_API_BASE}/material.json",
        headers=sm8_headers(api_key),
        params={"$filter": f"item_number eq '{escaped}' and active eq 1"},
        timeout=15,
    )
    resp.raise_for_status()
    matches = resp.json()
    return matches[0]["uuid"] if matches else None

def sm8_paginate_materials(api_key: str, filter_expr: Optional[str] = None):
    """Yield raw Material dicts from ServiceM8, following cursor pagination."""
    cursor = "-1"
    for _ in range(500):   # safety cap — 500 pages x 1000 records is far beyond any real catalog
        params = {"cursor": cursor}
        if filter_expr:
            params["$filter"] = filter_expr
        try:
            resp = sm8_request(
                "GET", f"{SM8_API_BASE}/material.json", headers=sm8_headers(api_key), params=params, timeout=20,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise HTTPException(502, f"ServiceM8 request failed: {e}")
        yield from resp.json()
        cursor = resp.headers.get("x-next-cursor")
        if not cursor:
            break

def sm8_fetch_all_materials(api_key: str) -> dict:
    """Every active Material in SM8 (any inventoried state), keyed by lowercased item_number."""
    by_item_number = {}
    for m in sm8_paginate_materials(api_key, "active eq 1"):
        item_number = (m.get("item_number") or "").strip()
        if not item_number:
            continue
        by_item_number[item_number.lower()] = {
            "uuid":                m.get("uuid"),
            "name":                m.get("name") or "",
            "cost":                m.get("cost"),
            "price":               m.get("price"),
            "item_is_inventoried": m.get("item_is_inventoried"),
        }
    return by_item_number

# SM8 silently flattens typographic punctuation to plain ASCII when it saves
# a Material name (en/em dash -> hyphen, curly quotes -> straight quotes).
# Supplier CSV descriptions (Middys etc.) often contain the typographic form,
# so without normalizing here the diff would flag these as changed forever.
_SM8_CHAR_NORMALIZE = str.maketrans({
    "–": "-",    # en dash
    "—": "-",    # em dash
    "‘": "'",    # left single quote
    "’": "'",    # right single quote / apostrophe
    "“": '"',    # left double quote
    "”": '"',    # right double quote
    "™": "(TM)", # trademark symbol -> SM8 transliterates to (TM)
    "®": "(R)",  # registered symbol -> SM8 transliterates to (R)
    "©": "(C)",  # copyright symbol -> SM8 transliterates to (C)
    "|": "",     # pipe -> SM8 deletes it outright (leaving the surrounding spaces as-is)
})

def sm8_clean_name(name: Optional[str]) -> str:
    """
    This app's Middys import formats names as "CODE: Description", but SM8
    material names don't use that colon convention. Strip it and normalize
    punctuation so pushed/compared names match what's actually in SM8
    instead of drifting forever.
    """
    cleaned = re.sub(r"\s*:\s*", " ", name or "").strip()
    return cleaned.translate(_SM8_CHAR_NORMALIZE)

def sm8_build_payload(product: dict) -> dict:
    return {
        "name":                sm8_clean_name(product.get("name")),
        "item_number":         (product.get("sku") or "").strip(),
        "cost":                f"{product['unit_cost']:.4f}",
        "price":               f"{product['sell_price']:.4f}",
        "item_is_inventoried": 1 if product.get("is_stocked") else 0,
    }

def sm8_bool(v) -> bool:
    """
    SM8 returns boolean-ish fields inconsistently — item_is_inventoried comes
    back as the *string* "0"/"1" (where bool("0") is True in Python!), while
    other flags come back as real ints. Handle both.
    """
    if isinstance(v, str):
        return v.strip() not in ("", "0", "false", "False", "no", "No")
    return bool(v)

def sm8_diff_fields(payload: dict, existing: dict) -> dict:
    """Compare an intended SM8 payload against SM8's current record. Returns only changed fields."""
    changes = {}

    if (existing.get("name") or "") != (payload["name"] or ""):
        changes["name"] = {"from": existing.get("name") or "", "to": payload["name"]}

    def _num(v):
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return None

    existing_cost, intended_cost = _num(existing.get("cost")), _num(payload["cost"])
    if existing_cost != intended_cost:
        changes["cost"] = {"from": existing_cost, "to": intended_cost}

    existing_price, intended_price = _num(existing.get("price")), _num(payload["price"])
    if existing_price != intended_price:
        changes["price"] = {"from": existing_price, "to": intended_price}

    existing_inv  = sm8_bool(existing.get("item_is_inventoried"))
    intended_inv  = sm8_bool(payload["item_is_inventoried"])
    if existing_inv != intended_inv:
        changes["item_is_inventoried"] = {"from": existing_inv, "to": intended_inv}

    return changes

def sm8_create_material(api_key: str, payload: dict) -> Optional[str]:
    resp = sm8_request(
        "POST", f"{SM8_API_BASE}/material.json", headers=sm8_headers(api_key), json=payload, timeout=15
    )
    resp.raise_for_status()
    return resp.headers.get("x-record-uuid")

def sm8_update_material(api_key: str, uuid: str, payload: dict):
    resp = sm8_request(
        "POST", f"{SM8_API_BASE}/material/{uuid}.json", headers=sm8_headers(api_key), json=payload, timeout=15
    )
    resp.raise_for_status()

def sm8_push_product(api_key: str, product: dict) -> tuple[str, Optional[str]]:
    """
    Push one product to SM8. Returns (result, uuid) where result is 'created' or 'updated'.

    Always resolves the target record via the active item_number lookup rather
    than trusting a locally cached sm8_uuid — SM8 accounts can end up with an
    inactive/deleted duplicate sharing the same item_number (e.g. from an
    earlier failed sync), and a stale cached uuid pointing at that duplicate
    would otherwise keep updating a dead record forever while the real active
    one never changes. Re-resolving each time is self-healing: a bad cached
    uuid gets silently replaced with the correct one.
    """
    sku     = (product.get("sku") or "").strip()
    payload = sm8_build_payload(product)

    uuid = sm8_find_uuid_by_item_number(api_key, sku)
    if uuid:
        sm8_update_material(api_key, uuid, payload)
        return "updated", uuid

    uuid = sm8_create_material(api_key, payload)
    return "created", uuid

def sm8_sync_row(conn, api_key: str, product: dict) -> dict:
    """Push one product to SM8 and persist its uuid. Never raises — failures come back in the result."""
    sku = (product.get("sku") or "").strip()
    if not sku:
        return {"id": product["id"], "sku": sku, "name": product.get("name"), "action": "failed", "error": "No SKU / Item Number set"}
    try:
        result, uuid = sm8_push_product(api_key, product)
        if uuid and uuid != product.get("sm8_uuid"):
            conn.execute("UPDATE products SET sm8_uuid=? WHERE id=?", (uuid, product["id"]))
        return {"id": product["id"], "sku": sku, "name": product.get("name"), "action": result, "error": None}
    except requests.exceptions.RequestException as e:
        return {"id": product["id"], "sku": sku, "name": product.get("name"), "action": "failed", "error": str(e)}

@app.post("/api/sm8/sync/one")
def sm8_sync_one(body: SM8SyncOneRequest):
    """Sync a single product — used by the frontend to show per-item sync progress."""
    conn    = get_conn()
    api_key = get_sm8_api_key(conn)
    markups = markup_dict(conn)

    row = conn.execute(f"SELECT {PRODUCT_COLS}, sm8_uuid FROM products WHERE id=?", (body.product_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Product not found")

    product = enrich_product(dict(row), markups)
    result  = sm8_sync_row(conn, api_key, product)
    conn.commit()
    conn.close()
    return result

@app.post("/api/sm8/sync")
def sm8_sync(body: SM8SyncRequest):
    conn    = get_conn()
    api_key = get_sm8_api_key(conn)
    markups = markup_dict(conn)

    query  = f"SELECT {PRODUCT_COLS}, sm8_uuid FROM products"
    params: tuple = ()
    if body.product_ids:
        ph = ",".join("?" * len(body.product_ids))
        query += f" WHERE id IN ({ph})"
        params = tuple(body.product_ids)
    rows = conn.execute(query, params).fetchall()

    created, updated, failed = [], [], []
    for row in rows:
        product = enrich_product(dict(row), markups)
        result  = sm8_sync_row(conn, api_key, product)
        if result["action"] == "created":
            created.append(result)
        elif result["action"] == "updated":
            updated.append(result)
        else:
            failed.append(result)

    conn.commit()
    conn.close()
    return {
        "total":   len(rows),
        "created": len(created),
        "updated": len(updated),
        "failed":  failed,
    }

@app.get("/api/sm8/materials")
def sm8_list_materials():
    """
    Pull inventoried Materials from ServiceM8 via the API. Same filter as
    the SM8 CSV import (Item is Inventoried = Yes only). Uses ServiceM8's
    cursor-based pagination (up to 1000 records per page).
    """
    conn    = get_conn()
    api_key = get_sm8_api_key(conn)
    conn.close()

    materials = []
    for m in sm8_paginate_materials(api_key, "item_is_inventoried eq 1 and active eq 1"):
        item_number = (m.get("item_number") or "").strip()
        if not item_number:
            continue
        cost = m.get("cost")
        materials.append({
            "item_number": item_number,
            "name":        m.get("name") or "",
            "cost":        str(cost) if cost is not None else "",
        })
    return materials

@app.post("/api/sm8/sync/preview")
def sm8_sync_preview(body: SM8SyncRequest):
    """
    Dry run for /api/sm8/sync: fetches SM8's current Materials and diffs them
    against the local products in scope, without writing anything to SM8.
    Returns counts plus a short sample of the actual field-level changes so
    the user can review before approving the real sync.
    """
    conn    = get_conn()
    api_key = get_sm8_api_key(conn)
    markups = markup_dict(conn)

    query  = f"SELECT {PRODUCT_COLS}, sm8_uuid FROM products"
    params: tuple = ()
    if body.product_ids:
        ph = ",".join("?" * len(body.product_ids))
        query += f" WHERE id IN ({ph})"
        params = tuple(body.product_ids)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    sm8_materials = sm8_fetch_all_materials(api_key)

    no_sku, unchanged = 0, 0
    diffs = []
    for row in rows:
        product = enrich_product(dict(row), markups)
        sku     = (product.get("sku") or "").strip()
        if not sku:
            no_sku += 1
            continue

        payload  = sm8_build_payload(product)
        existing = sm8_materials.get(sku.lower())

        if not existing:
            diffs.append({
                "id": product["id"], "sku": sku, "name": product.get("name"),
                "action": "create",
                "changes": {
                    "name":                {"from": None, "to": payload["name"]},
                    "cost":                {"from": None, "to": round(float(payload["cost"]), 4)},
                    "price":               {"from": None, "to": round(float(payload["price"]), 4)},
                    "item_is_inventoried": {"from": None, "to": bool(payload["item_is_inventoried"])},
                },
            })
            continue

        changes = sm8_diff_fields(payload, existing)
        if changes:
            diffs.append({"id": product["id"], "sku": sku, "name": product.get("name"), "action": "update", "changes": changes})
        else:
            unchanged += 1

    diffs.sort(key=lambda d: (d["sku"] or "").lower())

    return {
        "total_checked": len(rows),
        "no_sku":        no_sku,
        "creates":       sum(1 for d in diffs if d["action"] == "create"),
        "updates":       sum(1 for d in diffs if d["action"] == "update"),
        "unchanged":     unchanged,
        "changed_ids":   [d["id"] for d in diffs],
        "preview":       diffs[:5],
    }


# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
