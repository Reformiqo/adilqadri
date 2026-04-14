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
		});

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
					} else {
						html += `<p>Diagnosis: <b>${frappe.utils.escape_html(m.diagnosis || "")}</b></p>`;
						html += `<details><summary>Raw response (first 500 chars)</summary><pre>${frappe.utils.escape_html(m.preview || "")}</pre></details>`;
					}
					frappe.msgprint({ title, indicator, message: html, wide: true });
				})
				.catch(() => frappe.dom.unfreeze());
		});
	},
});
