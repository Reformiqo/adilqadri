# import frappe
# from frappe.model.mapper import get_mapped_doc


# def on_submit(doc, method=None):
#     print('\n\n On submit called :', '\n\n')
#     if not doc.custom_inter_branch_transfer:
#         return

#     def postprocess(source, target):
#         print('\n\n postprocess called', '\n\n')
#         target.supplier = source.company_address
#         target.billing_address = source.customer
#         target.bill_no = source.name

#     pi = get_mapped_doc(
#         "Sales Invoice",
#         doc.name,
#         {
#             "Sales Invoice": {
#                 "doctype": "Purchase Invoice",
#             },
#             "Sales Invoice Item": {
#                 "doctype": "Purchase Invoice Item",
#             },
#         },
#         None,
#         postprocess,
#     )

#     pi.insert(ignore_permissions=True)
#     pi.submit()

import frappe
from frappe.model.mapper import get_mapped_doc

def on_submit(self, method=None):
    if not self.custom_inter_branch_transfer:
        return

    pi = frappe.new_doc("Purchase Invoice")

    account_mapping = {
        "BRANCH TRANSFER (WITHIN STATE) - AEPL": "STOCK INWARD (WITHIN STATE) - AEPL",
        "BRANCH TRANSFER (OTHER STATE) - AEPL": "STOCK INWARD  (OTHER STATE) - AEPL",
    }

    # Header fields
    # NOTE: On a Purchase Invoice, `company_gstin` is read-only and is auto-fetched
    # from `billing_address.gstin`. Setting pi.company_gstin directly does NOT work --
    # it gets overwritten during validation. Set a valid company Address instead.
    pi.supplier = self.company_address  # the Supplier representing the selling branch
    # pi.supplier_address = self.company_address_display
    # pi.supplier_address = self.company_address  # seller branch address -> supplier_gstin
    # pi.billing_address = self.customer_address  # buyer (company) address -> company_gstin
    pi.bill_no = self.name
    pi.bill_date = self.posting_date
    pi.custom_expense_account = account_mapping.get(self.custom_income_account_)
    pi.billing_address = self.customer_address  # Address ("Al Nuaim-Billing-2") -> company_gstin
    pi.billing_address_display = self.address_display

    # Optional fields
    pi.company = self.company
    pi.posting_date = self.posting_date
    pi.due_date = self.due_date

    # Map items
    for item in self.items:
        pi.append("items", {
            "item_code": item.item_code,
            "item_name": item.item_name,
            "description": item.description,
            "qty": item.qty,
            "uom": item.uom,
            "stock_uom": item.stock_uom,
            "conversion_factor": item.conversion_factor,
            "rate": item.rate,
            "amount": item.amount,
            "warehouse": item.warehouse,
            # "expense_account": account_mapping.get(item.income_account),
        })

    # for tax in self.taxes:
    #     pi.append("taxes", {
    #         "charge_type": tax.charge_type,
    #         "account_head": tax.account_head,
    #         "cost_center": tax.cost_center,
    #         "rate": tax.rate
    #     })

    pi.insert(ignore_permissions=True)
    pi.submit()