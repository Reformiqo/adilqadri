"""
State-wise CONSOLIDATED Sales Invoice sync from Uniware (AQ1-I32).

Unlike ``invoice_pull.pull_invoices`` (one Sales Invoice per Uniware order, real
customer per order), this consolidates per the I32 FRD:

  * Group eligible orders by (State, Channel).
  * Create ONE Draft Sales Invoice per group.
  * Customer = the Channel name (Amazon / Flipkart / ...), auto-created under
    Customer Group 'Channel Partners'.
  * Every Uniware order in the group contributes its line items.

Business rules implemented (FRD §7):
  BR-01 duplicate prevention via Uniware Sync Log (already-synced order codes)
  BR-02 unresolved item SKU -> skip that line, keep the rest of the group
  BR-03 blank state   -> group 'State_Unknown'
  BR-04 blank channel -> group 'Channel_Unknown'
  BR-05 channel auto-created as Customer (group 'Channel Partners')
  BR-06 zero-qty line -> excluded
  BR-07 CANCELLED order -> excluded from every group
  BR-08 same-day re-sync -> skip the group (configurable; 'append' not yet built)
  BR-10 group with 0 valid items -> no invoice, logged as Skipped

Safety: Draft only (finance submits), ``dry_run=1`` default, no auto-create of
Items (BR-02 skips the line instead), naming series left to the site default
unless pinned. Territory is set only when the mapped state actually exists as an
ERPNext Territory (verified: Maharashtra/Gujarat are NOT Territories on the live
site), otherwise the state is kept only in ``custom_uniware_state``.
"""

from __future__ import annotations

import re
from collections import OrderedDict

import frappe
from frappe.utils import now_datetime, nowdate

from adilqadri.adilqadri.unicommerce.client import UniwareAPIError, UniwareClient
from adilqadri.adilqadri.unicommerce.invoice_pull import INDIAN_STATE_MAP
from adilqadri.adilqadri.unicommerce.order_pull import (
	_default_company,
	_ensure_sales_channel,
	_resolve_items,
)

ELIGIBLE_STATUSES = ("DISPATCHED", "INVOICED")
STATE_UNKNOWN = "State_Unknown"
CHANNEL_UNKNOWN = "Channel_Unknown"
CHANNEL_PARTNERS_GROUP = "Channel Partners"

# Full-name lookup so "maharashtra"/"MH"/"Maharashtra" all normalise to one name.
_STATE_NAMES = {v.lower(): v for v in INDIAN_STATE_MAP.values()}


# ---------------------------------------------------------------------------
# Pure helpers (no DB — unit-testable).
# ---------------------------------------------------------------------------
def normalise_channel(raw) -> str:
	"""Title-case + strip stray separators. Blank -> 'Channel_Unknown' (BR-04)."""
	if not raw or not str(raw).strip():
		return CHANNEL_UNKNOWN
	cleaned = re.sub(r"[_\-]+", " ", str(raw).strip())
	cleaned = re.sub(r"\s+", " ", cleaned)
	return cleaned.title()


def normalise_state(raw) -> str:
	"""Map a Uniware state string to an ERPNext-style state name.

	Handles 2-letter GST codes (MH -> Maharashtra), full names in any case
	(maharashtra -> Maharashtra), and blank -> 'State_Unknown' (BR-03).
	Unknown free-text is title-cased and returned as-is.
	"""
	if not raw or not str(raw).strip():
		return STATE_UNKNOWN
	s = str(raw).strip()
	code = INDIAN_STATE_MAP.get(s.upper())
	if code:
		return code
	full = _STATE_NAMES.get(s.lower())
	if full:
		return full
	return s.title()


def extract_state(dto) -> str:
	"""Ship-to state preferred, billing state as fallback (FRD §3.1)."""
	for key in ("shippingAddress", "billingAddress"):
		addr = dto.get(key) or {}
		if addr.get("state"):
			return normalise_state(addr.get("state"))
	return STATE_UNKNOWN


def group_key(dto):
	return (extract_state(dto), normalise_channel(dto.get("channel")))


def group_orders(dtos):
	"""Group order DTOs by (state, channel). CANCELLED excluded (BR-07).
	Returns OrderedDict[(state, channel)] -> [dto, ...]."""
	groups = OrderedDict()
	for dto in dtos:
		if (dto.get("status") or "").upper() == "CANCELLED":
			continue
		groups.setdefault(group_key(dto), []).append(dto)
	return groups


# ---------------------------------------------------------------------------
# DB helpers.
# ---------------------------------------------------------------------------
def get_synced_order_ids():
	"""All Uniware order codes already pushed, from the Uniware Sync Log (BR-01)."""
	synced = set()
	for row in frappe.get_all("Uniware Sync Log", fields=["order_ids"]):
		for code in (row.get("order_ids") or "").splitlines():
			code = code.strip()
			if code:
				synced.add(code)
	return synced


def _ensure_channel_partner_group():
	"""Customer Group 'Channel Partners', created under the root if missing."""
	if frappe.db.exists("Customer Group", CHANNEL_PARTNERS_GROUP):
		return CHANNEL_PARTNERS_GROUP
	parent = frappe.db.get_value("Customer Group", {"is_group": 1}, "name")
	doc = frappe.new_doc("Customer Group")
	doc.customer_group_name = CHANNEL_PARTNERS_GROUP
	if parent:
		doc.parent_customer_group = parent
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return CHANNEL_PARTNERS_GROUP


def _resolve_territory(state):
	"""The mapped state only if it exists as an ERPNext Territory, else None.
	(Live site has no per-state Territories, so this usually returns None.)"""
	if not state or state == STATE_UNKNOWN:
		return None
	return state if frappe.db.exists("Territory", state) else None


def get_or_create_channel_customer(channel, state, dry_run=False):
	"""BR-05: the channel is the Customer. Reuse if present, else create under
	'Channel Partners' with territory = state (only if that Territory exists)."""
	name = channel[:140]
	if frappe.db.exists("Customer", name):
		return name
	if dry_run:
		return name

	group = _ensure_channel_partner_group()
	cust = frappe.new_doc("Customer")
	cust.customer_name = name
	cust.customer_type = "Company"
	cust.customer_group = group
	territory = _resolve_territory(state) or frappe.db.get_value(
		"Territory", {"is_group": 0}, "name"
	)
	if territory:
		cust.territory = territory
	cust.flags.ignore_permissions = True
	cust.flags.ignore_mandatory = True
	cust.insert(ignore_permissions=True)
	return name


def _resolve_line(sales_channel, li, resolve_stats, dry_run):
	"""Resolve one Uniware line, tolerating failures (BR-02 returns None)."""
	if (li.get("quantity") or 1) == 0:  # BR-06
		return None
	try:
		resolved = _resolve_items(
			sales_channel, [li], auto_create_items=False,
			resolve_stats=resolve_stats, dry_run=dry_run,
		)
	except ValueError:
		return None
	return resolved[0] if resolved else None


def build_group_lines(sales_channel, dtos, resolve_stats, dry_run):
	"""Flatten + resolve all lines across the group's orders. Skips unresolved
	(BR-02) and zero-qty (BR-06) lines. Annotates each with its order code."""
	lines = []
	skipped = []
	for dto in dtos:
		order_code = dto.get("code")
		for li in dto.get("saleOrderItems") or []:
			row = _resolve_line(sales_channel, li, resolve_stats, dry_run)
			if not row:
				skipped.append({"order": order_code, "sku": li.get("itemSku")})
				continue
			row = dict(row)
			row["custom_order_id"] = order_code
			row["custom_sku"] = li.get("itemSku")
			row["custom_shipment_id"] = li.get("shippingPackageCode") or li.get("shippingProvider")
			lines.append(row)
	return lines, skipped


def _draft_exists_today(state, channel):
	"""BR-08: an invoice for this state+channel already created today."""
	return bool(
		frappe.db.exists(
			"Sales Invoice",
			{
				"custom_uniware_state": state,
				"custom_channel_name": channel,
				"custom_sync_date": [">=", nowdate()],
			},
		)
	)


def _create_consolidated_invoice(state, channel, dtos, lines, company, naming_series):
	customer = get_or_create_channel_customer(channel, state, dry_run=False)
	order_codes = [d.get("code") for d in dtos if d.get("code")]
	sync_ts = now_datetime()

	doc = frappe.new_doc("Sales Invoice")
	doc.customer = customer
	if company:
		doc.company = company
	if naming_series:
		doc.naming_series = naming_series
	doc.posting_date = nowdate()
	doc.due_date = nowdate()
	doc.currency = "INR"
	doc.update_stock = 0
	doc.remarks = f"State: {state} | Channel: {channel} | Orders: {len(dtos)}"

	territory = _resolve_territory(state)
	if territory:
		doc.territory = territory

	doc.set("uniware_channel", channel if frappe.db.exists("Sales Channel", channel) else None)
	doc.set("custom_uniware_state", state)
	doc.set("custom_channel_name", channel)
	doc.set("custom_order_count", len(dtos))
	doc.set("custom_sync_date", sync_ts)
	doc.set("custom_order_ids", ", ".join(order_codes))

	for row in lines:
		line = doc.append(
			"items",
			{"item_code": row["item_code"], "qty": row["qty"], "rate": row["rate"]},
		)
		line.set("uniware_item_code", row.get("uniware_item_code"))
		line.set("uniware_item_sku", row.get("uniware_item_sku"))
		line.set("custom_order_id", row.get("custom_order_id"))
		line.set("custom_sku", row.get("custom_sku"))
		line.set("custom_shipment_id", row.get("custom_shipment_id"))

	doc.flags.ignore_permissions = True
	doc.set_missing_values()
	doc.insert(ignore_permissions=True)  # DRAFT
	return doc.name


def _log_sync(state, channel, dtos, invoice_name, status, error_log=None):
	log = frappe.new_doc("Uniware Sync Log")
	log.sync_date = now_datetime()
	log.state = state
	log.channel = channel
	log.order_count = len(dtos)
	log.sales_invoice = invoice_name
	log.status = status
	log.order_ids = "\n".join(d.get("code") for d in dtos if d.get("code"))
	if error_log:
		log.error_log = error_log
	log.flags.ignore_permissions = True
	log.insert(ignore_permissions=True)
	return log.name


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------
@frappe.whitelist()
def pull_invoices_statewise(
	updated_since_minutes=1440, limit=100, dry_run=1, naming_series=None, same_day="skip"
):
	"""Consolidated state-wise Uniware -> ERPNext Sales Invoice sync (AQ1-I32).

	Args:
		updated_since_minutes: Uniware search window (default 24h).
		limit:    max orders to fetch.
		dry_run:  1 (default) previews per-group; 0 creates Draft invoices + logs.
		naming_series: optional SI naming series to pin (else site default).
		same_day: 'skip' (default) skips a group already invoiced today (BR-08).
	"""
	updated_since_minutes = int(updated_since_minutes)
	limit = int(limit)
	dry_run = bool(int(dry_run))

	client = UniwareClient()
	company = _default_company()

	try:
		search_data = client.post(
			"/services/rest/v1/oms/saleOrder/search",
			json={
				"updatedSinceInMinutes": updated_since_minutes,
				"searchOptions": {"displayStart": 0, "displayLength": limit, "getCount": True},
			},
		)
	except UniwareAPIError as e:
		return {"ok": False, "error": f"search failed: {e}"}

	elements = search_data.get("elements") or []
	synced = get_synced_order_ids()

	# Fetch full DTOs for not-yet-synced orders.
	dtos = []
	fetch_errors = []
	for summary in elements:
		code = summary.get("code")
		if code in synced:
			continue
		try:
			detail = client.post("/services/rest/v1/oms/saleorder/get", json={"code": code})
		except UniwareAPIError as e:
			fetch_errors.append({"code": code, "error": str(e)})
			continue
		dto = detail.get("saleOrderDTO") or detail.get("saleOrder") or {}
		if dto:
			dtos.append(dto)

	groups = group_orders(dtos)
	result = {
		"ok": True,
		"dry_run": dry_run,
		"company": company,
		"fetched": len(dtos),
		"groups": len(groups),
		"created": 0,
		"skipped_groups": 0,
		"fetch_errors": fetch_errors,
		"sample": [],
	}

	resolve_stats = {"items_created": 0, "mappings_created": 0}

	for (state, channel), group_dtos in groups.items():
		lines, skipped = build_group_lines(channel, group_dtos, resolve_stats, dry_run)

		if not lines:  # BR-10
			result["skipped_groups"] += 1
			if not dry_run:
				_log_sync(state, channel, group_dtos, None, "Skipped",
				          error_log="No resolvable items in group.")
			continue

		if not dry_run and same_day == "skip" and _draft_exists_today(state, channel):  # BR-08
			result["skipped_groups"] += 1
			continue

		if dry_run:
			result["sample"].append(
				{
					"state": state,
					"channel": channel,
					"orders": len(group_dtos),
					"line_count": len(lines),
					"skipped_lines": len(skipped),
					"total": round(sum(l["qty"] * l["rate"] for l in lines), 2),
				}
			)
			continue

		try:
			si = _create_consolidated_invoice(state, channel, group_dtos, lines, company, naming_series)
			status = "Partial" if skipped else "Success"
			_log_sync(state, channel, group_dtos, si, status,
			          error_log=(f"{len(skipped)} line(s) skipped" if skipped else None))
			result["created"] += 1
			result["sample"].append({"state": state, "channel": channel, "sales_invoice": si})
		except Exception as e:
			frappe.log_error(
				title="Uniware statewise sync — create failed",
				message=f"{state}/{channel}: {type(e).__name__}: {e}",
			)
			_log_sync(state, channel, group_dtos, None, "Error",
			          error_log=f"{type(e).__name__}: {e}")
			result.setdefault("errors", []).append(
				{"state": state, "channel": channel, "error": f"{type(e).__name__}: {e}"}
			)

	if not dry_run:
		frappe.db.commit()

	return result
