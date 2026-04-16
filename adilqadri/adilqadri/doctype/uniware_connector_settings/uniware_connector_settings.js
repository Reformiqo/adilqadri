// Copyright (c) 2026, erpera and contributors
// For license information, please see license.txt

frappe.ui.form.on("Uniware Connector Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test OAuth Token"), () => {
			frappe.dom.freeze(__("Fetching token from Uniware…"));
			frm.call("test_oauth")
				.then((r) => {
					frappe.dom.unfreeze();
					if (r.message && r.message.ok) {
						frappe.msgprint({
							title: __("OAuth OK"),
							indicator: "green",
							message: `
								<p><b>Token fetched successfully.</b></p>
								<ul>
									<li>token_type: <code>${frappe.utils.escape_html(r.message.token_type || "")}</code></li>
									<li>expires_in: <code>${r.message.expires_in} s</code></li>
									<li>scope: <code>${frappe.utils.escape_html(r.message.scope || "")}</code></li>
								</ul>
							`,
						});
						frm.reload_doc();
					} else {
						frappe.msgprint({
							title: __("OAuth Failed"),
							indicator: "red",
							message: `<pre>${frappe.utils.escape_html((r.message && r.message.error) || "Unknown error")}</pre>`,
						});
					}
				})
				.catch(() => frappe.dom.unfreeze());
		}, __("Test"));

		frm.add_custom_button(__("Test REST API Call"), () => {
			frappe.dom.freeze(__("Calling a sample REST endpoint…"));
			frm.call("test_rest_call")
				.then((r) => {
					frappe.dom.unfreeze();
					const m = r.message || {};
					const indicator = m.ok ? "green" : "red";
					const title = m.ok ? __("REST OK") : __("REST Failed");
					let html = `<p><b>${frappe.utils.escape_html(m.endpoint || "")}</b></p>`;
					html += `<p>HTTP: <code>${m.status_code}</code>, Content-Type: <code>${frappe.utils.escape_html(m.content_type || "")}</code></p>`;
					if (m.ok) {
						html += `<p>JSON keys: <code>${frappe.utils.escape_html((m.json_keys || []).join(", "))}</code></p>`;
						if (m.diagnosis) html += `<p>${frappe.utils.escape_html(m.diagnosis)}</p>`;
					} else {
						html += `<p>Diagnosis: <b>${frappe.utils.escape_html(m.diagnosis || "")}</b></p>`;
						html += `<details><summary>Raw response (first 500 chars)</summary><pre>${frappe.utils.escape_html(m.preview || "")}</pre></details>`;
					}
					frappe.msgprint({ title, indicator, message: html, wide: true });
				})
				.catch(() => frappe.dom.unfreeze());
		}, __("Test"));

		frm.add_custom_button(__("Pull Orders (Dry Run)"), () => {
			run_pull_orders(frm, true);
		}, __("Sync"));

		frm.add_custom_button(__("Pull Orders (Live)"), () => {
			frappe.confirm(
				__("This will CREATE Sales Orders in ERPNext from Uniware. Proceed?"),
				() => run_pull_orders(frm, false),
			);
		}, __("Sync"));

		frm.add_custom_button(__("Pull Invoices (Dry Run)"), () => {
			run_pull_invoices(frm, true);
		}, __("Sync"));

		frm.add_custom_button(__("Pull Invoices (Live)"), () => {
			frappe.confirm(
				__("This will CREATE Sales Invoices in ERPNext from Uniware. Proceed?"),
				() => run_pull_invoices(frm, false),
			);
		}, __("Sync"));
	},
});

function run_pull_invoices(frm, dry_run) {
	const d = new frappe.ui.Dialog({
		title: dry_run ? __("Pull Invoices — Dry Run") : __("Pull Invoices — LIVE"),
		fields: [
			{
				fieldname: "updated_since_minutes",
				label: __("Lookback (minutes)"),
				fieldtype: "Int",
				default: 60,
				reqd: 1,
			},
			{
				fieldname: "limit",
				label: __("Max invoices"),
				fieldtype: "Int",
				default: dry_run ? 5 : 10,
				reqd: 1,
			},
		],
		primary_action_label: dry_run ? __("Run Dry Run") : __("CREATE"),
		primary_action(values) {
			d.hide();
			frappe.dom.freeze(__("Pulling invoices from Uniware…"));
			frappe.call({
				method: "adilqadri.adilqadri.unicommerce.invoice_pull.pull_invoices",
				args: {
					updated_since_minutes: values.updated_since_minutes,
					limit: values.limit,
					dry_run: dry_run ? 1 : 0,
				},
				callback(r) {
					frappe.dom.unfreeze();
					const m = r.message || {};
					if (!m.ok && m.error) {
						frappe.msgprint({
							title: __("Pull Failed"),
							indicator: "red",
							message: `<pre>${frappe.utils.escape_html(m.error)}</pre>`,
						});
						return;
					}
					const indicator = (m.errors || []).length === 0 ? "green" : "orange";
					const title = dry_run ? __("Invoice Dry Run Summary") : __("Invoice Pull Summary");
					let html = `<p><b>Total matching:</b> ${m.total_matching} | <b>Fetched:</b> ${m.fetched} | <b>Created:</b> ${m.created} | <b>Skipped:</b> ${m.skipped_exists} | <b>Errors:</b> ${(m.errors || []).length}</p>`;
					if ((m.sample || []).length) {
						html += `<h4>Sample</h4><pre style="max-height:300px;overflow:auto">${frappe.utils.escape_html(JSON.stringify(m.sample, null, 2))}</pre>`;
					}
					if ((m.errors || []).length) {
						html += `<h4 style="color:#c0392b">Errors</h4><pre style="max-height:300px;overflow:auto">${frappe.utils.escape_html(JSON.stringify(m.errors, null, 2))}</pre>`;
					}
					frappe.msgprint({ title, indicator, message: html, wide: true });
					if (!dry_run) frm.reload_doc();
				},
				error() { frappe.dom.unfreeze(); },
			});
		},
	});
	d.show();
}

function run_pull_orders(frm, dry_run) {
	const d = new frappe.ui.Dialog({
		title: dry_run ? __("Pull Orders — Dry Run") : __("Pull Orders — LIVE"),
		fields: [
			{
				fieldname: "updated_since_minutes",
				label: __("Lookback (minutes)"),
				fieldtype: "Int",
				default: 60,
				reqd: 1,
				description: __("Fetch orders updated in the last N minutes."),
			},
			{
				fieldname: "limit",
				label: __("Max orders"),
				fieldtype: "Int",
				default: dry_run ? 5 : 10,
				reqd: 1,
				description: __("Cap this run to N orders. Start small."),
			},
		],
		primary_action_label: dry_run ? __("Run Dry Run") : __("CREATE"),
		primary_action(values) {
			d.hide();
			frappe.dom.freeze(__("Calling Uniware…"));
			frappe.call({
				method: "adilqadri.adilqadri.unicommerce.order_pull.pull_orders",
				args: {
					updated_since_minutes: values.updated_since_minutes,
					limit: values.limit,
					dry_run: dry_run ? 1 : 0,
				},
				callback(r) {
					frappe.dom.unfreeze();
					const m = r.message || {};
					if (!m.ok && m.error) {
						frappe.msgprint({
							title: __("Pull Failed"),
							indicator: "red",
							message: `<pre>${frappe.utils.escape_html(m.error)}</pre>`,
						});
						return;
					}
					const indicator = (m.errors || []).length === 0 ? "green" : "orange";
					const title = dry_run ? __("Dry Run Summary") : __("Pull Summary");
					let html = "";
					html += `<p><b>Total matching:</b> ${m.total_matching} &nbsp; `;
					html += `<b>Fetched:</b> ${m.fetched} &nbsp; `;
					html += `<b>Created:</b> ${m.created} &nbsp; `;
					html += `<b>Skipped (already synced):</b> ${m.skipped_exists} &nbsp; `;
					html += `<b>Errors:</b> ${(m.errors || []).length}</p>`;

					if ((m.sample || []).length) {
						html += `<h4>Sample</h4><pre style="max-height:300px;overflow:auto">${frappe.utils.escape_html(JSON.stringify(m.sample, null, 2))}</pre>`;
					}
					if ((m.errors || []).length) {
						html += `<h4 style="color:#c0392b">Errors</h4><pre style="max-height:300px;overflow:auto">${frappe.utils.escape_html(JSON.stringify(m.errors, null, 2))}</pre>`;
					}

					frappe.msgprint({ title, indicator, message: html, wide: true });
					if (!dry_run) frm.reload_doc();
				},
				error() {
					frappe.dom.unfreeze();
				},
			});
		},
	});
	d.show();
}
