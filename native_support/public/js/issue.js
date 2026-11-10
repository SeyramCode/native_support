frappe.ui.form.on("Issue", {
	refresh(frm) {
		// the form dashboard drops custom sections on every refresh
		frm.native_support_slas = null;
		native_support.render_active_slas(frm);
	},
	customer(frm) {
		native_support.render_active_slas(frm);
	},
	ns_items_remove(frm) {
		native_support.render_active_slas(frm);
	},
});

frappe.ui.form.on("Issue SLA Item", {
	serial_no(frm) {
		native_support.render_active_slas(frm);
	},
	part_number(frm) {
		native_support.render_active_slas(frm);
	},
});

frappe.provide("native_support");

// "Active SLAs" card in the form dashboard, below Connections
native_support.get_sla_section = function (frm) {
	if (!frm.native_support_slas) {
		frm.native_support_slas = frm.dashboard.add_section("", __("Active SLAs"), "custom native-support-active-slas");
		frm.dashboard.show();
	}
	return frm.native_support_slas;
};

native_support.remove_sla_section = function (frm) {
	if (frm.native_support_slas) {
		frm.native_support_slas.closest(".form-dashboard-section").remove();
		frm.native_support_slas = null;
	}
};

native_support.open_sla_section = function (body) {
	// through Frappe's own collapse handler, so the open / closed choice is remembered
	if (body.hasClass("hide")) {
		body.closest(".form-dashboard-section").find(".section-head").trigger("click");
	}
};

native_support.render_active_slas = function (frm) {
	// only tickets Native Support handles (see ns_sla_section depends_on)
	if (!(frm.is_new() || frm.doc.ns_cover_status)) {
		native_support.remove_sla_section(frm);
		return;
	}

	const esc = frappe.utils.escape_html;
	if (!frm.doc.customer) {
		native_support
			.get_sla_section(frm)
			.html(`<div class="text-muted small">${__("Select a customer to see their active SLAs.")}</div>`);
		return;
	}

	const rows = (frm.doc.ns_items || []).filter((row) => row.serial_no || row.part_number);
	frappe.call({
		method: "native_support.sla.get_customer_slas",
		args: {
			customer: frm.doc.customer,
			items: rows.map((row) => ({ serial_no: row.serial_no, part_number: row.part_number })),
		},
		callback(r) {
			// null when Native Support is disabled
			frm.toggle_display(["ns_sla_section", "ns_sla_summary_section"], r.message !== null);
			if (r.message === null) {
				native_support.remove_sla_section(frm);
				return;
			}

			const body = native_support.get_sla_section(frm);
			if (!rows.length) {
				body.html(native_support.sla_table(frm, r.message.slas, false) || `<div class="text-muted small">${__(
					"No active SLAs for {0}.",
					[esc(frm.doc.customer)]
				)}</div>`);
				return;
			}

			// with a few items show them all; with many, open only those needing attention and the governing one
			const open_all = r.message.items.length <= 3;
			body.html(
				native_support.split_hint(frm) +
					r.message.items
						.map((item, i) =>
							native_support.item_block(frm, item, rows[i], open_all || !item.slas.length || rows[i].governing)
						)
						.join("")
			);
			native_support.bind_actions(frm, body, rows);
			native_support.open_sla_section(body);
		},
	});
};

// one collapsible block per Affected Item: the summary line says how it is covered, the body has the details
native_support.item_block = function (frm, item, row, open) {
	const esc = frappe.utils.escape_html;
	const label = item.serial_no
		? `${__("Serial No")} <b>${esc(item.serial_no)}</b>`
		: `${__("Part Number")} <b>${esc(item.part_number)}</b>`;
	const covering = item.slas[0];
	const status = covering
		? `<span class="indicator-pill green">${__("Covered")}</span>
			<span class="text-muted">${esc(covering.name)}${covering.ns_sla_tier ? ` &middot; ${esc(covering.ns_sla_tier)}` : ""}</span>`
		: `<span class="indicator-pill red">${__("Not Covered")}</span>`;
	const governing = row.governing ? `<span class="indicator-pill blue">${__("Governing")}</span>` : "";

	let content = native_support.sla_table(frm, item.slas, true);
	if (!content) {
		content = `<p class="text-muted small">${__("No active SLA for {0} covers this item.", [esc(frm.doc.customer)])}</p>`;
		if (item.suggestions.length) content += native_support.suggestions_table(item.suggestions, row);
	}

	return `<details class="mb-2" ${open ? "open" : ""}>
		<summary class="small" style="cursor: pointer; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
			${label} ${status} ${governing}
		</summary>
		<div class="mt-2">${content}</div>
	</details>`;
};

native_support.sla_table = function (frm, slas, with_items) {
	if (!slas.length) return "";

	const esc = frappe.utils.escape_html;
	const body = slas
		.map((sla) => {
			const parts = (sla.parts || [])
				.map(
					(p) =>
						`${esc(p.part_number)} &times; ${p.qty} &ndash; ${esc(p.description)}${
							p.serial_no ? ` (${esc(p.serial_no)})` : ""
						}`
				)
				.join("<br>");
			const action =
				sla.name === frm.doc.service_level_agreement
					? `<span class="indicator-pill green">${__("Applied")}</span>`
					: with_items
					? `<button class="btn btn-xs btn-default" data-sla="${esc(sla.name)}">${__("Apply")}</button>`
					: "";
			return `<tr>
				<td>${esc(sla.name)}</td>
				<td>${esc(sla.ns_sla_tier || "")}</td>
				<td>${sla.end_date ? frappe.datetime.str_to_user(sla.end_date) : ""}</td>
				${with_items ? `<td>${parts}</td>` : ""}
				<td>${action}</td>
			</tr>`;
		})
		.join("");

	return `<div class="table-responsive"><table class="table table-bordered table-sm mb-0">
		<thead><tr>
			<th>${__("SLA")}</th><th>${__("Tier")}</th><th>${__("Ends")}</th>
			${with_items ? `<th>${__("Covered Items")}</th>` : ""}<th></th>
		</tr></thead>
		<tbody>${body}</tbody>
	</table></div>`;
};

// close matches for an item nothing covers; only applied when the agent picks one
native_support.suggestions_table = function (suggestions, row) {
	const esc = frappe.utils.escape_html;
	const body = suggestions
		.map(
			(s) => `<tr>
				<td><b>${esc(s.value)}</b></td>
				<td>${s.score}%</td>
				<td>${esc(s.sla)}</td>
				<td>${esc(s.tier || "")}</td>
				<td>${esc(s.part_number)} &ndash; ${esc(s.description)}</td>
				<td><button class="btn btn-xs btn-default" data-row="${esc(row.name)}" data-field="${s.field}"
					data-value="${esc(s.value)}">${__("Use")}</button></td>
			</tr>`
		)
		.join("");

	return `<p class="small">${__("Did you mean:")}</p>
		<div class="table-responsive"><table class="table table-bordered table-sm mb-0">
			<thead><tr>
				<th>${__("Value")}</th><th>${__("Similarity")}</th><th>${__("SLA")}</th><th>${__("Tier")}</th>
				<th>${__("Item")}</th><th></th>
			</tr></thead>
			<tbody>${body}</tbody>
		</table></div>`;
};

// one SLA per ticket: when saved items sit under different SLAs, point to ERPNext's Split Issue
native_support.split_hint = function (frm) {
	const slas = new Set(
		(frm.doc.ns_items || [])
			.filter((row) => row.cover_status === "Covered" && row.service_level_agreement)
			.map((row) => row.service_level_agreement)
	);
	if (frm.is_new() || slas.size < 2) return "";

	return `<p class="small text-warning">${__(
		"Several devices are under different SLAs. Use <b>Split Issue</b> on the email in the timeline if each needs its own timers."
	)}</p>`;
};

native_support.bind_actions = function (frm, body, rows) {
	body.find("button[data-sla]").on("click", function () {
		frm.set_value("service_level_agreement", $(this).attr("data-sla"));
		frappe.show_alert({ message: __("Save the ticket to apply this SLA."), indicator: "blue" });
	});

	body.find("button[data-row]").on("click", function () {
		// writing the value into the row re-runs the exact lookup, which shows the covering SLA
		const row = rows.find((r) => r.name === $(this).attr("data-row"));
		if (row) frappe.model.set_value(row.doctype, row.name, $(this).attr("data-field"), $(this).attr("data-value"));
	});
};
