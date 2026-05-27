frappe.ui.form.on("GoFrugal Sales Import", {
	refresh(frm) {
		frm.disable_save();
		frm.add_custom_button(__("Preview (Dry Run)"), () => run(frm, 1));
		frm
			.add_custom_button(__("Import (Create Drafts)"), () => {
				frappe.confirm(
					__("Create DRAFT Sales Invoices from this file? Finance still reviews + submits."),
					() => run(frm, 0)
				);
			})
			.addClass("btn-primary");
	},
});

function run(frm, dry_run) {
	if (!frm.doc.upload_file) {
		frappe.msgprint(__("Attach the GoFrugal export file first."));
		return;
	}
	const proceed = () =>
		frm
			.call({
				doc: frm.doc,
				method: "run_import",
				args: { dry_run },
				freeze: true,
				freeze_message: dry_run
					? __("Previewing GoFrugal file…")
					: __("Creating Draft Sales Invoices…"),
			})
			.then((r) => render_result(frm, r.message, dry_run));

	// Persist the attached file / options before running the server method.
	if (frm.is_dirty()) {
		frm.save().then(proceed);
	} else {
		proceed();
	}
}

function render_result(frm, res, dry_run) {
	if (!res) return;
	if (!res.ok) {
		frappe.msgprint({ title: __("Import failed"), message: frappe.utils.escape_html(res.error || "Unknown error"), indicator: "red" });
		return;
	}

	let rows = (res.sample || [])
		.map((s) => {
			if (dry_run) {
				return `<tr><td>${frappe.utils.escape_html(s.bill_number || "")}</td>
					<td>${frappe.utils.escape_html(s.customer || "")}</td>
					<td style="text-align:right">${s.item_count ?? ""}</td>
					<td style="text-align:right">${s.total ?? ""}</td></tr>`;
			}
			return `<tr><td>${frappe.utils.escape_html(s.bill_number || "")}</td>
				<td colspan="3"><a href="/app/sales-invoice/${encodeURIComponent(s.sales_invoice || "")}">${frappe.utils.escape_html(s.sales_invoice || "")}</a></td></tr>`;
		})
		.join("");

	const head = dry_run
		? "<tr><th>Bill</th><th>Customer</th><th style='text-align:right'>Items</th><th style='text-align:right'>Total</th></tr>"
		: "<tr><th>Bill</th><th colspan='3'>Sales Invoice</th></tr>";

	let errs = (res.errors || [])
		.map((e) => `<li><b>${frappe.utils.escape_html(e.bill_number || "")}</b>: ${frappe.utils.escape_html(e.error || "")}</li>`)
		.join("");

	const summary = dry_run
		? `<b>Preview</b> — ${res.total_invoices} invoice(s) would be created, ${res.skipped_exists} already imported.`
		: `<b>Created ${res.created}</b> draft invoice(s); ${res.skipped_exists} already imported.`;

	frappe.msgprint({
		title: dry_run ? __("GoFrugal Preview") : __("GoFrugal Import Done"),
		indicator: (res.errors || []).length ? "orange" : "green",
		message: `
			<p>${summary} Company: <b>${frappe.utils.escape_html(res.company || "")}</b>, posting date: <b>${frappe.utils.escape_html(res.posting_date || "today")}</b>.</p>
			${rows ? `<table class="table table-bordered"><thead>${head}</thead><tbody>${rows}</tbody></table>` : ""}
			${errs ? `<p style="color:#b00"><b>Errors:</b></p><ul>${errs}</ul>` : ""}
		`,
	});
	frm.reload_doc();
}
