"""Tests for the GoFrugal → ERPNext Sales Invoice importer.

Two layers:
- TestGoFrugalTransforms: pure, no DB — exercises the FRD transformation rules
  against the exact examples documented in the FRD.
- TestGoFrugalImport: integration — reads a generated .xlsx and creates a real
  Draft Sales Invoice, asserting dedup and "no auto-create item" behaviour.
"""

import os
import tempfile
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from adilqadri.adilqadri.gofrugal import invoice_import as gi


class TestGoFrugalTransforms(unittest.TestCase):
	def test_clean_int_str(self):
		# FRD examples: floats / scientific notation -> clean integer strings
		self.assertEqual(gi.clean_int_str(199.0), "199")
		self.assertEqual(gi.clean_int_str("199"), "199")
		self.assertEqual(gi.clean_int_str(617571077272), "617571077272")
		self.assertEqual(gi.clean_int_str(9.033301687e9), "9033301687")
		self.assertEqual(gi.clean_int_str(""), "")
		self.assertEqual(gi.clean_int_str(None), "")
		self.assertEqual(gi.clean_int_str("nan"), "")
		# Non-numeric text passes through stripped
		self.assertEqual(gi.clean_int_str("  ABC123-X  "), "ABC123-X")

	def test_parse_posting_date(self):
		self.assertEqual(
			gi.parse_posting_date("DATE : from 21-05-2026 21-05-2026"), "2026-05-21"
		)
		self.assertEqual(gi.parse_posting_date("01/02/2026"), "2026-02-01")
		self.assertIsNone(gi.parse_posting_date("no date here"))
		self.assertIsNone(gi.parse_posting_date(""))

	def test_resolve_customer_name(self):
		# Doc Name '.' is a placeholder -> fall back to Cust Name
		self.assertEqual(
			gi.resolve_customer_name(".", "Udaipur Shakti Nagar"), "Udaipur Shakti Nagar"
		)
		self.assertEqual(gi.resolve_customer_name("Real Doc", "Cust"), "Real Doc")
		self.assertEqual(gi.resolve_customer_name("", "Cust Only"), "Cust Only")
		self.assertEqual(gi.resolve_customer_name("", ""), "")

	def test_build_remarks(self):
		self.assertEqual(gi.build_remarks("RETAIL INVOICE", ""), "RETAIL INVOICE")
		self.assertEqual(gi.build_remarks("RETAIL", "urgent"), "RETAIL | urgent")
		self.assertEqual(gi.build_remarks("", ""), "")

	def test_group_rows_by_bill(self):
		rows = [
			{gi.COL_BILL_NUMBER: "B1", gi.COL_ITEM_CODE: 1},
			{gi.COL_BILL_NUMBER: "B1", gi.COL_ITEM_CODE: 2},
			{gi.COL_BILL_NUMBER: "", gi.COL_ITEM_CODE: 0},  # subtotal -> skipped
			{gi.COL_BILL_NUMBER: "B2", gi.COL_ITEM_CODE: 3},
		]
		groups = gi.group_rows_by_bill(rows)
		self.assertEqual(list(groups.keys()), ["B1", "B2"])
		self.assertEqual(len(groups["B1"]), 2)
		self.assertEqual(len(groups["B2"]), 1)

	def test_build_invoice_payload(self):
		rows = [
			{
				gi.COL_BILL_NUMBER: "WHB022627000330",
				gi.COL_DOC_NAME: ".",
				gi.COL_CUST_BILLING: "Udaipur Shakti Nagar",
				gi.COL_MOBILE: 9033301687.0,
				gi.COL_OUTLET: "Head Office",
				gi.COL_AREA: "AREA",
				gi.COL_BILL_DISC_PCT: 0,
				gi.COL_BILL_TYPE: "RETAIL INVOICE",
				gi.COL_ITEM_CODE: 199.0,
				gi.COL_ITEM_NAME: "AQ Perfume Discovery Set",
				gi.COL_EAN: 617571077272.0,
				gi.COL_QTY: 24,
				gi.COL_SELLING: 599,
				gi.COL_ACTUAL_SELLING: 999,
				gi.COL_ITEM_DISC_PCT: 0,
				gi.COL_NET_AMT: 14376,
			}
		]
		groups = gi.group_rows_by_bill(rows)
		payload = gi.build_invoice_payload("WHB022627000330", groups["WHB022627000330"], "2026-05-21")
		self.assertEqual(payload["customer"], "Udaipur Shakti Nagar")
		self.assertEqual(payload["mobile"], "9033301687")
		self.assertEqual(payload["outlet"], "Head Office")
		self.assertEqual(payload["remarks"], "RETAIL INVOICE")
		self.assertEqual(len(payload["items"]), 1)
		item = payload["items"][0]
		self.assertEqual(item["item_code"], "199")
		self.assertEqual(item["barcode"], "617571077272")
		self.assertEqual(item["qty"], 24.0)
		self.assertEqual(item["rate"], 599.0)
		self.assertEqual(item["price_list_rate"], 999.0)


def _write_sample_xlsx(path):
	"""Generate a minimal GoFrugal-shaped .xlsx: header rows + column row + data."""
	import openpyxl

	wb = openpyxl.Workbook()
	ws = wb.active
	ws.append(["Adilqadri", None, None])
	ws.append(["3111 Sales Detail - Itemwise", None, None])
	ws.append(["DATE : from 21-05-2026 21-05-2026", None, None])
	cols = [
		gi.COL_OUTLET, gi.COL_CUST_BILLING, gi.COL_BILL_NUMBER, gi.COL_MOBILE,
		gi.COL_ITEM_CODE, gi.COL_ITEM_NAME, gi.COL_QTY, gi.COL_SELLING,
		gi.COL_NET_AMT, gi.COL_ITEM_DISC_PCT, gi.COL_ITEM_DISC, gi.COL_BILL_DISC_PCT,
		gi.COL_ACTUAL_SELLING, gi.COL_AREA, gi.COL_BILL_TYPE, gi.COL_DOC_NAME, gi.COL_EAN,
	]
	ws.append(cols)

	def row(**kw):
		ws.append([kw.get(c, "") for c in cols])

	# Bill 1 — two item lines
	row(**{gi.COL_OUTLET: "Head Office", gi.COL_CUST_BILLING: "Udaipur Shakti Nagar",
	       gi.COL_BILL_NUMBER: "TESTBILL001", gi.COL_MOBILE: 9033301687.0,
	       gi.COL_ITEM_CODE: 0, gi.COL_ITEM_NAME: "Item A", gi.COL_QTY: 2,
	       gi.COL_SELLING: 100, gi.COL_ACTUAL_SELLING: 150, gi.COL_DOC_NAME: ".",
	       gi.COL_BILL_TYPE: "RETAIL INVOICE"})
	row(**{gi.COL_OUTLET: "Head Office", gi.COL_CUST_BILLING: "Udaipur Shakti Nagar",
	       gi.COL_BILL_NUMBER: "TESTBILL001", gi.COL_MOBILE: 9033301687.0,
	       gi.COL_ITEM_CODE: 0, gi.COL_ITEM_NAME: "Item A", gi.COL_QTY: 3,
	       gi.COL_SELLING: 100, gi.COL_ACTUAL_SELLING: 150, gi.COL_DOC_NAME: "."})
	# subtotal row (blank bill) -> must be skipped
	row(**{gi.COL_BILL_NUMBER: "", gi.COL_QTY: 5})
	wb.save(path)


class TestGoFrugalImport(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or (
			frappe.get_all("Company", limit=1, pluck="name") or [None]
		)[0]
		# india_compliance caps the SI name at 16 chars; pick a short series that
		# exists on this site so the test mirrors the live GST-series behaviour.
		options = (frappe.get_meta("Sales Invoice").get_field("naming_series").options or "").split("\n")
		cls.naming_series = next(
			(o for o in options if o.strip() in ("SINV-.YY.-", "SRET-.YY.-")),
			(options[0].strip() if options else None),
		)
		# A known item the importer can resolve. ITEM_CODE 0 -> clean_int_str -> "0".
		cls.item_code = "0"
		if not frappe.db.exists("Item", cls.item_code):
			item = frappe.new_doc("Item")
			item.item_code = cls.item_code
			item.item_name = "GoFrugal Test Item"
			item.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
			item.stock_uom = gi.DEFAULT_UOM
			item.is_stock_item = 0
			# india_compliance requires an HSN code on items; reuse any existing one.
			hsn = frappe.get_all("GST HSN Code", limit=1, pluck="name")
			if hsn:
				item.gst_hsn_code = hsn[0]
			item.flags.ignore_permissions = True
			item.insert(ignore_permissions=True)

	def setUp(self):
		# Clean any leftover from a prior run so dedup assertions are deterministic.
		for name in frappe.get_all(
			"Sales Invoice", filters={"gofrugal_bill_number": ("like", "TESTBILL%")}, pluck="name"
		):
			frappe.delete_doc("Sales Invoice", name, force=1, ignore_permissions=True)

	def test_parse_file_groups_and_dates(self):
		with tempfile.TemporaryDirectory() as d:
			path = os.path.join(d, "sample.xlsx")
			_write_sample_xlsx(path)
			meta, payloads = gi.parse_file(path)
		self.assertEqual(meta["posting_date"], "2026-05-21")
		self.assertEqual(len(payloads), 1)  # subtotal row skipped
		p = payloads[0]
		self.assertEqual(p["bill_number"], "TESTBILL001")
		self.assertEqual(p["customer"], "Udaipur Shakti Nagar")
		self.assertEqual(len(p["items"]), 2)

	def test_create_draft_invoice_and_dedup(self):
		with tempfile.TemporaryDirectory() as d:
			path = os.path.join(d, "sample.xlsx")
			_write_sample_xlsx(path)
			res = gi.import_sales_invoices(
				file_path=path, dry_run=0, update_stock=0, naming_series=self.naming_series
			)

		self.assertTrue(res["ok"], msg=str(res))
		self.assertEqual(res["created"], 1, msg=str(res))
		self.assertEqual(res["errors"], [], msg=str(res))

		si_name = res["sample"][0]["sales_invoice"]
		si = frappe.get_doc("Sales Invoice", si_name)
		self.assertEqual(si.docstatus, 0)  # DRAFT, never auto-submitted
		self.assertEqual(si.gofrugal_bill_number, "TESTBILL001")
		self.assertEqual(si.gofrugal_outlet, "Head Office")
		self.assertEqual(si.posting_date.isoformat(), "2026-05-21")
		self.assertEqual(si.update_stock, 0)
		self.assertEqual(len(si.items), 2)
		self.assertEqual(si.items[0].uom, gi.DEFAULT_UOM)

		# Re-import the same file -> deduped, nothing created.
		with tempfile.TemporaryDirectory() as d:
			path = os.path.join(d, "sample.xlsx")
			_write_sample_xlsx(path)
			res2 = gi.import_sales_invoices(file_path=path, dry_run=0, update_stock=0)
		self.assertEqual(res2["created"], 0, msg=str(res2))
		self.assertEqual(res2["skipped_exists"], 1, msg=str(res2))

	def test_unknown_item_fails_invoice(self):
		payload = {
			"bill_number": "TESTBILL999",
			"customer": "Walkin",
			"customer_name": "Walkin",
			"mobile": "",
			"outlet": "Head Office",
			"area": "",
			"additional_discount_percentage": 0,
			"remarks": "",
			"posting_date": "2026-05-21",
			"items": [{"item_code": "NO_SUCH_ITEM_XYZ", "qty": 1, "rate": 10}],
		}
		with self.assertRaises(ValueError):
			gi._create_sales_invoice(payload, self.company, update_stock=False, warehouse_map={})


class TestGoFrugalSalesImportUI(IntegrationTestCase):
	"""Exercises the 'GoFrugal Sales Import' screen's server path (run_import)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.item_code = "0"
		if not frappe.db.exists("Item", cls.item_code):
			item = frappe.new_doc("Item")
			item.item_code = cls.item_code
			item.item_name = "GoFrugal Test Item"
			item.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
			item.stock_uom = gi.DEFAULT_UOM
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

	def setUp(self):
		for name in frappe.get_all(
			"Sales Invoice", filters={"gofrugal_bill_number": ("like", "TESTBILL%")}, pluck="name"
		):
			frappe.delete_doc("Sales Invoice", name, force=1, ignore_permissions=True)

	def _attach_sample(self):
		import base64

		with tempfile.TemporaryDirectory() as d:
			path = os.path.join(d, "sample.xlsx")
			_write_sample_xlsx(path)
			content = open(path, "rb").read()
		f = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "gofrugal_sample.xlsx",
				"is_private": 1,
				"content": base64.b64encode(content).decode(),
				"decode": True,
			}
		).insert(ignore_permissions=True)
		return f.file_url

	def test_run_import_via_screen(self):
		tool = frappe.get_single("GoFrugal Sales Import")
		tool.upload_file = self._attach_sample()
		tool.update_stock = 0
		tool.naming_series_override = self.naming_series
		tool.flags.ignore_permissions = True
		tool.save(ignore_permissions=True)

		preview = tool.run_import(dry_run=1)
		self.assertTrue(preview["ok"], msg=str(preview))
		self.assertEqual(preview["total_invoices"], 1)
		self.assertEqual(preview["created"], 0)  # dry run writes nothing

		done = tool.run_import(dry_run=0)
		self.assertEqual(done["created"], 1, msg=str(done))
		self.assertTrue(frappe.db.exists("Sales Invoice", {"gofrugal_bill_number": "TESTBILL001"}))
		# last_result persisted for audit
		self.assertIn("created", frappe.get_single("GoFrugal Sales Import").last_result or "")
