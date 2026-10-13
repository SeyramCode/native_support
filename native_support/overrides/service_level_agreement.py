import frappe
from erpnext.support.doctype.service_level_agreement.service_level_agreement import ServiceLevelAgreement
from frappe import _
from frappe.utils import flt

from native_support.utils import is_enabled


class NativeSupportServiceLevelAgreement(ServiceLevelAgreement):
	def validate(self):
		super().validate()
		if is_enabled():
			self.validate_sla_items()

	def validate_doc(self):
		"""ERPNext allows one SLA per Customer / Customer Group / Territory.

		Item based SLAs are told apart by their SLA Items, so a customer may hold several of them alongside
		one general SLA. The rest of ERPNext's checks still run unchanged.
		"""
		if not (is_enabled() and self.entity_type and self.entity):
			return super().validate_doc()

		entity_type, entity = self.entity_type, self.entity
		self.entity_type = self.entity = None
		try:
			super().validate_doc()
		finally:
			self.entity_type, self.entity = entity_type, entity

		if self.get("ns_sla_items"):
			return

		item_slas = frappe.get_all(
			"SLA Item", filters={"parenttype": self.doctype, "parentfield": "ns_sla_items"}, pluck="parent"
		)
		if frappe.db.exists(
			self.doctype,
			{
				"entity_type": entity_type,
				"entity": entity,
				"name": ["not in", [self.name, *item_slas]],
			},
		):
			frappe.throw(
				_("Service Level Agreement for {0} {1} already exists.").format(
					frappe.bold(entity_type), frappe.bold(entity)
				)
			)

	def validate_sla_items(self):
		if self.get("ns_sla_items") and self.document_type != "Issue":
			frappe.throw(_("SLA Items can only be used on Service Level Agreements for Issue."))

		seen = set()
		for row in self.get("ns_sla_items"):
			# runs before Frappe's mandatory check, so empty values are left for that check to report
			if row.qty is not None and flt(row.qty) <= 0:
				frappe.throw(_("Row {0}: Quantity must be greater than 0.").format(row.idx))

			row.part_number = (row.part_number or "").strip() or None
			row.serial_no = (row.serial_no or "").strip() or None
			if not row.serial_no:
				continue
			if row.serial_no.upper() in seen:
				frappe.throw(_("Row {0}: Serial No {1} is repeated.").format(row.idx, frappe.bold(row.serial_no)))
			seen.add(row.serial_no.upper())

		if not (self.enabled and seen):
			return

		taken = frappe.db.sql(
			"""
			select item.serial_no, item.parent
			from `tabSLA Item` item
			join `tabService Level Agreement` sla on sla.name = item.parent
			where item.parenttype = 'Service Level Agreement' and item.parentfield = 'ns_sla_items'
				and sla.enabled = 1 and sla.name != %(name)s and item.serial_no in %(serials)s
			""",
			{"name": self.name, "serials": list(seen)},
			as_dict=True,
		)
		if taken:
			frappe.throw(
				_("Serial No {0} is already covered by enabled Service Level Agreement {1}.").format(
					frappe.bold(taken[0].serial_no), frappe.bold(taken[0].parent)
				)
			)
