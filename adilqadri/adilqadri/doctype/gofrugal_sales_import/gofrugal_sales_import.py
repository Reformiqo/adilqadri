import frappe
from frappe.model.document import Document

from adilqadri.adilqadri.gofrugal.invoice_import import import_sales_invoices


class GoFrugalSalesImport(Document):
	@frappe.whitelist()
	def run_import(self, dry_run=1):
		"""Run the GoFrugal importer against the attached file.

		dry_run=1 previews (writes nothing); dry_run=0 creates Draft invoices.
		The result is stored on ``last_result`` for an audit trail.
		"""
		if not self.upload_file:
			frappe.throw("Please attach the GoFrugal export file first.")

		result = import_sales_invoices(
			file_url=self.upload_file,
			dry_run=int(dry_run),
			update_stock=1 if self.update_stock else 0,
			naming_series=self.naming_series_override or None,
		)
		self.db_set("last_result", frappe.as_json(result))
		return result
