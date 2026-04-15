"""
Scheduled sync dispatcher for Unicommerce integration.

The scheduler fires `scheduled_sync` every 15 minutes. It checks
Uniware Connector Settings to decide which sync flows are enabled, then
dispatches to the appropriate handlers.
"""

import frappe
from frappe.utils import now_datetime

from adilqadri.adilqadri.unicommerce.client import UniwareAPIError, UniwareClient
from adilqadri.adilqadri.unicommerce.order_pull import pull_orders


def scheduled_sync():
	settings = frappe.get_single("Uniware Connector Settings")
	if not settings.enabled:
		return

	try:
		if settings.sync_inventory:
			push_inventory(settings)
		if settings.pull_sale_orders:
			scheduled_pull_sale_orders(settings)
	except UniwareAPIError as e:
		frappe.log_error(title="Unicommerce sync failed", message=str(e))
		return

	frappe.db.set_value(
		"Uniware Connector Settings",
		None,
		"last_sync_at",
		now_datetime(),
		update_modified=False,
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


def scheduled_pull_sale_orders(settings):
	"""
	Pull orders updated in the last `sync_frequency_minutes * 2` minutes
	(2x buffer to handle missed runs). Limit per run is 50 orders —
	tune via Uniware Connector Settings once we see real traffic.
	"""
	lookback = max((settings.sync_frequency_minutes or 15) * 2, 30)
	result = pull_orders(updated_since_minutes=lookback, limit=50, dry_run=0)
	if result.get("errors"):
		frappe.log_error(
			title="Uniware order pull — partial errors",
			message=f"created={result.get('created')}, errors={result.get('errors')}",
		)
