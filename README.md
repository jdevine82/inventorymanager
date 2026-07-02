# Border to Border Inventory Manager

A single-file FastAPI + SQLite app for managing a parts/inventory catalog,
computing sell prices from markup codes, and keeping that catalog in sync
with [ServiceM8](https://www.servicem8.com/) via CSV import or its API.

- Backend: `app.py` (FastAPI, SQLite at `prices.db`)
- Frontend: `static/index.html` (single-page vanilla JS/HTML/CSS, no build step)
- Install/deploy: see [INSTALL.md](INSTALL.md) (Proxmox Ubuntu LXC + systemd)

Run locally:
```bash
pip install -r requirements.txt
python app.py
# http://localhost:8000
```

---

## Core concepts

**Product** — one catalog row: `name`, `sku`, `category`, `pack_qty`,
`pack_cost`, `markup_code`, `preferred_supplier`, `bin_location`,
`supplier_part_number`, `is_stocked` (the "Stocked" checkbox).

**Computed fields** (never stored, always derived on read):
| Field | Formula |
|---|---|
| `unit_cost` | `pack_cost / pack_qty` |
| `sell_price` | `unit_cost × (1 + markup_pct / 100)` |
| `margin` | `sell_price − unit_cost` |
| `margin_pct` | `margin / sell_price × 100` |

**Markup Code** — a named percentage (e.g. `DEFAULT` 30%, `HIGH` 100%)
applied to a product's unit cost to get its sell price. Editing a markup
code's percentage instantly repricess every product using that code.

**Category** — a free-text grouping, managed as its own list (rename/delete
propagates to all products using it).

---

## CSV import formats

Every import button expects a specific column layout. Rows that don't match
(missing SKU, wrong headers, etc.) are skipped or rejected — check the
in-app preview before confirming an import.

### Import CSV (generic, flexible mapping)

Any CSV with a header row. Column headers are auto-matched (case-insensitive,
spaces/hyphens normalized to underscores) against these aliases, then you can
correct the mapping manually before importing:

| DB field | Recognized header aliases |
|---|---|
| `name` | name, product, product_name, item, description, title |
| `sku` | sku, code, item_code, product_code, part_no, part_number, barcode |
| `category` | category, cat, type, dept, department, group |
| `pack_qty` | pack_qty, pack_size, qty, quantity, units_per_pack, pack |
| `pack_cost` | pack_cost, cost, buy_price, purchase_price, buy, price_each, unit_cost |
| `markup_code` | markup_code, markup, price_code, margin_code, code |

Unmapped columns are ignored. Modes:
- **Replace All** — deletes every existing product, imports every row fresh.
- **Update Existing** — matches by SKU, updates mapped fields on matches, skips unmatched rows.
- **Merge** — same as Update, but unmatched rows are added as new products.

The **Export CSV** button produces a file in this same shape (`name, sku,
category, pack_qty, pack_cost, markup_code, unit_cost, markup_pct,
sell_price, margin, margin_pct`) — handy as a round-trip backup format or
a starting template.

### Import Middys

CSV with a header row. Required columns (exact names, case-insensitive):

| Middys column | → DB field |
|---|---|
| `New Product Code` | `sku` (also becomes `supplier_part_number`) |
| `Description` | `name` (new items only) |
| `Purchases Unit Price` | `pack_cost` |

- Matches existing products by SKU.
- **Existing products:** `pack_cost` is always updated. `name` is **never**
  overwritten. `preferred_supplier` (→ "Middys") and `supplier_part_number`
  are only updated if you tick the corresponding checkbox.
- **New products:** created with `pack_cost`, `pack_qty = 1`,
  `preferred_supplier = "Middys"`, and the markup code selected in the
  import dialog (or none).
- Prints a printable report of additions/updates after import.

### Import Voltex

Same shape and behavior as Middys, with SM8-style header names:

| Voltex column | → DB field |
|---|---|
| `Item Number` | `sku` (also becomes `supplier_part_number`) |
| `Name` | `name` (new items only) |
| `Purchase Cost` | `pack_cost` |

Update/new-item rules are identical to Middys (`preferred_supplier` becomes
`"Voltex"`).

### Import Actrol

CSV with **no header row** — columns are read by fixed position, and rows
with fewer than 6 columns are skipped:

| Column index (0-based) | → DB field |
|---|---|
| 2 | `sku` (also becomes `supplier_part_number`) |
| 3 | `name` (new items only) |
| 5 | `pack_cost` (Price 1 / Trade) |

Update/new-item rules are identical to Middys/Voltex (`preferred_supplier`
becomes `"Actrol"`, new items default `is_stocked = false`).

### Import SM8 (API) — not a CSV

This pulls **inventoried, active** Materials directly from your ServiceM8
account via the API (no file needed) and feeds them into the same
mapping/preview modal as a CSV would. See [ServiceM8 integration](#servicem8-integration)
below.

---

## ServiceM8 integration

Set your API key first: **Settings → ServiceM8 API Key** (from ServiceM8:
Settings → API Keys). The key is stored in `prices.db`, which is
`.gitignore`d — it's never committed to source control.

### ⬇ Import SM8 (API)

Fetches every Material in your SM8 account where `item_is_inventoried = Yes`
and `active = Yes`, and opens the standard import modal (Replace All /
Update Existing / Merge — same semantics as the CSV imports above). Field
mapping: SM8 `item_number` → SKU (match key), `name` → Name (new items
only), `cost` → shown in preview only, never imported (pricing comes from
this app's markup codes, not from SM8's own price field).

### ⇄ Sync SM8 API — push local products to ServiceM8

This is a **read → diff → preview → approve** flow, not a blind push:

1. Click the button — it fetches every active Material currently in your
   SM8 account and diffs it against whatever's currently visible in the
   product table (respects the search box filter).
2. A preview modal shows counts (new / updated / already in sync) and a
   sample of the actual field-level changes.
3. Nothing is sent to ServiceM8 until you click **Approve & Sync**. Cancel
   makes zero API calls.
4. On approve, each changed product is pushed one at a time with live
   progress, matched to SM8 by SKU (`item_number`), only among **active**
   SM8 records (an inactive/deleted record sharing the same item_number is
   never matched or updated).

Field mapping when pushing:

| Local field | → SM8 field |
|---|---|
| `sku` | `item_number` (match key) |
| `name` | `name` (typographic punctuation normalized — see below) |
| `unit_cost` | `cost` |
| `sell_price` | `price` |
| `is_stocked` | `item_is_inventoried` |

`quantity_in_stock` is deliberately **never** sent — this app doesn't track
stock quantity, and pushing it would zero out real stock counts already in
SM8.

**Name normalization:** ServiceM8 silently flattens certain characters when
it saves a Material name, so names are cleaned before comparing/pushing to
avoid an endless false "changed" diff:
- The `"CODE: Description"` colon convention this app's imports use → colon stripped.
- En dash `–` / em dash `—` → `-`
- Curly quotes `‘’“”` → straight `'`/`"`
- `™` `®` `©` → `(TM)` `(R)` `(C)`
- Pipe `|` → deleted entirely (SM8 removes it with no replacement)

---

## Backup & Restore

**⬇ Backup DB** downloads the live `prices.db` (includes every product,
markup code, category, and your ServiceM8 API key — treat the downloaded
file as sensitive). **⬆ Restore DB** replaces the live database with an
uploaded `.db` file after validating it's a real SQLite file with the
expected tables.

## Reports

**📋 Bin Report** — printable listing of all stocked products grouped by
`bin_location`, unassigned items grouped last.

## Settings

- **Default Markup Code** — pre-filled markup code for new items added via
  any import or the Add Product form.
- **ServiceM8 API Key** — see [ServiceM8 integration](#servicem8-integration).
