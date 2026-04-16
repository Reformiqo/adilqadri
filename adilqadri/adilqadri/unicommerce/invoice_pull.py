"""
Pull Sales Invoices from Uniware (Unicommerce) into ERPNext.

Unlike order_pull.py (which creates Sales Orders from any recent order),
this module creates **Sales Invoices** — the financial/accounting document.

Flow:
1. Search recent orders via saleOrder/search (same API as order pull).
2. Skip orders already synced (by uniware_order_code on Sales Invoice).
3. Fetch full order details via saleOrder/get.
4. Resolve items via the Channel Item Code child table (same 5-level chain
   as order_pull, with optional auto-create).
5. Create ERPNext Sales Invoice with Uniware metadata in custom fields.

The key difference from order_pull: this creates a Sales Invoice (which
posts to the general ledger) rather than a Sales Order (which is just a
commitment). For already-fulfilled Uniware orders, Sales Invoices are
the right document type.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime, nowdate

from adilqadri.adilqadri.unicommerce.client import UniwareAPIError, UniwareClient
from adilqadri.adilqadri.unicommerce.order_pull import (
	_default_company,
	_ensure_customer,
	_ensure_sales_channel,
	_order_already_synced as _so_already_synced,
	_resolve_items,
)

# Reuse the same custom field names from order_pull for consistency.
# These custom fields must also be added to Sales Invoice (via fixture).
UNIWARE_ORDER_CODE_FIELD = "uniware_order_code"
UNIWARE_DISPLAY_CODE_FIELD = "uniware_display_order_code"
UNIWARE_CHANNEL_FIELD = "uniware_channel"
UNIWARE_FACILITY_FIELD = "uniware_facility_code"
UNIWARE_STATUS_FIELD = "uniware_status"
UNIWARE_SYNCED_AT_FIELD = "uniware_last_synced_at"
UNIWARE_LINE_CODE_FIELD = "uniware_item_code"
UNIWARE_LINE_SKU_FIELD = "uniware_item_sku"


@frappe.whitelist()
def pull_invoices(updated_since_minutes=60, limit=10, dry_run=1, auto_create_items=0):
	"""
	Pull recent sale orders from Uniware and create ERPNext Sales Invoices.

	Args:
	    updated_since_minutes: Only fetch orders updated in the last N minutes.
	    limit: Max number of orders to process in this run.
	    dry_run: 1 = preview only, 0 = actually create Sales Invoices.
	    auto_create_items: 1 = create ERPNext Items for unresolved SKUs.

	Returns:
	    dict summary with counts and a sample preview.
	"""
	updated_since_minutes = int(updated_since_minutes)
	limit = int(limit)
	dry_run = bool(int(dry_run))
	auto_create_items = bool(int(auto_create_items))

	client = UniwareClient()
	default_company = _default_company()

	search_body = {
		"updatedSinceInMinutes": updated_since_minutes,
		"searchOptions": {
			"displayStart": 0,
			"displayLength": limit,
			"getCount": True,
		},
	}
	try:
		search_data = client.post("/services/rest/v1/oms/saleOrder/search", json=search_body)
	except UniwareAPIError as e:
		return {"ok": False, "error": f"search failed: {e}"}

	elements = search_data.get("elements") or []
	total_matching = search_data.get("totalRecords", len(elements))

	result = {
		"ok": True,
		"dry_run": dry_run,
		"auto_create_items": auto_create_items,
		"total_matching": total_matching,
		"fetched": len(elements),
		"created": 0,
		"skipped_exists": 0,
		"items_created": 0,
		"mappings_created": 0,
		"errors": [],
		"sample": [],
	}

	resolve_stats = {"items_created": 0, "mappings_created": 0}

	for summary in elements:
		order_code = summary.get("code")
		display_code = summary.get("displayOrderCode")

		# Skip if already synced as Sales Invoice
		if _invoice_already_synced(order_code):
			result["skipped_exists"] += 1
			continue

		try:
			detail = client.post("/services/rest/v1/oms/saleorder/get", json={"code": order_code})
		except UniwareAPIError as e:
			result["errors"].append({"code": order_code, "stage": "fetch", "error": str(e)})
			continue

		dto = detail.get("saleOrderDTO") or detail.get("saleOrder") or {}
		if not dto:
			result["errors"].append({"code": order_code, "stage": "parse", "error": "no saleOrderDTO"})
			continue

		try:
			prepared = _prepare_invoice(
				dto, default_company, auto_create_items, resolve_stats, dry_run=dry_run
			)
		except Exception as e:
			result["errors"].append(
				{"code": order_code, "stage": "prepare", "error": f"{type(e).__name__}: {e}"}
			)
			continue

		if dry_run:
			result["sample"].append(
				{
					"uniware_code": order_code,
					"display_code": display_code,
					"channel": prepared["channel"],
					"facility": prepared["facility_code"],
					"customer": prepared["customer"],
					"item_count": len(prepared["items"]),
					"total": round(
						sum(row["qty"] * row["rate"] for row in prepared["items"]), 2
					),
					"items": [
						{
							"item_code": r["item_code"],
							"qty": r["qty"],
							"rate": r["rate"],
							"uniware_sku": r["uniware_item_sku"],
							"resolved_via": r.get("resolved_via"),
						}
						for r in prepared["items"][:5]
					],
				}
			)
		else:
			try:
				si_name = _create_sales_invoice(dto, prepared)
				result["created"] += 1
				result["sample"].append({"uniware_code": order_code, "sales_invoice": si_name})
			except Exception as e:
				frappe.log_error(
					title="Uniware invoice pull — create failed",
					message=f"{order_code}: {type(e).__name__}: {e}",
				)
				result["errors"].append(
					{"code": order_code, "stage": "create", "error": f"{type(e).__name__}: {e}"}
				)

	result["items_created"] = resolve_stats["items_created"]
	result["mappings_created"] = resolve_stats["mappings_created"]

	if not dry_run:
		frappe.db.commit()

	return result


def _invoice_already_synced(order_code):
	if not order_code:
		return False
	return bool(frappe.db.exists("Sales Invoice", {UNIWARE_ORDER_CODE_FIELD: order_code}))


def _prepare_invoice(dto, default_company, auto_create_items, resolve_stats, dry_run=False):
	channel_str = dto.get("channel") or ""
	sales_channel = _ensure_sales_channel(channel_str, dry_run=dry_run)
	customer = _ensure_customer(sales_channel, dry_run=dry_run)
	items = _resolve_items(
		sales_channel,
		dto.get("saleOrderItems") or [],
		auto_create_items=auto_create_items,
		resolve_stats=resolve_stats,
		dry_run=dry_run,
	)
	if not items:
		raise ValueError(f"no resolvable items on order {dto.get('code')}")

	facility_code = None
	for li in dto.get("saleOrderItems") or []:
		if li.get("facilityCode"):
			facility_code = li["facilityCode"]
			break

	return {
		"company": default_company,
		"customer": customer,
		"channel": sales_channel,
		"facility_code": facility_code,
		"items": items,
	}


def _create_sales_invoice(dto, prepared):
	doc = frappe.new_doc("Sales Invoice")
	doc.customer = prepared["customer"]
	if prepared["company"]:
		doc.company = prepared["company"]
	doc.posting_date = nowdate()
	doc.due_date = nowdate()
	doc.currency = dto.get("currencyCode") or "INR"
	doc.update_stock = 0  # don't touch stock from invoice pull

	doc.set(UNIWARE_ORDER_CODE_FIELD, dto.get("code"))
	doc.set(UNIWARE_DISPLAY_CODE_FIELD, dto.get("displayOrderCode"))
	doc.set(UNIWARE_CHANNEL_FIELD, prepared["channel"])
	doc.set(UNIWARE_FACILITY_FIELD, prepared["facility_code"])
	doc.set(UNIWARE_STATUS_FIELD, dto.get("status"))
	doc.set(UNIWARE_SYNCED_AT_FIELD, now_datetime())

	for row in prepared["items"]:
		line = doc.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"rate": row["rate"],
			},
		)
		line.set(UNIWARE_LINE_CODE_FIELD, row.get("uniware_item_code"))
		line.set(UNIWARE_LINE_SKU_FIELD, row.get("uniware_item_sku"))

	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.set_missing_values()
	doc.insert(ignore_permissions=True)
	return doc.name
