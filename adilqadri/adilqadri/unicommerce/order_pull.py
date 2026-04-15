"""
Pull Sales Orders from Uniware (Unicommerce) into ERPNext.

Flow:
1. Search recent orders via POST /services/rest/v1/oms/saleOrder/search
   using updatedSinceInMinutes for incremental pulls.
2. For each summary, skip if already synced (uniware_order_code match).
3. Fetch full order via POST /services/rest/v1/oms/saleorder/get.
4. Auto-create Sales Channel record if this channel is new.
5. Resolve each line item via Channel Item Code child table (FRD §8.2)
   with fallbacks to sellerSkuCode and then direct ERPNext Item match.
6. Auto-create a per-channel Customer if missing.
7. Create ERPNext Sales Order with Uniware metadata in custom fields.

Dry-run mode (default) returns a preview without writing anything,
so you can verify resolution before flipping to live.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, now_datetime, nowdate

from adilqadri.adilqadri.unicommerce.client import UniwareAPIError, UniwareClient

UNIWARE_ORDER_CODE_FIELD = "uniware_order_code"
UNIWARE_DISPLAY_CODE_FIELD = "uniware_display_order_code"
UNIWARE_CHANNEL_FIELD = "uniware_channel"
UNIWARE_FACILITY_FIELD = "uniware_facility_code"
UNIWARE_STATUS_FIELD = "uniware_status"
UNIWARE_SYNCED_AT_FIELD = "uniware_last_synced_at"
UNIWARE_LINE_CODE_FIELD = "uniware_item_code"
UNIWARE_LINE_SKU_FIELD = "uniware_item_sku"


@frappe.whitelist()
def pull_orders(updated_since_minutes=60, limit=10, dry_run=1):
	"""
	Pull recent sale orders from Uniware.

	Args:
	    updated_since_minutes: Only fetch orders updated in the last N minutes.
	    limit: Max number of orders to process in this run.
	    dry_run: 1 = preview only, 0 = actually create Sales Orders.

	Returns:
	    dict summary with counts and a sample preview.
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
		"errors": [],
		"sample": [],
	}

	for summary in elements:
		order_code = summary.get("code")
		display_code = summary.get("displayOrderCode")

		if _order_already_synced(order_code):
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
			prepared = _prepare_sales_order(dto, default_company)
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
						}
						for r in prepared["items"][:5]
					],
				}
			)
		else:
			try:
				so_name = _create_sales_order(dto, prepared)
				result["created"] += 1
				result["sample"].append({"uniware_code": order_code, "sales_order": so_name})
			except Exception as e:
				frappe.log_error(
					title="Uniware order pull — create failed",
					message=f"{order_code}: {type(e).__name__}: {e}\n\nDTO code: {dto.get('code')}",
				)
				result["errors"].append(
					{"code": order_code, "stage": "create", "error": f"{type(e).__name__}: {e}"}
				)

	if not dry_run:
		frappe.db.commit()

	return result


def _default_company():
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		rows = frappe.get_all("Company", limit=1, pluck="name")
		company = rows[0] if rows else None
	return company


def _order_already_synced(order_code):
	if not order_code:
		return False
	return bool(frappe.db.exists("Sales Order", {UNIWARE_ORDER_CODE_FIELD: order_code}))


def _prepare_sales_order(dto, default_company):
	channel_str = dto.get("channel") or ""
	sales_channel = _ensure_sales_channel(channel_str)
	customer = _ensure_customer(sales_channel)
	items = _resolve_items(sales_channel, dto.get("saleOrderItems") or [])
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


def _ensure_sales_channel(uniware_channel):
	"""
	If a Sales Channel record doesn't exist with channel_code == uniware_channel,
	create one so future orders through this channel are mapped automatically.
	"""
	if not uniware_channel:
		return None
	if frappe.db.exists("Sales Channel", uniware_channel):
		return uniware_channel
	doc = frappe.new_doc("Sales Channel")
	doc.channel_code = uniware_channel
	doc.channel_name = uniware_channel.replace("_", " ").title()
	doc.platform = "Other"
	doc.is_active = 1
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return uniware_channel


def _ensure_customer(channel):
	"""
	MVP: one generic Customer per channel. Named "Uniware - <CHANNEL>".
	Switch to per-buyer customers in a later iteration if needed.
	"""
	name = f"Uniware - {channel or 'Unknown'}"
	if frappe.db.exists("Customer", name):
		return name

	default_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	default_territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")

	doc = frappe.new_doc("Customer")
	doc.customer_name = name
	if default_group:
		doc.customer_group = default_group
	if default_territory:
		doc.territory = default_territory
	doc.customer_type = "Company"
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	return name


def _resolve_items(sales_channel, uniware_items):
	"""
	Resolution chain for each Uniware line:
	  1. Channel Item Code (channel, channelProductId)
	  2. Channel Item Code (channel, sellerSkuCode)
	  3. ERPNext Item where item_code == Uniware itemSku
	Raises ValueError with enough context to debug if nothing matches.
	"""
	resolved = []
	for li in uniware_items:
		channel_product_id = li.get("channelProductId")
		seller_sku = li.get("sellerSkuCode")
		item_sku = li.get("itemSku")

		item_code = (
			_lookup_channel_item_code(sales_channel, channel_product_id)
			or _lookup_channel_item_code(sales_channel, seller_sku)
			or _lookup_by_item_code(item_sku)
		)
		if not item_code:
			raise ValueError(
				f"unresolved item: channel={sales_channel}, "
				f"channelProductId={channel_product_id}, sellerSkuCode={seller_sku}, "
				f"itemSku={item_sku}"
			)

		rate = li.get("sellingPrice") or li.get("totalPrice") or 0
		resolved.append(
			{
				"item_code": item_code,
				"qty": 1,  # Uniware splits qty into separate line items
				"rate": rate,
				"uniware_item_code": li.get("code"),
				"uniware_item_sku": item_sku,
			}
		)
	return resolved


def _lookup_channel_item_code(channel, product_code):
	if not channel or not product_code:
		return None
	return frappe.db.get_value(
		"Channel Item Code",
		{
			"parenttype": "Item",
			"parentfield": "channel_item_codes",
			"channel": channel,
			"channel_product_code": product_code,
			"is_active": 1,
		},
		"parent",
	)


def _lookup_by_item_code(item_sku):
	if not item_sku:
		return None
	if frappe.db.exists("Item", item_sku):
		return item_sku
	return None


def _create_sales_order(dto, prepared):
	doc = frappe.new_doc("Sales Order")
	doc.customer = prepared["customer"]
	if prepared["company"]:
		doc.company = prepared["company"]
	doc.transaction_date = nowdate()
	doc.delivery_date = add_days(nowdate(), 7)
	doc.currency = dto.get("currencyCode") or "INR"

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
	doc.insert(ignore_permissions=True)
	return doc.name
