frappe.ui.form.on("Issue", {
	refresh(frm) {
		// the form dashboard drops custom sections on every refresh
		frm.native_support_slas = null;
		native_support.render_active_slas(frm);
	},
	customer(frm) {
		native_support.render_active_slas(frm);
	},
	ns_part_number(frm) {
		native_support.render_active_slas(frm);
	},
	ns_serial_no(frm) {
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

native_support.render_active_slas = function (frm) {
	// only tickets Native Support handles (see ns_sla_section depends_on)
	if (!(frm.is_new() || frm.doc.ns_cover_status)) {
		native_support.remove_sla_section(frm);
		return;
	}

	const show = (html) => native_support.get_sla_section(frm).html(`<div class="text-muted small">${html}</div>`);
	if (!frm.doc.customer) {
		show(__("Select a customer to see their active SLAs."));
		return;
	}

	const search = frm.doc.ns_serial_no
		? { label: __("Serial No"), value: frm.doc.ns_serial_no }
		: frm.doc.ns_part_number
		? { label: __("Part Number"), value: frm.doc.ns_part_number }
		: null;

	frappe.call({
		method: "native_support.sla.get_customer_slas",
		args: {
			customer: frm.doc.customer,
			part_number: frm.doc.ns_part_number,
			serial_no: frm.doc.ns_serial_no,
		},
		callback(r) {
			// null when Native Support is disabled
			frm.toggle_display("ns_sla_section", r.message !== null);
			if (r.message === null) {
				native_support.remove_sla_section(frm);
				return;
			}

			const esc = frappe.utils.escape_html;
			const { slas, suggestions } = r.message;
			if (!slas.length) {
				show(
					search
						? __("No active SLA for {0} covers {1} {2}.", [
								esc(frm.doc.customer),
								search.label,
								`<b>${esc(search.value)}</b>`,
						  ])
						: __("No active SLAs for {0}.", [esc(frm.doc.customer)])
				);
				if (suggestions.length) native_support.render_suggestions(frm, suggestions);
				return;
			}

			const rows = slas.map((sla) => {
				const applied = sla.name === frm.doc.service_level_agreement;
				const parts = sla.parts
					.map(
						(p) =>
							`${esc(p.part_number)} &times; ${p.qty} &ndash; ${esc(p.description)}${
								p.serial_no ? ` (${esc(p.serial_no)})` : ""
							}`
					)
					.join("<br>");
				const action = applied
					? `<span class="indicator-pill green">${__("Applied")}</span>`
					: search
					? `<button class="btn btn-xs btn-default" data-sla="${esc(sla.name)}">${__("Apply")}</button>`
					: "";
				return `<tr>
					<td>${esc(sla.name)}</td>
					<td>${esc(sla.ns_sla_tier || "")}</td>
					<td>${sla.end_date ? frappe.datetime.str_to_user(sla.end_date) : ""}</td>
					${search ? `<td>${parts}</td>` : ""}
					<td>${action}</td>
				</tr>`;
			});

			const body = native_support.get_sla_section(frm);
			body.html(`
				${search ? `<p class="small">${__("SLAs covering {0} {1}", [search.label, `<b>${esc(search.value)}</b>`])}</p>` : ""}
				<table class="table table-bordered table-sm">
					<thead><tr>
						<th>${__("SLA")}</th><th>${__("Tier")}</th><th>${__("Ends")}</th>
						${search ? `<th>${__("Covered Items")}</th>` : ""}<th></th>
					</tr></thead>
					<tbody>${rows.join("")}</tbody>
				</table>`);

			body.find("button[data-sla]").on("click", function () {
				frm.set_value("service_level_agreement", $(this).attr("data-sla"));
				frappe.show_alert({ message: __("Save the ticket to apply this SLA."), indicator: "blue" });
			});

			// an item was entered and is covered: open the card (through Frappe's own collapse handler)
			if (search && body.hasClass("hide")) {
				body.closest(".form-dashboard-section").find(".section-head").trigger("click");
			}
		},
	});
};

// close matches for a serial / part number that matched nothing; only applied when the agent picks one
native_support.render_suggestions = function (frm, suggestions) {
	const esc = frappe.utils.escape_html;
	const fieldname = { serial_no: "ns_serial_no", part_number: "ns_part_number" };
	const rows = suggestions.map(
		(s) => `<tr>
			<td><b>${esc(s.value)}</b></td>
			<td>${s.score}%</td>
			<td>${esc(s.sla)}</td>
			<td>${esc(s.tier || "")}</td>
			<td>${esc(s.part_number)} &ndash; ${esc(s.description)}</td>
			<td><button class="btn btn-xs btn-default" data-field="${fieldname[s.field]}" data-value="${esc(s.value)}">${__("Use")}</button></td>
		</tr>`
	);

	const body = native_support.get_sla_section(frm);
	body.append(`
		<p class="small mt-3">${__("Did you mean:")}</p>
		<table class="table table-bordered table-sm">
			<thead><tr>
				<th>${__("Value")}</th><th>${__("Similarity")}</th><th>${__("SLA")}</th><th>${__("Tier")}</th>
				<th>${__("Item")}</th><th></th>
			</tr></thead>
			<tbody>${rows.join("")}</tbody>
		</table>`);

	body.find("button[data-field]").on("click", function () {
		// setting the field re-runs the exact lookup, which shows the covering SLA
		frm.set_value($(this).attr("data-field"), $(this).attr("data-value"));
	});

	if (body.hasClass("hide")) {
		body.closest(".form-dashboard-section").find(".section-head").trigger("click");
	}
};
