"""
Pull Sales Orders from Uniware (Unicommerce) into ERPNext.

Flow:
1. Search recent orders via POST /services/rest/v1/oms/saleOrder/search
   using updatedSinceInMinutes for incremental pulls.
2. For each summary, skip if already synced (uniware_order_code match).
3. Fetch full order via POST /services/rest/v1/oms/saleorder/get.
4. Auto-create Sales Channel record if this channel is new.
5. Resolve each line item via Channel Item Code child table (FRD §8.2)
   with fallbacks: sellerSkuCode → direct ERPNext item_code match →
   normalized-name match against existing ERPNext items → optional
   auto-create of a new ERPNext item with Uniware's data.
6. Auto-create a per-channel Customer if missing.
7. Create ERPNext Sales Order with Uniware metadata in custom fields.

Dry-run mode (default) returns a preview without writing anything,
so you can verify resolution before flipping to live.
"""

from __future__ import annotations

import re

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
def pull_orders(updated_since_minutes=60, limit=10, dry_run=1, auto_create_items=0):
	"""
	Pull recent sale orders from Uniware.

	Args:
	    updated_since_minutes: Only fetch orders updated in the last N minutes.
	    limit: Max number of orders to process in this run.
	    dry_run: 1 = preview only, 0 = actually create Sales Orders.
	    auto_create_items: 1 = if an order line's item can't be matched
	        against an existing ERPNext Item (via channel code, item_code,
	        or normalized name), create a new ERPNext Item using Uniware's
	        itemSku + itemName. 0 = raise an error and skip the order.

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
			prepared = _prepare_sales_order(
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

	result["items_created"] = resolve_stats["items_created"]
	result["mappings_created"] = resolve_stats["mappings_created"]

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


def _prepare_sales_order(dto, default_company, auto_create_items, resolve_stats, dry_run=False):
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


def _ensure_sales_channel(uniware_channel, dry_run=False):
	if not uniware_channel:
		return None
	if frappe.db.exists("Sales Channel", uniware_channel):
		return uniware_channel
	if dry_run:
		return uniware_channel  # pretend it's there
	doc = frappe.new_doc("Sales Channel")
	doc.channel_code = uniware_channel
	doc.channel_name = uniware_channel.replace("_", " ").title()
	doc.platform = "Other"
	doc.is_active = 1
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return uniware_channel


def _ensure_customer(channel, dry_run=False):
	name = f"Uniware - {channel or 'Unknown'}"
	if frappe.db.exists("Customer", name):
		return name
	if dry_run:
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


def _resolve_items(sales_channel, uniware_items, auto_create_items, resolve_stats, dry_run=False):
	"""
	Resolution chain for each Uniware line:
	  1. Channel Item Code (channel, channelProductId)
	  2. Channel Item Code (channel, sellerSkuCode)
	  3. ERPNext Item where item_code == Uniware itemSku
	  4. ERPNext Item matched by normalized item_name against Uniware itemName
	     → auto-create Channel Item Code mapping on that Item
	  5. If auto_create_items: create new ERPNext Item + mapping
	"""
	resolved = []
	for li in uniware_items:
		channel_product_id = li.get("channelProductId")
		seller_sku = li.get("sellerSkuCode")
		item_sku = li.get("itemSku")
		item_name = li.get("itemName") or ""

		# Chain of resolvers
		item_code, resolved_via = _try_all_resolvers(
			sales_channel=sales_channel,
			channel_product_id=channel_product_id,
			seller_sku=seller_sku,
			item_sku=item_sku,
			item_name=item_name,
			auto_create=auto_create_items,
			resolve_stats=resolve_stats,
			dry_run=dry_run,
		)

		if not item_code:
			raise ValueError(
				f"unresolved item: channel={sales_channel}, "
				f"channelProductId={channel_product_id}, sellerSkuCode={seller_sku}, "
				f"itemSku={item_sku}, itemName={item_name!r}"
			)

		# Guard: templates can't be used on Sales Order lines.
		if frappe.db.get_value("Item", item_code, "has_variants"):
			raise ValueError(
				f"mapped item '{item_code}' is a Template with variants; point the "
				f"Channel Item Code mapping at a concrete variant instead. "
				f"(channel={sales_channel}, code={channel_product_id or seller_sku})"
			)

		rate = li.get("sellingPrice") or li.get("totalPrice") or 0
		resolved.append(
			{
				"item_code": item_code,
				"qty": 1,  # Uniware splits qty into separate line items
				"rate": rate,
				"uniware_item_code": li.get("code"),
				"uniware_item_sku": item_sku,
				"resolved_via": resolved_via,
			}
		)
	return resolved


def _try_all_resolvers(
	sales_channel,
	channel_product_id,
	seller_sku,
	item_sku,
	item_name,
	auto_create,
	resolve_stats,
	dry_run=False,
):
	"""Return (item_code, resolved_via) or (None, None)."""

	# 1. Channel Item Code by channelProductId
	code = _lookup_channel_item_code(sales_channel, channel_product_id)
	if code:
		return code, "channel_item_code:channelProductId"

	# 2. Channel Item Code by sellerSkuCode
	code = _lookup_channel_item_code(sales_channel, seller_sku)
	if code:
		return code, "channel_item_code:sellerSkuCode"

	# 3. Direct item_code match on Uniware itemSku
	code = _lookup_by_item_code(item_sku)
	if code:
		return code, "item_code_exact"

	# 4. Name-match against existing ERPNext items, then auto-add mapping
	if item_name:
		matched = _lookup_by_normalized_name(item_name)
		if matched:
			if not dry_run:
				_add_channel_item_code_row(
					matched,
					sales_channel,
					channel_product_id or seller_sku,
					item_name,
				)
			resolve_stats["mappings_created"] += 1
			return matched, "name_match"

	# 5. Last resort: create a brand-new ERPNext Item from Uniware data
	if auto_create and item_sku:
		if dry_run:
			# Would-create path: don't touch DB, just report
			resolve_stats["items_created"] += 1
			resolve_stats["mappings_created"] += 1
			return item_sku, "would_auto_create"
		new_code = _auto_create_item(item_sku, item_name)
		if new_code:
			_add_channel_item_code_row(
				new_code,
				sales_channel,
				channel_product_id or seller_sku or item_sku,
				item_name,
			)
			resolve_stats["items_created"] += 1
			resolve_stats["mappings_created"] += 1
			return new_code, "auto_created"

	return None, None


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


_NORMALIZE_NAME_CACHE: dict[str, str] = {}


def _normalize_name(name: str) -> str:
	if not name:
		return ""
	s = name.lower()
	# Replace non-word characters with space
	s = re.sub(r"[^\w\s]", " ", s)
	# Collapse whitespace
	s = re.sub(r"\s+", " ", s).strip()
	return s


def _build_name_index() -> dict[str, str]:
	"""Build a normalized-name → item_code index of current ERPNext items."""
	index: dict[str, str] = {}
	rows = frappe.get_all(
		"Item",
		filters={"disabled": 0, "has_variants": 0, "is_sales_item": 1},
		fields=["name", "item_name"],
	)
	for r in rows:
		key = _normalize_name(r.item_name or r.name)
		if key and key not in index:
			index[key] = r.name
	return index


def _lookup_by_normalized_name(uniware_name: str) -> str | None:
	key = _normalize_name(uniware_name)
	if not key:
		return None
	# Per-request cache of the index since the pull may process many orders
	request_cache = getattr(frappe.local, "adilqadri_name_index", None)
	if request_cache is None:
		request_cache = _build_name_index()
		frappe.local.adilqadri_name_index = request_cache
	return request_cache.get(key)


def _add_channel_item_code_row(item_code, channel, channel_product_code, channel_item_name):
	"""Append a Channel Item Code row to an existing Item if not already present.

	FRD V-01: at most one row per channel per item. So if the channel already
	has a row (with ANY product code), don't add a duplicate — that's a sign
	the name match resolved to the wrong item, or this is a different SKU
	for the same product.
	"""
	if not item_code or not channel or not channel_product_code:
		return
	item = frappe.get_doc("Item", item_code)
	for row in item.get("channel_item_codes") or []:
		if row.channel == channel:
			# Channel already mapped — V-01 forbids duplicates. Skip.
			return
	item.append(
		"channel_item_codes",
		{
			"channel": channel,
			"channel_product_code": channel_product_code,
			"channel_item_name": (channel_item_name or "")[:140] or None,
			"is_active": 1,
			"sync_status": "Synced",
			"last_sync_at": now_datetime(),
			"remarks": "Auto-mapped from Uniware order pull",
		},
	)
	item.flags.ignore_permissions = True
	item.flags.ignore_mandatory = True
	item.save(ignore_permissions=True)
	# Invalidate the request-level name index
	if hasattr(frappe.local, "adilqadri_name_index"):
		delattr(frappe.local, "adilqadri_name_index")


def _auto_create_item(uniware_sku, uniware_name):
	"""
	Create a new ERPNext Item using Uniware data. Uses the Uniware SKU
	as the ERPNext item_code for stability (guaranteed unique, matches
	what Uniware will send on future orders).

	Reads the default item group and HSN code from Uniware Connector
	Settings so the creator can override them without code changes.
	"""
	if not uniware_sku:
		return None
	if frappe.db.exists("Item", uniware_sku):
		return uniware_sku

	settings = frappe.get_single("Uniware Connector Settings")
	default_group = (
		settings.default_item_group
		or frappe.db.get_value("Item Group", {"is_group": 0, "name": "Products"}, "name")
		or frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		or "All Item Groups"
	)
	# Fallback HSN: 33030090 = Perfumes and toilet waters. Correct for
	# Adilqadri's catalog. Override via Uniware Connector Settings if a
	# different HSN fits your items.
	default_hsn = (settings.default_hsn_code or "33030090").strip()

	doc = frappe.new_doc("Item")
	doc.item_code = uniware_sku
	doc.item_name = (uniware_name or uniware_sku)[:140]
	doc.item_group = default_group
	doc.stock_uom = "Nos"
	doc.is_sales_item = 1
	doc.is_stock_item = 1
	doc.include_item_in_manufacturing = 0
	# india_compliance hooks Item.validate to require this field. Ensure it's
	# set regardless of whether the Custom Field appears in doc's metadata.
	doc.set("gst_hsn_code", default_hsn)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	return doc.name


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
