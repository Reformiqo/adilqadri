"""
Channel ↔ Item mapping utilities.

Implements FRD section 7 (validation rules) and 8.2 (reverse lookup).
"""

import frappe

CHILD_FIELD = "channel_item_codes"


@frappe.whitelist()
def get_item_by_channel_code(channel_name: str, channel_product_code: str) -> str | None:
	"""
	Given a Sales Channel name and the channel's product code, return the
	ERPNext Item Code, or None if no active mapping exists.

	This is consumed by the order-sync middleware (FRD AC-04, AC-06).
	"""
	if not channel_name or not channel_product_code:
		return None

	row = frappe.db.get_value(
		"Channel Item Code",
		{
			"parenttype": "Item",
			"parentfield": CHILD_FIELD,
			"channel": channel_name,
			"channel_product_code": channel_product_code,
			"is_active": 1,
		},
		["parent"],
	)
	return row


def validate_item_channel_codes(doc, method=None):
	"""
	Attached to Item.validate via hooks.py.
	Enforces FRD rules V-01, V-02, V-03.
	"""
	rows = doc.get(CHILD_FIELD) or []
	if not rows:
		return

	seen_channels: set[str] = set()

	for idx, row in enumerate(rows, start=1):
		if not row.channel or not row.channel_product_code:
			frappe.throw(
				f"Channel Name and Channel Product Code are required in row {idx}.",
				title="Channel Mapping",
			)

		if row.channel in seen_channels:
			frappe.throw(
				f"Channel '{row.channel}' is already configured for this item.",
				title="Channel Mapping",
			)
		seen_channels.add(row.channel)

		clash = frappe.db.sql(
			"""
			SELECT parent
			FROM `tabChannel Item Code`
			WHERE parenttype = 'Item'
			  AND parentfield = %(field)s
			  AND channel = %(channel)s
			  AND channel_product_code = %(code)s
			  AND parent != %(parent)s
			LIMIT 1
			""",
			{
				"field": CHILD_FIELD,
				"channel": row.channel,
				"code": row.channel_product_code,
				"parent": doc.name or "",
			},
		)
		if clash:
			frappe.throw(
				f"Product Code '{row.channel_product_code}' is already assigned to "
				f"Item '{clash[0][0]}' for channel '{row.channel}'.",
				title="Channel Mapping",
			)
