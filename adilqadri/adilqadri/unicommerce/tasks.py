"""
Scheduled sync stubs for Unicommerce integration.

The scheduler fires `scheduled_sync` every 15 minutes. It checks
Unicommerce Settings to decide which sync flows are enabled, then
dispatches to the appropriate handlers.

Actual sync bodies (inventory push, order pull) are stubbed — they
will be implemented once the foundation is verified end-to-end with
a live connection test.
"""

import frappe
from frappe.utils import now_datetime

from adilqadri.adilqadri.unicommerce.client import UniwareAPIError, UniwareClient


def scheduled_sync():
	settings = frappe.get_single("Unicommerce Settings")
	if not settings.enabled:
		return

	try:
		if settings.sync_inventory:
			push_inventory(settings)
		if settings.pull_sale_orders:
			pull_sale_orders(settings)
	except UniwareAPIError as e:
		frappe.log_error(title="Unicommerce sync failed", message=str(e))
		return

	frappe.db.set_value(
		"Unicommerce Settings", None, "last_sync_at", now_datetime(), update_modified=False
	)
	frappe.db.commit()


def push_inventory(settings):
	"""
	TODO: iterate Item stock balances per mapped warehouse/facility and
	POST to /services/rest/v1/inventory/inventorySnapshot/edit.
	Stub — no-op for now.
	"""
	UniwareClient()  # validates auth works
	return


def pull_sale_orders(settings):
	"""
	TODO: call /services/rest/v1/oms/saleOrder/search with a time window,
	for each returned order resolve items via item_mapping.get_item_by_channel_code,
	and create an ERPNext Sales Order.
	Stub — no-op for now.
	"""
	UniwareClient()
	return
