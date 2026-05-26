"""
Import GoFrugal RayMedi HQ sales into ERPNext Sales Invoices (file-based).

Source: the GoFrugal report ``3111_Sales_Deta_-_Itemwise`` exported as .xls/.xlsx.
Layout: rows 1-3 = company/date header, the header row contains "Bill Number",
data rows follow (one row per item line, many rows per bill).

This is a FINANCE document import, so it is conservative by design:
- Creates Sales Invoices as DRAFT only (docstatus=0). Finance reviews + submits.
- ``dry_run=1`` by default — returns a preview, writes nothing.
- Never auto-creates Items. An unknown Item Code skips the whole invoice with an
  error (matches the Uniware importer's "no auto-create items" rule).
- Dedups by the ``gofrugal_bill_number`` custom field on Sales Invoice.
- ``update_stock`` defaults to 0 (no stock movement) — set to 1 only once the
  outlet→warehouse map is confirmed, since a wrong warehouse posts bad stock.

Verified against live adilqadri.m.frappe.cloud (2026-05-26), correcting the FRD:
- Company is read from Global Defaults, NOT the literal FRD string.
- Territory "AREA" does not exist → only set territory if it resolves to a real
  Territory, otherwise leave blank.
- Naming series is left to ERPNext's default (live default is the GST series);
  the FRD's "ACC-SINV-.YYYY.-" does not exist on the site, so we never set it.

The transformation logic (date parse, casts, customer rule, grouping, payload
build) is kept as pure functions with no Frappe dependency so it is unit-testable
without a bench.
"""

from __future__ import annotations

import re
from collections import OrderedDict

import frappe

# --- Source column names (exact headers in the GoFrugal 3111 report) ----------
COL_OUTLET = "Outlet Name"
COL_CUST_BILLING = "Cust Name(During Billing)"
COL_BILL_NUMBER = "Bill Number"
COL_MOBILE = "Mobile No/SMS Alerts"
COL_ITEM_CODE = "Item Code"
COL_ITEM_NAME = "Item Name"
COL_QTY = "Sold Qty"
COL_SELLING = "Selling"
COL_NET_AMT = "Item Net Amt"
COL_ITEM_DISC_PCT = "Item Disc%"
COL_ITEM_DISC = "Item Disc"
COL_BILL_DISC_PCT = "Bill Disc%"
COL_ACTUAL_SELLING = "Actual Selling"
COL_AREA = "Area Name"
COL_BILL_TYPE = "Bill Type"
COL_DOC_NAME = "Doc Name(During Billing)"
COL_EAN = "EAN Code"
COL_BILL_REMARKS = "Bill Remarks"

# Constants from the FRD (abbreviation-based; company name comes from the DB).
DEFAULT_UOM = "Nos"
DEFAULT_CURRENCY = "INR"

# Outlet (GoFrugal) -> ERPNext Warehouse. Verified target "Stores - AEPL" exists.
# Only used when update_stock=1.
DEFAULT_WAREHOUSE_MAP = {"Head Office": "Stores - AEPL"}

_BLANK = {"", ".", "nan", "none", "null"}


# ---------------------------------------------------------------------------
# Pure transformation helpers (no Frappe dependency — unit-testable).
# ---------------------------------------------------------------------------
def is_blank(value) -> bool:
	"""True for None / empty / placeholder values ('', '.', 'nan')."""
	if value is None:
		return True
	return str(value).strip().lower() in _BLANK


def clean_int_str(value) -> str:
	"""Cast a numeric-ish cell to a clean integer string.

	GoFrugal exports Item Code / EAN / Mobile as floats, so 199.0 and
	scientific notation (9.033302e+09) must become '199' / '9033301687'.
	Returns '' for blanks. Non-numeric text is returned stripped, as-is.
	"""
	if is_blank(value):
		return ""
	s = str(value).strip()
	try:
		return str(int(float(s)))
	except (ValueError, OverflowError):
		return s


def to_float(value, default=0.0) -> float:
	if is_blank(value):
		return default
	try:
		return float(str(value).strip())
	except ValueError:
		return default


def parse_posting_date(header_text):
	"""Extract an ISO date (YYYY-MM-DD) from the report's header line.

	Example header: 'DATE : from 21-05-2026 21-05-2026' -> '2026-05-21'.
	Uses the first dd-mm-yyyy (or dd/mm/yyyy) token found. Returns None if
	no date is present (caller falls back to today()).
	"""
	if not header_text:
		return None
	m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", str(header_text))
	if not m:
		return None
	dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
	return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def resolve_customer_name(doc_name, cust_name) -> str:
	"""Customer rule from the FRD: use 'Doc Name(During Billing)' unless it is
	blank/'.'/'nan', otherwise fall back to 'Cust Name(During Billing)'."""
	if not is_blank(doc_name):
		return str(doc_name).strip()
	if not is_blank(cust_name):
		return str(cust_name).strip()
	return ""


def build_remarks(bill_type, bill_remarks) -> str:
	"""Combine Bill Type + Bill Remarks for traceability."""
	bt = "" if is_blank(bill_type) else str(bill_type).strip()
	br = "" if is_blank(bill_remarks) else str(bill_remarks).strip()
	if bt and br:
		return f"{bt} | {br}"
	return bt or br


def group_rows_by_bill(rows):
	"""Group source data rows by Bill Number, preserving first-seen order.

	Rows with a blank Bill Number (subtotal/summary lines) are skipped.
	Returns an OrderedDict {bill_number: [row, ...]}.
	"""
	groups = OrderedDict()
	for row in rows:
		bill = row.get(COL_BILL_NUMBER)
		if is_blank(bill):
			continue
		bill = str(bill).strip()
		groups.setdefault(bill, []).append(row)
	return groups


def build_invoice_payload(bill_number, group_rows, posting_date):
	"""Build a plain-dict invoice payload from one bill's item rows.

	No DB access: item_code/customer are raw strings here; resolution to
	ERPNext records happens in the DB layer. ``territory``/``warehouse`` carry
	the source values for the DB layer to validate against real masters.
	"""
	head = group_rows[0]
	customer = resolve_customer_name(head.get(COL_DOC_NAME), head.get(COL_CUST_BILLING))

	items = []
	for r in group_rows:
		item_code = clean_int_str(r.get(COL_ITEM_CODE))
		if not item_code:
			# A line with no item code is unusable; record it so the caller
			# can fail the whole invoice rather than silently dropping a line.
			items.append({"item_code": "", "error": "blank item code"})
			continue
		items.append(
			{
				"item_code": item_code,
				"item_name": ("" if is_blank(r.get(COL_ITEM_NAME)) else str(r.get(COL_ITEM_NAME)).strip()),
				"barcode": clean_int_str(r.get(COL_EAN)),
				"qty": to_float(r.get(COL_QTY)),
				"rate": to_float(r.get(COL_SELLING)),
				"price_list_rate": to_float(r.get(COL_ACTUAL_SELLING)),
				"discount_percentage": to_float(r.get(COL_ITEM_DISC_PCT)),
				"net_amount": to_float(r.get(COL_NET_AMT)),
			}
		)

	return {
		"bill_number": bill_number,
		"customer": customer,
		"customer_name": ("" if is_blank(head.get(COL_CUST_BILLING)) else str(head.get(COL_CUST_BILLING)).strip()),
		"mobile": clean_int_str(head.get(COL_MOBILE)),
		"outlet": ("" if is_blank(head.get(COL_OUTLET)) else str(head.get(COL_OUTLET)).strip()),
		"area": ("" if is_blank(head.get(COL_AREA)) else str(head.get(COL_AREA)).strip()),
		"additional_discount_percentage": to_float(head.get(COL_BILL_DISC_PCT)),
		"remarks": build_remarks(head.get(COL_BILL_TYPE), head.get(COL_BILL_REMARKS)),
		"posting_date": posting_date,
		"items": items,
	}


# ---------------------------------------------------------------------------
# File reading (.xls via xlrd, .xlsx via openpyxl).
# ---------------------------------------------------------------------------
def _cell_values_xlsx(path):
	import openpyxl

	wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
	ws = wb.active
	return [list(r) for r in ws.iter_rows(values_only=True)]


def _cell_values_xls(path):
	import xlrd

	wb = xlrd.open_workbook(path)
	ws = wb.sheet_by_index(0)
	return [ws.row_values(i) for i in range(ws.nrows)]


def read_source(path):
	"""Read the GoFrugal export into (meta, rows).

	- Locates the header row by finding the row that contains "Bill Number".
	- Rows above it are scanned for a date to use as posting_date.
	- Returns rows as list[dict] keyed by the header labels.
	"""
	lower = path.lower()
	if lower.endswith(".xls"):
		grid = _cell_values_xls(path)
	elif lower.endswith((".xlsx", ".xlsm")):
		grid = _cell_values_xlsx(path)
	else:
		raise ValueError(f"Unsupported file type (need .xls/.xlsx): {path}")

	header_idx = None
	for i, row in enumerate(grid):
		labels = [str(c).strip() for c in row if c is not None]
		if COL_BILL_NUMBER in labels:
			header_idx = i
			break
	if header_idx is None:
		raise ValueError(f"Header row with '{COL_BILL_NUMBER}' not found in {path}")

	# Posting date from any line above the header (e.g. 'DATE : from 21-05-2026 ...').
	posting_date = None
	for row in grid[:header_idx]:
		text = " ".join(str(c) for c in row if c is not None)
		posting_date = parse_posting_date(text)
		if posting_date:
			break

	headers = [str(c).strip() if c is not None else "" for c in grid[header_idx]]
	rows = []
	for raw in grid[header_idx + 1 :]:
		row = {headers[j]: raw[j] for j in range(min(len(headers), len(raw)))}
		rows.append(row)

	return {"posting_date": posting_date}, rows


def parse_file(path):
	"""Convenience: read a file and return (meta, grouped_payloads)."""
	meta, rows = read_source(path)
	groups = group_rows_by_bill(rows)
	payloads = [
		build_invoice_payload(bill, grp, meta["posting_date"])
		for bill, grp in groups.items()
	]
	return meta, payloads


# ---------------------------------------------------------------------------
# Frappe / DB layer.
# ---------------------------------------------------------------------------
def _default_company():
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		rows = frappe.get_all("Company", limit=1, pluck="name")
		company = rows[0] if rows else None
	return company


def _resolve_territory(area):
	"""Return the area as a Territory only if it actually exists. The FRD's
	'AREA' value does not exist on the live site, so we never invent it."""
	if is_blank(area):
		return None
	return area if frappe.db.exists("Territory", area) else None


def _resolve_warehouse(outlet, warehouse_map):
	name = (warehouse_map or {}).get(str(outlet).strip())
	if name and frappe.db.exists("Warehouse", name):
		return name
	return None


def _get_or_create_customer(payload, dry_run):
	"""Find a Customer by exact name, else create one. Mirrors the Uniware
	importer: default (non-group) Customer Group + Territory."""
	name = payload["customer"] or payload["customer_name"]
	if not name:
		raise ValueError(f"bill {payload['bill_number']}: no customer name")
	name = name[:140]

	existing = frappe.db.get_value("Customer", {"customer_name": name}, "name")
	if existing:
		return existing
	if dry_run:
		return name  # preview only

	cust = frappe.new_doc("Customer")
	cust.customer_name = name
	cust.customer_type = "Individual"
	group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	if group:
		cust.customer_group = group
	territory = _resolve_territory(payload.get("area")) or frappe.db.get_value(
		"Territory", {"is_group": 0}, "name"
	)
	if territory:
		cust.territory = territory
	cust.flags.ignore_permissions = True
	cust.flags.ignore_mandatory = True
	cust.insert(ignore_permissions=True)
	return cust.name


def _bill_already_imported(bill_number):
	return bool(
		frappe.db.exists("Sales Invoice", {"gofrugal_bill_number": bill_number})
	)


def _create_sales_invoice(payload, company, update_stock, warehouse_map, naming_series=None):
	from frappe.utils import nowdate

	# All items must resolve to real ERPNext Items — no auto-create.
	missing = []
	resolved = []
	for it in payload["items"]:
		code = it.get("item_code")
		if not code:
			missing.append("<blank>")
			continue
		if not frappe.db.exists("Item", code):
			missing.append(code)
			continue
		resolved.append(it)
	if missing:
		raise ValueError(f"unknown Item(s): {', '.join(sorted(set(missing)))}")
	if not resolved:
		raise ValueError("no resolvable items")

	customer = _get_or_create_customer(payload, dry_run=False)
	warehouse = _resolve_warehouse(payload["outlet"], warehouse_map) if update_stock else None
	if update_stock and not warehouse:
		raise ValueError(
			f"update_stock=1 but no warehouse mapped for outlet '{payload['outlet']}'"
		)

	doc = frappe.new_doc("Sales Invoice")
	doc.customer = customer
	if company:
		doc.company = company
	# Naming series is left to the site default unless explicitly pinned. The
	# live adilqadri default is a GST series; under india_compliance the SI name
	# must be <= 16 chars, so finance may pass a compliant series here.
	if naming_series:
		doc.naming_series = naming_series
	doc.posting_date = payload.get("posting_date") or nowdate()
	doc.set_posting_time = 1
	doc.due_date = doc.posting_date
	doc.currency = DEFAULT_CURRENCY
	doc.update_stock = 1 if update_stock else 0
	if payload.get("additional_discount_percentage"):
		doc.additional_discount_percentage = payload["additional_discount_percentage"]
	if payload.get("remarks"):
		doc.remarks = payload["remarks"]

	territory = _resolve_territory(payload.get("area"))
	if territory:
		doc.territory = territory

	doc.set("gofrugal_bill_number", payload["bill_number"])
	doc.set("gofrugal_outlet", payload.get("outlet"))

	for it in resolved:
		line = {
			"item_code": it["item_code"],
			"qty": it["qty"],
			"rate": it["rate"],
			"uom": DEFAULT_UOM,
		}
		if it.get("price_list_rate"):
			line["price_list_rate"] = it["price_list_rate"]
		if it.get("discount_percentage"):
			line["discount_percentage"] = it["discount_percentage"]
		if warehouse:
			line["warehouse"] = warehouse
		doc.append("items", line)

	doc.flags.ignore_permissions = True
	doc.set_missing_values()
	doc.insert(ignore_permissions=True)  # DRAFT — finance submits later
	return doc.name


@frappe.whitelist()
def import_sales_invoices(
	file_url=None, file_path=None, dry_run=1, update_stock=0, warehouse_map=None, naming_series=None
):
	"""Import GoFrugal sales into Draft Sales Invoices.

	Args:
		file_url:   a Frappe File URL (e.g. /private/files/x.xls) — preferred.
		file_path:  absolute path on the server (alternative to file_url).
		dry_run:    1 (default) previews and writes nothing; 0 creates drafts.
		update_stock: 0 (default) no stock movement; 1 requires a mapped warehouse.
		warehouse_map: optional {outlet: warehouse} override; defaults to the
		               verified {'Head Office': 'Stores - AEPL'}.
		naming_series: optional Sales Invoice naming series to pin (e.g. the live
		               GST series). Leave blank to use the site default.
	"""
	import json as _json

	dry_run = bool(int(dry_run))
	update_stock = bool(int(update_stock))
	if isinstance(warehouse_map, str):
		warehouse_map = _json.loads(warehouse_map) if warehouse_map.strip() else None
	warehouse_map = warehouse_map or DEFAULT_WAREHOUSE_MAP

	path = file_path
	if not path and file_url:
		path = frappe.get_doc("File", {"file_url": file_url}).get_full_path()
	if not path:
		return {"ok": False, "error": "provide file_url or file_path"}

	meta, payloads = parse_file(path)
	company = _default_company()

	result = {
		"ok": True,
		"dry_run": dry_run,
		"company": company,
		"posting_date": meta["posting_date"],
		"total_invoices": len(payloads),
		"created": 0,
		"skipped_exists": 0,
		"errors": [],
		"sample": [],
	}

	for payload in payloads:
		bill = payload["bill_number"]
		if _bill_already_imported(bill):
			result["skipped_exists"] += 1
			continue

		if dry_run:
			result["sample"].append(
				{
					"bill_number": bill,
					"customer": payload["customer"] or payload["customer_name"],
					"item_count": len([i for i in payload["items"] if i.get("item_code")]),
					"total": round(sum(i.get("qty", 0) * i.get("rate", 0) for i in payload["items"]), 2),
				}
			)
			continue

		try:
			si = _create_sales_invoice(payload, company, update_stock, warehouse_map, naming_series)
			result["created"] += 1
			result["sample"].append({"bill_number": bill, "sales_invoice": si})
		except Exception as e:
			frappe.log_error(
				title="GoFrugal invoice import — create failed",
				message=f"bill {bill}: {type(e).__name__}: {e}",
			)
			result["errors"].append({"bill_number": bill, "error": f"{type(e).__name__}: {e}"})

	if not dry_run:
		frappe.db.commit()

	return result
