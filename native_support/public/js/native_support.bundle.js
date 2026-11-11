// Loaded with the desk (app_include_js), not as an Issue form script: the browser keeps form
// scripts in its DocType cache, which ERPNext's unchanged Issue DocType never invalidates.

// SLA Tier pill next to the status pill in the form header
frappe.ui.form.on("Issue", {
	refresh(frm) {
		// the header is reused between tickets: drop the previous ticket's pill
		frm.page.wrapper.find(".ns-tier-pill").remove();

		// only Native Support sets a tier on a ticket
		const tier = frm.doc.ns_sla_tier;
		if (!tier) return;

		const color = { Premium: "purple", Standard: "blue" }[tier] || "gray";
		$(`<span class="indicator-pill no-indicator-dot whitespace-nowrap ml-2 ns-tier-pill ${color}"></span>`)
			.text(__("{0} SLA", [__(tier)]))
			.attr("title", __("SLA Tier"))
			.insertAfter(frm.page.indicator);
	},
});
