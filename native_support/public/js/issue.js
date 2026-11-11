// SLA Tier pill next to the status pill in the form header
frappe.ui.form.on("Issue", {
	refresh(frm) {
		// the header is reused between tickets: drop the previous ticket's pill
		frm.page.wrapper.find(".ns-tier-pill").remove();

		const tier = frm.doc.ns_sla_tier;
		if (!tier || !frappe.boot.native_support_enabled) return;

		const color = { Premium: "purple", Standard: "blue" }[tier] || "gray";
		$(`<span class="indicator-pill no-indicator-dot whitespace-nowrap ml-2 ns-tier-pill ${color}"></span>`)
			.text(__("{0} SLA", [__(tier)]))
			.attr("title", __("SLA Tier"))
			.insertAfter(frm.page.indicator);
	},
});
