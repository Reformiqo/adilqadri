// Adilqadri — Item form enhancements for Uniware channel sync.

frappe.ui.form.on("Item", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.has_variants) return;

		frm.add_custom_button(
			__("Sync to Uniware"),
			() => run_sync_to_uniware(frm),
			__("Uniware"),
		);
	},
});

function run_sync_to_uniware(frm) {
	const rows = frm.doc.channel_item_codes || [];
	if (rows.length === 0) {
		frappe.msgprint({
			title: __("No Channel Mappings"),
			indicator: "orange",
			message: __(
				"This item has no rows in the Channel Mapping tab. Add at least one channel + product code before syncing.",
			),
		});
		return;
	}

	const active = rows.filter((r) => r.is_active).length;
	frappe.confirm(
		__("Push this item to Uniware for {0} active channel(s)?", [active]),
		() => {
			frappe.dom.freeze(__("Pushing to Uniware…"));
			frappe.call({
				method: "adilqadri.adilqadri.unicommerce.item_push.sync_item_to_channels",
				args: { item_code: frm.doc.name },
				callback(r) {
					frappe.dom.unfreeze();
					render_sync_result(r.message || {});
					frm.reload_doc();
				},
				error() {
					frappe.dom.unfreeze();
				},
			});
		},
	);
}

function render_sync_result(m) {
	if (!m || typeof m !== "object") {
		frappe.msgprint({ title: __("Sync Failed"), indicator: "red", message: __("No response") });
		return;
	}
	if (m.error) {
		frappe.msgprint({
			title: __("Sync Failed"),
			indicator: "red",
			message: `<pre>${frappe.utils.escape_html(m.error)}</pre>`,
		});
		return;
	}

	const indicator = m.failed === 0 ? (m.synced > 0 ? "green" : "orange") : "red";
	let html = `
		<p>
			<b>${__("Item")}:</b> ${frappe.utils.escape_html(m.item_code || "")}<br>
			<b>${__("Synced")}:</b> ${m.synced} &nbsp;
			<b>${__("Failed")}:</b> ${m.failed} &nbsp;
			<b>${__("Skipped")}:</b> ${m.skipped} &nbsp;
			(${m.total} total)
		</p>
	`;

	if ((m.rows || []).length) {
		html += '<table class="table table-bordered" style="margin-top:8px">';
		html += `
			<thead><tr>
				<th>${__("Channel")}</th>
				<th>${__("Channel Product Code")}</th>
				<th>${__("Status")}</th>
				<th>${__("Error")}</th>
			</tr></thead><tbody>
		`;
		for (const row of m.rows) {
			const color =
				row.status === "Synced"
					? "#28a745"
					: row.status === "Failed"
						? "#dc3545"
						: "#6c757d";
			html += `<tr>
				<td>${frappe.utils.escape_html(row.channel || "")}</td>
				<td><code>${frappe.utils.escape_html(row.channel_product_code || "")}</code></td>
				<td><b style="color:${color}">${frappe.utils.escape_html(row.status || "")}</b></td>
				<td>${frappe.utils.escape_html(row.error || "")}</td>
			</tr>`;
		}
		html += "</tbody></table>";
	}

	frappe.msgprint({
		title: m.failed === 0 ? __("Uniware Sync Complete") : __("Uniware Sync Partial"),
		indicator,
		message: html,
		wide: true,
	});
}
