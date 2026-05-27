"""Tests for the state-wise consolidated Uniware invoice sync (AQ1-I32).

The Uniware HTTP fetch is thin glue, so tests exercise the building blocks
directly with synthetic order DTOs: grouping, normalisation, line building,
consolidated invoice creation, Sync Log dedup and the BR-* rules.
"""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from adilqadri.adilqadri.unicommerce import invoice_pull_statewise as sw


def _dto(code, channel, state, skus, status="DISPATCHED"):
	return {
		"code": code,
		"channel": channel,
		"status": status,
		"shippingAddress": {"state": state} if state is not None else {},
		"saleOrderItems": [
			{"code": f"{code}-{i}", "itemSku": sku, "sellingPrice": 100, "quantity": 1}
			for i, sku in enumerate(skus)
		],
	}


class TestStatewiseTransforms(unittest.TestCase):
	def test_normalise_channel(self):
		self.assertEqual(sw.normalise_channel("AMAZON"), "Amazon")
		self.assertEqual(sw.normalise_channel("FLIPKART_SELLER"), "Flipkart Seller")
		self.assertEqual(sw.normalise_channel(""), sw.CHANNEL_UNKNOWN)
		self.assertEqual(sw.normalise_channel(None), sw.CHANNEL_UNKNOWN)

	def test_normalise_state(self):
		self.assertEqual(sw.normalise_state("MH"), "Maharashtra")
		self.assertEqual(sw.normalise_state("maharashtra"), "Maharashtra")
		self.assertEqual(sw.normalise_state("Gujarat"), "Gujarat")
		self.assertEqual(sw.normalise_state(""), sw.STATE_UNKNOWN)
		self.assertEqual(sw.normalise_state(None), sw.STATE_UNKNOWN)

	def test_extract_state_prefers_shipping(self):
		dto = {"shippingAddress": {"state": "GJ"}, "billingAddress": {"state": "MH"}}
		self.assertEqual(sw.extract_state(dto), "Gujarat")
		dto2 = {"billingAddress": {"state": "MH"}}
		self.assertEqual(sw.extract_state(dto2), "Maharashtra")

	def test_group_orders(self):
		dtos = [
			_dto("SO1", "Amazon", "Gujarat", ["X"]),
			_dto("SO2", "Amazon", "Gujarat", ["Y"]),
			_dto("SO3", "Flipkart", "Maharashtra", ["Z"]),
			_dto("SO4", "Amazon", "Gujarat", ["Q"], status="CANCELLED"),  # BR-07 excluded
		]
		groups = sw.group_orders(dtos)
		self.assertEqual(len(groups), 2)
		self.assertEqual(len(groups[("Gujarat", "Amazon")]), 2)
		self.assertEqual(len(groups[("Maharashtra", "Flipkart")]), 1)

	def test_blank_state_and_channel_grouping(self):
		dtos = [_dto("SO9", "", None, ["X"])]
		groups = sw.group_orders(dtos)
		self.assertIn((sw.STATE_UNKNOWN, sw.CHANNEL_UNKNOWN), groups)


class TestStatewiseImport(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or (
			frappe.get_all("Company", limit=1, pluck="name") or [None]
		)[0]
		cls.sku = "SW_TEST_ITEM"
		if not frappe.db.exists("Item", cls.sku):
			item = frappe.new_doc("Item")
			item.item_code = cls.sku
			item.item_name = "Statewise Test Item"
			item.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
			item.stock_uom = "Nos"
			item.is_stock_item = 0
			hsn = frappe.get_all("GST HSN Code", limit=1, pluck="name")
			if hsn:
				item.gst_hsn_code = hsn[0]
			item.flags.ignore_permissions = True
			item.insert(ignore_permissions=True)
		options = (frappe.get_meta("Sales Invoice").get_field("naming_series").options or "").split("\n")
		cls.naming_series = next(
			(o for o in options if o.strip() in ("SINV-.YY.-", "SRET-.YY.-")),
			(options[0].strip() if options else None),
		)

	def tearDown(self):
		for name in frappe.get_all(
			"Sales Invoice", filters={"custom_channel_name": ("like", "ZZTest%")}, pluck="name"
		):
			frappe.delete_doc("Sales Invoice", name, force=1, ignore_permissions=True)
		for name in frappe.get_all(
			"Uniware Sync Log", filters={"channel": ("like", "ZZTest%")}, pluck="name"
		):
			frappe.delete_doc("Uniware Sync Log", name, force=1, ignore_permissions=True)

	def test_consolidated_invoice_and_log(self):
		dtos = [
			_dto("SWSO1", "ZZTestChan", "Gujarat", [self.sku, self.sku]),
			_dto("SWSO2", "ZZTestChan", "Gujarat", [self.sku]),
		]
		groups = sw.group_orders(dtos)
		(state, channel), group_dtos = next(iter(groups.items()))
		stats = {"items_created": 0, "mappings_created": 0}
		lines, skipped = sw.build_group_lines(channel, group_dtos, stats, dry_run=False)
		self.assertEqual(len(lines), 3)  # 2 + 1 line items
		self.assertEqual(skipped, [])

		si_name = sw._create_consolidated_invoice(
			state, channel, group_dtos, lines, self.company, self.naming_series
		)
		si = frappe.get_doc("Sales Invoice", si_name)
		self.assertEqual(si.docstatus, 0)  # Draft
		self.assertEqual(si.custom_channel_name, "Zztestchan")  # title-cased
		self.assertEqual(si.custom_uniware_state, "Gujarat")
		self.assertEqual(si.custom_order_count, 2)
		self.assertEqual(len(si.items), 3)
		self.assertIn("Orders: 2", si.remarks)
		self.assertEqual(si.items[0].custom_order_id, "SWSO1")

		# Customer auto-created under 'Channel Partners'
		self.assertTrue(frappe.db.exists("Customer", "Zztestchan"))
		self.assertEqual(
			frappe.db.get_value("Customer", "Zztestchan", "customer_group"),
			sw.CHANNEL_PARTNERS_GROUP,
		)

		# Log + dedup
		log = sw._log_sync(state, channel, group_dtos, si_name, "Success")
		synced = sw.get_synced_order_ids()
		self.assertIn("SWSO1", synced)
		self.assertIn("SWSO2", synced)
		frappe.delete_doc("Customer", "Zztestchan", force=1, ignore_permissions=True)

	def test_unresolved_line_skipped(self):
		# One good SKU + one unknown -> BR-02: unknown skipped, good one kept.
		dto = _dto("SWSO3", "ZZTestChan", "Gujarat", [self.sku, "NO_SUCH_SKU_XYZ"])
		groups = sw.group_orders([dto])
		(state, channel), group_dtos = next(iter(groups.items()))
		stats = {"items_created": 0, "mappings_created": 0}
		lines, skipped = sw.build_group_lines(channel, group_dtos, stats, dry_run=False)
		self.assertEqual(len(lines), 1)
		self.assertEqual(len(skipped), 1)
		self.assertEqual(skipped[0]["sku"], "NO_SUCH_SKU_XYZ")
