"""
Pull Sales Invoices from Uniware (Unicommerce) into ERPNext.

Creates Sales Invoices with REAL customers and addresses extracted from
the Uniware order's billingAddress, not generic per-channel placeholders.

Flow:
1. Search recent orders via saleOrder/search.
2. Skip orders already synced (by uniware_order_code on Sales Invoice).
3. Fetch full order details via saleOrder/get.
4. Create/match a real Customer from billingAddress name + phone/email.
5. Create/match an Address linked to that Customer.
6. Resolve items via Channel Item Code child table (5-level chain).
7. Create ERPNext Sales Invoice with real customer, address, and Uniware
   metadata in custom fields.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime, nowdate

from adilqadri.adilqadri.unicommerce.client import UniwareAPIError, UniwareClient
from adilqadri.adilqadri.unicommerce.order_pull import (
	_default_company,
	_ensure_sales_channel,
	_resolve_items,
)

UNIWARE_ORDER_CODE_FIELD = "uniware_order_code"
UNIWARE_DISPLAY_CODE_FIELD = "uniware_display_order_code"
UNIWARE_CHANNEL_FIELD = "uniware_channel"
UNIWARE_FACILITY_FIELD = "uniware_facility_code"
UNIWARE_STATUS_FIELD = "uniware_status"
UNIWARE_SYNCED_AT_FIELD = "uniware_last_synced_at"
UNIWARE_LINE_CODE_FIELD = "uniware_item_code"
UNIWARE_LINE_SKU_FIELD = "uniware_item_sku"


@frappe.whitelist()
def pull_invoices(updated_since_minutes=60, limit=10, dry_run=1):
	"""Pull recent Uniware orders and create ERPNext Sales Invoices.

	Items MUST be pre-mapped in Channel Item Code. Unmapped items cause the
	order to be skipped with an error — NO auto-creation of ERPNext Items.
	"""
	updated_since_minutes = int(updated_since_minutes)
	limit = int(limit)
	dry_run = bool(int(dry_run))

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
		"total_matching": total_matching,
		"fetched": len(elements),
		"created": 0,
		"skipped_exists": 0,
		"mappings_created": 0,
		"errors": [],
		"sample": [],
	}

	resolve_stats = {"items_created": 0, "mappings_created": 0}

	for summary in elements:
		order_code = summary.get("code")
		display_code = summary.get("displayOrderCode")

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
				dto, default_company, resolve_stats, dry_run=dry_run
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
					"customer": prepared["customer"],
					"item_count": len(prepared["items"]),
					"total": round(
						sum(row["qty"] * row["rate"] for row in prepared["items"]), 2
					),
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

	result["mappings_created"] = resolve_stats["mappings_created"]

	if not dry_run:
		frappe.db.commit()

	return result


def _invoice_already_synced(order_code):
	if not order_code:
		return False
	return bool(frappe.db.exists("Sales Invoice", {UNIWARE_ORDER_CODE_FIELD: order_code}))


def _prepare_invoice(dto, default_company, resolve_stats, dry_run=False):
	channel_str = dto.get("channel") or ""
	sales_channel = _ensure_sales_channel(channel_str, dry_run=dry_run)
	customer_name, customer_address = _ensure_customer_and_address(dto, dry_run=dry_run)
	items = _resolve_items(
		sales_channel,
		dto.get("saleOrderItems") or [],
		auto_create_items=False,
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
		"customer": customer_name,
		"customer_address": customer_address,
		"channel": sales_channel,
		"facility_code": facility_code,
		"items": items,
	}


def _ensure_customer_and_address(dto, dry_run=False):
	"""
	Create or find a Customer + Address from the Uniware order's billing info.

	Deduplication: by (customer_name, phone). If a Customer with the same
	normalized name + phone exists, reuse it.
	"""
	billing = dto.get("billingAddress") or {}
	raw_name = (billing.get("name") or "").strip()
	phone = (billing.get("phone") or dto.get("notificationMobile") or "").strip()
	email = (billing.get("email") or dto.get("notificationEmail") or "").strip()
	channel = dto.get("channel") or "Unknown"
	display_code = dto.get("displayOrderCode") or ""

	# Flipkart and some marketplaces mask customer PII — the name comes as
	# "***" or similar. Detect and fall back to a useful identifier.
	import re

	is_masked = not raw_name or bool(re.fullmatch(r"[\*\.\-_\s]+", raw_name))
	if is_masked:
		# Use channel + display order code for a meaningful customer name
		customer_name = f"{channel} - {display_code}" if display_code else f"Uniware Customer - {channel}"
	else:
		customer_name = raw_name
	customer_name = customer_name[:140]

	if dry_run:
		return customer_name, None

	# Only try dedup when we have a REAL (non-masked) customer name.
	# Marketplace orders with masked PII should each get their own
	# Customer record — there's no reliable data to dedup on.
	existing = None
	if not is_masked:
		existing = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
		if not existing and phone and len(phone) >= 8:
			contact = frappe.db.get_value(
				"Contact Phone",
				{"phone": phone, "parenttype": "Contact"},
				"parent",
			)
			if contact:
				link = frappe.db.get_value(
					"Dynamic Link",
					{"parent": contact, "link_doctype": "Customer"},
					"link_name",
				)
				if link:
					existing = link

	if existing:
		addr = frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Customer", "link_name": existing, "parenttype": "Address"},
			"parent",
		)
		return existing, addr

	# Create new Customer
	default_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	default_territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")

	cust = frappe.new_doc("Customer")
	cust.customer_name = customer_name
	cust.customer_type = "Individual"
	if default_group:
		cust.customer_group = default_group
	if default_territory:
		cust.territory = default_territory
	cust.flags.ignore_permissions = True
	cust.flags.ignore_mandatory = True
	cust.insert(ignore_permissions=True)

	# Create Address if we have data
	addr_name = None
	if billing.get("addressLine1") or billing.get("city"):
		addr = frappe.new_doc("Address")
		addr.address_title = customer_name[:100]
		addr.address_type = "Billing"
		addr.address_line1 = (billing.get("addressLine1") or "-")[:140]
		addr.address_line2 = (billing.get("addressLine2") or "")[:140] or None
		addr.city = (billing.get("city") or "-")[:140]
		addr.state = _resolve_state(billing.get("state"))
		addr.pincode = (billing.get("pincode") or "")[:10] or None
		addr.country = _resolve_country(billing.get("country"))
		addr.phone = phone or None
		addr.email_id = email or None
		addr.append("links", {"link_doctype": "Customer", "link_name": cust.name})
		addr.flags.ignore_permissions = True
		addr.flags.ignore_mandatory = True
		addr.insert(ignore_permissions=True)
		addr_name = addr.name

	# Create Contact if we have phone or email
	if phone or email:
		contact = frappe.new_doc("Contact")
		parts = customer_name.split(" ", 1)
		contact.first_name = parts[0][:140]
		if len(parts) > 1:
			contact.last_name = parts[1][:140]
		if phone:
			contact.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
		if email:
			contact.append("email_ids", {"email_id": email, "is_primary": 1})
		contact.append("links", {"link_doctype": "Customer", "link_name": cust.name})
		contact.flags.ignore_permissions = True
		contact.flags.ignore_mandatory = True
		contact.insert(ignore_permissions=True)

	return cust.name, addr_name


INDIAN_STATE_MAP = {
	"AN": "Andaman and Nicobar Islands", "AP": "Andhra Pradesh",
	"AR": "Arunachal Pradesh", "AS": "Assam", "BR": "Bihar",
	"CH": "Chandigarh", "CT": "Chhattisgarh", "CG": "Chhattisgarh",
	"DD": "Daman and Diu", "DL": "Delhi", "GA": "Goa",
	"GJ": "Gujarat", "HP": "Himachal Pradesh", "HR": "Haryana",
	"JH": "Jharkhand", "JK": "Jammu and Kashmir", "KA": "Karnataka",
	"KL": "Kerala", "LA": "Ladakh", "LD": "Lakshadweep",
	"MH": "Maharashtra", "ML": "Meghalaya", "MN": "Manipur",
	"MP": "Madhya Pradesh", "MZ": "Mizoram", "NL": "Nagaland",
	"OD": "Odisha", "OR": "Odisha", "PB": "Punjab",
	"PY": "Puducherry", "RJ": "Rajasthan", "SK": "Sikkim",
	"TN": "Tamil Nadu", "TS": "Telangana", "TG": "Telangana",
	"TR": "Tripura", "UK": "Uttarakhand", "UT": "Uttarakhand",
	"UP": "Uttar Pradesh", "WB": "West Bengal", "DN": "Dadra and Nagar Haveli",
}


def _resolve_state(code):
	"""Convert 2-letter Indian state code (GJ, MH, etc.) to the full name
	that india_compliance expects in the state field."""
	if not code:
		return None
	code = code.strip().upper()
	return INDIAN_STATE_MAP.get(code, code)


def _resolve_country(code):
	"""Convert 2-letter country code (e.g. 'IN') to ERPNext country name."""
	if not code:
		return "India"
	name = frappe.db.get_value("Country", {"code": code.lower()}, "name")
	return name or "India"


def _create_sales_invoice(dto, prepared):
	doc = frappe.new_doc("Sales Invoice")
	doc.customer = prepared["customer"]
	if prepared.get("customer_address"):
		doc.customer_address = prepared["customer_address"]
	if prepared["company"]:
		doc.company = prepared["company"]
	doc.posting_date = nowdate()
	doc.due_date = nowdate()
	doc.currency = dto.get("currencyCode") or "INR"
	doc.update_stock = 0

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
