"""
Push ERPNext Item master data to Uniware for each active channel mapping.

Flow per row in Item.channel_item_codes:
  1. Skip if is_active == 0 (row toggled off)
  2. Check whether the SKU already exists in Uniware:
       POST /services/rest/v1/catalog/itemType/get  body={"skuCode": <code>}
  3. Build an itemType payload from the ERPNext Item's fields
  4. If it exists → POST /services/rest/v1/catalog/itemType/edit
     If it doesn't → POST /services/rest/v1/catalog/itemType/createOrEdit
  5. Stamp the row's sync_status, last_sync_at, last_sync_error based on result

The row's sync_status is what the client asked for: a per (item, channel)
indicator of whether the item was successfully pushed.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime, flt

from adilqadri.adilqadri.unicommerce.client import UniwareAPIError, UniwareClient


GET_ENDPOINT = "/services/rest/v1/catalog/itemType/get"
CREATE_ENDPOINT = "/services/rest/v1/catalog/itemType/createOrEdit"
EDIT_ENDPOINT = "/services/rest/v1/catalog/itemType/edit"


@frappe.whitelist()
def sync_item_to_channels(item_code: str) -> dict:
	"""
	Push an ERPNext Item to Uniware for every active Channel Item Code row.

	Returns a summary dict with per-row results so the UI can render a clear
	confirmation dialog.
	"""
	item = frappe.get_doc("Item", item_code)

	if item.has_variants:
		return {
			"ok": False,
			"error": (
				f"Item '{item_code}' is a Template with variants. "
				"Push the variants individually, not the template."
			),
		}

	rows = item.get("channel_item_codes") or []
	if not rows:
		return {"ok": False, "error": f"Item '{item_code}' has no channel mappings."}

	client = UniwareClient()
	results = []
	any_changed = False

	for row in rows:
		if not row.is_active:
			row.sync_status = "Skipped"
			row.last_sync_at = now_datetime()
			row.last_sync_error = "Sync disabled on this row"
			any_changed = True
			results.append(
				{
					"channel": row.channel,
					"channel_product_code": row.channel_product_code,
					"status": "Skipped",
					"error": "is_active=0",
				}
			)
			continue

		status, error = _push_one_row(client, item, row)
		row.sync_status = status
		row.last_sync_at = now_datetime()
		row.last_sync_error = error
		any_changed = True
		results.append(
			{
				"channel": row.channel,
				"channel_product_code": row.channel_product_code,
				"status": status,
				"error": error,
			}
		)

	if any_changed:
		item.flags.ignore_permissions = True
		item.flags.ignore_mandatory = True
		item.save(ignore_permissions=True)
		frappe.db.commit()

	synced = sum(1 for r in results if r["status"] == "Synced")
	failed = sum(1 for r in results if r["status"] == "Failed")
	skipped = sum(1 for r in results if r["status"] == "Skipped")

	return {
		"ok": failed == 0,
		"item_code": item_code,
		"total": len(results),
		"synced": synced,
		"failed": failed,
		"skipped": skipped,
		"rows": results,
	}


def _push_one_row(client: UniwareClient, item, row) -> tuple[str, str | None]:
	"""Push a single channel mapping. Returns (status, error_message)."""
	sku = (row.channel_product_code or "").strip()
	if not sku:
		return "Failed", "Missing channel_product_code"

	payload = _build_item_type_payload(item, row, sku)

	try:
		existing = client.post(GET_ENDPOINT, json={"skuCode": sku})
	except UniwareAPIError as e:
		# On get failure, assume not found and try create
		existing = None
		get_error = str(e)
	else:
		get_error = None

	exists = bool(
		existing and existing.get("successful") and existing.get("itemTypeDTO")
	)
	endpoint = EDIT_ENDPOINT if exists else CREATE_ENDPOINT

	try:
		result = client.post(endpoint, json={"itemType": payload})
	except UniwareAPIError as e:
		return "Failed", f"{endpoint}: {e}"

	if result.get("successful"):
		return "Synced", None

	errors = result.get("errors") or []
	if errors:
		msg = errors[0].get("message") or errors[0].get("description") or str(errors[0])
		return "Failed", f"Uniware: {msg}"

	return "Failed", f"Uniware returned successful=false on {endpoint}"


def _build_item_type_payload(item, row, sku: str) -> dict:
	"""
	Map ERPNext Item fields to Uniware itemType schema.

	Keep this minimal for safety — we only push fields we clearly own.
	Fields intentionally omitted: category, brand, HSN, tax code, images —
	those live in Uniware already and we don't want to clobber them on the
	first iteration.
	"""
	name = row.channel_item_name or item.item_name or item.name
	payload: dict = {
		"skuCode": sku,
		"name": name[:255],
	}

	if item.description:
		payload["description"] = item.description[:2000]

	if item.weight_per_unit and item.weight_per_unit > 0:
		payload["weight"] = flt(item.weight_per_unit)

	if item.standard_rate and item.standard_rate > 0:
		payload["mrp"] = flt(item.standard_rate)
		payload["sellingPrice"] = flt(item.standard_rate)

	return payload
